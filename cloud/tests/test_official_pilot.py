"""Plan 10: wave-2 figures (staff left out, queues), the pilot's figures against the
success criteria, and the pilot log."""

import uuid
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.orm import Session

from cctv_cloud.db.models import AlertDelivery, CameraHealthMinute, Event
from tests.conftest import login
from tests.test_edge_api import pair_device
from tests.test_integration import as_admin

KUWAIT = ZoneInfo("Asia/Kuwait")


@pytest.fixture
def h(client, org):
    cache = {}

    def headers(name: str) -> dict[str, str]:
        if name not in cache:
            cache[name] = login(client, org, name)
        return cache[name]

    return headers


def today() -> date:
    return datetime.now(KUWAIT).date()


# --- 10.2: wave 2 -------------------------------------------------------------------------


def test_queue_and_staff_figures_reach_the_overview_and_reports(client, org, h):
    owner = h("owner_a")
    cam = org.ids["cam_a1"]
    zones = {}
    for name, kind, points in (
        ("Door", "entrance_line", [[0, 0.5], [1, 0.5]]),
        ("Queue", "queue", [[0, 0.6], [0.5, 0.6], [0.5, 1], [0, 1]]),
    ):
        r = client.post(
            f"/v1/cameras/{cam}/zones",
            json={"name": name, "type": kind, "points": points},
            headers=owner,
        )
        assert r.status_code == 201, r.text
        zones[kind] = r.json()["id"]
    for feature in ("queue", "customer_count"):
        client.put(
            "/v1/feature-settings",
            json={"scope": "tenant", "feature_key": feature, "enabled": True},
            headers=owner,
        )
    device = pair_device(client, org, owner)
    now = datetime.now(timezone.utc)
    start = now.replace(minute=now.minute - now.minute % 5, second=0, microsecond=0)
    earlier = start - timedelta(minutes=5)
    buckets = [
        {
            "camera_id": str(cam),
            "zone_id": zones["entrance_line"],
            "bucket_start": b.isoformat(),
            "in_count": 10,
            "out_count": 8,
            "staff_crossings": 3,
        }
        for b in (earlier, start)
    ] + [
        {
            "camera_id": str(cam),
            "zone_id": zones["queue"],
            "bucket_start": earlier.isoformat(),
            "max_occupancy": 6,
            "occupied_seconds": 280,
            "waits": 4,
            "wait_total_seconds": 400,
            "wait_max_seconds": 180,
        },
        {
            "camera_id": str(cam),
            "zone_id": zones["queue"],
            "bucket_start": start.isoformat(),
            "max_occupancy": 3,
            "occupied_seconds": 100,
            "waits": 1,
            "wait_total_seconds": 100,
            "wait_max_seconds": 100,
        },
    ]
    r = client.post("/v1/edge/counts", json={"buckets": buckets}, headers=device["headers"])
    assert r.status_code == 200 and r.json()["accepted"] == 4, r.text

    [a1] = client.get(
        "/v1/overview", params={"branch_id": str(org.ids["a1"])}, headers=owner
    ).json()
    assert a1["queue"] == {
        "peak": 6,
        "waits": 5,
        "wait_avg_s": 100,
        "wait_max_s": 180,
        "long_queues": 0,
    }
    assert a1["customers"]["customers_in"] == 20
    report = client.post(
        "/v1/reports",
        json={"branch_id": str(org.ids["a1"]), "period": "day", "day": str(today())},
        headers=owner,
    ).json()
    totals = report["data"]["totals"]
    assert (totals["staff_crossings"], totals["queue_peak"], totals["queue_wait_max_s"]) == (
        6,
        6,
        180,
    )
    # Queue off for the branch: no queue section.
    client.put(
        "/v1/feature-settings",
        json={
            "scope": "branch",
            "branch_id": str(org.ids["a1"]),
            "feature_key": "queue",
            "enabled": False,
        },
        headers=owner,
    )
    [a1] = client.get(
        "/v1/overview", params={"branch_id": str(org.ids["a1"])}, headers=owner
    ).json()
    assert a1["queue"] is None


# --- 10.3, 10.5: pilot figures --------------------------------------------------------------


