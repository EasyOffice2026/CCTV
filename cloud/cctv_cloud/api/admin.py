"""Tenant, branches, working hours and users (plan 3.3)."""

import uuid
from datetime import datetime, time
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from cctv_cloud.api.auth import send_password_link
from cctv_cloud.api.deps import Context, PublisherDep, context, forbidden, not_found
from cctv_cloud.db.enums import UserRole
from cctv_cloud.db.models import Branch, Tenant, User, UserBranchAccess, WorkingHours
from cctv_cloud.rbac import ALL_BRANCH_ROLES, Permission
from cctv_cloud.services.audit import AuditAction, record
from cctv_cloud.services.edge_config import bump_config_version
from cctv_cloud.services.mailer import Mailer, get_mailer

router = APIRouter(prefix="/v1", tags=["admin"])

AnyMember = Annotated[Context, Depends(context())]
TenantAdmin = Annotated[Context, Depends(context(Permission.TENANT_ADMIN))]
BranchEditor = Annotated[Context, Depends(context(Permission.BRANCH_EDIT))]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


def notify_config(background: BackgroundTasks, publisher, branch_ids: list[uuid.UUID]) -> None:
    for branch_id in branch_ids:
        background.add_task(publisher.publish, f"config:{branch_id}", {"branch_id": branch_id})


def accessible_branch(ctx: Context, branch_id: uuid.UUID) -> Branch:
    branch = ctx.db.get(Branch, branch_id)
    if branch is None or not ctx.principal.can_access_branch(branch_id):
        raise not_found("branch not found")
    return branch


# --- tenant -------------------------------------------------------------------------


class TenantOut(ORMModel):
    id: uuid.UUID
    name: str
    slug: str
    status: str
    retention_media_days: int
    retention_event_days: int


class TenantUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    retention_media_days: int | None = Field(default=None, ge=1, le=3650)
    retention_event_days: int | None = Field(default=None, ge=1, le=3650)


@router.get("/tenant", response_model=TenantOut)
def get_tenant(ctx: AnyMember) -> Tenant:
    return ctx.db.get_one(Tenant, ctx.principal.tenant_id)


@router.patch("/tenant", response_model=TenantOut)
def update_tenant(body: TenantUpdate, ctx: TenantAdmin, request: Request) -> Tenant:
    tenant = ctx.db.get_one(Tenant, ctx.principal.tenant_id)
    changes = body.model_dump(exclude_unset=True)
    for key, value in changes.items():
        setattr(tenant, key, value)
    if tenant.retention_media_days > tenant.retention_event_days:
        raise HTTPException(422, "media retention can't be longer than event retention")
    record(
        ctx.db, ctx.principal, AuditAction.SETTINGS_CHANGE, "tenant", tenant.id, request, changes
    )
    return tenant


# --- branches -----------------------------------------------------------------------


def _check_timezone(value: str | None) -> str | None:
    if value is not None:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown time zone {value!r}") from exc
    return value


class BranchIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    timezone: str = "Asia/Kuwait"
    address: str | None = Field(default=None, max_length=500)

    _tz = field_validator("timezone")(_check_timezone)


class BranchUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    timezone: str | None = None
    address: str | None = Field(default=None, max_length=500)

    _tz = field_validator("timezone")(_check_timezone)


class BranchOut(ORMModel):
    id: uuid.UUID
    name: str
    timezone: str
    address: str | None
    config_version: int
    created_at: datetime


@router.get("/branches", response_model=list[BranchOut])
def list_branches(ctx: AnyMember) -> list[Branch]:
    query = select(Branch).order_by(Branch.name)
    if ctx.principal.branch_ids is not None:
        query = query.where(Branch.id.in_(ctx.principal.branch_ids))
    return list(ctx.db.scalars(query))


@router.post("/branches", response_model=BranchOut, status_code=status.HTTP_201_CREATED)
def create_branch(body: BranchIn, ctx: TenantAdmin) -> Branch:
    branch = Branch(tenant_id=ctx.principal.tenant_id, **body.model_dump())
    ctx.db.add(branch)
    ctx.db.flush()
    ctx.db.refresh(branch)
    return branch


