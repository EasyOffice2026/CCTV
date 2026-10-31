"""API keys for integrations (plan 3.9) and the audit log viewer (plan 7.3)."""

import csv
import io
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from cctv_cloud.api.admin import ORMModel
from cctv_cloud.api.deps import Context, context, not_found, require_platform
from cctv_cloud.api.events import csv_cell
from cctv_cloud.config import get_settings
from cctv_cloud.db.models import ApiKey, AuditLog, User
from cctv_cloud.db.session import tenant_session
from cctv_cloud.rbac import API_KEY_SCOPES, Permission, Principal
from cctv_cloud.security import new_secret, sha256_hex
from cctv_cloud.services.audit import AuditAction, record

router = APIRouter(prefix="/v1", tags=["integrations"])

TenantAdmin = Annotated[Context, Depends(context(Permission.TENANT_ADMIN), scope="function")]


class ApiKeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    scopes: list[str] = Field(min_length=1)

    @field_validator("scopes")
    @classmethod
    def _known(cls, scopes: list[str]) -> list[str]:
        unknown = set(scopes) - API_KEY_SCOPES.keys()
        if unknown:
            raise ValueError(f"unknown scopes {sorted(unknown)}; known: {sorted(API_KEY_SCOPES)}")
        return sorted(set(scopes))


class ApiKeyOut(ORMModel):
    id: uuid.UUID
    name: str
    prefix: str
    scopes: list[str]
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


class ApiKeyCreated(BaseModel):
    api_key: ApiKeyOut
    key: str = Field(description="Shown once. Send it in the X-API-Key header.")


@router.get("/api-keys", response_model=list[ApiKeyOut])
def list_api_keys(ctx: TenantAdmin) -> list[ApiKey]:
    return list(ctx.db.scalars(select(ApiKey).order_by(ApiKey.created_at.desc())))


@router.post("/api-keys", response_model=ApiKeyCreated, status_code=status.HTTP_201_CREATED)
def create_api_key(body: ApiKeyIn, ctx: TenantAdmin, request: Request) -> ApiKeyCreated:
    prefix = new_secret(6).replace("-", "x").replace("_", "x")[:8]
    key = f"cctv_{prefix}_{new_secret()}"
    api_key = ApiKey(
        tenant_id=ctx.principal.tenant_id,
        name=body.name,
        prefix=prefix,
        key_hash=sha256_hex(key),
        scopes=body.scopes,
        created_by=ctx.principal.user_id,
    )
    ctx.db.add(api_key)
    ctx.db.flush()
    ctx.db.refresh(api_key)
    record(
        ctx.db,
        ctx.principal,
        AuditAction.API_KEY_CHANGE,
        "api_key",
        api_key.id,
        request,
        {"created": body.name, "scopes": body.scopes},
    )
    return ApiKeyCreated(api_key=ApiKeyOut.model_validate(api_key), key=key)


