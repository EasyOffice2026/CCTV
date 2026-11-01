"""Branch reports (plan 6.6): a day, week or month of events, attendance, customers and
alert times in one summary.

`build_report` computes the figures (stored as JSON in `reports.data`); PDF and Excel
files are rendered from them on download (services/report_files.py), in the reader's
language. The worker makes yesterday's report each morning (after `report_hour`, branch
time, so events the edge buffered overnight are in it), the weekly one after the last day
of the week and the monthly one after the month, then sends a link to the users who asked
for it and queues a `report.ready` webhook message. Managers can also (re)generate any
period from the dashboard, including the current one ("so far").
"""

import calendar
import logging
import uuid
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from cctv_cloud.config import get_settings
from cctv_cloud.db.enums import (
    AlertChannel,
    AlertKind,
    AttendanceStatus,
    DeliveryStatus,
    ReportPeriod,
    ReviewStatus,
    UserRole,
)
from cctv_cloud.db.models import (
    AlertDelivery,
    AttendanceRecord,
    Branch,
    Camera,
    CountBucket,
    Event,
    Report,
    Staff,
    User,
    UserBranchAccess,
    Zone,
)
from cctv_cloud.db.session import tenant_session
from cctv_cloud.rbac import ALL_BRANCH_ROLES, ROLE_PERMISSIONS, Permission
from cctv_cloud.services import texts, webhooks
from cctv_cloud.services.alerts import Senders, latency_stats
from cctv_contracts.enums import FeatureKey, Severity, ZoneType

log = logging.getLogger(__name__)

REPORT_VERSION = 1
MAX_SECURITY_ROWS = 50
# Missed reports are made up for this many days back (e.g. after the worker was down).
CATCH_UP_DAYS = 3


class Totals(BaseModel):
    events: int = 0
    critical: int = 0
    security: int = 0
    compliance: int = 0
    cashier: int = 0
    false_alarms: int = 0
    unreviewed: int = 0
    on_time: int = 0
    late: int = 0
    absent: int = 0
    day_off: int = 0
    attendance_pct: float | None = None  # present / (present + absent)
    punctuality_pct: float | None = None  # on time / present
    customers_in: int | None = None  # None: no entrance counting line
    peak_hour: int | None = None  # branch-time hour with the most customers in
    peak_occupancy: int | None = None
    counter_staffed_pct: float | None = None
    # Plan 10.2: entrance crossings left out as staff; queues (None: no queue zone).
    staff_crossings: int | None = None
    queue_peak: int | None = None
    queue_waits: int | None = None
    queue_wait_avg_s: int | None = None
    queue_wait_max_s: int | None = None


class DayRow(BaseModel):
    date: date
    events: int = 0
    critical: int = 0
    security: int = 0
    compliance: int = 0
    cashier: int = 0
    false_alarms: int = 0
    on_time: int = 0
    late: int = 0
    absent: int = 0
    customers_in: int | None = None


class TypeRow(BaseModel):
    event_type: str
    feature: str
    total: int
    valid: int
    false_alarm: int
    unreviewed: int


class StaffRow(BaseModel):
    staff_id: uuid.UUID
    name: str
    employee_code: str
    on_time: int = 0
    late: int = 0
    absent: int = 0
    day_off: int = 0
    attendance_pct: float | None = None
    punctuality_pct: float | None = None
    # Compliance events matched to this person (needs face-matched kitchen events).
    compliance_issues: int = 0


class SecurityRow(BaseModel):
    event_id: uuid.UUID
    started_at: datetime
    event_type: str
    severity: str
    camera: str
    review_status: str


class AlertSummary(BaseModel):
    sent: int = 0
    failed: int = 0
    p50_ms: int | None = None
    p95_ms: int | None = None
    within_10s_pct: float | None = None
    # Until the phone confirmed receipt (WhatsApp delivery reports).
    delivered_p95_ms: int | None = None


