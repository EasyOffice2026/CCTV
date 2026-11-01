"""Figures of the official pilot (plan 10.3, 10.5), per day and for the whole period,
against the brief's success criteria:

| Check | Target | From |
|---|---|---|
| Accuracy | precision >= 90% per feature | review results (valid / (valid + false alarm)) |
| False alarms | < 5 per branch per day | events marked false alarm / days |
| Alert time | p95 < 10 s | alert latency per delivery (plan 6.7) |
| Uptime | >= 99% | camera minutes streaming / expected (heartbeats, plan 9.2) |
| Pilot period | >= 30 days | from the branch's pilot start |

Recall can't be measured from reviews (missed events are never reviewed): it comes from
the offline evaluation on the labelled test set (ml/README.md), which the sign-off form
asks for next to these figures. Search speed and the owner's feedback are checked by hand.
"""

import uuid
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from pydantic import BaseModel
from sqlalchemy import Date, cast, func, select
from sqlalchemy.orm import Session

from cctv_cloud.db.enums import AlertKind, DeliveryStatus, ReviewStatus
from cctv_cloud.db.models import AlertDelivery, Branch, Camera, CameraHealthMinute, Event
from cctv_cloud.services.alerts import latency_stats

TARGET_PRECISION = 0.9
TARGET_FALSE_PER_DAY = 5.0
TARGET_ALERT_P95_MS = 10_000
TARGET_UPTIME_PCT = 99.0
TARGET_DAYS = 30
# A feature needs this many reviewed events before its precision says anything.
MIN_REVIEWED = 20


class Counts(BaseModel):
    events: int = 0
    reviewed: int = 0
    valid: int = 0
    false_alarm: int = 0
    precision: float | None = None
    false_alarms_per_day: float | None = None


class DayKpis(Counts):
    date: date
    alerts_sent: int = 0
    alert_p95_ms: int | None = None
    camera_uptime_pct: float | None = None
    box_uptime_pct: float | None = None


class FeatureKpis(Counts):
    feature: str
    meets_target: bool | None = None  # None: fewer than MIN_REVIEWED reviewed


class Check(BaseModel):
    key: str  # accuracy | false_alarms | alert_time | uptime | pilot_period
    value: float | None
    target: float
    met: bool | None  # None: not enough data yet


class PilotKpis(BaseModel):
    branch_id: uuid.UUID
    pilot_started_on: date | None
    pilot_day: int | None  # 1 on the start day
    since: date
    until: date
    summary: DayKpis  # the whole period (date = until)
    features: list[FeatureKpis]
    days: list[DayKpis]
    checks: list[Check]


def _finish(c: Counts, days: int) -> None:
    decided = c.valid + c.false_alarm
    c.precision = round(c.valid / decided, 3) if decided else None
    c.false_alarms_per_day = round(c.false_alarm / days, 2) if days else None