@router.get("/branches/{branch_id}", response_model=BranchOut)
def get_branch(branch_id: uuid.UUID, ctx: AnyMember) -> Branch:
    return accessible_branch(ctx, branch_id)


@router.patch("/branches/{branch_id}", response_model=BranchOut)
def update_branch(
    branch_id: uuid.UUID,
    body: BranchUpdate,
    ctx: BranchEditor,
    background: BackgroundTasks,
    publisher: PublisherDep,
) -> Branch:
    branch = accessible_branch(ctx, branch_id)
    changes = body.model_dump(exclude_unset=True)
    for key, value in changes.items():
        setattr(branch, key, value)
    if "timezone" in changes:
        notify_config(background, publisher, bump_config_version(ctx.db, [branch.id]))
    ctx.db.flush()
    ctx.db.refresh(branch)
    return branch


@router.delete("/branches/{branch_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_branch(branch_id: uuid.UUID, ctx: TenantAdmin) -> None:
    branch = accessible_branch(ctx, branch_id)
    ctx.db.delete(branch)
    try:
        ctx.db.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "branch still has cameras, devices or events"
        ) from exc


# --- working hours ------------------------------------------------------------------


class WorkingHoursIn(BaseModel):
    weekday: int = Field(ge=0, le=6, description="0 = Monday")
    opens_at: time
    closes_at: time = Field(description="Not after opens_at: closes the next day")


class WorkingHoursOut(ORMModel, WorkingHoursIn):
    id: uuid.UUID


@router.get("/branches/{branch_id}/working-hours", response_model=list[WorkingHoursOut])
def list_working_hours(branch_id: uuid.UUID, ctx: AnyMember) -> list[WorkingHours]:
    accessible_branch(ctx, branch_id)
    return list(
        ctx.db.scalars(
            select(WorkingHours)
            .where(WorkingHours.branch_id == branch_id)
            .order_by(WorkingHours.weekday, WorkingHours.opens_at)
        )
    )


@router.put("/branches/{branch_id}/working-hours", response_model=list[WorkingHoursOut])
def replace_working_hours(
    branch_id: uuid.UUID,
    body: list[WorkingHoursIn],
    ctx: BranchEditor,
    request: Request,
    background: BackgroundTasks,
    publisher: PublisherDep,
) -> list[WorkingHours]:
    accessible_branch(ctx, branch_id)
    ctx.db.execute(delete(WorkingHours).where(WorkingHours.branch_id == branch_id))
    rows = [
        WorkingHours(tenant_id=ctx.principal.tenant_id, branch_id=branch_id, **h.model_dump())
        for h in body
    ]
    ctx.db.add_all(rows)
    ctx.db.flush()
    record(ctx.db, ctx.principal, AuditAction.SETTINGS_CHANGE, "working_hours", branch_id, request)
    notify_config(background, publisher, bump_config_version(ctx.db, [branch_id]))
    return rows


# --- users --------------------------------------------------------------------------

TENANT_ROLES = [r for r in UserRole if r is not UserRole.SUPER_ADMIN]


class UserIn(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=1, max_length=200)
    role: UserRole
    branch_ids: list[uuid.UUID] = Field(default_factory=list)

    @field_validator("role")
    @classmethod
    def _tenant_role(cls, role: UserRole) -> UserRole:
        if role is UserRole.SUPER_ADMIN:
            raise ValueError("super_admin users can't be created here")
        return role


class UserUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    role: UserRole | None = None
    branch_ids: list[uuid.UUID] | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def _tenant_role(self) -> "UserUpdate":
        if self.role is UserRole.SUPER_ADMIN:
            raise ValueError("super_admin can't be assigned here")
        return self


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str
    role: UserRole
    is_active: bool
    mfa_enabled: bool
    branch_ids: list[uuid.UUID]
    last_login_at: datetime | None


