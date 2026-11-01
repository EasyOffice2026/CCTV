"""Owner overview (plan 8.2-8.4): each branch's day at a glance, for the dashboard's
Overview page, the same page embedded in the client's operational system, and
integrations that pull KPIs with an API key (scope `kpis:read`).

The sections follow the feature toggles (8.3): a feature that is off on every camera of
the branch has no section (null) and its events aren't listed. Branch totals only;
recent events are listed for callers who may view events (their snapshots and clips are
separate, audited requests), recent reports for callers who may read reports.
"""

import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select

from cctv_cloud.api.deps import Context, context, not_found
from cctv_cloud.api.events import EventOut, event_out
from cctv_cloud.config import get_settings
from cctv_cloud.db.enums import DeviceStatus, ReportPeriod
from cctv_cloud.db.models import Branch, Camera, EdgeDevice, Event, Feature, Report
from cctv_cloud.rbac import Permission
from cctv_cloud.services.edge_config import resolve_features
from cctv_cloud.services.reports import AlertSummary, build_report
from cctv_contracts.enums import CameraHealth, FeatureKey

router = APIRouter(prefix="/v1", tags=["overview"])

KpiReader = Annotated[Context, Depends(context(Permission.KPIS_VIEW), scope="function")]


class EventKpis(BaseModel):
    total: int
    critical: int  # not dismissed as false alarms
    false_alarms: int
    unreviewed: int


class AttendanceKpis(BaseModel):
    on_time: int
    late: int
    absent: int
    day_off: int
    attendance_pct: float | None  # present / (present + absent)
    punctuality_pct: float | None  # on time / present


class CashierKpis(BaseModel):
    issues: int
    counter_staffed_pct: float | None


class CustomerKpis(BaseModel):
    customers_in: int | None  # None: no entrance counting line
    peak_hour: int | None  # branch-time hour with the most customers in
    peak_occupancy: int | None
    hours: list[int] | None  # customers in per branch-time hour (0-23)


class SystemKpis(BaseModel):
    devices_online: int
    devices_offline: int
    cameras: int
    cameras_online: int


class ReportLink(BaseModel):
    id: uuid.UUID
    period: ReportPeriod
    period_start: date
    period_end: date


class BranchOverview(BaseModel):
    branch_id: uuid.UUID
    branch_name: str
    timezone: str
    day: date
    generated_at: datetime
    partial: bool  # the day isn't over yet
    features: dict[str, bool]  # on for at least one camera of the branch
    events: EventKpis
    attendance: AttendanceKpis | None
    kitchen_compliance: int | None  # issues not dismissed as false alarms
    cashier: CashierKpis | None
    security: int | None
    customers: CustomerKpis | None
    alerts: AlertSummary
    system: SystemKpis
    recent_events: list[EventOut] | None
    reports: list[ReportLink] | None


def branch_features(ctx: Context, branch: Branch) -> dict[str, bool]:
    cameras = ctx.db.scalars(
        select(Camera.id).where(Camera.branch_id == branch.id, Camera.enabled)
    ).all()
    on = dict.fromkeys(sorted(ctx.db.scalars(select(Feature.key))), False)
    for per_camera in resolve_features(ctx.db, branch.id, cameras).values():
        for key, feature in per_camera.items():
            on[key] = on[key] or feature.enabled
    return on


def _system(ctx: Context, branch: Branch) -> SystemKpis:
    devices = dict(
        ctx.db.execute(
            select(EdgeDevice.status, func.count())
            .where(EdgeDevice.branch_id == branch.id)
            .group_by(EdgeDevice.status)
        ).all()
    )
    cameras = ctx.db.execute(
        select(Camera.health, func.count())
        .where(Camera.branch_id == branch.id, Camera.enabled)
        .group_by(Camera.health)
    ).all()
    return SystemKpis(
        devices_online=devices.get(DeviceStatus.ONLINE.value, 0),
        devices_offline=devices.get(DeviceStatus.OFFLINE.value, 0),
        cameras=sum(n for _, n in cameras),
        cameras_online=sum(n for h, n in cameras if h != CameraHealth.OFFLINE.value),
    )


