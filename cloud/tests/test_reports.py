"""Branch reports: figures, files, permissions and the scheduled run (plan 6.6, 6.7)."""

import io
import uuid
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from openpyxl import load_workbook
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from cctv_cloud.db.enums import ReportPeriod
from cctv_cloud.db.models import (
    AlertDelivery,
    AttendanceRecord,
    AuditLog,
    Branch,
    CountBucket,
    Event,
    Report,
    Staff,
    User,
    Zone,
)
from cctv_cloud.services.alerts import Senders, latency_stats
from cctv_cloud.services.reports import period_bounds
from cctv_cloud.worker import make_reports
from tests.conftest import login
from tests.test_alerts import FakePush, FakeWhatsApp

KUWAIT = ZoneInfo("Asia/Kuwait")
DAY = datetime.now(KUWAIT).date() - timedelta(days=2)


def at(hh: int, mm: int = 0, day: date = DAY) -> datetime:
    return datetime.combine(day, time(hh, mm), KUWAIT)


def test_periods():
    wednesday = date(2026, 10, 28)
    assert period_bounds(ReportPeriod.DAY, wednesday) == (wednesday, wednesday)
    # Weeks start on Sunday (Kuwait), or on Monday when configured.
    assert period_bounds(ReportPeriod.WEEK, wednesday, 6) == (
        date(2026, 10, 25),
        date(2026, 10, 31),
    )
    assert period_bounds(ReportPeriod.WEEK, wednesday, 0) == (date(2026, 10, 26), date(2026, 11, 1))
    assert period_bounds(ReportPeriod.MONTH, wednesday) == (date(2026, 10, 1), date(2026, 10, 31))
    assert period_bounds(ReportPeriod.MONTH, date(2028, 2, 10)) == (
        date(2028, 2, 1),
        date(2028, 2, 29),
    )


def test_latency_percentiles():
    assert latency_stats([]) == {"p50_ms": None, "p95_ms": None, "within_10s_pct": None}
    stats = latency_stats(list(range(1000, 21000, 1000)))  # 1..20 s
    assert stats == {"p50_ms": 10000, "p95_ms": 19000, "within_10s_pct": 50.0}


@pytest.fixture
def seeded(org, owner_engine):
    """A day at branch a1: events, attendance, counts and alert deliveries."""
    t, b, cam = org.ids["a"], org.ids["a1"], org.ids["cam_a1"]
    with Session(owner_engine) as s, s.begin():
        zones = {}
        for name, kind in (("door", "entrance_line"), ("tables", "dining"), ("till", "counter")):
            zone = Zone(
                tenant_id=t,
                camera_id=cam,
                name=name,
                zone_type=kind,
                points=[[0.1, 0.1], [0.9, 0.9]],
            )
            s.add(zone)
            s.flush()
            zones[kind] = zone.id

        def ev(hh, event_type, feature, severity="warning", review="new", staff=None):
            e = Event(
                id=uuid.uuid4(),
                tenant_id=t,
                branch_id=b,
                camera_id=cam,
                feature_key=feature,
                event_type=event_type,
                severity=severity,
                status="closed",
                started_at=at(hh),
                confidence=0.8,
                review_status=review,
                staff_id=staff,
            )
            s.add(e)
            return e

        sara = Staff(tenant_id=t, branch_id=b, employee_code="E1", full_name="Sara Ahmed")
        omar = Staff(tenant_id=t, branch_id=b, employee_code="E2", full_name="Omar Ali")
        s.add_all([sara, omar])
        s.flush()
        people = {"sara": sara.id, "omar": omar.id}
        intrusion = ev(2, "after_hours_person", "security", "critical", "valid")
        ev(3, "after_hours_motion", "security", "warning", "false_alarm")
        ev(11, "missing_gloves", "kitchen_compliance", review="valid", staff=omar.id)
        ev(12, "missing_mask", "kitchen_compliance")
        ev(12, "missing_mask", "kitchen_compliance", review="false_alarm")
        ev(13, "drawer_open", "cashier", "info")
        # The day before and after don't count.
        other = Event(
            id=uuid.uuid4(),
            tenant_id=t,
            branch_id=b,
            camera_id=cam,
            feature_key="security",
            event_type="after_hours_person",
            severity="critical",
            status="closed",
            started_at=at(23, 59, DAY - timedelta(days=1)),
            confidence=0.9,
        )
        s.add(other)
        s.flush()
        s.add_all(
            [
                AttendanceRecord(
                    tenant_id=t,
                    branch_id=b,
                    staff_id=sara.id,
                    work_date=DAY,
                    status="on_time",
                    check_in_at=at(7, 55),
                ),
                AttendanceRecord(
                    tenant_id=t,
                    branch_id=b,
                    staff_id=omar.id,
                    work_date=DAY,
                    status="late",
                    check_in_at=at(8, 30),
                ),
            ]
        )
        for hh, people in ((12, 30), (13, 50), (19, 20)):
            s.add(
                CountBucket(
                    tenant_id=t,
                    camera_id=cam,
                    zone_id=zones["entrance_line"],
                    bucket_start=at(hh),
                    bucket_seconds=300,
                    in_count=people,
                    out_count=people - 5,
                )
            )
        s.add(
            CountBucket(
                tenant_id=t,
                camera_id=cam,
                zone_id=zones["dining"],
                bucket_start=at(13),
                bucket_seconds=300,
                max_occupancy=22,
            )
        )
        for hh, staffed in ((12, 300), (13, 150)):
            s.add(
                CountBucket(
                    tenant_id=t,
                    camera_id=cam,
                    zone_id=zones["counter"],
                    bucket_start=at(hh),
                    bucket_seconds=300,
                    occupied_seconds=staffed,
                )
            )
        for user, channel, latency, status in (
            ("owner_a", "web", 2500, "sent"),
            ("owner_a", "whatsapp", 4100, "delivered"),
            ("manager_a", "web", 12000, "sent"),
            ("manager_a", "whatsapp", None, "failed"),
        ):
            s.add(
                AlertDelivery(
                    tenant_id=t,
                    event_id=intrusion.id,
                    user_id=org.ids[user],
                    channel=channel,
                    status=status,
                    latency_ms=latency,
                    created_at=at(2, 1),
                    delivered_at=at(2, 0) + timedelta(seconds=6) if status == "delivered" else None,
                )
            )
    return people


