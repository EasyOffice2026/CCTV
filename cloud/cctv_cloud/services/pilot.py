"""Install and shadow mode (plan 9): stream health per minute and the setup test, shadow
mode, and the review figures used for tuning.

Setup test (9.2, the brief's test #1): every camera streams for 48 h without a drop.
Each heartbeat (every 30 s) adds a sample to the camera's row for that minute; the box
also counts how often a stream ended and was reopened. A camera passes when, over the
whole window, the stream never restarted, no minute was offline, and no minute is missing
(a missing minute means the box didn't report, so streaming can't be shown).

Reviewed events can be exported with their media for retraining (9.5, `cli
export-reviewed`).

Shadow mode (9.4): while `branches.shadow_until` is in the future, alerts only go to
users marked `shadow_reviewer` (our support team's accounts in the tenant), and no
webhooks or report links go to the client. Events are stored and shown as usual.
"""

import csv
import json
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from cctv_cloud.db.enums import ReviewStatus
from cctv_cloud.db.models import Branch, Camera, CameraHealthMinute, Event, Zone
from cctv_cloud.db.session import tenant_session
from cctv_cloud.services.audit import AuditAction, record
from cctv_cloud.services.storage import ObjectStorage
from cctv_contracts.enums import CameraHealth, MediaKind
from cctv_contracts.v1 import CameraStatus

# Kept for the setup test and the pilot's uptime figures, then removed by the retention job.
KEEP_HEALTH = timedelta(days=60)
MAX_TEST_HOURS = 24 * 7


def shadow_active(shadow_until: datetime | None, now: datetime) -> bool:
    return shadow_until is not None and shadow_until > now


def branch_in_shadow(db: Session, branch_id: uuid.UUID, now: datetime) -> bool:
    return shadow_active(db.scalar(select(Branch.shadow_until).where(Branch.id == branch_id)), now)


# --- stream health ----------------------------------------------------------------------


def record_health(
    db: Session, tenant_id: uuid.UUID, branch_id: uuid.UUID, cameras: list[CameraStatus], now
) -> None:
    """From a heartbeat: current health on the camera, and a sample for this minute.
    Only cameras of the device's branch are taken."""
    if not cameras:
        return
    known = {
        c.id: c
        for c in db.scalars(
            select(Camera).where(
                Camera.branch_id == branch_id, Camera.id.in_([c.camera_id for c in cameras])
            )
        )
    }
    minute = now.replace(second=0, microsecond=0)
    for status in cameras:
        camera = known.get(status.camera_id)
        if camera is None:
            continue
        # The box counts up from 0 after each start of its service.
        current, previous = status.stream_restarts, camera.stream_restarts
        new = current - previous if current >= previous else current
        camera.health = status.health.value
        camera.last_seen_at = status.last_frame_at or camera.last_seen_at
        camera.stream_restarts = status.stream_restarts
        offline = int(status.health is CameraHealth.OFFLINE)
        degraded = int(status.health is CameraHealth.DEGRADED)
        fps = status.fps if not offline else None
        table = CameraHealthMinute.__table__
        db.execute(
            insert(CameraHealthMinute)
            .values(
                tenant_id=tenant_id,
                camera_id=camera.id,
                minute=minute,
                samples=1,
                degraded=degraded,
                offline=offline,
                restarts=min(new, 32_000),
                min_fps=fps,
            )
            .on_conflict_do_update(
                index_elements=["camera_id", "minute"],
                set_={
                    "samples": table.c.samples + 1,
                    "degraded": table.c.degraded + degraded,
                    "offline": table.c.offline + offline,
                    "restarts": func.least(table.c.restarts + new, 32_000),
                    "min_fps": func.least(table.c.min_fps, fps),
                },
            )
        )


def first_heartbeat(db: Session, branch: Branch, since: datetime) -> datetime | None:
    return db.scalar(
        select(func.min(CameraHealthMinute.minute))
        .join(Camera, Camera.id == CameraHealthMinute.camera_id)
        .where(Camera.branch_id == branch.id, CameraHealthMinute.minute >= since)
    )


class CameraTest(BaseModel):
    camera_id: uuid.UUID
    name: str
    expected_minutes: int
    reported_minutes: int
    missing_minutes: int  # no heartbeat from the box
    offline_minutes: int
    degraded_minutes: int  # streaming below 60% of the analysis frame rate
    restarts: int
    longest_outage_minutes: int  # offline or missing in a row
    uptime_pct: float | None
    min_fps: float | None
    hours: list[float | None]  # share of good minutes per hour of the window; None: future
    passed: bool | None  # None while the window isn't over
    problems: list[str]


class StreamTest(BaseModel):
    branch_id: uuid.UUID
    since: datetime
    until: datetime
    generated_at: datetime
    complete: bool
    passed: bool | None
    cameras: list[CameraTest]


