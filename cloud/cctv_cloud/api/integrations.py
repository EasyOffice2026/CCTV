"""API keys for integrations (plan 3.9), the audit log viewer (plan 7.3), and the
operational-system integration (plan 8): sign-in (SSO) integrations, set up by our team,
and webhooks, managed by the owner.
"""

import csv
import io
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import Select, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cctv_cloud.api.admin import ORMModel, accessible_branch
from cctv_cloud.api.deps import Context, context, not_found, require_platform
from cctv_cloud.api.events import csv_cell
from cctv_cloud.config import get_settings
from cctv_cloud.db.enums import DeliveryStatus, SsoAlgorithm, WebhookTopic
from cctv_cloud.db.models import (
    ApiKey,
    AuditLog,
    SsoIntegration,
    User,
    Webhook,
    WebhookDelivery,
)
from cctv_cloud.db.session import tenant_session
from cctv_cloud.rbac import API_KEY_SCOPES, Permission, Principal
from cctv_cloud.security import new_secret, sha256_hex
from cctv_cloud.services import sso, webhooks
from cctv_cloud.services.audit import AuditAction, record
from cctv_contracts.enums import Severity

router = APIRouter(prefix="/v1", tags=["integrations"])

TenantAdmin = Annotated[Context, Depends(context(Permission.TENANT_ADMIN), scope="function")]
# Our team, acting on a tenant (X-Tenant-Id).
PlatformAdmin = Annotated[Context, Depends(context(Permission.PLATFORM_ADMIN), scope="function")]


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


# --- sign-in from the operational system (plan 8.1) ---------------------------------------


class SsoIntegrationOut(ORMModel):
    id: uuid.UUID
    name: str
    issuer: str
    algorithm: SsoAlgorithm
    public_key: str | None
    require_mfa: bool
    enabled: bool
    created_at: datetime
    updated_at: datetime | None
    last_used_at: datetime | None
    audience: str = Field(default="", description="The `aud` its tokens must carry")

    @model_validator(mode="after")
    def _audience(self) -> "SsoIntegrationOut":
        self.audience = get_settings().sso_expected_audience
        return self


class SsoIntegrationIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    issuer: str = Field(min_length=3, max_length=200, pattern=r"^\S+$")
    algorithm: SsoAlgorithm
    public_key: str | None = Field(default=None, max_length=10_000)
    require_mfa: bool = True


class SsoIntegrationUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    public_key: str | None = Field(default=None, max_length=10_000)
    require_mfa: bool | None = None
    enabled: bool | None = None
    new_secret: bool = Field(default=False, description="HS256: replace the shared secret")


class SsoIntegrationSaved(BaseModel):
    integration: SsoIntegrationOut
    secret: str | None = Field(
        default=None, description="HS256 only, shown once: give it to the operational system"
    )


def _public_key(algorithm: SsoAlgorithm, pem: str | None) -> str:
    if not pem:
        raise HTTPException(422, f"{algorithm.value} needs the operational system's public key")
    try:
        return sso.check_public_key(algorithm, pem)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/sso-integrations", response_model=list[SsoIntegrationOut])
def list_sso_integrations(ctx: TenantAdmin) -> list[SsoIntegration]:
    """Owners can see how their operational system signs users in; our team sets it up."""
    return list(ctx.db.scalars(select(SsoIntegration).order_by(SsoIntegration.created_at)))


@router.post(
    "/sso-integrations", response_model=SsoIntegrationSaved, status_code=status.HTTP_201_CREATED
)
def create_sso_integration(
    body: SsoIntegrationIn, ctx: PlatformAdmin, request: Request
) -> SsoIntegrationSaved:
    secret = None
    integration = SsoIntegration(
        tenant_id=ctx.principal.tenant_id,
        name=body.name,
        issuer=body.issuer,
        algorithm=body.algorithm.value,
        require_mfa=body.require_mfa,
        created_by=ctx.principal.user_id,
    )
    if body.algorithm is SsoAlgorithm.HS256:
        secret = sso.new_shared_secret()
        integration.secret_encrypted = sso.encrypt_secret(secret)
    else:
        integration.public_key = _public_key(body.algorithm, body.public_key)
    try:
        with ctx.db.begin_nested():
            ctx.db.add(integration)
            ctx.db.flush()
    except IntegrityError as exc:
        raise HTTPException(409, "that issuer is already used") from exc
    ctx.db.refresh(integration)
    record(
        ctx.db,
        ctx.principal,
        AuditAction.SSO_CHANGE,
        "sso_integration",
        integration.id,
        request,
        {"created": body.name, "issuer": body.issuer, "algorithm": body.algorithm.value},
    )
    return SsoIntegrationSaved(
        integration=SsoIntegrationOut.model_validate(integration), secret=secret
    )


