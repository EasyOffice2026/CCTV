"""Plan 11: monitoring and on-call (incidents, pages, /metrics) and the onboarding checklist."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from cctv_cloud.config import get_settings
from cctv_cloud.db.models import Camera, EdgeDevice, HealthIncident
from cctv_cloud.services import monitoring
from cctv_cloud.worker import mark_offline_devices, tenant_ids
from tests.conftest import login
from tests.test_edge_api import pair_device
from tests.test_integration import as_admin
from tests.test_pilot import beat


@pytest.fixture
def h(client, org):
    cache = {}

    def headers(name: str) -> dict[str, str]:
        if name not in cache:
            cache[name] = login(client, org, name)
        return cache[name]

    return headers


class FakePager:
    def __init__(self) -> None:
        self.pages: list[tuple[str, str]] = []

    def page(self, subject: str, body: str) -> None:
        self.pages.append((subject, body))


def heartbeat(client, org, device, *, disk_gb=200, temp=55.0, pending=0, health="online"):
    body = beat(org, health=health)
    body["system"] = {
        "cpu_percent": 30,
        "gpu_percent": 40,
        "temperature_c": temp,
        "disk_free_bytes": int(disk_gb * 1024**3),
        "outbox_pending": pending,
    }
    r = client.post("/v1/edge/heartbeat", json=body, headers=device["headers"])
    assert r.status_code == 200, r.text


def open_incidents(owner_engine) -> dict[str, HealthIncident]:
    with Session(owner_engine) as s:
        rows = s.scalars(select(HealthIncident).where(HealthIncident.resolved_at.is_(None)))
        return {i.kind: i for i in rows}


def test_incidents_open_and_resolve(client, org, h, owner_engine):
    device = pair_device(client, org, h("owner_a"))
    heartbeat(client, org, device, disk_gb=8, temp=91, pending=2500)
    listed = client.get("/v1/edge-devices", headers=h("owner_a")).json()[0]
    assert listed["temperature_c"] == 91 and listed["outbox_pending"] == 2500
    now = datetime.now(timezone.utc)
    pager = FakePager()
    monitoring.check_all(tenant_ids(), pager, now)
    incidents = open_incidents(owner_engine)
    assert set(incidents) == {"disk_low", "temperature_high", "sync_backlog"}
    assert incidents["disk_low"].value == 8.0 and incidents["disk_low"].branch_id == org.ids["a1"]
    assert pager.pages == []  # not lasted long enough yet

    # A camera without frames for 10 minutes while its box is online.
    with Session(owner_engine) as s, s.begin():
        s.execute(
            update(Camera)
            .where(Camera.id == org.ids["cam_a1"])
            .values(health="offline", last_seen_at=now - timedelta(minutes=10))
        )
    later = now + timedelta(minutes=6)
    monitoring.check_all(tenant_ids(), pager, later)
    assert "stream_down" in open_incidents(owner_engine)
    subjects = sorted(s for s, _ in pager.pages)
    assert subjects == [
        "[CCTV] Edge box can't upload at Tenant a / Branch a1",
        "[CCTV] Edge box disk almost full at Tenant a / Branch a1",
        "[CCTV] Edge box too hot at Tenant a / Branch a1",
    ]
    assert "edge-recovery.md#disk-low" in next(b for s, b in pager.pages if "disk" in s)

    # The box goes quiet: one incident for it; its other incidents and its cameras' resolve.
    with Session(owner_engine) as s, s.begin():
        s.execute(update(EdgeDevice).values(last_seen_at=later - timedelta(minutes=5)))
    mark_offline_devices(later)
    monitoring.check_all(tenant_ids(), pager, later + timedelta(minutes=1))
    assert set(open_incidents(owner_engine)) == {"device_offline"}
    resolved = [s for s, _ in pager.pages if s.startswith("[CCTV] Resolved")]
    assert len(resolved) == 3  # the three paged ones; the stream one was never paged
    # Still offline 10 minutes later: paged. Back online: resolved, and on-call is told.
    monitoring.check_all(tenant_ids(), pager, later + timedelta(minutes=10))
    assert pager.pages[-1][0] == "[CCTV] Edge box offline at Tenant a / Branch a1"
    heartbeat(client, org, device)
    monitoring.check_all(tenant_ids(), pager, later + timedelta(minutes=20))
    assert open_incidents(owner_engine) == {}
    assert pager.pages[-1][0] == "[CCTV] Resolved: Edge box offline at Tenant a / Branch a1"


def test_incidents_api_and_metrics(client, org, h, owner_engine, monkeypatch):
    device = pair_device(client, org, h("owner_a"))
    heartbeat(client, org, device, temp=95)
    monitoring.check_all(tenant_ids(), FakePager())
    [incident] = client.get("/v1/incidents", headers=h("installer_a")).json()
    assert (incident["kind"], incident["branch"], incident["device"]) == (
        "temperature_high",
        "Branch a1",
        "Box SN-1",
    )
    assert client.get("/v1/incidents", headers=h("reviewer_a")).status_code == 403
    assert client.get("/v1/incidents", headers=h("owner_b")).json() == []
    platform = client.get("/v1/platform/incidents", headers=h("admin")).json()
    assert [(i["tenant"], i["kind"]) for i in platform] == [("Tenant a", "temperature_high")]
    assert client.get("/v1/platform/incidents", headers=h("owner_a")).status_code == 403

    # /metrics: off without a token, then with it.
    assert client.get("/metrics").status_code == 401
    monkeypatch.setenv("CCTV_METRICS_TOKEN", "m" * 32)
    get_settings.cache_clear()
    try:
        assert client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
        r = client.get("/metrics", headers={"Authorization": f"Bearer {'m' * 32}"})
        assert r.status_code == 200
        assert 'cctv_edge_devices{status="online"} 1' in r.text
        assert 'cctv_incidents_open{kind="temperature_high"} 1' in r.text
        assert "Tenant a" not in r.text and str(org.ids["a"]) not in r.text
    finally:
        get_settings.cache_clear()


def test_short_incidents_page_nobody(client, org, h, owner_engine):
    device = pair_device(client, org, h("owner_a"))
    pager = FakePager()
    now = datetime.now(timezone.utc)
    heartbeat(client, org, device, temp=95)
    monitoring.check_all(tenant_ids(), pager, now)
    heartbeat(client, org, device, temp=60)  # cooled down within a minute
    monitoring.check_all(tenant_ids(), pager, now + timedelta(minutes=1))
    monitoring.check_all(tenant_ids(), pager, now + timedelta(minutes=30))
    assert pager.pages == []


def test_onboarding_checklist(client, org, h):
    owner = h("owner_a")
    url = f"/v1/branches/{org.ids['a1']}/onboarding"

    def steps() -> dict[str, tuple]:
        r = client.get(url, headers=h("installer_a"))
        assert r.status_code == 200, r.text
        return {s["key"]: (s["done"], s["done_count"], s["total"]) for s in r.json()}

    start = steps()
    assert list(start) == [
        "branch",
        "cameras",
        "edge_box",
        "streaming",
        "setup_test",
        "zones",
        "features",
        "staff",
        "alerts",
        "shadow_mode",
        "go_live",
    ]
    assert start["branch"][0] is False and start["cameras"] == (True, 1, None)
    assert start["edge_box"] == (False, 0, 0) and start["staff"][0] is None

    client.put(
        f"/v1/branches/{org.ids['a1']}/working-hours",
        json=[{"weekday": 0, "opens_at": "08:00", "closes_at": "23:00"}],
        headers=owner,
    )
    device = pair_device(client, org, owner)
    heartbeat(client, org, device)
    client.post(
        f"/v1/cameras/{org.ids['cam_a1']}/zones",
        json={"name": "Kitchen", "type": "kitchen", "points": [[0, 0], [1, 0], [1, 1]]},
        headers=owner,
    )
    client.put(
        "/v1/feature-settings",
        json={"scope": "tenant", "feature_key": "attendance", "enabled": True},
        headers=owner,
    )
    client.put(
        f"/v1/branches/{org.ids['a1']}/shadow",
        json={"until": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()},
        headers=as_admin(h, org),
    )
    now = steps()
    assert now["branch"][0] and now["edge_box"] == (True, 1, 1)
    assert now["streaming"] == (True, 1, 1) and now["setup_test"][0] is False
    assert now["zones"] == (True, 1, 1) and now["features"] == (True, 1, 1)
    assert now["staff"] == (False, 0, 0)  # attendance on, nobody enrolled yet
    assert now["shadow_mode"][0] is True and now["go_live"][0] is False
    assert client.get(url, headers=h("reviewer_a")).status_code == 403
