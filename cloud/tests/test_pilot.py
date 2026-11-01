"""Plan 9: the stream setup test, shadow mode, review figures and the training export."""

import csv
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from cctv_cloud.db.models import AuditLog, CameraHealthMinute, Event, WebhookDelivery
from cctv_cloud.main import app
from cctv_cloud.services import webhooks
from cctv_cloud.services.pilot import export_reviewed
from cctv_contracts.enums import MediaKind
from tests.conftest import login
from tests.test_alerts import add_rule, deliveries, send_events
from tests.test_edge_api import closed, event, pair_device
from tests.test_integration import FakePoster, as_admin


@pytest.fixture
def h(client, org):
    cache = {}

    def headers(name: str) -> dict[str, str]:
        if name not in cache:
            cache[name] = login(client, org, name)
        return cache[name]

    return headers


def beat(org, restarts=0, health="online", camera="cam_a1") -> dict:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "sent_at": now,
        "software_version": "0.1.0",
        "config_version_applied": 1,
        "uptime_seconds": 60,
        "cameras": [
            {
                "camera_id": str(org.ids[camera]),
                "health": health,
                "fps": 5,
                "last_frame_at": now,
                "stream_restarts": restarts,
            }
        ],
        "system": {"cpu_percent": 20, "disk_free_bytes": 10**9, "outbox_pending": 0},
    }


# --- 9.2: setup test ---------------------------------------------------------------------


def test_heartbeats_record_health_per_minute(client, org, h, owner_engine):
    device = pair_device(client, org, h("owner_a"))
    for restarts, health in ((0, "online"), (2, "offline"), (1, "degraded")):  # 1: box restarted
        r = client.post(
            "/v1/edge/heartbeat", json=beat(org, restarts, health), headers=device["headers"]
        )
        assert r.status_code == 200
    # Another branch's camera is ignored.
    client.post("/v1/edge/heartbeat", json=beat(org, 5, camera="cam_a2"), headers=device["headers"])
    with Session(owner_engine) as s:
        rows = s.scalars(select(CameraHealthMinute)).all()
    assert {r.camera_id for r in rows} == {org.ids["cam_a1"]}
    assert sum(r.samples for r in rows) == 3
    assert sum(r.restarts for r in rows) == 3  # 2, then 1 after the box restarted
    assert sum(r.offline for r in rows) == 1 and sum(r.degraded for r in rows) == 1


def seed_minutes(owner_engine, org, camera, since, minutes, gaps=(), offline=(), restarts=()):
    with Session(owner_engine) as s, s.begin():
        for i in range(minutes):
            if i in gaps:
                continue
            s.add(
                CameraHealthMinute(
                    tenant_id=org.ids["a"],
                    camera_id=org.ids[camera],
                    minute=since + timedelta(minutes=i),
                    samples=2,
                    offline=1 if i in offline else 0,
                    restarts=1 if i in restarts else 0,
                    min_fps=None if i in offline else 5.0,
                )
            )