def _user_out(ctx: Context, user: User) -> UserOut:
    branches = ctx.db.scalars(
        select(UserBranchAccess.branch_id).where(UserBranchAccess.user_id == user.id)
    ).all()
    return UserOut(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=UserRole(user.role),
        is_active=user.is_active,
        mfa_enabled=user.totp_secret_encrypted is not None,
        branch_ids=sorted(branches),
        last_login_at=user.last_login_at,
    )


def _set_branches(ctx: Context, user: User, branch_ids: list[uuid.UUID]) -> None:
    for branch_id in branch_ids:
        accessible_branch(ctx, branch_id)
    ctx.db.execute(delete(UserBranchAccess).where(UserBranchAccess.user_id == user.id))
    if UserRole(user.role) not in ALL_BRANCH_ROLES:
        ctx.db.add_all(
            UserBranchAccess(tenant_id=user.tenant_id, user_id=user.id, branch_id=b)
            for b in set(branch_ids)
        )


def _tenant_user(ctx: Context, user_id: uuid.UUID) -> User:
    user = ctx.db.get(User, user_id)
    if user is None or user.tenant_id != ctx.principal.tenant_id:
        raise not_found("user not found")
    return user


@router.get("/users", response_model=list[UserOut])
def list_users(ctx: TenantAdmin) -> list[UserOut]:
    users = ctx.db.scalars(
        select(User).where(User.tenant_id == ctx.principal.tenant_id).order_by(User.email)
    )
    return [_user_out(ctx, u) for u in users]


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(
    body: UserIn,
    ctx: TenantAdmin,
    request: Request,
    mailer: Annotated[Mailer, Depends(get_mailer)],
) -> UserOut:
    """Creates the user without a password and emails them a link to set one."""
    user = User(
        tenant_id=ctx.principal.tenant_id,
        email=body.email.lower(),
        full_name=body.full_name,
        role=body.role.value,
    )
    ctx.db.add(user)
    try:
        ctx.db.flush()
    except IntegrityError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "a user with this email exists") from exc
    _set_branches(ctx, user, body.branch_ids)
    record(
        ctx.db,
        ctx.principal,
        AuditAction.USER_CHANGE,
        "user",
        user.id,
        request,
        {"created": body.email, "role": body.role.value},
    )
    send_password_link(user, mailer, purpose="invite")
    return _user_out(ctx, user)


@router.patch("/users/{user_id}", response_model=UserOut)
def update_user(
    user_id: uuid.UUID, body: UserUpdate, ctx: TenantAdmin, request: Request
) -> UserOut:
    user = _tenant_user(ctx, user_id)
    changes = body.model_dump(exclude_unset=True, mode="json")
    if user.id == ctx.principal.user_id and ({"role", "is_active"} & changes.keys()):
        raise forbidden("you can't change your own role or deactivate yourself")
    if body.full_name is not None:
        user.full_name = body.full_name
    if body.role is not None:
        user.role = body.role.value
    if body.is_active is not None:
        user.is_active = body.is_active  # takes effect on the user's next request
    if body.branch_ids is not None or body.role is not None:
        current = ctx.db.scalars(
            select(UserBranchAccess.branch_id).where(UserBranchAccess.user_id == user.id)
        ).all()
        _set_branches(ctx, user, body.branch_ids if body.branch_ids is not None else list(current))
    record(ctx.db, ctx.principal, AuditAction.USER_CHANGE, "user", user.id, request, changes)
    ctx.db.flush()
    return _user_out(ctx, user)


@router.post("/users/{user_id}/reset-mfa", status_code=status.HTTP_204_NO_CONTENT)
def reset_user_mfa(user_id: uuid.UUID, ctx: TenantAdmin, request: Request) -> None:
    """For a lost phone: the user sets up 2FA again at their next login."""
    user = _tenant_user(ctx, user_id)
    user.totp_secret_encrypted = None
    user.totp_last_step = None
    record(
        ctx.db, ctx.principal, AuditAction.USER_CHANGE, "user", user.id, request, {"mfa": "reset"}
    )