def stream_test(
    db: Session, branch: Branch, since: datetime, until: datetime, now: datetime | None = None
) -> StreamTest:
    now = now or datetime.now(timezone.utc)
    since = since.replace(second=0, microsecond=0)
    until = until.replace(second=0, microsecond=0)
    end = min(until, now.replace(second=0, microsecond=0))
    total = max(0, int((end - since).total_seconds() // 60))
    cameras = db.scalars(
        select(Camera).where(Camera.branch_id == branch.id, Camera.enabled).order_by(Camera.name)
    ).all()
    rows: dict[uuid.UUID, dict[datetime, CameraHealthMinute]] = defaultdict(dict)
    for row in db.scalars(
        select(CameraHealthMinute).where(
            CameraHealthMinute.camera_id.in_([c.id for c in cameras]),
            CameraHealthMinute.minute >= since,
            CameraHealthMinute.minute < end,
        )
    ):
        rows[row.camera_id][row.minute] = row
    complete = now >= until
    hours_in_window = int((until - since).total_seconds() // 3600) or 1
    results = []
    for camera in cameras:
        minutes = rows.get(camera.id, {})
        offline = degraded = restarts = run = longest = 0
        good_by_hour = [0] * hours_in_window
        fps_values = []
        for i in range(total):
            row = minutes.get(since + timedelta(minutes=i))
            bad = row is None or row.offline > 0
            if row is not None:
                offline += row.offline > 0
                degraded += row.degraded > 0 and row.offline == 0
                restarts += row.restarts
                if row.min_fps is not None:
                    fps_values.append(row.min_fps)
            run = run + 1 if bad else 0
            longest = max(longest, run)
            if not bad and i // 60 < hours_in_window:
                good_by_hour[i // 60] += 1
        reported = len(minutes)
        missing = total - reported
        hours = [
            round(good / min(60, total - h * 60), 3) if h * 60 < total else None
            for h, good in enumerate(good_by_hour)
        ]
        problems = []
        if restarts:
            problems.append(f"{restarts} stream restarts")
        if offline:
            problems.append(f"{offline} min offline")
        if missing:
            problems.append(f"{missing} min without a heartbeat")
        if degraded:
            problems.append(f"{degraded} min below the frame rate")  # noted, doesn't fail
        failed = bool(restarts or offline or missing)
        results.append(
            CameraTest(
                camera_id=camera.id,
                name=camera.name,
                expected_minutes=total,
                reported_minutes=reported,
                missing_minutes=missing,
                offline_minutes=offline,
                degraded_minutes=degraded,
                restarts=restarts,
                longest_outage_minutes=longest,
                uptime_pct=round(100 * (reported - offline) / total, 2) if total else None,
                min_fps=min(fps_values) if fps_values else None,
                hours=hours,
                passed=(not failed) if complete else (False if failed else None),
                problems=problems,
            )
        )
    if any(r.passed is False for r in results) or (complete and not results):
        passed: bool | None = False
    else:
        passed = True if complete else None
    return StreamTest(
        branch_id=branch.id,
        since=since,
        until=until,
        generated_at=now,
        complete=complete,
        passed=passed,
        cameras=results,
    )


# --- review figures (9.4, 9.5) -----------------------------------------------------------


class DayReview(BaseModel):
    date: date
    total: int
    reviewed: int  # valid, false alarm or reviewed
    valid: int
    false_alarm: int


class GroupReview(BaseModel):
    feature: str
    event_type: str
    camera_id: uuid.UUID
    camera: str
    zone_id: uuid.UUID | None
    zone: str | None
    total: int
    valid: int
    false_alarm: int
    unreviewed: int
    precision: float | None  # valid / (valid + false alarm)
    false_alarms_per_day: float
    mean_confidence_valid: float | None
    mean_confidence_false: float | None


class ReviewSummary(BaseModel):
    branch_id: uuid.UUID
    since: date
    until: date  # last day included
    days: list[DayReview]
    groups: list[GroupReview]  # most false alarms first


def review_summary(db: Session, branch: Branch, since: date, until: date) -> ReviewSummary:
    tz = ZoneInfo(branch.timezone)
    start = datetime.combine(since, datetime.min.time(), tz)
    end = datetime.combine(until + timedelta(days=1), datetime.min.time(), tz)
    n_days = (until - since).days + 1
    cameras = dict(
        db.execute(select(Camera.id, Camera.name).where(Camera.branch_id == branch.id)).all()
    )
    zones = dict(
        db.execute(
            select(Zone.id, Zone.name)
            .join(Camera, Camera.id == Zone.camera_id)
            .where(Camera.branch_id == branch.id)
        ).all()
    )
    days = {
        since + timedelta(days=i): DayReview(
            date=since + timedelta(days=i), total=0, reviewed=0, valid=0, false_alarm=0
        )
        for i in range(n_days)
    }
    groups: dict[tuple, dict] = {}
    rows = db.execute(
        select(
            Event.feature_key,
            Event.event_type,
            Event.camera_id,
            Event.zone_id,
            Event.review_status,
            Event.confidence,
            Event.started_at,
        ).where(Event.branch_id == branch.id, Event.started_at >= start, Event.started_at < end)
    ).all()
    for e in rows:
        valid = e.review_status == ReviewStatus.VALID.value
        false = e.review_status == ReviewStatus.FALSE_ALARM.value
        day = days.get(e.started_at.astimezone(tz).date())
        if day is not None:
            day.total += 1
            day.reviewed += e.review_status != ReviewStatus.NEW.value
            day.valid += valid
            day.false_alarm += false
        g = groups.setdefault(
            (e.feature_key, e.event_type, e.camera_id, e.zone_id),
            {"total": 0, "valid": 0, "false": 0, "new": 0, "cv": [], "cf": []},
        )
        g["total"] += 1
        g["valid"] += valid
        g["false"] += false
        g["new"] += e.review_status == ReviewStatus.NEW.value
        if valid:
            g["cv"].append(e.confidence)
        if false:
            g["cf"].append(e.confidence)

    def mean(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 3) if values else None

    out = [
        GroupReview(
            feature=feature,
            event_type=event_type,
            camera_id=camera_id,
            camera=cameras.get(camera_id, ""),
            zone_id=zone_id,
            zone=zones.get(zone_id) if zone_id else None,
            total=g["total"],
            valid=g["valid"],
            false_alarm=g["false"],
            unreviewed=g["new"],
            precision=round(g["valid"] / (g["valid"] + g["false"]), 3)
            if g["valid"] + g["false"]
            else None,
            false_alarms_per_day=round(g["false"] / n_days, 2),
            mean_confidence_valid=mean(g["cv"]),
            mean_confidence_false=mean(g["cf"]),
        )
        for (feature, event_type, camera_id, zone_id), g in groups.items()
    ]
    out.sort(key=lambda g: (-g.false_alarm, -g.total, g.event_type, g.camera))
    return ReviewSummary(
        branch_id=branch.id, since=since, until=until, days=list(days.values()), groups=out
    )


# --- reviewed events for retraining (9.5) ---------------------------------------------------

EXPORT_COLUMNS = [
    "event_id",
    "branch_id",
    "camera_id",
    "camera",
    "zone",
    "feature",
    "event_type",
    "severity",
    "started_at",
    "ended_at",
    "confidence",
    "review_status",
    "review_note",
    "snapshot_file",
    "clip_file",
    "attributes",
]


def export_reviewed(
    storage: ObjectStorage,
    tenant_id: uuid.UUID,
    out: Path,
    since: datetime,
    until: datetime,
    statuses: list[str],
    media: list[MediaKind],
    branch_id: uuid.UUID | None = None,
) -> int:
    """Reviewed events with their snapshots/clips, for hard-negative mining (plan 9.5):
    `out/manifest.csv` (for `python -m cctv_ml.review_sweep`) and one file per media.
    Files already in `out` aren't fetched again. Every media file fetched is written to
    the audit log, like a download on the dashboard. Returns the number of events."""
    out.mkdir(parents=True, exist_ok=True)
    with tenant_session(tenant_id) as db:
        query = (
            select(Event, Camera.name, Zone.name)
            .join(Camera, Camera.id == Event.camera_id)
            .outerjoin(Zone, Zone.id == Event.zone_id)
            .where(
                Event.review_status.in_(statuses),
                Event.started_at >= since,
                Event.started_at < until,
            )
            .order_by(Event.started_at)
        )
        if branch_id is not None:
            query = query.where(Event.branch_id == branch_id)
        manifest = []
        for event, camera_name, zone_name in db.execute(query).all():
            files = {}
            for kind in media:
                key = event.snapshot_key if kind is MediaKind.SNAPSHOT else event.clip_key
                if not key:
                    continue
                name = f"{event.id}-{kind.value}{Path(key).suffix}"
                if not (out / name).exists():
                    storage.download(key, out / name)
                    action = (
                        AuditAction.EXPORT_SNAPSHOT
                        if kind is MediaKind.SNAPSHOT
                        else AuditAction.EXPORT_CLIP
                    )
                    record(
                        db,
                        None,
                        action,
                        "event",
                        event.id,
                        None,
                        {"via": "cli", "purpose": "training"},
                        tenant_id=tenant_id,
                    )
                files[kind] = name
            manifest.append(
                {
                    "event_id": event.id,
                    "branch_id": event.branch_id,
                    "camera_id": event.camera_id,
                    "camera": camera_name,
                    "zone": zone_name or "",
                    "feature": event.feature_key,
                    "event_type": event.event_type,
                    "severity": event.severity,
                    "started_at": event.started_at.isoformat(),
                    "ended_at": event.ended_at.isoformat() if event.ended_at else "",
                    "confidence": event.confidence,
                    "review_status": event.review_status,
                    "review_note": event.review_note or "",
                    "snapshot_file": files.get(MediaKind.SNAPSHOT, ""),
                    "clip_file": files.get(MediaKind.CLIP, ""),
                    "attributes": json.dumps(event.attributes, ensure_ascii=False, sort_keys=True),
                }
            )
    with (out / "manifest.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, EXPORT_COLUMNS)
        writer.writeheader()
        writer.writerows(manifest)
    return len(manifest)
