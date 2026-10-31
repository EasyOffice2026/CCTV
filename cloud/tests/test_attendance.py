"""Attendance from face sightings, manager corrections, absences, face templates for the
edge box and people counts (plan 5.2, 5.4)."""

import uuid
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from cctv_cloud.db.models import AuditLog, Branch, FaceTemplate
from cctv_cloud.services.attendance import ShiftTimes, status_for, work_date_for
from cctv_cloud.services.face_keys import seal_embedding
from cctv_cloud.worker import mark_absent_staff
from tests.conftest import login
from tests.test_edge_api import pair_device
from tests.test_staff import add_staff, consent

KUWAIT = ZoneInfo("Asia/Kuwait")
EMBEDDING = [round(i / 128, 4) for i in range(128)]


@pytest.fixture
def owner(client, org):
    return login(client, org, "owner_a")


def local(day: date, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(day, time(hh, mm), KUWAIT)


def past_day(days: int = 3) -> date:
    return datetime.now(KUWAIT).date() - timedelta(days=days)


def add_shift(client, headers, org, starts="08:00", ends="16:00", branch="a1") -> str:
    r = client.post(
        "/v1/shifts",
        json={
            "branch_id": str(org.ids[branch]),
            "name": f"Shift {starts}",
            "starts_at": starts,
            "ends_at": ends,
            "grace_minutes": 10,
        },
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def enrolled_staff(client, headers, org, code, owner_engine=None, **extra) -> dict:
    staff = add_staff(client, headers, org, code=code, **extra)
    assert consent(client, headers, staff["id"]).status_code == 200
    if owner_engine is not None:  # a face template, as if enrolled 10 days ago
        staff_id = uuid.UUID(staff["id"])
        sealed, key_id = seal_embedding(EMBEDDING, org.ids["a"], staff_id)
        with Session(owner_engine) as s, s.begin():
            s.add(
                FaceTemplate(
                    tenant_id=org.ids["a"],
                    staff_id=staff_id,
                    embedding_encrypted=sealed,
                    key_id=key_id,
                    model_version="sface-test",
                    created_at=datetime.now(timezone.utc) - timedelta(days=10),
                )
            )
    return staff


def sighting(org, staff_id, seen_at: datetime, camera="cam_a1") -> dict:
    return {
        "staff_id": staff_id,
        "camera_id": str(org.ids[camera]),
        "seen_at": seen_at.isoformat(),
        "similarity": 0.6,
    }


def send(client, device, *sightings) -> dict:
    r = client.post(
        "/v1/edge/attendance", json={"sightings": list(sightings)}, headers=device["headers"]
    )
    assert r.status_code == 200, r.text
    return r.json()


def days(client, headers, day: date, **params) -> dict[str, dict]:
    r = client.get(
        "/v1/attendance",
        params={"date_from": day.isoformat(), "date_to": day.isoformat(), **params},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return {row["staff_id"]: row for row in r.json()}


# --- rules ----------------------------------------------------------------------------


def test_work_day_and_status_rules():
    morning = ShiftTimes(time(8), time(16), 10)
    night = ShiftTimes(time(16), time(2), 10)
    day = date(2026, 10, 31)
    assert work_date_for(local(day, 7, 50), KUWAIT, morning) == day
    assert work_date_for(local(day, 23, 0), KUWAIT, morning) == day
    # Overnight shift: until 4 h after its end, the morning belongs to the day before.
    assert work_date_for(local(day, 1, 30), KUWAIT, night) == day - timedelta(days=1)
    assert work_date_for(local(day, 5, 59), KUWAIT, night) == day - timedelta(days=1)
    assert work_date_for(local(day, 6, 0), KUWAIT, night) == day
    assert work_date_for(local(day, 1, 30), KUWAIT, None) == day
    # UTC input is converted to branch time first (05:00 UTC = 08:00 Kuwait).
    assert work_date_for(datetime(2026, 10, 31, 5, tzinfo=timezone.utc), KUWAIT, morning) == day

    assert status_for(local(day, 8, 10), day, KUWAIT, morning).value == "on_time"
    assert status_for(local(day, 8, 11), day, KUWAIT, morning).value == "late"
    assert status_for(local(day, 16, 30), day, KUWAIT, night).value == "late"
    assert status_for(local(day, 11), day, KUWAIT, None).value == "on_time"
    assert status_for(None, day, KUWAIT, morning).value == "absent"


# --- sightings and corrections --------------------------------------------------------


def test_sightings_become_attendance_and_corrections_stick(client, org, owner, owner_engine):
    device = pair_device(client, org, owner)
    shift = add_shift(client, owner, org)
    sara = enrolled_staff(client, owner, org, "E1", shift_id=shift)
    omar = enrolled_staff(client, owner, org, "E2", shift_id=shift)
    no_consent = add_staff(client, owner, org, code="E3")
    elsewhere = enrolled_staff(client, owner, org, "E4", branch="a2")
    day = past_day()

    result = send(
        client,
        device,
        sighting(org, sara["id"], local(day, 8, 30)),
        sighting(org, sara["id"], local(day, 8, 5)),  # out of order is fine
        sighting(org, omar["id"], local(day, 9, 0)),
        sighting(org, no_consent["id"], local(day, 9, 0)),
        sighting(org, elsewhere["id"], local(day, 9, 0)),
        sighting(org, sara["id"], local(day, 9, 0), camera="cam_a2"),
        sighting(org, str(uuid.uuid4()), local(day, 9, 0)),
    )
    assert result["accepted"] == 3
    assert [r["index"] for r in result["rejected"]] == [3, 4, 5, 6]

    rows = days(client, owner, day)
    assert set(rows) == {sara["id"], omar["id"]}
    assert rows[sara["id"]]["status"] == "on_time"
    assert datetime.fromisoformat(rows[sara["id"]]["check_in_at"]) == local(day, 8, 5)
    assert datetime.fromisoformat(rows[sara["id"]]["check_out_at"]) == local(day, 8, 30)
    assert rows[omar["id"]]["status"] == "late"
    assert rows[omar["id"]]["check_out_at"] is None  # seen once: no check-out yet

    # Resending is harmless; a later sighting moves the check-out.
    send(client, device, sighting(org, sara["id"], local(day, 8, 5)))
    send(client, device, sighting(org, sara["id"], local(day, 16, 2)))
    rows = days(client, owner, day)
    assert datetime.fromisoformat(rows[sara["id"]]["check_in_at"]) == local(day, 8, 5)
    assert datetime.fromisoformat(rows[sara["id"]]["check_out_at"]) == local(day, 16, 2)

    # The manager excuses Omar; later sightings don't undo that.
    manager = login(client, org, "manager_a")
    fixed = client.put(
        f"/v1/attendance/{omar['id']}/{day.isoformat()}",
        json={
            "status": "on_time",
            "check_in_at": local(day, 8).isoformat(),
            "check_out_at": local(day, 16).isoformat(),
            "note": "Approved: doctor's appointment",
        },
        headers=manager,
    )
    assert fixed.status_code == 200, fixed.text
    assert fixed.json()["source"] == "manual"
    assert fixed.json()["corrected_by"] == "manager_a"
    send(client, device, sighting(org, omar["id"], local(day, 7, 45)))
    row = days(client, manager, day)[omar["id"]]
    assert row["status"] == "on_time"
    assert datetime.fromisoformat(row["check_in_at"]) == local(day, 8)
    assert datetime.fromisoformat(row["first_seen_at"]) == local(day, 7, 45)

    # Removing the correction goes back to what was detected (now on time: seen 07:45).
    reverted = client.delete(f"/v1/attendance/{omar['id']}/{day.isoformat()}", headers=manager)
    assert reverted.status_code == 200, reverted.text
    body = reverted.json()
    assert body["source"] == "face" and body["status"] == "on_time" and body["note"] is None
    assert datetime.fromisoformat(body["check_in_at"]) == local(day, 7, 45)
    assert (
        client.delete(f"/v1/attendance/{omar['id']}/{day.isoformat()}", headers=manager).status_code
        == 404
    )

    with Session(owner_engine) as s:
        actions = list(s.scalars(select(AuditLog.action)))
    assert actions.count("attendance_change") == 2


def test_corrections_are_validated_and_scoped(client, org, owner):
    sara = enrolled_staff(client, owner, org, "E1")
    elsewhere = enrolled_staff(client, owner, org, "E2", branch="a2")
    day = past_day()
    url = f"/v1/attendance/{sara['id']}/{day.isoformat()}"

    cases = [
        {"status": "late"},  # needs a check-in
        {"status": "absent", "check_in_at": local(day, 9).isoformat()},
        {"status": "on_time", "check_in_at": "2026-10-28T08:00:00"},  # no offset
        {
            "status": "on_time",
            "check_in_at": local(day, 9).isoformat(),
            "check_out_at": local(day, 8).isoformat(),
        },
        {"status": "on_time", "check_in_at": local(day + timedelta(days=3), 9).isoformat()},
    ]
    for body in cases:
        assert client.put(url, json=body, headers=owner).status_code == 422, body

    future = datetime.now(KUWAIT).date() + timedelta(days=2)
    r = client.put(
        f"/v1/attendance/{sara['id']}/{future.isoformat()}",
        json={"status": "absent"},
        headers=owner,
    )
    assert r.status_code == 422
    r = client.put(
        f"/v1/attendance/{sara['id']}/{future.isoformat()}",
        json={"status": "day_off", "note": "Annual leave"},
        headers=owner,
    )
    assert r.status_code == 200, r.text

    # A day set by hand with nothing detected disappears when the correction is removed.
    r = client.put(url, json={"status": "day_off"}, headers=owner)
    assert r.status_code == 200
    r = client.delete(url, headers=owner)
    assert r.status_code == 200 and r.json() is None
    assert days(client, owner, day) == {}

    # Branch scoping and permissions.
    manager = login(client, org, "manager_a")
    r = client.put(
        f"/v1/attendance/{elsewhere['id']}/{day.isoformat()}",
        json={"status": "day_off"},
        headers=manager,
    )
    assert r.status_code == 404
    reviewer = login(client, org, "reviewer_a")
    assert (
        client.get(
            "/v1/attendance", params={"date_from": day, "date_to": day}, headers=reviewer
        ).status_code
        == 403
    )
    r = client.get(
        "/v1/attendance",
        params={"date_from": day, "date_to": day + timedelta(days=100)},
        headers=owner,
    )
    assert r.status_code == 422
    owner_b = login(client, org, "owner_b")
    assert client.put(url, json={"status": "day_off"}, headers=owner_b).status_code == 404


def test_overnight_shift_keeps_the_night_on_one_day(client, org, owner):
    device = pair_device(client, org, owner)
    night = add_shift(client, owner, org, starts="16:00", ends="02:00")
    ali = enrolled_staff(client, owner, org, "N1", shift_id=night)
    day = past_day()
    send(
        client,
        device,
        sighting(org, ali["id"], local(day, 16, 5)),
        sighting(org, ali["id"], local(day + timedelta(days=1), 1, 50)),
    )
    row = days(client, owner, day)[ali["id"]]
    assert row["status"] == "on_time"
    assert datetime.fromisoformat(row["check_out_at"]) == local(day + timedelta(days=1), 1, 50)
    assert days(client, owner, day + timedelta(days=1)) == {}


def test_absences_for_enrolled_staff_after_their_shift(client, org, owner, owner_engine):
    device = pair_device(client, org, owner)
    shift = add_shift(client, owner, org)
    sara = enrolled_staff(client, owner, org, "E1", owner_engine, shift_id=shift)
    enrolled_staff(client, owner, org, "E2", shift_id=shift)  # no face template: skipped
    enrolled_staff(client, owner, org, "E3", owner_engine)  # no shift: skipped

    # "Now" is yesterday evening, so every sighting below is in the past.
    day = past_day(1)
    evening = local(day, 20).astimezone(timezone.utc)
    send(client, device, sighting(org, sara["id"], local(day - timedelta(days=1), 8)))
    assert mark_absent_staff(evening) == 2  # that day and 2 days before
    assert mark_absent_staff(evening) == 0  # already there

    rows = client.get(
        "/v1/attendance",
        params={"date_from": day - timedelta(days=2), "date_to": day},
        headers=owner,
    ).json()
    assert sorted((r["work_date"], r["status"]) for r in rows) == [
        ((day - timedelta(days=2)).isoformat(), "absent"),
        ((day - timedelta(days=1)).isoformat(), "on_time"),
        (day.isoformat(), "absent"),
    ]
    # Seen after all (late): the absence becomes a late arrival.
    send(client, device, sighting(org, sara["id"], local(day, 9)))
    assert days(client, owner, day)[sara["id"]]["status"] == "late"

    # Days the branch is closed are not absences: open one weekday only, then look at
    # three days of which exactly one is that weekday.
    open_day = (day.weekday() + 1) % 7
    r = client.put(
        f"/v1/branches/{org.ids['a1']}/working-hours",
        json=[{"weekday": open_day, "opens_at": "08:00", "closes_at": "23:00"}],
        headers=owner,
    )
    assert r.status_code == 200, r.text
    assert mark_absent_staff(local(day + timedelta(days=3), 20).astimezone(timezone.utc)) == 1


# --- face templates for the box ---------------------------------------------------------


def test_face_templates_for_the_box(client, org, owner, owner_engine):
    device = pair_device(client, org, owner)
    sara = enrolled_staff(client, owner, org, "E1", owner_engine)
    left = enrolled_staff(client, owner, org, "E2", owner_engine)
    enrolled_staff(client, owner, org, "E3", owner_engine, branch="a2")
    assert (
        client.patch(f"/v1/staff/{left['id']}", json={"status": "left"}, headers=owner).status_code
        == 200
    )

    r = client.get("/v1/edge/face-templates", headers=device["headers"])
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    templates = r.json()["templates"]
    assert [t["staff_id"] for t in templates] == [sara["id"]]
    assert templates[0]["model_version"] == "sface-test"
    assert templates[0]["embedding"] == pytest.approx(EMBEDDING)

    etag = r.headers["etag"]
    again = client.get(
        "/v1/edge/face-templates", headers={**device["headers"], "If-None-Match": etag}
    )
    assert again.status_code == 304

    # Deleting the face changes the list and bumps the config version for the box.
    with Session(owner_engine) as s:
        version = s.scalar(select(Branch.config_version).where(Branch.id == org.ids["a1"]))
    assert client.delete(f"/v1/staff/{sara['id']}/face", headers=owner).status_code == 200
    r = client.get("/v1/edge/face-templates", headers={**device["headers"], "If-None-Match": etag})
    assert r.status_code == 200 and r.json()["templates"] == []
    with Session(owner_engine) as s:
        assert s.scalar(select(Branch.config_version).where(Branch.id == org.ids["a1"])) > version

    assert client.get("/v1/edge/face-templates").status_code == 401


# --- counts -----------------------------------------------------------------------------


def test_counts_are_replaced_on_resend_and_scoped(client, org, owner):
    device = pair_device(client, org, owner)
    camera = org.ids["cam_a1"]
    zone = client.post(
        f"/v1/cameras/{camera}/zones",
        json={"name": "Counter", "type": "counter", "points": [[0, 0], [0.5, 0], [0.5, 1]]},
        headers=owner,
    ).json()["id"]
    other_zone = client.post(
        f"/v1/cameras/{org.ids['cam_a2']}/zones",
        json={"name": "Counter", "type": "counter", "points": [[0, 0], [0.5, 0], [0.5, 1]]},
        headers=owner,
    ).json()["id"]
    start = datetime(2026, 10, 30, 9, 0, tzinfo=timezone.utc)

    def post(*buckets):
        r = client.post(
            "/v1/edge/counts", json={"buckets": list(buckets)}, headers=device["headers"]
        )
        assert r.status_code == 200, r.text
        return r.json()

    line = {"camera_id": str(camera), "bucket_start": start.isoformat(), "in_count": 4}
    occupancy = {
        "camera_id": str(camera),
        "zone_id": zone,
        "bucket_start": start.isoformat(),
        "max_occupancy": 2,
        "occupied_seconds": 240,
    }
    result = post(
        line,
        occupancy,
        {**line, "camera_id": str(org.ids["cam_a2"])},
        {**occupancy, "zone_id": other_zone},
    )
    assert result["accepted"] == 2
    assert [(r["index"], r["error"]) for r in result["rejected"]] == [
        (2, "unknown camera"),
        (3, "unknown zone"),
    ]
    assert post({**line, "in_count": 6, "out_count": 1}, {**line, "in_count": 7})["accepted"] == 2

    params = {
        "from": (start - timedelta(hours=1)).isoformat(),
        "to": (start + timedelta(hours=1)).isoformat(),
    }
    rows = client.get("/v1/counts", params=params, headers=owner).json()
    by_zone = {r["zone_id"]: r for r in rows}
    assert by_zone[None]["in_count"] == 7  # the last one sent wins
    assert by_zone[zone]["occupied_seconds"] == 240 and by_zone[zone]["max_occupancy"] == 2
    assert (
        len(client.get("/v1/counts", params={**params, "zone_id": zone}, headers=owner).json()) == 1
    )

    manager = login(client, org, "manager_a")
    assert len(client.get("/v1/counts", params=params, headers=manager).json()) == 2
    owner_b = login(client, org, "owner_b")
    assert client.get("/v1/counts", params=params, headers=owner_b).json() == []
    too_long = {**params, "to": (start + timedelta(days=40)).isoformat()}
    assert client.get("/v1/counts", params=too_long, headers=owner).status_code == 422