def branch_overview(ctx: Context, branch: Branch, day: date, recent: int) -> BranchOverview:
    features = branch_features(ctx, branch)
    data = build_report(ctx.db, branch, ReportPeriod.DAY, day)
    totals = data.totals

    def on(feature: FeatureKey) -> bool:
        return features.get(feature.value, False)

    recent_events = None
    if ctx.principal.can(Permission.EVENTS_VIEW):
        tz = ZoneInfo(branch.timezone)
        since = datetime.combine(day, time(), tzinfo=tz)
        rows = ctx.db.scalars(
            select(Event)
            .where(
                Event.branch_id == branch.id,
                Event.started_at >= since,
                Event.started_at < since + timedelta(days=1),
                Event.feature_key.in_([k for k, v in features.items() if v]),
            )
            .order_by(Event.started_at.desc())
            .limit(recent)
        )
        recent_events = [event_out(e) for e in rows]
    reports = None
    if ctx.principal.can(Permission.REPORTS_VIEW):
        reports = [
            ReportLink(
                id=r.id,
                period=ReportPeriod(r.period),
                period_start=r.period_start,
                period_end=r.period_end,
            )
            for r in ctx.db.scalars(
                select(Report)
                .where(Report.branch_id == branch.id)
                .order_by(Report.generated_at.desc())
                .limit(3)
            )
        ]
    return BranchOverview(
        branch_id=branch.id,
        branch_name=branch.name,
        timezone=branch.timezone,
        day=day,
        generated_at=data.generated_at,
        partial=data.partial,
        features=features,
        events=EventKpis(
            total=totals.events,
            critical=totals.critical,
            false_alarms=totals.false_alarms,
            unreviewed=totals.unreviewed,
        ),
        attendance=AttendanceKpis(
            on_time=totals.on_time,
            late=totals.late,
            absent=totals.absent,
            day_off=totals.day_off,
            attendance_pct=totals.attendance_pct,
            punctuality_pct=totals.punctuality_pct,
        )
        if on(FeatureKey.ATTENDANCE)
        else None,
        kitchen_compliance=totals.compliance if on(FeatureKey.KITCHEN_COMPLIANCE) else None,
        cashier=CashierKpis(issues=totals.cashier, counter_staffed_pct=totals.counter_staffed_pct)
        if on(FeatureKey.CASHIER)
        else None,
        security=totals.security if on(FeatureKey.SECURITY) else None,
        customers=CustomerKpis(
            customers_in=totals.customers_in,
            peak_hour=totals.peak_hour,
            peak_occupancy=totals.peak_occupancy,
            hours=data.hours,
        )
        if on(FeatureKey.CUSTOMER_COUNT)
        else None,
        alerts=data.alerts,
        system=_system(ctx, branch),
        recent_events=recent_events,
        reports=reports,
    )


@router.get("/overview", response_model=list[BranchOverview])
def get_overview(
    ctx: KpiReader,
    branch_id: uuid.UUID | None = None,
    day: Annotated[date | None, Query(description="Branch-local day; default today")] = None,
    recent: Annotated[int, Query(ge=0, le=50, description="Recent events to list")] = 8,
) -> list[BranchOverview]:
    """One entry per branch the caller can see (or just `branch_id`). Today's figures
    are "so far"; they are worked out on each call, so poll at most once a minute."""
    query = select(Branch).order_by(Branch.name)
    if branch_id is not None:
        query = query.where(Branch.id == branch_id)
    branches = [b for b in ctx.db.scalars(query) if ctx.principal.can_access_branch(b.id)]
    if branch_id is not None and not branches:
        raise not_found("branch not found")
    result = []
    for branch in branches:
        today = datetime.now(timezone.utc).astimezone(ZoneInfo(branch.timezone)).date()
        wanted = day or today
        if wanted > today:
            raise HTTPException(422, "that day hasn't started")
        if wanted < today - timedelta(days=get_settings().report_retention_days):
            raise HTTPException(422, "older than report retention")
        result.append(branch_overview(ctx, branch, wanted, recent))
    return result
