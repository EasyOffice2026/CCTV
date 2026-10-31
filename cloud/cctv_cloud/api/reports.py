"""Branch reports (plan 6.6): list, view, (re)generate, and download as PDF or Excel.

Reports include attendance per person, so they need `reports_view`; downloads are written
to the audit log.
"""

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from cctv_cloud.api.deps import Context, context, not_found
from cctv_cloud.config import get_settings
from cctv_cloud.db.enums import Locale, ReportPeriod
from cctv_cloud.db.models import Branch, Report
from cctv_cloud.rbac import Permission
from cctv_cloud.services.audit import AuditAction, record
from cctv_cloud.services.report_files import file_name, render_pdf, render_xlsx
from cctv_cloud.services.reports import ReportData, save_report

router = APIRouter(prefix="/v1/reports", tags=["reports"])

Reader = Annotated[Context, Depends(context(Permission.REPORTS_VIEW), scope="function")]

FORMATS = {
    "pdf": "application/pdf",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


class ReportSummary(BaseModel):
    id: uuid.UUID
    branch_id: uuid.UUID
    period: ReportPeriod
    period_start: date
    period_end: date
    generated_at: datetime
    partial: bool


class ReportOut(ReportSummary):
    data: ReportData


class ReportRequest(BaseModel):
    branch_id: uuid.UUID
    period: ReportPeriod
    day: date = Field(description="Any day in the period; it may be the current period")


def _summary(report: Report) -> ReportSummary:
    return ReportSummary(
        id=report.id,
        branch_id=report.branch_id,
        period=ReportPeriod(report.period),
        period_start=report.period_start,
        period_end=report.period_end,
        generated_at=report.generated_at,
        partial=bool(report.data.get("partial")),
    )


def _out(report: Report) -> ReportOut:
    return ReportOut(**_summary(report).model_dump(), data=ReportData.model_validate(report.data))


def _branch(ctx: Context, branch_id: uuid.UUID) -> Branch:
    branch = ctx.db.get(Branch, branch_id)
    if branch is None or not ctx.principal.can_access_branch(branch_id):
        raise not_found("branch not found")
    return branch


def _report(ctx: Context, report_id: uuid.UUID) -> Report:
    report = ctx.db.get(Report, report_id)
    if report is None or not ctx.principal.can_access_branch(report.branch_id):
        raise not_found("report not found")
    return report


@router.get("", response_model=list[ReportSummary])
def list_reports(
    ctx: Reader,
    branch_id: uuid.UUID,
    period: ReportPeriod | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 60,
) -> list[ReportSummary]:
    """Newest first."""
    _branch(ctx, branch_id)
    query = select(Report).where(Report.branch_id == branch_id)
    if period is not None:
        query = query.where(Report.period == period.value)
    reports = ctx.db.scalars(query.order_by(Report.period_start.desc()).limit(limit))
    return [_summary(r) for r in reports]


@router.post("", response_model=ReportOut)
def generate_report(body: ReportRequest, ctx: Reader) -> ReportOut:
    """Make (or remake) the report of the period containing `date`, from current data."""
    branch = _branch(ctx, body.branch_id)
    today = datetime.now(timezone.utc).astimezone(ZoneInfo(branch.timezone)).date()
    if body.day > today:
        raise HTTPException(422, "the period hasn't started")
    if body.day < today - timedelta(days=get_settings().report_retention_days):
        raise HTTPException(422, "older than report retention")
    return _out(save_report(ctx.db, branch, body.period, body.day))


@router.get("/{report_id}", response_model=ReportOut)
def get_report(report_id: uuid.UUID, ctx: Reader) -> ReportOut:
    return _out(_report(ctx, report_id))


@router.get(
    "/{report_id}/file",
    response_class=Response,
    responses={200: {"content": {t: {} for t in FORMATS.values()}}},
)
def download_report(
    report_id: uuid.UUID,
    ctx: Reader,
    request: Request,
    format: Literal["pdf", "xlsx"] = "pdf",
    lang: Locale = Locale.EN,
) -> Response:
    report = _report(ctx, report_id)
    data = ReportData.model_validate(report.data)
    content = render_pdf(data, lang.value) if format == "pdf" else render_xlsx(data, lang.value)
    record(
        ctx.db,
        ctx.principal,
        AuditAction.EXPORT_REPORT,
        "report",
        report.id,
        request,
        {"format": format, "lang": lang.value},
    )
    return Response(
        content,
        media_type=FORMATS[format],
        headers={
            "Content-Disposition": f'attachment; filename="{file_name(data, format)}"',
            "Cache-Control": "no-store",
        },
    )