def _integration(ctx: Context, integration_id: uuid.UUID) -> SsoIntegration:
    integration = ctx.db.get(SsoIntegration, integration_id)
    if integration is None:
        raise not_found("SSO integration not found")
    return integration


@router.patch("/sso-integrations/{integration_id}", response_model=SsoIntegrationSaved)
def update_sso_integration(
    integration_id: uuid.UUID, body: SsoIntegrationUpdate, ctx: PlatformAdmin, request: Request
) -> SsoIntegrationSaved:
    integration = _integration(ctx, integration_id)
    algorithm = SsoAlgorithm(integration.algorithm)
    changes = body.model_dump(exclude_unset=True, exclude={"public_key"})
    secret = None
    if body.name is not None:
        integration.name = body.name
    if body.require_mfa is not None:
        integration.require_mfa = body.require_mfa
    if body.enabled is not None:
        integration.enabled = body.enabled
    if body.public_key is not None:
        integration.public_key = _public_key(algorithm, body.public_key)
        changes["public_key"] = "replaced"
    if body.new_secret:
        if algorithm is not SsoAlgorithm.HS256:
            raise HTTPException(422, "only HS256 integrations have a shared secret")
        secret = sso.new_shared_secret()
        integration.secret_encrypted = sso.encrypt_secret(secret)
    integration.updated_at = datetime.now(timezone.utc)
    record(
        ctx.db,
        ctx.principal,
        AuditAction.SSO_CHANGE,
        "sso_integration",
        integration.id,
        request,
        changes,
    )
    ctx.db.flush()
    ctx.db.refresh(integration)
    return SsoIntegrationSaved(
        integration=SsoIntegrationOut.model_validate(integration), secret=secret
    )


