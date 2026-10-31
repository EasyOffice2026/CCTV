"""Attendance records and people counts for the dashboard (plan 5.2, 5.4).

Attendance comes from face sightings on the edge (`services/attendance.py`). Managers can
correct a day (late with a reason, forgotten check-out, day off); the correction keeps
the detected times next to it and can be removed to go back to them.
"""

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select

from cctv_cloud.api.admin import ORMModel
from cctv_cloud.api.deps import Context, context, not_found
from cctv_cloud.db.enums import AttendanceSource, AttendanceStatus
from cctv_cloud.db.models import AttendanceRecord, Branch, Camera, CountBucket, Shift, Staff, User
from cctv_cloud.rbac import Permission
from cctv_cloud.services.attendance import ShiftTimes, refresh_from_sightings
from cctv_cloud.services.audit import AuditAction, record

router = APIRouter(prefix="/v1", tags=["attendance"])

StaffAdmin = Annotated[Context, Depends(context(Permission.STAFF_MANAGE))]
Viewer = Annotated[Context, Depends(context(Permission.EVENTS_VIEW))]

MAX_DAYS = 62
MAX_COUNT_RANGE = timedelta(days=31)


class AttendanceOut(BaseModel):
    id: uuid.UUID
    staff_id: uuid.UUID
    staff_name: str
    employee_code: str
    branch_id: uuid.UUID
    work_date: date
    status: AttendanceStatus
    source: AttendanceSource
    check_in_at: datetime | None
    check_out_at: datetime | None
    first_seen_at: datetime | None = Field(description="Detected by face recognition")
    last_seen_at: datetime | None
    note: str | None
    corrected_by: str | None = Field(description="Name of the manager who corrected the day")
    corrected_at: datetime | None


class AttendanceCorrection(BaseModel):
    status: AttendanceStatus
    check_in_at: datetime | None = None
    check_out_at: datetime | None = None
    note: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _check(self) -> "AttendanceCorrection":
        for value in (self.check_in_at, self.check_out_at):
            if value is not None and value.tzinfo is None:
                raise ValueError("times need a UTC offset")
        if self.status in (AttendanceStatus.ABSENT, AttendanceStatus.DAY_OFF):
            if self.check_in_at or self.check_out_at:
                raise ValueError(f"{self.status.value} days have no check-in or check-out")
        elif self.check_in_at is None:
            raise ValueError(f"{self.status.value} needs check_in_at")
        if self.check_in_at and self.check_out_at and self.check_out_at < self.check_in_at:
            raise ValueError("check_out_at is before check_in_at")
        return self


def _out(ctx: Context, rows: list[AttendanceRecord]) -> list[AttendanceOut]:
    staff = {
        s.id: s
        for s in ctx.db.scalars(select(Staff).where(Staff.id.in_({r.staff_id for r in rows})))
    }
    managers = dict(
        ctx.db.execute(
            select(User.id, User.full_name).where(
                User.id.in_({r.corrected_by for r in rows if r.corrected_by})
            )
        ).all()
    )
    return [
        AttendanceOut(
            id=r.id,
            staff_id=r.staff_id,
            staff_name=staff[r.staff_id].full_name,
            employee_code=staff[r.staff_id].employee_code,
            branch_id=r.branch_id,
            work_date=r.work_date,
            status=AttendanceStatus(r.status),
            source=AttendanceSource(r.source),
            check_in_at=r.check_in_at,
            check_out_at=r.check_out_at,
            first_seen_at=r.first_seen_at,
            last_seen_at=r.last_seen_at,
            note=r.note,
            corrected_by=managers.get(r.corrected_by) if r.corrected_by else None,
            corrected_at=r.corrected_at,
        )
        for r in rows
    ]


@router.get("/attendance", response_model=list[AttendanceOut])
def list_attendance(
    ctx: StaffAdmin,
    date_from: date,
    date_to: date,
    branch_id: uuid.UUID | None = None,
    staff_id: uuid.UUID | None = None,
) -> list[AttendanceOut]:
    """Work days from `date_from` to `date_to` (inclusive, branch-local dates)."""
    if date_to < date_from:
        raise HTTPException(422, "date_to is before date_from")
    if (date_to - date_from).days >= MAX_DAYS:
        raise HTTPException(422, f"choose at most {MAX_DAYS} days")
    query = (
        select(AttendanceRecord)
        .where(AttendanceRecord.work_date.between(date_from, date_to))
        .order_by(AttendanceRecord.work_date.desc(), AttendanceRecord.check_in_at)
    )
    if branch_id is not None:
        query = query.where(AttendanceRecord.branch_id == branch_id)
    if staff_id is not None:
        query = query.where(AttendanceRecord.staff_id == staff_id)
    if ctx.principal.branch_ids is not None:
        query = query.where(AttendanceRecord.branch_id.in_(ctx.principal.branch_ids))
    return _out(ctx, list(ctx.db.scalars(query)))


def _staff_day(
    ctx: Context, staff_id: uuid.UUID, work_date: date
) -> tuple[Staff, AttendanceRecord | None]:
    staff = ctx.db.get(Staff, staff_id)
    if staff is None or not ctx.principal.can_access_branch(staff.branch_id):
        raise not_found("staff member not found")
    day = ctx.db.scalars(
        select(AttendanceRecord)
        .where(AttendanceRecord.staff_id == staff_id, AttendanceRecord.work_date == work_date)
        .with_for_update()
    ).one_or_none()
    if day is not None and not ctx.principal.can_access_branch(day.branch_id):
        raise not_found("attendance record not found")
    return staff, day