def test_daily_report_figures_and_files(client, org, owner_engine, seeded):
    owner = login(client, org, "owner_a")
    r = client.post(
        "/v1/reports",
        json={"branch_id": str(org.ids["a1"]), "period": "day", "day": DAY.isoformat()},
        headers=owner,
    )
    assert r.status_code == 200, r.text
    report = r.json()
    data = report["data"]
    assert report["period_start"] == report["period_end"] == DAY.isoformat()
    assert report["partial"] is False
    totals = data["totals"]
    assert totals["events"] == 6
    assert totals["false_alarms"] == 2 and totals["unreviewed"] == 2
    assert totals["critical"] == 1 and totals["security"] == 1
    assert totals["compliance"] == 2 and totals["cashier"] == 1
    assert (totals["on_time"], totals["late"], totals["absent"]) == (1, 1, 0)
    assert totals["attendance_pct"] == 100 and totals["punctuality_pct"] == 50
    assert totals["customers_in"] == 100 and totals["peak_hour"] == 13
    assert totals["peak_occupancy"] == 22 and totals["counter_staffed_pct"] == 75
    assert data["hours"][12:14] == [30, 50]
    staff = {s["name"]: s for s in data["staff"]}
    assert staff["Omar Ali"]["late"] == 1 and staff["Omar Ali"]["compliance_issues"] == 1
    assert staff["Sara Ahmed"]["punctuality_pct"] == 100
    assert [e["event_type"] for e in data["security_events"]] == [
        "after_hours_person",
        "after_hours_motion",
    ]
    assert {t["event_type"]: t["total"] for t in data["types"]}["missing_mask"] == 2
    alerts = data["alerts"]
    assert (alerts["sent"], alerts["failed"]) == (3, 1)
    assert alerts["p95_ms"] == 12000 and alerts["within_10s_pct"] == pytest.approx(66.7)
    assert alerts["delivered_p95_ms"] == 6000

    # Regenerating replaces the report; it shows up in the list.
    again = client.post(
        "/v1/reports",
        json={"branch_id": str(org.ids["a1"]), "period": "day", "day": DAY.isoformat()},
        headers=owner,
    ).json()
    assert again["id"] == report["id"]
    listed = client.get(
        "/v1/reports", params={"branch_id": str(org.ids["a1"])}, headers=owner
    ).json()
    assert [x["id"] for x in listed] == [report["id"]]

    for fmt, lang in (("pdf", "en"), ("pdf", "ar"), ("xlsx", "en"), ("xlsx", "ar")):
        r = client.get(
            f"/v1/reports/{report['id']}/file", params={"format": fmt, "lang": lang}, headers=owner
        )
        assert r.status_code == 200, r.text
        assert f"report-day-{DAY}.{fmt}" in r.headers["content-disposition"]
        if fmt == "pdf":
            assert r.content.startswith(b"%PDF")
        else:
            book = load_workbook(io.BytesIO(r.content))
            assert book.active["B4"].value == "6"
            assert book.active.sheet_view.rightToLeft is (lang == "ar")
    with Session(owner_engine) as s:
        exports = s.scalars(select(AuditLog).where(AuditLog.action == "export_report")).all()
    assert len(exports) == 4 and exports[0].target_id == report["id"]