@router.delete("/sso-integrations/{integration_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_sso_integration(integration_id: uuid.UUID, ctx: PlatformAdmin, request: Request) -> None:
    integration = _integration(ctx, integration_id)
    record(
        ctx.db,
        ctx.principal,
        AuditAction.SSO_CHANGE,
        "sso_integration",
        integration.id,
        request,
        {"deleted": integration.name},
    )
    ctx.db.delete(integration)


# --- webhooks (plan 8.4) ------------------------------------------------------------------


class WebhookOut(ORMModel):
    id: uuid.UUID
    name: str
    url: str
    topics: list[WebhookTopic]
    branch_id: uuid.UUID | None
    min_severity: Severity
    enabled: bool
    created_at: datetime
    updated_at: datetime | None
    last_success_at: datetime | None
    last_failure_at: datetime | None
    last_error: str | None


def _check_url(url: str) -> str:
    try:
        webhooks.check_url(url)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return url


class WebhookIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(max_length=1000)
    topics: list[WebhookTopic] = Field(min_length=1)
    branch_id: uuid.UUID | None = None
    min_severity: Severity = Severity.INFO


class WebhookUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    url: str | None = Field(default=None, max_length=1000)
    topics: list[WebhookTopic] | None = Field(default=None, min_length=1)
    branch_id: uuid.UUID | None = None
    all_branches: bool = Field(default=False, description="Clear branch_id")
    min_severity: Severity | None = None
    enabled: bool | None = None
    new_secret: bool = False


class WebhookSaved(BaseModel):
    webhook: WebhookOut
    secret: str | None = Field(
        default=None, description="Shown once: verify X-CCTV-Signature with it"
    )


class WebhookDeliveryOut(ORMModel):
    id: int
    topic: str
    dedup_key: str
    status: DeliveryStatus
    attempts: int
    created_at: datetime
    sent_at: datetime | None
    next_attempt_at: datetime
    response_status: int | None
    error: str | None


class WebhookTestResult(BaseModel):
    ok: bool
    status: int | None
    error: str | None


def _webhook(ctx: Context, webhook_id: uuid.UUID) -> Webhook:
    hook = ctx.db.get(Webhook, webhook_id)
    if hook is None:
        raise not_found("webhook not found")
    return hook


def _topics(topics: list[WebhookTopic]) -> list[str]:
    return sorted({t.value for t in topics})


@router.get("/webhooks", response_model=list[WebhookOut])
def list_webhooks(ctx: TenantAdmin) -> list[Webhook]:
    return list(ctx.db.scalars(select(Webhook).order_by(Webhook.created_at)))


@router.post("/webhooks", response_model=WebhookSaved, status_code=status.HTTP_201_CREATED)
def create_webhook(body: WebhookIn, ctx: TenantAdmin, request: Request) -> WebhookSaved:
    if body.branch_id is not None:
        accessible_branch(ctx, body.branch_id)
    secret = webhooks.new_webhook_secret()
    hook = Webhook(
        tenant_id=ctx.principal.tenant_id,
        name=body.name,
        url=_check_url(body.url),
        secret_encrypted=webhooks.encrypt_secret(secret),
        topics=_topics(body.topics),
        branch_id=body.branch_id,
        min_severity=body.min_severity.value,
        created_by=ctx.principal.user_id,
    )
    ctx.db.add(hook)
    ctx.db.flush()
    ctx.db.refresh(hook)
    record(
        ctx.db,
        ctx.principal,
        AuditAction.WEBHOOK_CHANGE,
        "webhook",
        hook.id,
        request,
        {"created": body.name, "url": body.url, "topics": hook.topics},
    )
    return WebhookSaved(webhook=WebhookOut.model_validate(hook), secret=secret)


@router.patch("/webhooks/{webhook_id}", response_model=WebhookSaved)
def update_webhook(
    webhook_id: uuid.UUID, body: WebhookUpdate, ctx: TenantAdmin, request: Request
) -> WebhookSaved:
    hook = _webhook(ctx, webhook_id)
    secret = None
    if body.name is not None:
        hook.name = body.name
    if body.url is not None:
        hook.url = _check_url(body.url)
    if body.topics is not None:
        hook.topics = _topics(body.topics)
    if body.branch_id is not None:
        hook.branch_id = accessible_branch(ctx, body.branch_id).id
    elif body.all_branches:
        hook.branch_id = None
    if body.min_severity is not None:
        hook.min_severity = body.min_severity.value
    if body.enabled is not None:
        hook.enabled = body.enabled
    if body.new_secret:
        secret = webhooks.new_webhook_secret()
        hook.secret_encrypted = webhooks.encrypt_secret(secret)
    hook.updated_at = datetime.now(timezone.utc)
    record(
        ctx.db,
        ctx.principal,
        AuditAction.WEBHOOK_CHANGE,
        "webhook",
        hook.id,
        request,
        body.model_dump(mode="json", exclude_unset=True),
    )
    ctx.db.flush()
    ctx.db.refresh(hook)
    return WebhookSaved(webhook=WebhookOut.model_validate(hook), secret=secret)


@router.delete("/webhooks/{webhook_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_webhook(webhook_id: uuid.UUID, ctx: TenantAdmin, request: Request) -> None:
    hook = _webhook(ctx, webhook_id)
    record(
        ctx.db,
        ctx.principal,
        AuditAction.WEBHOOK_CHANGE,
        "webhook",
        hook.id,
        request,
        {"deleted": hook.name},
    )
    ctx.db.delete(hook)


@router.get("/webhooks/{webhook_id}/deliveries", response_model=list[WebhookDeliveryOut])
def list_webhook_deliveries(
    webhook_id: uuid.UUID, ctx: TenantAdmin, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> list[WebhookDelivery]:
    """Newest first (kept 30 days)."""
    _webhook(ctx, webhook_id)
    return list(
        ctx.db.scalars(
            select(WebhookDelivery)
            .where(WebhookDelivery.webhook_id == webhook_id)
            .order_by(WebhookDelivery.id.desc())
            .limit(limit)
        )
    )


@router.post("/webhooks/{webhook_id}/test", response_model=WebhookTestResult)
def test_webhook(
    webhook_id: uuid.UUID,
    ctx: TenantAdmin,
    poster: Annotated[webhooks.WebhookPoster, Depends(webhooks.get_webhook_poster)],
) -> WebhookTestResult:
    """Send a signed `ping` now and report what the receiver answered."""
    hook = _webhook(ctx, webhook_id)
    now = datetime.now(timezone.utc)
    try:
        code = webhooks.send_ping(poster, hook)
    except webhooks.WebhookError as exc:
        hook.last_failure_at, hook.last_error = now, str(exc)[:1000]
        return WebhookTestResult(ok=False, status=exc.status, error=str(exc)[:500])
    hook.last_success_at = now
    return WebhookTestResult(ok=True, status=code, error=None)
