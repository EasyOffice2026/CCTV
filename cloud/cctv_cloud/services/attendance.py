"""Staff attendance from face sightings (plan 5.4).

The edge box sends a sighting whenever it recognises an enrolled staff member (at most
about once a minute per person and camera). Sightings are not stored; each one moves the
person's work-day record: the first sighting is the check-in, the latest one the
check-out, and the check-in compared with the shift start (plus grace) gives on time or
late. A manager's correction (`source = 'manual'`) is never overwritten by sightings.

Work day: the branch-local calendar day of the sighting, except that for a shift running
past midnight (e.g. 16:00-02:00) sightings until NEXT_DAY_MARGIN after the shift end
still count for the day the shift started.

Absent: once a shift is over (plus grace), staff who are enrolled for face recognition,
have a shift and were not seen get an 'absent' record, on the days the branch is open.
Staff without a face template are skipped: the system can't see them, so it would mark
them absent every day.
"""

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from cctv_cloud.db.enums import AttendanceSource, AttendanceStatus, StaffStatus
from cctv_cloud.db.models import (
    AttendanceRecord,
    Branch,
    Camera,
    FaceTemplate,
    Shift,
    Staff,
    WorkingHours,
)
from cctv_contracts.v1 import BatchResult, RejectedItem, StaffSighting

log = logging.getLogger(__name__)

NEXT_DAY_MARGIN = timedelta(hours=4)
# Days looked back when marking absences, so a missed worker run catches up.
ABSENCE_LOOKBACK_DAYS = 2


@dataclass(frozen=True)
class ShiftTimes:
    starts_at: time
    ends_at: time
    grace_minutes: int

    @classmethod
    def of(cls, shift: Shift | None) -> "ShiftTimes | None":
        if shift is None:
            return None
        return cls(shift.starts_at, shift.ends_at, shift.grace_minutes)

    @property
    def overnight(self) -> bool:
        return self.ends_at <= self.starts_at

    def start_on(self, work_date: date, tz: ZoneInfo) -> datetime:
        return datetime.combine(work_date, self.starts_at, tz)

    def end_on(self, work_date: date, tz: ZoneInfo) -> datetime:
        end_date = work_date + timedelta(days=1) if self.overnight else work_date
        return datetime.combine(end_date, self.ends_at, tz)


def _seconds(value: time) -> float:
    return value.hour * 3600 + value.minute * 60 + value.second


def work_date_for(seen_at: datetime, tz: ZoneInfo, shift: ShiftTimes | None) -> date:
    local = seen_at.astimezone(tz)
    if shift is not None and shift.overnight:
        # Early-morning sightings belong to the previous day's shift, up to the margin
        # after its end (and never past the next start).
        limit = min(
            _seconds(shift.ends_at) + NEXT_DAY_MARGIN.total_seconds(), _seconds(shift.starts_at)
        )
        if _seconds(local.time()) < limit:
            return local.date() - timedelta(days=1)
    return local.date()


def status_for(
    check_in: datetime | None, work_date: date, tz: ZoneInfo, shift: ShiftTimes | None
) -> AttendanceStatus:
    if check_in is None:
        return AttendanceStatus.ABSENT
    if shift is None:
        return AttendanceStatus.ON_TIME
    deadline = shift.start_on(work_date, tz) + timedelta(minutes=shift.grace_minutes)
    return AttendanceStatus.LATE if check_in > deadline else AttendanceStatus.ON_TIME


def refresh_from_sightings(
    record: AttendanceRecord, tz: ZoneInfo, shift: ShiftTimes | None
) -> None:
    """Derive the effective times and status from what was detected."""
    first, last = record.first_seen_at, record.last_seen_at
    record.check_in_at = first
    record.check_out_at = last if first is not None and last is not None and last > first else None
    record.status = status_for(first, record.work_date, tz, shift).value


def _branch_tz(db: Session, branch_id: uuid.UUID) -> ZoneInfo:
    return ZoneInfo(db.get_one(Branch, branch_id).timezone)