def seed_pilot_day(owner_engine, org, day: date) -> None:
    noon = datetime.combine(day, time(12), KUWAIT)
    midnight = datetime.combine(day, time(0), KUWAIT)
    with Session(owner_engine) as s, s.begin():
        events = []
        # Kitchen: 23 valid, 2 false alarms (92%). Security: 15 valid, 5 false (75%).
        for feature, event_type, valid, false in (
            ("kitchen_compliance", "missing_gloves", 23, 2),
            ("security", "after_hours_person", 15, 5),
        ):
            for i in range(valid + false):
                event = Event(
                    id=uuid.uuid4(),
                    tenant_id=org.ids["a"],
                    branch_id=org.ids["a1"],
                    camera_id=org.ids["cam_a1"],
                    feature_key=feature,
                    event_type=event_type,
                    severity="warning",
                    status="closed",
                    started_at=noon + timedelta(minutes=i),
                    confidence=0.8,
                    review_status="valid" if i < valid else "false_alarm",
                )
                events.append(event)
                s.add(event)
        s.flush()
        for ms in (2000, 4000, 6000):
            s.add(
                AlertDelivery(
                    tenant_id=org.ids["a"],
                    event_id=events[0].id,
                    user_id=org.ids["owner_a"],
                    channel=("web", "whatsapp", "email")[ms // 2000 - 1],
                    status="sent",
                    created_at=noon,
                    sent_at=noon,
                    latency_ms=ms,
                )
            )
        # The camera streamed all day except 10 minutes; the box reported every minute.
        for i in range(1440):
            s.add(
                CameraHealthMinute(
                    tenant_id=org.ids["a"],
                    camera_id=org.ids["cam_a1"],
                    minute=(midnight + timedelta(minutes=i)).astimezone(timezone.utc),
                    samples=2,
                    offline=1 if i < 10 else 0,
                    min_fps=5.0,
                )
            )


def test_pilot_figures_against_the_success_criteria(client, org, h, owner_engine):
    yesterday = today() - timedelta(days=1)
    seed_pilot_day(owner_engine, org, yesterday)
    pilot = f"/v1/branches/{org.ids['a1']}/pilot"
    started = str(yesterday - timedelta(days=29))
    assert client.put(pilot, json={"started_on": started}, headers=h("owner_a")).status_code == 403
    r = client.put(pilot, json={"started_on": started}, headers=as_admin(h, org))
    assert r.status_code == 200 and r.json()["pilot_started_on"] == started

    params = {"branch_id": str(org.ids["a1"]), "since": str(yesterday), "until": str(yesterday)}
    r = client.get("/v1/pilot/kpis", params=params, headers=h("manager_a"))
    assert r.status_code == 200, r.text
    kpis = r.json()
    assert kpis["pilot_day"] == 31
    summary = kpis["summary"]
    assert (summary["events"], summary["valid"], summary["false_alarm"]) == (45, 38, 7)
    assert summary["false_alarms_per_day"] == 7.0
    assert summary["alerts_sent"] == 3 and summary["alert_p95_ms"] <= 6000
    assert (summary["camera_uptime_pct"], summary["box_uptime_pct"]) == (99.31, 100.0)
    features = {f["feature"]: f for f in kpis["features"]}
    assert features["kitchen_compliance"]["precision"] == 0.92
    assert features["kitchen_compliance"]["meets_target"] is True
    assert features["security"]["meets_target"] is False
    checks = {c["key"]: (c["value"], c["met"]) for c in kpis["checks"]}
    assert checks == {
        "accuracy": (0.75, False),
        "false_alarms": (7.0, False),
        "alert_time": (summary["alert_p95_ms"], True),
        "uptime": (99.31, True),
        "pilot_period": (31, True),
    }
    [day] = kpis["days"]
    assert day["date"] == str(yesterday) and day["precision"] == round(38 / 45, 3)

    # Defaults: from the pilot start to today. Others can't see it.
    full = client.get(
        "/v1/pilot/kpis", params={"branch_id": str(org.ids["a1"])}, headers=h("owner_a")
    ).json()
    assert full["since"] == started and len(full["days"]) == 31
    assert client.get("/v1/pilot/kpis", params=params, headers=h("reviewer_a")).status_code == 403
    a2 = {"branch_id": str(org.ids["a2"])}
    assert client.get("/v1/pilot/kpis", params=a2, headers=h("manager_a")).status_code == 404


# --- 10.4: pilot log ------------------------------------------------------------------------


def test_pilot_log(client, org, h):
    owner, manager = h("owner_a"), h("manager_a")
    week = {"noted_on": str(today()), "kind": "check_in", "body": "Week 1: owner happy with alerts"}
    r = client.post("/v1/pilot/log", json={**week, "branch_id": str(org.ids["a1"])}, headers=owner)
    assert r.status_code == 201 and r.json()["author_name"] == "owner_a"
    client.post(
        "/v1/pilot/log",
        json={
            "noted_on": str(today()),
            "kind": "change",
            "body": "a2: queue threshold 6",
            "branch_id": str(org.ids["a2"]),
        },
        headers=owner,
    )
    client.post(
        "/v1/pilot/log",
        json={"noted_on": str(today()), "kind": "decision", "body": "Gloves target 80%"},
        headers=as_admin(h, org),
    )
    # The manager of a1 sees a1 and client-wide entries, not a2's; can't write.
    seen = client.get("/v1/pilot/log", headers=manager).json()
    assert {e["kind"] for e in seen} == {"check_in", "decision"}
    assert client.post("/v1/pilot/log", json=week, headers=manager).status_code == 403
    bad = {**week, "kind": "gossip"}
    assert client.post("/v1/pilot/log", json=bad, headers=owner).status_code == 422
    # Only the author (or our team) removes an entry.
    decision = next(e for e in seen if e["kind"] == "decision")
    assert client.delete(f"/v1/pilot/log/{decision['id']}", headers=owner).status_code == 403
    mine = next(e for e in seen if e["kind"] == "check_in")
    assert client.delete(f"/v1/pilot/log/{mine['id']}", headers=owner).status_code == 204
    assert client.get("/v1/pilot/log", headers=h("owner_b")).json() == []
