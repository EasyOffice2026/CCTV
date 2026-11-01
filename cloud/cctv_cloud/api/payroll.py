"""Attendance for payroll (later phase: HRM integration): one row per staff member and day
with the times worked, by employee code, as JSON or CSV. For the owner and for the payroll
system's API key (`attendance:read`). See docs/integration/pos-and-payroll.md."""

import csv
import io
import uuid
from datetime import date, datetime, timedelta
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy import select

from cctv_cloud.api.deps import Context, context
from cctv_cloud.api.events import csv_cell
from cctv_cloud.db.enums import AttendanceSource, AttendanceStatus
from cctv_cloud.db.models import AttendanceRecord, Branch, Staff
from cctv_cloud.rbac import Permission
from cctv_cloud.services.audit import AuditAction, record

router = APIRouter(prefix="/v1/payroll", tags=["integrations"])

Payroll = Annotated[Context, Depends(context(Permission.PAYROLL_EXPORT), scope="function")]

MAX_DAYS = 62
CSV_COLUMNS = [
    "employee_code",
    "name",
    "branch",
    "date",
    "status",
    "check_in",
    "check_out",
    "worked_minutes",
    "corrected",
    "note",
]


class PayrollDay(BaseModel):
    staff_id: uuid.UUID
    employee_code: str
    name: str
    branch: str
    date: date
    status: AttendanceStatus
    check_in: str | None  # local time, HH:MM
    check_out: str | None
    worked_minutes: int
    corrected: bool  # set or changed by a manager
    note: str | None


class PayrollTotals(BaseModel):
    staff_id: uuid.UUID
    employee_code: str
    name: str
    days_on_time: int = 0
    days_late: int = 0
    days_absent: int = 0
    days_off: int = 0
    worked_minutes: int = 0


class PayrollOut(BaseModel):
    start: date
    end: date
    days: list[PayrollDay]
    staff: list[PayrollTotals]


def _local(value: datetime | None, tz: ZoneInfo) -> str | None:
    return value.astimezone(tz).strftime("%H:%M") if value else None


@router.get(
    "/attendance",
    response_model=PayrollOut,
    responses={200: {"content": {"text/csv": {}}, "description": "As JSON, or CSV"}},
)
def payroll_attendance(
    ctx: Payroll,
    request: Request,
    start: Annotated[date, Query(alias="from")],
    end: Annotated[date, Query(alias="to", description="Last day, included")],
    branch_id: uuid.UUID | None = None,
    format: Literal["json", "csv"] = "json",
) -> PayrollOut | Response:
    """Each staff member's work days from `from` to `to` (at most 62 days): status, check-in
    and check-out in the branch's time, minutes worked; and totals per person. Written to
    the audit log."""
    if not timedelta(0) <= end - start < timedelta(days=MAX_DAYS):
        raise HTTPException(422, f"choose at most {MAX_DAYS} days, from before to")
    query = (
        select(AttendanceRecord, Staff, Branch)
        .join(Staff, Staff.id == AttendanceRecord.staff_id)
        .join(Branch, Branch.id == AttendanceRecord.branch_id)
        .where(AttendanceRecord.work_date >= start, AttendanceRecord.work_date <= end)
        .order_by(Staff.employee_code, AttendanceRecord.work_date)
    )
    if branch_id is not None:
        query = query.where(AttendanceRecord.branch_id == branch_id)
    if ctx.principal.branch_ids is not None:
        query = query.where(AttendanceRecord.branch_id.in_(ctx.principal.branch_ids))
    days: list[PayrollDay] = []
    totals: dict[uuid.UUID, PayrollTotals] = {}
    for rec, staff, branch in ctx.db.execute(query).all():
        tz = ZoneInfo(branch.timezone)
        worked = 0
        if rec.check_in_at and rec.check_out_at and rec.check_out_at > rec.check_in_at:
            worked = int((rec.check_out_at - rec.check_in_at).total_seconds() // 60)
        status = AttendanceStatus(rec.status)
        days.append(
            PayrollDay(
                staff_id=staff.id,
                employee_code=staff.employee_code,
                name=staff.full_name,
                branch=branch.name,
                date=rec.work_date,
                status=status,
                check_in=_local(rec.check_in_at, tz),
                check_out=_local(rec.check_out_at, tz),
                worked_minutes=worked,
                corrected=rec.source == AttendanceSource.MANUAL.value,
                note=rec.note,
            )
        )
        total = totals.setdefault(
            staff.id,
            PayrollTotals(
                staff_id=staff.id, employee_code=staff.employee_code, name=staff.full_name
            ),
        )
        total.worked_minutes += worked
        if status is AttendanceStatus.ON_TIME:
            total.days_on_time += 1
        elif status is AttendanceStatus.LATE:
            total.days_late += 1
        elif status is AttendanceStatus.ABSENT:
            total.days_absent += 1
        else:
            total.days_off += 1
    record(
        ctx.db,
        ctx.principal,
        AuditAction.EXPORT_ATTENDANCE,
        "attendance",
        None,
        request,
        {"from": start.isoformat(), "to": end.isoformat(), "rows": len(days), "format": format},
    )
    if format == "json":
        return PayrollOut(start=start, end=end, days=days, staff=list(totals.values()))
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(CSV_COLUMNS)
    for day in days:
        writer.writerow(
            [
                csv_cell(v)
                for v in (
                    day.employee_code,
                    day.name,
                    day.branch,
                    day.date.isoformat(),
                    day.status.value,
                    day.check_in,
                    day.check_out,
                    day.worked_minutes,
                    "yes" if day.corrected else "no",
                    day.note,
                )
            ]
        )
    filename = f"attendance-{start.isoformat()}-{end.isoformat()}.csv"
    return Response(
        out.getvalue().encode("utf-8-sig"),  # Excel opens Arabic names correctly with a BOM
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
