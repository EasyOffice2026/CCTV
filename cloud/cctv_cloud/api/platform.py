"""Super-admin (our team) endpoints: tenants. Everything else a super-admin does uses the
normal tenant endpoints with the X-Tenant-Id header."""

import re
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from cctv_cloud.api.auth import send_password_link
from cctv_cloud.api.deps import require_platform
from cctv_cloud.db.enums import UserRole
from cctv_cloud.db.models import Tenant, User
from cctv_cloud.db.session import tenant_session
from cctv_cloud.rbac import Principal
from cctv_cloud.services.audit import AuditAction, record
from cctv_cloud.services.mailer import Mailer, get_mailer

router = APIRouter(prefix="/v1/platform", tags=["platform"])

PlatformAdmin = Annotated[Principal, Depends(require_platform)]


class TenantSummary(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    status: str


class TenantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(min_length=2, max_length=63)
    owner_email: EmailStr
    owner_name: str = Field(min_length=1, max_length=200)

    @field_validator("slug")
    @classmethod
    def _slug(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", value):
            raise ValueError("lowercase letters, digits and single dashes only")
        return value


class TenantCreated(BaseModel):
    tenant: TenantSummary
    owner_id: uuid.UUID


@router.get("/tenants", response_model=list[TenantSummary])
def list_tenants(_: PlatformAdmin) -> list[TenantSummary]:
    with tenant_session(None) as db:
        rows = db.execute(text("SELECT * FROM platform_list_tenants()")).all()
    return [TenantSummary(id=r.tenant_id, name=r.name, slug=r.slug, status=r.status) for r in rows]


@router.post("/tenants", response_model=TenantCreated, status_code=status.HTTP_201_CREATED)
def create_tenant(
    body: TenantCreate,
    principal: PlatformAdmin,
    request: Request,
    mailer: Annotated[Mailer, Depends(get_mailer)],
) -> TenantCreated:
    """Creates the tenant and its first owner, who gets an email to set a password."""
    tenant_id = uuid.uuid4()
    try:
        # RLS only lets the new row in when the session is set to its id.
        with tenant_session(tenant_id, principal.user_id) as db:
            tenant = Tenant(id=tenant_id, name=body.name, slug=body.slug)
            db.add(tenant)
            db.flush()
            owner = User(
                tenant_id=tenant_id,
                email=body.owner_email.lower(),
                full_name=body.owner_name,
                role=UserRole.OWNER.value,
            )
            db.add(owner)
            db.flush()
            record(
                db,
                principal,
                AuditAction.USER_CHANGE,
                "tenant",
                tenant_id,
                request,
                {"created": body.slug, "owner": body.owner_email},
                tenant_id=tenant_id,
            )
            send_password_link(owner, mailer, purpose="invite")
            db.refresh(tenant)
            return TenantCreated(
                tenant=TenantSummary(
                    id=tenant.id, name=tenant.name, slug=tenant.slug, status=tenant.status
                ),
                owner_id=owner.id,
            )
    except IntegrityError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "slug or owner email already in use") from exc