def test_setup_test_over_a_window(client, org, h, owner_engine):
    owner = h("owner_a")
    since = (datetime.now(timezone.utc) - timedelta(hours=3)).replace(second=0, microsecond=0)
    seed_minutes(owner_engine, org, "cam_a1", since, 120)
    url = f"/v1/branches/{org.ids['a1']}/stream-test"
    r = client.get(url, params={"since": since.isoformat(), "hours": 2}, headers=owner)
    assert r.status_code == 200, r.text
    test = r.json()
    assert test["complete"] and test["passed"] is True
    [camera] = test["cameras"]
    assert camera["expected_minutes"] == 120 and camera["uptime_pct"] == 100.0
    assert camera["hours"] == [1.0, 1.0] and camera["problems"] == []

    # A drop: a restart, two minutes offline, then three with no heartbeat.
    with Session(owner_engine) as s, s.begin():
        s.execute(
            update(CameraHealthMinute)
            .where(CameraHealthMinute.minute.in_([since + timedelta(minutes=m) for m in (10, 11)]))
            .values(offline=1, restarts=1)
        )
        s.query(CameraHealthMinute).filter(
            CameraHealthMinute.minute.in_([since + timedelta(minutes=m) for m in (12, 13, 14)])
        ).delete()
    camera = client.get(url, params={"since": since.isoformat(), "hours": 2}, headers=owner).json()
    result = camera["cameras"][0]
    assert camera["passed"] is False and result["passed"] is False
    assert (result["restarts"], result["offline_minutes"], result["missing_minutes"]) == (2, 2, 3)
    assert result["longest_outage_minutes"] == 5
    assert result["problems"] == [
        "2 stream restarts",
        "2 min offline",
        "3 min without a heartbeat",
    ]

    # A window still running: no verdict yet for a camera without problems.
    recent = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=10)
    seed_minutes(owner_engine, org, "cam_a1", recent, 10)
    running = client.get(
        url, params={"since": recent.isoformat(), "hours": 48}, headers=owner
    ).json()
    assert running["complete"] is False and running["passed"] is None

    # Without a start, the window starts at the first heartbeat (install day).
    auto = client.get(url, params={"hours": 48}, headers=owner).json()
    assert datetime.fromisoformat(auto["since"].replace("Z", "+00:00")) == since
    assert auto["complete"] is False


def test_setup_test_permissions(client, org, h):
    url = f"/v1/branches/{org.ids['a1']}/stream-test"
    assert client.get(url, headers=h("installer_a")).status_code == 200
    assert client.get(url, headers=h("reviewer_a")).status_code == 403
    other = f"/v1/branches/{org.ids['a2']}/stream-test"
    assert client.get(other, headers=h("installer_a")).status_code == 404
    assert client.get(url, params={"hours": 500}, headers=h("owner_a")).status_code == 422


# --- 9.4: shadow mode --------------------------------------------------------------------


@pytest.fixture
def poster():
    fake = FakePoster()
    app.dependency_overrides[webhooks.get_webhook_poster] = lambda: fake
    yield fake
    app.dependency_overrides.pop(webhooks.get_webhook_poster, None)


def test_shadow_mode_sends_alerts_only_to_the_support_team(client, org, h, owner_engine, poster):
    owner, support = h("owner_a"), h("reviewer_a")
    for headers in (owner, h("manager_a"), support):
        add_rule(client, headers)
    client.post(
        "/v1/webhooks",
        json={"name": "Ops", "url": "https://ops.example.com/h", "topics": ["event.new"]},
        headers=owner,
    )
    shadow = f"/v1/branches/{org.ids['a1']}/shadow"
    until = (datetime.now(timezone.utc) + timedelta(days=14)).isoformat()
    # Our team switches it on and marks its own account; the client can't.
    assert client.put(shadow, json={"until": until}, headers=owner).status_code == 403
    r = client.put(shadow, json={"until": until}, headers=as_admin(h, org))
    assert r.status_code == 200 and r.json()["shadow_until"]
    reviewer = f"/v1/users/{org.ids['reviewer_a']}"
    assert client.patch(reviewer, json={"shadow_reviewer": True}, headers=owner).status_code == 403
    r = client.patch(reviewer, json={"shadow_reviewer": True}, headers=as_admin(h, org))
    assert r.status_code == 200 and r.json()["shadow_reviewer"] is True

    device = pair_device(client, org, owner)
    first = event(org)
    send_events(client, device, first)
    assert {d.user_id for d in deliveries(owner_engine, first["event_id"])} == {
        org.ids["reviewer_a"]
    }
    assert poster.sent == []  # nothing reaches the ops system either
    branches = {b["id"]: b for b in client.get("/v1/branches", headers=owner).json()}
    assert branches[str(org.ids["a1"])]["shadow_until"] is not None

    # Shadow mode over: everyone's rules apply again.
    client.put(shadow, json={"until": None}, headers=as_admin(h, org))
    second = event(org, camera_id=str(org.ids["cam_a1"]), event_type="restricted_area_entry")
    send_events(client, device, closed(org, first), second)
    users = {d.user_id for d in deliveries(owner_engine, second["event_id"])}
    assert {org.ids["owner_a"], org.ids["manager_a"]} <= users
    assert [m["topic"] for _, m, _ in poster.sent] == ["event.new"]
    with Session(owner_engine) as s:
        assert s.scalars(select(WebhookDelivery)).all()  # queued only after shadow mode