def apply_sightings(
    db: Session,
    tenant_id: uuid.UUID,
    branch_id: uuid.UUID,
    sightings: Sequence[StaffSighting],
    now: datetime | None = None,
) -> BatchResult:
    """Fold sightings from the branch's edge box into attendance records."""
    now = now or datetime.now(timezone.utc)
    tz = _branch_tz(db, branch_id)
    cameras = set(db.scalars(select(Camera.id).where(Camera.branch_id == branch_id)))
    staff_ids = {s.staff_id for s in sightings}
    # Only active staff of this branch with recorded consent can be recognised.
    shifts: dict[uuid.UUID, ShiftTimes | None] = {
        row.id: ShiftTimes(row.starts_at, row.ends_at, row.grace_minutes)
        if row.starts_at is not None
        else None
        for row in db.execute(
            select(Staff.id, Shift.starts_at, Shift.ends_at, Shift.grace_minutes)
            .outerjoin(Shift, Shift.id == Staff.shift_id)
            .where(
                Staff.id.in_(staff_ids),
                Staff.branch_id == branch_id,
                Staff.status == StaffStatus.ACTIVE.value,
                Staff.consent_signed_at.is_not(None),
            )
        )
    }

    rejected: list[RejectedItem] = []
    days: dict[tuple[uuid.UUID, date], tuple[datetime, datetime]] = {}
    for index, sighting in enumerate(sightings):
        if sighting.camera_id not in cameras:
            rejected.append(RejectedItem(index=index, error="unknown camera"))
            continue
        if sighting.staff_id not in shifts:
            rejected.append(
                RejectedItem(index=index, error="unknown staff member, or not active/consented")
            )
            continue
        if sighting.seen_at > now + timedelta(minutes=5):
            rejected.append(RejectedItem(index=index, error="seen_at is in the future"))
            continue
        key = (
            sighting.staff_id,
            work_date_for(sighting.seen_at, tz, shifts[sighting.staff_id]),
        )
        first, last = days.get(key, (sighting.seen_at, sighting.seen_at))
        days[key] = (min(first, sighting.seen_at), max(last, sighting.seen_at))

    for (staff_id, work_date), (first, last) in sorted(days.items()):
        _record_day(db, tenant_id, branch_id, staff_id, work_date, first, last, tz, shifts, now)
    return BatchResult(accepted=len(sightings) - len(rejected), rejected=rejected)


def _record_day(
    db: Session,
    tenant_id: uuid.UUID,
    branch_id: uuid.UUID,
    staff_id: uuid.UUID,
    work_date: date,
    first: datetime,
    last: datetime,
    tz: ZoneInfo,
    shifts: dict[uuid.UUID, ShiftTimes | None],
    now: datetime,
) -> None:
    # Create the day if it's new; concurrent requests can't both insert it.
    db.execute(
        insert(AttendanceRecord)
        .values(
            tenant_id=tenant_id,
            branch_id=branch_id,
            staff_id=staff_id,
            work_date=work_date,
            status=AttendanceStatus.ABSENT.value,
            source=AttendanceSource.FACE.value,
        )
        .on_conflict_do_nothing(index_elements=["staff_id", "work_date"])
    )
    record = db.scalars(
        select(AttendanceRecord)
        .where(AttendanceRecord.staff_id == staff_id, AttendanceRecord.work_date == work_date)
        .with_for_update()
    ).one()
    record.first_seen_at = min(first, record.first_seen_at or first)
    record.last_seen_at = max(last, record.last_seen_at or last)
    if record.source == AttendanceSource.FACE.value:
        refresh_from_sightings(record, tz, shifts[staff_id])
    record.updated_at = now


def mark_absences(db: Session, now: datetime | None = None) -> int:
    """Add 'absent' records for finished shifts without a sighting (one tenant)."""
    now = now or datetime.now(timezone.utc)
    added = 0
    for branch in db.scalars(select(Branch)):
        tz = ZoneInfo(branch.timezone)
        open_days = set(
            db.scalars(select(WorkingHours.weekday).where(WorkingHours.branch_id == branch.id))
        )
        enrolled = (
            select(FaceTemplate.staff_id, func.min(FaceTemplate.created_at).label("since"))
            .group_by(FaceTemplate.staff_id)
            .subquery()
        )
        rows = db.execute(
            select(Staff.id, Shift, enrolled.c.since)
            .join(Shift, Shift.id == Staff.shift_id)
            .join(enrolled, enrolled.c.staff_id == Staff.id)
            .where(
                Staff.branch_id == branch.id,
                Staff.status == StaffStatus.ACTIVE.value,
                Staff.consent_signed_at.is_not(None),
            )
        ).all()
        today = now.astimezone(tz).date()
        for offset in range(ABSENCE_LOOKBACK_DAYS, -1, -1):
            day = today - timedelta(days=offset)
            # No working hours configured counts as open every day (like the edge).
            if open_days and day.weekday() not in open_days:
                continue
            for staff_id, shift, since in rows:
                times = ShiftTimes.of(shift)
                assert times is not None  # noqa: S101 - inner join
                if since > times.start_on(day, tz):
                    continue  # not enrolled yet that day
                if now < times.end_on(day, tz) + timedelta(minutes=times.grace_minutes):
                    continue
                created = db.scalar(
                    insert(AttendanceRecord)
                    .values(
                        tenant_id=branch.tenant_id,
                        branch_id=branch.id,
                        staff_id=staff_id,
                        work_date=day,
                        status=AttendanceStatus.ABSENT.value,
                        source=AttendanceSource.FACE.value,
                        updated_at=now,
                    )
                    .on_conflict_do_nothing(index_elements=["staff_id", "work_date"])
                    .returning(AttendanceRecord.id)
                )
                added += created is not None
    return added