class ReportData(BaseModel):
    version: int = REPORT_VERSION
    branch_id: uuid.UUID
    branch_name: str
    timezone: str
    period: ReportPeriod
    start: date
    end: date  # last day included
    generated_at: datetime
    partial: bool  # the period isn't over yet
    totals: Totals
    days: list[DayRow]
    hours: list[int] | None  # customers in per branch-time hour (0-23)
    types: list[TypeRow]
    staff: list[StaffRow]
    security_events: list[SecurityRow]
    alerts: AlertSummary


def period_bounds(
    period: ReportPeriod, day: date, week_start: int | None = None
) -> tuple[date, date]:
    """First and last day of the period containing `day`."""
    if period is ReportPeriod.DAY:
        return day, day
    if period is ReportPeriod.WEEK:
        first = week_start if week_start is not None else get_settings().report_week_start
        start = day - timedelta(days=(day.weekday() - first) % 7)
        return start, start + timedelta(days=6)
    last = calendar.monthrange(day.year, day.month)[1]
    return day.replace(day=1), day.replace(day=last)


def _pct(part: int, whole: int) -> float | None:
    return round(100 * part / whole, 1) if whole else None


def _local_midnight(day: date, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, time(), tzinfo=tz).astimezone(timezone.utc)


def build_report(
    db: Session, branch: Branch, period: ReportPeriod, start: date, now: datetime | None = None
) -> ReportData:
    now = now or datetime.now(timezone.utc)
    tz = ZoneInfo(branch.timezone)
    start, end = period_bounds(period, start)
    since, until = _local_midnight(start, tz), _local_midnight(end + timedelta(days=1), tz)
    days = {
        start + timedelta(days=i): DayRow(date=start + timedelta(days=i))
        for i in range((end - start).days + 1)
    }
    totals = Totals()

    # Events.
    cameras = dict(
        db.execute(select(Camera.id, Camera.name).where(Camera.branch_id == branch.id)).all()
    )
    events = db.execute(
        select(
            Event.id,
            Event.camera_id,
            Event.feature_key,
            Event.event_type,
            Event.severity,
            Event.review_status,
            Event.started_at,
            Event.staff_id,
        )
        .where(Event.branch_id == branch.id, Event.started_at >= since, Event.started_at < until)
        .order_by(Event.started_at)
    ).all()
    types: dict[str, TypeRow] = {}
    staff_issues: Counter[uuid.UUID] = Counter()
    security: list[SecurityRow] = []
    for e in events:
        row = days.get(e.started_at.astimezone(tz).date())
        false_alarm = e.review_status == ReviewStatus.FALSE_ALARM.value
        kind = types.setdefault(
            e.event_type,
            TypeRow(
                event_type=e.event_type,
                feature=e.feature_key,
                total=0,
                valid=0,
                false_alarm=0,
                unreviewed=0,
            ),
        )
        kind.total += 1
        kind.valid += e.review_status == ReviewStatus.VALID.value
        kind.false_alarm += false_alarm
        kind.unreviewed += e.review_status == ReviewStatus.NEW.value
        totals.events += 1
        totals.false_alarms += false_alarm
        totals.unreviewed += e.review_status == ReviewStatus.NEW.value
        if row is not None:
            row.events += 1
            row.false_alarms += false_alarm
        critical = e.severity == Severity.CRITICAL.value
        # Listed with their review status, false alarms included.
        if (critical or e.feature_key == FeatureKey.SECURITY.value) and len(
            security
        ) < MAX_SECURITY_ROWS:
            security.append(
                SecurityRow(
                    event_id=e.id,
                    started_at=e.started_at,
                    event_type=e.event_type,
                    severity=e.severity,
                    camera=cameras.get(e.camera_id, ""),
                    review_status=e.review_status,
                )
            )
        if false_alarm:
            continue  # issues count what wasn't dismissed
        totals.critical += critical
        for feature, field in (
            (FeatureKey.SECURITY, "security"),
            (FeatureKey.KITCHEN_COMPLIANCE, "compliance"),
            (FeatureKey.CASHIER, "cashier"),
        ):
            if e.feature_key == feature.value:
                setattr(totals, field, getattr(totals, field) + 1)
                if row is not None:
                    setattr(row, field, getattr(row, field) + 1)
        if row is not None:
            row.critical += critical
        if e.feature_key == FeatureKey.KITCHEN_COMPLIANCE.value and e.staff_id:
            staff_issues[e.staff_id] += 1

    # Attendance.
    people: dict[uuid.UUID, StaffRow] = {}
    records = db.execute(
        select(
            AttendanceRecord.work_date,
            AttendanceRecord.status,
            Staff.id,
            Staff.full_name,
            Staff.employee_code,
        )
        .join(Staff, Staff.id == AttendanceRecord.staff_id)
        .where(
            AttendanceRecord.branch_id == branch.id,
            AttendanceRecord.work_date >= start,
            AttendanceRecord.work_date <= end,
        )
    ).all()
    for r in records:
        person = people.setdefault(
            r.id, StaffRow(staff_id=r.id, name=r.full_name, employee_code=r.employee_code)
        )
        field = {
            AttendanceStatus.ON_TIME.value: "on_time",
            AttendanceStatus.LATE.value: "late",
            AttendanceStatus.ABSENT.value: "absent",
            AttendanceStatus.DAY_OFF.value: "day_off",
        }[r.status]
        setattr(person, field, getattr(person, field) + 1)
        setattr(totals, field, getattr(totals, field) + 1)
        if field != "day_off" and r.work_date in days:
            row = days[r.work_date]
            setattr(row, field, getattr(row, field) + 1)
    for staff_id, issues in staff_issues.items():
        if staff_id in people:
            people[staff_id].compliance_issues = issues
        elif (s := db.get(Staff, staff_id)) is not None:
            people[staff_id] = StaffRow(
                staff_id=s.id,
                name=s.full_name,
                employee_code=s.employee_code,
                compliance_issues=issues,
            )
    for person in people.values():
        present = person.on_time + person.late
        person.attendance_pct = _pct(present, present + person.absent)
        person.punctuality_pct = _pct(person.on_time, present)
    present = totals.on_time + totals.late
    totals.attendance_pct = _pct(present, present + totals.absent)
    totals.punctuality_pct = _pct(totals.on_time, present)

    # Customers (entrance counting lines), occupancy (dining) and the counter.
    buckets = db.execute(
        select(
            CountBucket.bucket_start,
            CountBucket.bucket_seconds,
            CountBucket.in_count,
            CountBucket.max_occupancy,
            CountBucket.occupied_seconds,
            CountBucket.staff_crossings,
            CountBucket.waits,
            CountBucket.wait_total_seconds,
            CountBucket.wait_max_seconds,
            Zone.zone_type,
        )
        .join(Camera, Camera.id == CountBucket.camera_id)
        .join(Zone, Zone.id == CountBucket.zone_id)
        .where(
            Camera.branch_id == branch.id,
            CountBucket.bucket_start >= since,
            CountBucket.bucket_start < until,
        )
    ).all()
    hours: list[int] | None = None
    staffed = watched = 0
    wait_total = 0
    for b in buckets:
        local = b.bucket_start.astimezone(tz)
        if b.zone_type == ZoneType.QUEUE.value and b.max_occupancy is not None:
            totals.queue_peak = max(totals.queue_peak or 0, b.max_occupancy)
            if b.waits is not None:
                totals.queue_waits = (totals.queue_waits or 0) + b.waits
                wait_total += b.wait_total_seconds or 0
                totals.queue_wait_max_s = max(totals.queue_wait_max_s or 0, b.wait_max_seconds or 0)
        if b.zone_type == ZoneType.ENTRANCE_LINE.value:
            hours = hours or [0] * 24
            hours[local.hour] += b.in_count
            if b.staff_crossings is not None:
                totals.staff_crossings = (totals.staff_crossings or 0) + b.staff_crossings
            totals.customers_in = (totals.customers_in or 0) + b.in_count
            row = days.get(local.date())
            if row is not None:
                row.customers_in = (row.customers_in or 0) + b.in_count
        elif b.zone_type == ZoneType.DINING.value and b.max_occupancy is not None:
            totals.peak_occupancy = max(totals.peak_occupancy or 0, b.max_occupancy)
        elif b.zone_type == ZoneType.COUNTER.value and b.occupied_seconds is not None:
            staffed += b.occupied_seconds
            watched += b.bucket_seconds
    if hours and any(hours):
        totals.peak_hour = max(range(24), key=lambda h: hours[h])
    if totals.queue_waits:
        totals.queue_wait_avg_s = round(wait_total / totals.queue_waits)
    totals.counter_staffed_pct = _pct(staffed, watched)

    # Alert times (plan 6.7).
    deliveries = db.execute(
        select(
            AlertDelivery.status,
            AlertDelivery.latency_ms,
            AlertDelivery.delivered_at,
            Event.started_at,
        )
        .join(Event, Event.id == AlertDelivery.event_id)
        .where(
            Event.branch_id == branch.id,
            AlertDelivery.kind == AlertKind.ALERT.value,
            AlertDelivery.created_at >= since,
            AlertDelivery.created_at < until,
        )
    ).all()
    sent = [
        d
        for d in deliveries
        if d.status in (DeliveryStatus.SENT.value, DeliveryStatus.DELIVERED.value)
    ]
    stats = latency_stats([d.latency_ms for d in sent if d.latency_ms is not None])
    delivered = latency_stats(
        [
            int((d.delivered_at - d.started_at).total_seconds() * 1000)
            for d in sent
            if d.delivered_at
        ]
    )
    alerts = AlertSummary(
        sent=len(sent),
        failed=sum(d.status == DeliveryStatus.FAILED.value for d in deliveries),
        delivered_p95_ms=delivered["p95_ms"],
        **stats,
    )

    return ReportData(
        branch_id=branch.id,
        branch_name=branch.name,
        timezone=branch.timezone,
        period=period,
        start=start,
        end=end,
        generated_at=now,
        partial=now < until,
        totals=totals,
        days=list(days.values()),
        hours=hours,
        types=sorted(types.values(), key=lambda r: (-r.total, r.event_type)),
        staff=sorted(people.values(), key=lambda r: (r.name.lower(), r.employee_code)),
        security_events=security,
        alerts=alerts,
    )


