"""Later phase: the till's sales checked against cash drawer openings (POS), and attendance
for payroll (HRM)."""

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from cctv_cloud.db.models import AttendanceRecord, AuditLog, Event, Staff
from cctv_cloud.services import pos
from tests.conftest import login

T = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)  # 15:00 in Kuwait


@pytest.fixture
def h(client, org):
    cache = {}

    def headers(name: str) -> dict[str, str]:
        if name not in cache:
            cache[name] = login(client, org, name)
        return cache[name]

    return headers


def api_key(client, h, scope: str, owner: str = "owner_a") -> dict[str, str]:
    r = client.post("/v1/api-keys", json={"name": scope, "scopes": [scope]}, headers=h(owner))
    assert r.status_code == 201, r.text
    return {"X-API-Key": r.json()["key"]}


def sale(receipt: str, at: datetime, payment: str = "cash", total: str = "4.500") -> dict:
    return {"receipt_id": receipt, "sold_at": at.isoformat(), "total": total, "payment": payment}


def drawer_openings(owner_engine, org, *times: datetime) -> list[uuid.UUID]:
    ids = [uuid.uuid4() for _ in times]
    with Session(owner_engine) as s, s.begin():
        for event_id, at in zip(ids, times, strict=True):
            s.add(
                Event(
                    id=event_id,
                    tenant_id=org.ids["a"],
                    branch_id=org.ids["a1"],
                    camera_id=org.ids["cam_a1"],
                    feature_key="cashier",
                    event_type="drawer_open",
                    severity="info",
                    status="closed",
                    started_at=at,
                    ended_at=at + timedelta(seconds=20),
                    confidence=0.9,
                )
            )
    return ids


def test_drawer_openings_checked_against_the_till(client, org, h, owner_engine):
    till = api_key(client, h, "pos:write")
    early, rung_up, card_only, lonely, too_new = drawer_openings(
        owner_engine,
        org,
        T,
        T + timedelta(minutes=10),
        T + timedelta(minutes=20),
        T + timedelta(minutes=30),
        T + timedelta(minutes=59),
    )
    body = {
        "branch_id": str(org.ids["a1"]),
        "synced_until": (T + timedelta(hours=1)).isoformat(),
        "sales": [
            sale("R-1", T - timedelta(seconds=30)),  # rung up just before
            sale("R-2", T + timedelta(minutes=10, seconds=90)),  # after the drawer opened
            sale("R-3", T + timedelta(minutes=10, seconds=5), payment="card"),
            sale("R-4", T + timedelta(minutes=20, seconds=10), payment="card"),
            sale("R-5", T + timedelta(minutes=45)),  # nothing opened near it
        ],
    }
    r = client.post("/v1/pos/sales", json=body, headers=till)
    assert r.status_code == 200, r.text
    assert r.json()["accepted"] == 5
    # Sent again (a retry): replaced, not doubled.
    assert client.post("/v1/pos/sales", json=body, headers=till).json()["accepted"] == 5

    assert pos.reconcile_tenant(org.ids["a"], now=T + timedelta(hours=1)) == (3, 1)
    with Session(owner_engine) as s:
        found = {e.id: e for e in s.scalars(select(Event))}
    assert (found[early].pos_match, found[early].pos_receipt_id) == ("sale", "R-1")
    assert found[rung_up].pos_receipt_id == "R-2"  # cash first, though the card was closer
    assert found[card_only].pos_receipt_id == "R-4"
    assert (found[lonely].pos_match, found[lonely].severity) == ("no_sale", "warning")
    # The till is complete up to 13:00: an opening at 12:59 (sales up to 13:01 could
    # explain it) waits for the next batch.
    assert found[too_new].pos_checked_at is None
    assert pos.reconcile_tenant(org.ids["a"], now=T + timedelta(hours=1)) == (0, 0)

    owner = h("owner_a")
    listed = client.get("/v1/events", params={"pos_match": "no_sale"}, headers=owner).json()
    assert [e["id"] for e in listed["items"]] == [str(lonely)]
    assert listed["items"][0]["pos_match"] == "no_sale"