@router.delete("/api-keys/{api_key_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_api_key(api_key_id: uuid.UUID, ctx: TenantAdmin, request: Request) -> None:
    api_key = ctx.db.get(ApiKey, api_key_id)
    if api_key is None:
        raise not_found("API key not found")
    api_key.revoked_at = datetime.now(timezone.utc)
    record(
        ctx.db,
        ctx.principal,
        AuditAction.API_KEY_CHANGE,
        "api_key",
        api_key.id,
        request,
        {"revoked": api_key.name},
    )


class AuditLogOut(ORMModel):
    id: int
    user_id: uuid.UUID | None
    # None when the user isn't in this tenant (our support staff acting for the client).
    user_name: str | None = None
    user_email: str | None = None
    action: str
    target_type: str | None
    target_id: str | None
    ip: str | None
    user_agent: str | None
    details: dict[str, Any]
    at: datetime

    @field_validator("ip", mode="before")
    @classmethod
    def _ip_text(cls, value: object) -> str | None:
        return None if value is None else str(value)  # INET comes back as an ipaddress object


@dataclass
class AuditFilters:
    """Who did what, to what, when. Newest first; page with `before_id` = the last id of
    the previous page."""

    action: Annotated[list[AuditAction] | None, Query()] = None
    user_id: uuid.UUID | None = None
    target_type: Annotated[str | None, Query(max_length=50)] = None
    target_id: Annotated[str | None, Query(max_length=100)] = None
    since: datetime | None = None
    until: datetime | None = None

    def used(self) -> dict[str, Any]:
        return {
            k: [a.value for a in v] if k == "action" else str(v)
            for k, v in asdict(self).items()
            if v is not None
        }

    def query(self) -> Select:
        query = select(AuditLog, User.full_name, User.email).outerjoin(
            User, User.id == AuditLog.user_id
        )
        if self.action:
            query = query.where(AuditLog.action.in_([a.value for a in self.action]))
        if self.user_id:
            query = query.where(AuditLog.user_id == self.user_id)
        if self.target_type:
            query = query.where(AuditLog.target_type == self.target_type)
        if self.target_id:
            query = query.where(AuditLog.target_id == self.target_id)
        if self.since:
            query = query.where(AuditLog.at >= self.since)
        if self.until:
            query = query.where(AuditLog.at < self.until)
        return query.order_by(AuditLog.id.desc())


AuditQuery = Annotated[AuditFilters, Depends()]


def _entries(db: Session, query: Select) -> list[AuditLogOut]:
    return [
        AuditLogOut.model_validate(entry).model_copy(
            update={"user_name": name, "user_email": email}
        )
        for entry, name, email in db.execute(query)
    ]


@router.get("/audit-logs", response_model=list[AuditLogOut])
def list_audit_logs(
    ctx: TenantAdmin,
    filters: AuditQuery,
    before_id: int | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[AuditLogOut]:
    """The tenant's audit log (plan 7.3): sign-ins, views and exports of footage, reports
    and event lists, and changes to settings, users, devices, staff and consent."""
    query = filters.query().limit(limit)
    if before_id:
        query = query.where(AuditLog.id < before_id)
    return _entries(ctx.db, query)


@router.get(
    "/audit-logs/export",
    response_class=Response,
    responses={200: {"content": {"text/csv": {}}, "description": "Matching entries as CSV"}},
)
def export_audit_logs(ctx: TenantAdmin, filters: AuditQuery, request: Request) -> Response:
    """The matching entries as CSV, newest first (up to `event_export_max_rows`). The export
    itself is written to the audit log."""
    entries = _entries(ctx.db, filters.query().limit(get_settings().event_export_max_rows))
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(AUDIT_COLUMNS)
    for e in entries:
        writer.writerow(
            [
                csv_cell(value)
                for value in (
                    e.id,
                    e.at.isoformat(),
                    e.user_name,
                    e.user_email,
                    e.user_id,
                    e.action,
                    e.target_type,
                    e.target_id,
                    e.ip,
                    json.dumps(e.details, ensure_ascii=False, sort_keys=True),
                    e.user_agent,
                )
            ]
        )
    record(
        ctx.db,
        ctx.principal,
        AuditAction.EXPORT_AUDIT,
        "audit_logs",
        None,
        request,
        {"rows": len(entries), "filters": filters.used()},
    )
    name = f"audit-log-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.csv"
    return Response(
        "﻿" + out.getvalue(),  # BOM: Excel reads the file as UTF-8
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Cache-Control": "no-store",
        },
    )


AUDIT_COLUMNS = [
    "id",
    "at",
    "user_name",
    "user_email",
    "user_id",
    "action",
    "target_type",
    "target_id",
    "ip",
    "details",
    "user_agent",
]


@router.get("/platform/audit-logs", response_model=list[AuditLogOut], tags=["platform"])
def list_platform_audit_logs(
    principal: Annotated[Principal, Depends(require_platform)],
    filters: AuditQuery,
    before_id: int | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[AuditLogOut]:
    """Entries without a tenant (super-admin sign-ins). What a super-admin does for a
    client is in that client's own audit log."""
    query = filters.query().where(AuditLog.tenant_id.is_(None)).limit(limit)
    if before_id:
        query = query.where(AuditLog.id < before_id)
    with tenant_session(None, principal.user_id) as db:
        return _entries(db, query)