def save_report(
    db: Session, branch: Branch, period: ReportPeriod, start: date, now: datetime | None = None
) -> Report:
    """Build and store (or replace) the report for the period containing `start`."""
    data = build_report(db, branch, period, start, now)
    report_id = db.scalar(
        insert(Report)
        .values(
            tenant_id=branch.tenant_id,
            branch_id=branch.id,
            period=period.value,
            period_start=data.start,
            period_end=data.end,
            generated_at=data.generated_at,
            data=data.model_dump(mode="json"),
        )
        .on_conflict_do_update(
            constraint="uq_reports_branch_id_period_period_start",
            set_={
                "period_end": data.end,
                "generated_at": data.generated_at,
                "data": data.model_dump(mode="json"),
            },
        )
        .returning(Report.id)
    )
    report = db.get_one(Report, report_id)
    db.refresh(report)
    return report


def period_label(period: str, start: date, end: date, locale: str = "en") -> str:
    if period == ReportPeriod.DAY.value:
        return start.isoformat()
    return f"{start} {texts.t(locale, 'report.to')} {end}"


def report_url(report_id: object) -> str:
    return f"{get_settings().web_url.rstrip('/')}/reports/?report={report_id}"


def notify_report(db: Session, report: Report, branch: Branch, senders: Senders) -> int:
    """Send the report link to users who asked for it and may see it. Returns messages sent."""
    users = db.scalars(
        select(User).where(User.is_active, func.cardinality(User.report_channels) > 0)
    ).all()
    access = set(
        db.scalars(select(UserBranchAccess.user_id).where(UserBranchAccess.branch_id == branch.id))
    )
    templates = get_settings().whatsapp_templates
    # Shadow mode (plan 9.4): only the support team's reviewers get the link.
    shadow = branch.shadow_until is not None and branch.shadow_until > datetime.now(timezone.utc)
    sent = 0
    for user in users:
        if shadow and not user.shadow_reviewer:
            continue
        role = UserRole(user.role)
        if Permission.REPORTS_VIEW not in ROLE_PERMISSIONS[role]:
            continue
        if role not in ALL_BRANCH_ROLES and user.id not in access:
            continue
        title = texts.t(user.locale, f"report.{report.period}")
        label = period_label(report.period, report.period_start, report.period_end, user.locale)
        text_line = texts.t(
            user.locale, "report.ready", title=title, branch=branch.name, period=label
        )
        for channel in user.report_channels:
            try:
                if channel == AlertChannel.EMAIL.value:
                    senders.mailer.send(
                        user.email,
                        f"{title}: {branch.name} ({label})",
                        f"{text_line}\n\n{report_url(report.id)}\n",
                    )
                elif (
                    channel == AlertChannel.WHATSAPP.value
                    and user.phone
                    and user.whatsapp_opt_in_at
                ):
                    senders.whatsapp.send_template(
                        user.phone,
                        templates["report"],
                        texts.lang(user.locale),
                        [title, branch.name, label],
                        str(report.id),
                    )
                else:
                    continue
                sent += 1
            except Exception as exc:  # one failed link doesn't stop the rest
                log.warning("report link to user %s by %s failed: %s", user.id, channel, exc)
    report.notified_at = datetime.now(timezone.utc)
    return sent