def test_the_till_is_checked(client, org, h):
    till = api_key(client, h, "pos:write")
    other_tenant = {"branch_id": str(org.ids["b1"]), "sales": [sale("X", T)]}
    assert client.post("/v1/pos/sales", json=other_tenant, headers=till).status_code == 404
    future = {
        "branch_id": str(org.ids["a1"]),
        "synced_until": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    }
    assert client.post("/v1/pos/sales", json=future, headers=till).status_code == 422
    # A key for events can't send sales, and the till's key can't read events.
    reader = api_key(client, h, "events:read")
    ok = {"branch_id": str(org.ids["a1"]), "sales": [sale("Y", T)]}
    assert client.post("/v1/pos/sales", json=ok, headers=reader).status_code == 403
    assert client.get("/v1/events", headers=till).status_code == 403


def test_attendance_for_payroll(client, org, h, owner_engine):
    tz_offset = timedelta(hours=3)  # Kuwait
    with Session(owner_engine) as s, s.begin():
        sara = Staff(
            tenant_id=org.ids["a"], branch_id=org.ids["a1"], employee_code="E1", full_name="Sara"
        )
        s.add(sara)
        s.flush()
        for day, status, hours in [(1, "on_time", 8), (2, "late", 7), (3, "absent", 0)]:
            start = datetime(2026, 10, day, 9, tzinfo=timezone.utc) - tz_offset
            s.add(
                AttendanceRecord(
                    tenant_id=org.ids["a"],
                    branch_id=org.ids["a1"],
                    staff_id=sara.id,
                    work_date=date(2026, 10, day),
                    status=status,
                    check_in_at=start if hours else None,
                    check_out_at=start + timedelta(hours=hours) if hours else None,
                    source="manual" if day == 2 else "face",
                    note="=HYPERLINK(1)" if day == 2 else None,
                )
            )
    params = {"from": "2026-10-01", "to": "2026-10-31"}
    r = client.get("/v1/payroll/attendance", params=params, headers=h("owner_a"))
    assert r.status_code == 200, r.text
    data = r.json()
    assert [
        (d["date"], d["check_in"], d["check_out"], d["worked_minutes"]) for d in data["days"]
    ] == [
        ("2026-10-01", "09:00", "17:00", 480),
        ("2026-10-02", "09:00", "16:00", 420),
        ("2026-10-03", None, None, 0),
    ]
    [sara_total] = data["staff"]
    assert sara_total | {"staff_id": None} == {
        "staff_id": None,
        "employee_code": "E1",
        "name": "Sara",
        "days_on_time": 1,
        "days_late": 1,
        "days_absent": 1,
        "days_off": 0,
        "worked_minutes": 900,
    }

    # The payroll system's key, as CSV (formulas defused; a BOM for Excel).
    hrm = api_key(client, h, "attendance:read")
    csv = client.get("/v1/payroll/attendance", params={**params, "format": "csv"}, headers=hrm)
    assert csv.headers["content-type"].startswith("text/csv")
    lines = csv.content.decode("utf-8-sig").splitlines()
    assert lines[0].startswith("employee_code,name,branch,date,status")
    assert lines[2].endswith(",yes,'=HYPERLINK(1)")

    # Branch managers see attendance in reports, but don't export payroll; nor do other
    # tenants see these people.
    assert (
        client.get("/v1/payroll/attendance", params=params, headers=h("manager_a")).status_code
        == 403
    )
    assert (
        client.get("/v1/payroll/attendance", params=params, headers=h("owner_b")).json()["days"]
        == []
    )
    long = {"from": "2026-01-01", "to": "2026-12-31"}
    assert (
        client.get("/v1/payroll/attendance", params=long, headers=h("owner_a")).status_code == 422
    )
    with Session(owner_engine) as s:
        exports = s.scalars(select(AuditLog).where(AuditLog.action == "export_attendance")).all()
    assert len(exports) == 3 and {e.details["format"] for e in exports} == {"json", "csv"}
