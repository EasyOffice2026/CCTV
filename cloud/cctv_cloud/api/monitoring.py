"""Monitoring (plan 11.4): system incidents per tenant, across tenants for our team, and
counts for Prometheus (services/monitoring.py)."""

import hmac
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Response
from pydantic import BaseModel
from sqlalchemy import select

from cctv_cloud.api.deps import Context, context, require_platform, unauthorized
from cctv_cloud.config import get_settings
from cctv_cloud.db.enums import IncidentKind
from cctv_cloud.db.models import Branch, Camera, EdgeDevice, HealthIncident, Tenant
from cctv_cloud.db.session import tenant_session
from cctv_cloud.rbac import Permission, Principal
from cctv_cloud.services.monitoring import platform_counts, prometheus

router = APIRouter(tags=["monitoring"])

Installer = Annotated[Context, Depends(context(Permission.DEVICES_MANAGE), scope="function")]


class IncidentOut(BaseModel):
    id: uuid.UUID
    tenant: str | None = None  # platform list only
    branch_id: uuid.UUID
    branch: str
    kind: IncidentKind
    device_id: uuid.UUID | None
    device: str | None
    camera_id: uuid.UUID | None
    camera: str | None
    detail: str
    value: float | None
    opened_at: datetime
    resolved_at: datetime | None
    notified_at: datetime | None


def _incidents(db, query) -> list[IncidentOut]:
    rows = db.execute(
        query.add_columns(Branch.name, EdgeDevice.name, Camera.name)
        .join(Branch, Branch.id == HealthIncident.branch_id)
        .outerjoin(EdgeDevice, EdgeDevice.id == HealthIncident.device_id)
        .outerjoin(Camera, Camera.id == HealthIncident.camera_id)
        .order_by(HealthIncident.resolved_at.is_not(None), HealthIncident.opened_at.desc())
    ).all()
    return [
        IncidentOut(
            id=i.id,
            branch_id=i.branch_id,
            branch=branch,
            kind=IncidentKind(i.kind),
            device_id=i.device_id,
            device=device,
            camera_id=i.camera_id,
            camera=camera,
            detail=i.detail,
            value=i.value,
            opened_at=i.opened_at,
            resolved_at=i.resolved_at,
            notified_at=i.notified_at,
        )
        for i, branch, device, camera in rows
    ]


@router.get("/v1/incidents", response_model=list[IncidentOut])
def list_incidents(
    ctx: Installer,
    branch_id: uuid.UUID | None = None,
    open_only: bool = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[IncidentOut]:
    """System problems of the tenant's branches (open first, then newest): boxes offline,
    camera streams down, disks filling, boxes too hot, uploads backing up."""
    query = select(HealthIncident).limit(limit)
    if branch_id is not None:
        query = query.where(HealthIncident.branch_id == branch_id)
    if ctx.principal.branch_ids is not None:
        query = query.where(HealthIncident.branch_id.in_(ctx.principal.branch_ids))
    if open_only:
        query = query.where(HealthIncident.resolved_at.is_(None))
    return _incidents(ctx.db, query)


@router.get("/v1/platform/incidents", response_model=list[IncidentOut], tags=["platform"])
def list_platform_incidents(
    principal: Annotated[Principal, Depends(require_platform)],
) -> list[IncidentOut]:
    """Every client's open incidents, for our on-call team."""
    from cctv_cloud.worker import tenant_ids

    result: list[IncidentOut] = []
    for tenant_id in tenant_ids():
        with tenant_session(tenant_id, principal.user_id) as db:
            name = db.scalar(select(Tenant.name))
            query = select(HealthIncident).where(HealthIncident.resolved_at.is_(None))
            result += [i.model_copy(update={"tenant": name}) for i in _incidents(db, query)]
    return sorted(result, key=lambda i: i.opened_at)


@router.get("/metrics", include_in_schema=False)
def metrics(authorization: Annotated[str | None, Header()] = None) -> Response:
    """Prometheus counts across tenants (no tenant data), with
    `Authorization: Bearer <CCTV_METRICS_TOKEN>`; refused when no token is set."""
    token = get_settings().metrics_token.get_secret_value()
    given = (authorization or "").removeprefix("Bearer ").strip()
    if not token or not hmac.compare_digest(given.encode(), token.encode()):
        raise unauthorized()
    return Response(prometheus(platform_counts()), media_type="text/plain; version=0.0.4")
