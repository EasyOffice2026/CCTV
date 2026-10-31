"""Event search, review and audited media access (plan 3.6, 3.7); retention and device
status jobs (plan 3.8)."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from cctv_cloud.db.models import AuditLog, EdgeDevice, Event, Tenant
from cctv_cloud.worker import apply_retention, mark_offline_devices
from tests.conftest import login

NOW = datetime.now(timezone.utc)


def add_events(owner_engine, org, specs) -> list[uuid.UUID]:
    """specs: (branch, minutes_ago, event_type, feature, review_status)."""
    ids = []
    with Session(owner_engine) as s, s.begin():
        for branch, minutes_ago, event_type, feature, review in specs:
            event_id = uuid.uuid4()
            tenant = org.ids["a"] if branch.startswith("a") else org.ids["b"]
            s.add(
                Event(
                    id=event_id,
                    tenant_id=tenant,
                    branch_id=org.ids[branch],
                    camera_id=org.ids[f"cam_{branch}"],
                    feature_key=feature,
                    event_type=event_type,
                    severity="warning",
                    status="closed",
                    started_at=NOW - timedelta(minutes=minutes_ago),
                    ended_at=NOW - timedelta(minutes=minutes_ago) + timedelta(seconds=5),
                    confidence=0.7,
                    snapshot_key=f"{tenant}/{org.ids[branch]}/{event_id}/snapshot-x.jpg",
                    clip_key=f"{tenant}/{org.ids[branch]}/{event_id}/clip-x.mp4",
                    review_status=review,
                )
            )
            ids.append(event_id)
    return ids


@pytest.fixture
def events(owner_engine, org):
    return add_events(
        owner_engine,
        org,
        [
            ("a1", 5, "after_hours_person", "security", "new"),
            ("a1", 10, "phone_use", "staff_activity", "new"),
            ("a2", 15, "after_hours_person", "security", "valid"),
            ("a1", 60 * 24 * 2, "missing_gloves", "kitchen_compliance", "false_alarm"),
            ("b1", 1, "after_hours_person", "security", "new"),
        ],
    )


def ids(page) -> list[str]:
    return [e["id"] for e in page["items"]]


def test_search_filters(client, org, events):
    owner = login(client, org, "owner_a")

    def search(**params):
        r = client.get("/v1/events", params=params, headers=owner)
        assert r.status_code == 200, r.text
        return ids(r.json())

    a1_recent, a1_phone, a2_person, a1_old, _b1 = map(str, events)
    assert search() == [a1_recent, a1_phone, a2_person, a1_old]  # newest first, tenant a only
    assert search(feature="security") == [a1_recent, a2_person]
    assert search(event_type=["phone_use", "missing_gloves"]) == [a1_phone, a1_old]
    assert search(branch_id=str(org.ids["a2"])) == [a2_person]
    assert search(review_status="new") == [a1_recent, a1_phone]
    assert search(**{"from": (NOW - timedelta(hours=1)).isoformat()}) == [
        a1_recent,
        a1_phone,
        a2_person,
    ]
    assert search(**{"to": (NOW - timedelta(days=1)).isoformat()}) == [a1_old]
    assert client.get("/v1/events", params={"cursor": "garbage"}, headers=owner).status_code == 422


def test_pagination(client, org, events):
    owner = login(client, org, "owner_a")
    seen, cursor = [], None
    while True:
        params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        page = client.get("/v1/events", params=params, headers=owner).json()
        seen += ids(page)
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == [str(e) for e in events[:4]]


def test_branch_manager_sees_only_their_branch(client, org, events):
    manager = login(client, org, "manager_a")
    page = client.get("/v1/events", headers=manager).json()
    assert {e["branch_id"] for e in page["items"]} == {str(org.ids["a1"])}
    assert client.get(f"/v1/events/{events[2]}", headers=manager).status_code == 404
    # Asking for the other branch explicitly returns nothing rather than leaking it.
    page = client.get("/v1/events", params={"branch_id": str(org.ids["a2"])}, headers=manager)
    assert page.json()["items"] == []


def test_review(client, org, events):
    reviewer = login(client, org, "reviewer_a")
    r = client.patch(
        f"/v1/events/{events[0]}/review",
        json={"review_status": "false_alarm", "note": "reflection on the glass door"},
        headers=reviewer,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["review_status"] == "false_alarm"
    assert body["reviewed_by"] == str(org.ids["reviewer_a"])
    # Other tenant's event: not found.
    assert (
        client.patch(
            f"/v1/events/{events[4]}/review", json={"review_status": "valid"}, headers=reviewer
        ).status_code
        == 404
    )


def test_media_links_are_audited(client, org, events, fakes, owner_engine):
    storage, _, _ = fakes
    reviewer = login(client, org, "reviewer_a")
    r = client.get(f"/v1/events/{events[0]}/media/clip", headers=reviewer)
    assert r.status_code == 200
    assert r.json()["url"].startswith("https://storage.test/")
    assert storage.gets[-1][1] is None  # viewed in the browser, not downloaded
    # Reviewers may watch but not export.
    r = client.get(f"/v1/events/{events[0]}/media/clip?download=true", headers=reviewer)
    assert r.status_code == 403

    manager = login(client, org, "manager_a")
    r = client.get(f"/v1/events/{events[0]}/media/snapshot?download=true", headers=manager)
    assert r.status_code == 200
    assert storage.gets[-1][1].endswith(".jpg")

    with Session(owner_engine) as s:
        actions = {
            (row.user_id, row.action)
            for row in s.scalars(select(AuditLog).where(AuditLog.target_id == str(events[0])))
        }
    assert (org.ids["reviewer_a"], "view_clip") in actions
    assert (org.ids["manager_a"], "export_snapshot") in actions


def test_retention(owner_engine, org, fakes):
    storage, _, _ = fakes
    with Session(owner_engine) as s, s.begin():
        s.execute(
            update(Tenant)
            .where(Tenant.id == org.ids["a"])
            .values(retention_media_days=7, retention_event_days=30)
        )
    fresh, media_expired, record_expired = add_events(
        owner_engine,
        org,
        [
            ("a1", 60, "after_hours_person", "security", "new"),
            ("a1", 60 * 24 * 10, "after_hours_person", "security", "new"),
            ("a1", 60 * 24 * 40, "after_hours_person", "security", "new"),
        ],
    )
    report = apply_retention(storage, tenants=[org.ids["a"]])
    assert report.failed_tenants == []
    assert report.events_deleted == 1
    assert len(storage.deleted) == 4  # snapshot + clip of the two old events
    with Session(owner_engine) as s:
        rows = {e.id: e for e in s.scalars(select(Event))}
    assert record_expired not in rows
    assert rows[media_expired].clip_key is None and rows[media_expired].snapshot_key is None
    assert rows[fresh].clip_key is not None


def test_devices_go_offline_without_heartbeats(owner_engine, org, client):
    owner = login(client, org, "owner_a")
    r = client.post(
        "/v1/edge-devices",
        json={"branch_id": str(org.ids["a1"]), "name": "Box", "serial": "SN-OFF"},
        headers=owner,
    )
    device_id = r.json()["device"]["id"]
    with Session(owner_engine) as s, s.begin():
        s.execute(
            update(EdgeDevice)
            .where(EdgeDevice.id == device_id)
            .values(status="online", last_seen_at=NOW - timedelta(minutes=10))
        )
    assert mark_offline_devices() == 1
    [device] = client.get("/v1/edge-devices", headers=owner).json()
    assert device["status"] == "offline"
