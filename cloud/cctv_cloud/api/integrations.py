"""API keys for integrations (plan 3.9) and the audit log viewer."""

import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from cctv_cloud.api.admin import ORMModel
from cctv_cloud.api.deps import Context, context, not_found
from cctv_cloud.db.models import ApiKey, AuditLog
from cctv_cloud.rbac import API_KEY_SCOPES, Permission
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


@router.get("/audit-logs", response_model=list[AuditLogOut])
def list_audit_logs(
    ctx: TenantAdmin,
    action: str | None = None,
    user_id: uuid.UUID | None = None,
    before_id: int | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[AuditLog]:
    """Newest first; page with `before_id` = the last id of the previous page."""
    query = select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)
    if action:
        query = query.where(AuditLog.action == action)
    if user_id:
        query = query.where(AuditLog.user_id == user_id)
    if before_id:
        query = query.where(AuditLog.id < before_id)
    return list(ctx.db.scalars(query))