def test_report_permissions_and_limits(client, org, seeded):
    owner = login(client, org, "owner_a")
    week = client.post(
        "/v1/reports",
        json={
            "branch_id": str(org.ids["a1"]),
            "period": "week",
            "day": datetime.now(KUWAIT).date().isoformat(),
        },
        headers=owner,
    )
    assert week.status_code == 200 and week.json()["partial"] is True
    report_id = week.json()["id"]
    tomorrow = (datetime.now(KUWAIT).date() + timedelta(days=1)).isoformat()
    r = client.post(
        "/v1/reports",
        json={"branch_id": str(org.ids["a1"]), "period": "day", "day": tomorrow},
        headers=owner,
    )
    assert r.status_code == 422
    # Reviewers don't see reports; managers only their branch; other tenants nothing.
    reviewer = login(client, org, "reviewer_a")
    assert client.get(f"/v1/reports/{report_id}", headers=reviewer).status_code == 403
    manager = login(client, org, "manager_a")
    assert client.get(f"/v1/reports/{report_id}", headers=manager).status_code == 200
    r = client.post(
        "/v1/reports",
        json={"branch_id": str(org.ids["a2"]), "period": "day", "day": DAY.isoformat()},
        headers=manager,
    )
    assert r.status_code == 404
    other = login(client, org, "owner_b")
    assert client.get(f"/v1/reports/{report_id}", headers=other).status_code == 404
    assert client.get(f"/v1/reports/{report_id}/file", headers=other).status_code == 404


def test_scheduled_reports_are_made_once_and_linked(client, org, owner_engine, fakes, seeded):
    _, publisher, mailer = fakes
    whatsapp = FakeWhatsApp()
    with Session(owner_engine) as s, s.begin():
        s.execute(update(Branch).values(created_at=datetime.now(timezone.utc) - timedelta(days=30)))
        s.execute(
            update(User)
            .where(User.id == org.ids["owner_a"])
            .values(
                report_channels=["email", "whatsapp"],
                phone="+96550000001",
                whatsapp_opt_in_at=datetime.now(timezone.utc),
                locale="ar",
            )
        )
        s.execute(
            update(User).where(User.id == org.ids["reviewer_a"]).values(report_channels=["email"])
        )  # may not see reports: nothing sent
    senders = Senders(publisher, FakePush(), whatsapp, mailer)

    early = datetime.combine(datetime.now(KUWAIT).date(), time(1), KUWAIT)
    assert make_reports(senders, now=early) == 0  # before report_hour
    morning = early.replace(hour=3)
    made = make_reports(senders, now=morning)
    with Session(owner_engine) as s:
        reports = s.execute(select(Report.branch_id, Report.period, Report.period_start)).all()
    days = {r.period_start for r in reports if r.period == "day" and r.branch_id == org.ids["a1"]}
    yesterday = morning.date() - timedelta(days=1)
    assert days == {yesterday, yesterday - timedelta(days=1), yesterday - timedelta(days=2)}
    assert made == len(reports)
    assert make_reports(senders, now=morning) == 0  # already there

    links = [m for m in mailer.sent if m[0] == "owner_a@example.com"]
    assert links and all("/reports/?report=" in body for _, _, body in links)
    assert not [m for m in mailer.sent if m[0] == "reviewer_a@example.com"]
    assert {m["template"] for m in whatsapp.sent} == {"cctv_report"}
    assert whatsapp.sent[0]["lang"] == "ar" and whatsapp.sent[0]["params"][0] == "التقرير اليومي"