def pilot_kpis(
    db: Session, branch: Branch, since: date, until: date, now: datetime | None = None
) -> PilotKpis:
    now = now or datetime.now(timezone.utc)
    tz = ZoneInfo(branch.timezone)
    start = datetime.combine(since, time(), tz)
    end = min(datetime.combine(until + timedelta(days=1), time(), tz), now)
    days = {
        since + timedelta(days=i): DayKpis(date=since + timedelta(days=i))
        for i in range((until - since).days + 1)
    }
    elapsed_days = max((end - start).total_seconds() / 86400, 1 / 24)
    summary = DayKpis(date=until)
    features: dict[str, FeatureKpis] = {}

    for e in db.execute(
        select(Event.feature_key, Event.review_status, Event.started_at).where(
            Event.branch_id == branch.id, Event.started_at >= start, Event.started_at < end
        )
    ):
        feature = features.setdefault(e.feature_key, FeatureKpis(feature=e.feature_key))
        for c in (days.get(e.started_at.astimezone(tz).date()), summary, feature):
            if c is None:
                continue
            c.events += 1
            c.reviewed += e.review_status != ReviewStatus.NEW.value
            c.valid += e.review_status == ReviewStatus.VALID.value
            c.false_alarm += e.review_status == ReviewStatus.FALSE_ALARM.value

    # Alert times (plan 6.7).
    latencies: dict[date, list[int]] = defaultdict(list)
    for d in db.execute(
        select(AlertDelivery.latency_ms, AlertDelivery.created_at)
        .join(Event, Event.id == AlertDelivery.event_id)
        .where(
            Event.branch_id == branch.id,
            AlertDelivery.kind == AlertKind.ALERT.value,
            AlertDelivery.status.in_([DeliveryStatus.SENT.value, DeliveryStatus.DELIVERED.value]),
            AlertDelivery.created_at >= start,
            AlertDelivery.created_at < end,
        )
    ):
        if d.latency_ms is not None:
            latencies[d.created_at.astimezone(tz).date()].append(d.latency_ms)

    # Uptime from the heartbeats: camera minutes streaming, and minutes the box reported.
    cameras = (
        db.scalar(select(func.count()).where(Camera.branch_id == branch.id, Camera.enabled)) or 0
    )
    local_day = cast(func.timezone(branch.timezone, CameraHealthMinute.minute), Date)
    health = {
        row.day: row
        for row in db.execute(
            select(
                local_day.label("day"),
                func.count().filter(CameraHealthMinute.offline == 0).label("good"),
                func.count(func.distinct(CameraHealthMinute.minute)).label("box"),
            )
            .join(Camera, Camera.id == CameraHealthMinute.camera_id)
            .where(
                Camera.branch_id == branch.id,
                Camera.enabled,
                CameraHealthMinute.minute >= start,
                CameraHealthMinute.minute < end,
            )
            .group_by(local_day)
        )
    }
    good_total = box_total = expected_total = 0
    for day, row in days.items():
        day_start = max(datetime.combine(day, time(), tz), start)
        day_end = min(datetime.combine(day + timedelta(days=1), time(), tz), end)
        minutes = max(0, int((day_end - day_start).total_seconds() // 60))
        stats = latency_stats(latencies.get(day, []))
        row.alerts_sent = len(latencies.get(day, []))
        row.alert_p95_ms = stats["p95_ms"]
        h = health.get(day)
        if minutes and cameras:
            good = h.good if h else 0
            box = h.box if h else 0
            row.camera_uptime_pct = round(100 * good / (cameras * minutes), 2)
            row.box_uptime_pct = round(100 * box / minutes, 2)
            good_total += good
            box_total += box
            expected_total += minutes
        _finish(row, 1)

    every = [ms for values in latencies.values() for ms in values]
    summary.alerts_sent = len(every)
    summary.alert_p95_ms = latency_stats(every)["p95_ms"]
    if expected_total and cameras:
        summary.camera_uptime_pct = round(100 * good_total / (cameras * expected_total), 2)
        summary.box_uptime_pct = round(100 * box_total / expected_total, 2)
    _finish(summary, round(elapsed_days, 2))
    for f in features.values():
        _finish(f, round(elapsed_days, 2))
        decided = f.valid + f.false_alarm
        f.meets_target = (f.precision or 0) >= TARGET_PRECISION if decided >= MIN_REVIEWED else None

    pilot_day = None
    if branch.pilot_started_on is not None:
        pilot_day = (now.astimezone(tz).date() - branch.pilot_started_on).days + 1
    judged = [f for f in features.values() if f.meets_target is not None]
    checks = [
        Check(
            key="accuracy",
            value=min((f.precision or 0) for f in judged) if judged else None,
            target=TARGET_PRECISION,
            met=all(f.meets_target for f in judged) if judged else None,
        ),
        Check(
            key="false_alarms",
            value=summary.false_alarms_per_day,
            target=TARGET_FALSE_PER_DAY,
            met=summary.false_alarms_per_day < TARGET_FALSE_PER_DAY if summary.reviewed else None,
        ),
        Check(
            key="alert_time",
            value=summary.alert_p95_ms,
            target=TARGET_ALERT_P95_MS,
            met=summary.alert_p95_ms < TARGET_ALERT_P95_MS
            if summary.alert_p95_ms is not None
            else None,
        ),
        Check(
            key="uptime",
            value=summary.camera_uptime_pct,
            target=TARGET_UPTIME_PCT,
            met=summary.camera_uptime_pct >= TARGET_UPTIME_PCT
            if summary.camera_uptime_pct is not None
            else None,
        ),
        Check(
            key="pilot_period",
            value=pilot_day,
            target=TARGET_DAYS,
            met=(pilot_day >= TARGET_DAYS) if pilot_day is not None else None,
        ),
    ]
    return PilotKpis(
        branch_id=branch.id,
        pilot_started_on=branch.pilot_started_on,
        pilot_day=pilot_day,
        since=since,
        until=until,
        summary=summary,
        features=sorted(features.values(), key=lambda f: f.feature),
        days=list(days.values()),
        checks=checks,
    )