@router.put("/attendance/{staff_id}/{work_date}", response_model=AttendanceOut)
def correct_attendance(
    staff_id: uuid.UUID,
    work_date: date,
    body: AttendanceCorrection,
    ctx: StaffAdmin,
    request: Request,
) -> AttendanceOut:
    """Set a day by hand. Sightings keep updating the detected times but no longer change
    the corrected ones."""
    staff, day = _staff_day(ctx, staff_id, work_date)
    branch_id = day.branch_id if day is not None else staff.branch_id
    tz = ZoneInfo(ctx.db.get_one(Branch, branch_id).timezone)
    today = datetime.now(timezone.utc).astimezone(tz).date()
    if work_date > today and body.status is not AttendanceStatus.DAY_OFF:
        raise HTTPException(422, "only a day off can be set in advance")
    for value in (body.check_in_at, body.check_out_at):
        # A shift may end the next morning, but not later.
        if value is not None and not (
            work_date <= value.astimezone(tz).date() <= work_date + timedelta(days=1)
        ):
            raise HTTPException(422, "times must be on the work day or the morning after")
    if day is None:
        day = AttendanceRecord(
            tenant_id=staff.tenant_id, branch_id=branch_id, staff_id=staff_id, work_date=work_date
        )
        ctx.db.add(day)
    now = datetime.now(timezone.utc)
    day.status = body.status.value
    day.source = AttendanceSource.MANUAL.value
    day.check_in_at = body.check_in_at
    day.check_out_at = body.check_out_at
    day.note = body.note
    day.corrected_by = ctx.principal.user_id
    day.corrected_at = now
    day.updated_at = now
    ctx.db.flush()
    record(
        ctx.db,
        ctx.principal,
        AuditAction.ATTENDANCE_CHANGE,
        "attendance",
        day.id,
        request,
        {"staff_id": str(staff_id), "work_date": work_date.isoformat(), "status": day.status},
    )
    return _out(ctx, [day])[0]


@router.delete("/attendance/{staff_id}/{work_date}", response_model=AttendanceOut | None)
def remove_correction(
    staff_id: uuid.UUID, work_date: date, ctx: StaffAdmin, request: Request
) -> AttendanceOut | None:
    """Go back to what face recognition detected for the day. Returns null when nothing
    was detected that day: the record is removed."""
    staff, day = _staff_day(ctx, staff_id, work_date)
    if day is None or day.source != AttendanceSource.MANUAL.value:
        raise not_found("no correction for this day")
    record(
        ctx.db,
        ctx.principal,
        AuditAction.ATTENDANCE_CHANGE,
        "attendance",
        day.id,
        request,
        {"staff_id": str(staff_id), "work_date": work_date.isoformat(), "correction": "removed"},
    )
    if day.first_seen_at is None:
        # Nothing detected; the absence job recreates the day if it was a missed shift.
        ctx.db.delete(day)
        ctx.db.flush()
        return None
    shift = ctx.db.get(Shift, staff.shift_id) if staff.shift_id else None
    tz = ZoneInfo(ctx.db.get_one(Branch, day.branch_id).timezone)
    day.source = AttendanceSource.FACE.value
    day.note = day.corrected_by = day.corrected_at = None
    day.updated_at = datetime.now(timezone.utc)
    refresh_from_sightings(day, tz, ShiftTimes.of(shift))
    ctx.db.flush()
    return _out(ctx, [day])[0]


# --- counts ---------------------------------------------------------------------------


class CountOut(ORMModel):
    camera_id: uuid.UUID
    zone_id: uuid.UUID | None
    bucket_start: datetime
    bucket_seconds: int
    in_count: int
    out_count: int
    max_occupancy: int | None
    occupied_seconds: int | None


@router.get("/counts", response_model=list[CountOut], tags=["counts"])
def list_counts(
    ctx: Viewer,
    start: Annotated[datetime, Query(alias="from")],
    end: Annotated[datetime, Query(alias="to")],
    branch_id: uuid.UUID | None = None,
    camera_id: uuid.UUID | None = None,
    zone_id: uuid.UUID | None = None,
) -> list[CountBucket]:
    """People counts (entrance lines) and zone occupancy, per 5-minute bucket."""
    if start.tzinfo is None or end.tzinfo is None:
        raise HTTPException(422, "from and to need a UTC offset")
    if not timedelta(0) < end - start <= MAX_COUNT_RANGE:
        raise HTTPException(422, f"choose a range of at most {MAX_COUNT_RANGE.days} days")
    query = (
        select(CountBucket)
        .join(Camera, Camera.id == CountBucket.camera_id)
        .where(CountBucket.bucket_start >= start, CountBucket.bucket_start < end)
        .order_by(CountBucket.bucket_start, CountBucket.camera_id)
    )
    if branch_id is not None:
        query = query.where(Camera.branch_id == branch_id)
    if camera_id is not None:
        query = query.where(CountBucket.camera_id == camera_id)
    if zone_id is not None:
        query = query.where(CountBucket.zone_id == zone_id)
    if ctx.principal.branch_ids is not None:
        query = query.where(Camera.branch_id.in_(ctx.principal.branch_ids))
    return list(ctx.db.scalars(query))