def _due_periods(local_today: date, created: date) -> list[tuple[ReportPeriod, date]]:
    """Finished periods whose report should exist (the last few days, to catch up)."""
    week_start = get_settings().report_week_start
    due = []
    for back in range(1, CATCH_UP_DAYS + 1):
        day = local_today - timedelta(days=back)
        if day < created:
            break
        due.append((ReportPeriod.DAY, day))
        for period in (ReportPeriod.WEEK, ReportPeriod.MONTH):
            start, end = period_bounds(period, day, week_start)
            if end == day:
                due.append((period, start))
    return due


def generate_due(tenant_id: uuid.UUID, senders: Senders, now: datetime | None = None) -> int:
    """The worker's job for one tenant: make missing reports, send their links."""
    now = now or datetime.now(timezone.utc)
    settings = get_settings()
    made = 0
    with tenant_session(tenant_id) as db:
        branches = db.scalars(select(Branch)).all()
        db.execute(
            delete(Report).where(
                Report.period_end < (now - timedelta(days=settings.report_retention_days)).date()
            )
        )
    for branch in branches:
        tz = ZoneInfo(branch.timezone)
        local_now = now.astimezone(tz)
        if local_now.hour < settings.report_hour:
            continue
        created = branch.created_at.astimezone(tz).date()
        for period, start in _due_periods(local_now.date(), created):
            with tenant_session(tenant_id) as db:
                exists = db.scalar(
                    select(Report.id).where(
                        Report.branch_id == branch.id,
                        Report.period == period.value,
                        Report.period_start == start,
                    )
                )
                if exists:
                    continue
                report = save_report(db, branch, period, start, now)
                made += 1
                notify_report(db, report, branch, senders)
                webhooks.queue_report(db, report, branch, report_url(report.id))
    return made