# --- 9.4, 9.5: review figures and the training export -------------------------------------


def reviewed_events(client, org, h) -> list[dict]:
    owner = h("owner_a")
    device = pair_device(client, org, owner)
    sent = [
        event(
            org,
            feature="kitchen_compliance",
            event_type="missing_gloves",
            severity="warning",
            confidence=c,
        )
        for c in (0.55, 0.6, 0.9)
    ] + [event(org, confidence=0.95)]
    send_events(client, device, *sent)
    for e, status in zip(sent, ("false_alarm", "false_alarm", "valid", "valid"), strict=True):
        r = client.patch(
            f"/v1/events/{e['event_id']}/review",
            json={"review_status": status, "note": "glove is blue"},
            headers=owner,
        )
        assert r.status_code == 200
    return sent


def test_review_summary_shows_where_false_alarms_come_from(client, org, h):
    reviewed_events(client, org, h)
    r = client.get(
        "/v1/review-summary", params={"branch_id": str(org.ids["a1"])}, headers=h("manager_a")
    )
    assert r.status_code == 200, r.text
    summary = r.json()
    assert len(summary["days"]) == 7
    today = summary["days"][-1]
    assert (today["total"], today["reviewed"], today["valid"], today["false_alarm"]) == (4, 4, 2, 2)
    gloves, security = summary["groups"]
    assert (gloves["event_type"], gloves["false_alarm"], gloves["precision"]) == (
        "missing_gloves",
        2,
        0.333,
    )
    assert gloves["camera"] == "Camera a1" and gloves["false_alarms_per_day"] == 0.29
    assert gloves["mean_confidence_false"] == 0.575 and gloves["mean_confidence_valid"] == 0.9
    assert security["precision"] == 1.0
    a2 = {"branch_id": str(org.ids["a2"])}
    assert client.get("/v1/review-summary", params=a2, headers=h("manager_a")).status_code == 404
    bad = {**a2, "since": "2026-01-01", "until": "2026-12-01"}
    assert client.get("/v1/review-summary", params=bad, headers=h("owner_a")).status_code == 422


def test_training_export(client, org, h, fakes, owner_engine, tmp_path):
    storage = fakes[0]
    sent = reviewed_events(client, org, h)
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    until = datetime.now(timezone.utc) + timedelta(hours=1)
    count = export_reviewed(
        storage, org.ids["a"], tmp_path, since, until, ["false_alarm"], [MediaKind.SNAPSHOT]
    )
    assert count == 2
    with (tmp_path / "manifest.csv").open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert {r["event_id"] for r in rows} == {sent[0]["event_id"], sent[1]["event_id"]}
    assert rows[0]["review_status"] == "false_alarm" and rows[0]["review_note"] == "glove is blue"
    assert (tmp_path / rows[0]["snapshot_file"]).read_bytes().startswith(b"media of ")
    with Session(owner_engine) as s:
        audited = s.scalars(select(AuditLog).where(AuditLog.action == "export_snapshot")).all()
    assert len(audited) == 2 and audited[0].details == {"via": "cli", "purpose": "training"}
    # Run again: files already there aren't fetched (or audited) twice; other tenants'
    # events never appear.
    export_reviewed(
        storage, org.ids["a"], tmp_path, since, until, ["false_alarm"], [MediaKind.SNAPSHOT]
    )
    assert (
        export_reviewed(storage, org.ids["b"], tmp_path / "b", since, until, ["false_alarm"], [])
        == 0
    )
    with Session(owner_engine) as s:
        assert (
            len(s.scalars(select(AuditLog).where(AuditLog.action == "export_snapshot")).all()) == 2
        )
        assert s.scalar(select(Event.review_note).where(Event.id == uuid.UUID(sent[0]["event_id"])))
