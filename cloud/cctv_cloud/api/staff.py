"""Staff, shifts, consent and face enrolment (plan 4.8, privacy rules §7.5).

Face enrolment only works after consent is recorded (also enforced by a database trigger).
The photos are sent to the branch's edge box, which turns them into an embedding with its
face model; only the encrypted embedding is stored, never the photos. Withdrawing consent
or marking someone as left deletes their face data (database trigger).

Every change to who can be recognised bumps the branch's config version, so its edge box
reloads the face templates (`GET /v1/edge/face-templates`) within a heartbeat.
"""

import base64
import uuid
from datetime import datetime, time
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

from cctv_cloud.api.admin import ORMModel, accessible_branch
from cctv_cloud.api.deps import Context, context, not_found, require
from cctv_cloud.api.remote import ask_edge, online_edge
from cctv_cloud.config import get_settings
from cctv_cloud.db.enums import StaffStatus
from cctv_cloud.db.models import FaceTemplate, Shift, Staff
from cctv_cloud.db.session import tenant_session
from cctv_cloud.rbac import Permission, Principal
from cctv_cloud.security import FACE_KEY_ID, encrypt_face_embedding
from cctv_cloud.services.audit import AuditAction, record
from cctv_cloud.services.edge_config import bump_config_version
from cctv_cloud.services.relay import EdgeRelay, get_relay
from cctv_contracts.v1 import FaceEmbeddingRequest

router = APIRouter(prefix="/v1", tags=["staff"])

StaffAdmin = Annotated[Context, Depends(context(Permission.STAFF_MANAGE), scope="function")]
StaffAdminPrincipal = Annotated[Principal, Depends(require(Permission.STAFF_MANAGE))]

MAX_PHOTOS = 5
MAX_PHOTO_BYTES = 5 * 1024 * 1024
_IMAGE_MAGIC = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n")


# --- shifts -------------------------------------------------------------------------


class ShiftIn(BaseModel):
    branch_id: uuid.UUID
    name: str = Field(min_length=1, max_length=100)
    starts_at: time
    ends_at: time = Field(description="Not after starts_at: the shift ends the next day")
    grace_minutes: int = Field(default=10, ge=0, le=240)


class ShiftUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    starts_at: time | None = None
    ends_at: time | None = None
    grace_minutes: int | None = Field(default=None, ge=0, le=240)


class ShiftOut(ORMModel, ShiftIn):
    id: uuid.UUID


def _shift(ctx: Context, shift_id: uuid.UUID) -> Shift:
    shift = ctx.db.get(Shift, shift_id)
    if shift is None or not ctx.principal.can_access_branch(shift.branch_id):
        raise not_found("shift not found")
    return shift


@router.get("/shifts", response_model=list[ShiftOut])
def list_shifts(ctx: StaffAdmin, branch_id: uuid.UUID | None = None) -> list[Shift]:
    query = select(Shift).order_by(Shift.starts_at, Shift.name)
    if branch_id is not None:
        query = query.where(Shift.branch_id == branch_id)
    return [s for s in ctx.db.scalars(query) if ctx.principal.can_access_branch(s.branch_id)]


@router.post("/shifts", response_model=ShiftOut, status_code=status.HTTP_201_CREATED)
def create_shift(body: ShiftIn, ctx: StaffAdmin, request: Request) -> Shift:
    accessible_branch(ctx, body.branch_id)
    shift = Shift(tenant_id=ctx.principal.tenant_id, **body.model_dump())
    ctx.db.add(shift)
    ctx.db.flush()
    record(ctx.db, ctx.principal, AuditAction.STAFF_CHANGE, "shift", shift.id, request)
    return shift


@router.patch("/shifts/{shift_id}", response_model=ShiftOut)
def update_shift(
    shift_id: uuid.UUID, body: ShiftUpdate, ctx: StaffAdmin, request: Request
) -> Shift:
    shift = _shift(ctx, shift_id)
    for name, value in body.model_dump(exclude_unset=True).items():
        if value is None:
            raise HTTPException(422, f"{name} can't be empty")
        setattr(shift, name, value)
    shift.updated_at = func.now()
    record(ctx.db, ctx.principal, AuditAction.STAFF_CHANGE, "shift", shift.id, request)
    ctx.db.flush()
    return shift


@router.delete("/shifts/{shift_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_shift(shift_id: uuid.UUID, ctx: StaffAdmin, request: Request) -> None:
    shift = _shift(ctx, shift_id)
    if ctx.db.scalar(select(func.count()).where(Staff.shift_id == shift.id)):
        raise HTTPException(status.HTTP_409_CONFLICT, "staff are still assigned to this shift")
    ctx.db.delete(shift)
    record(ctx.db, ctx.principal, AuditAction.STAFF_CHANGE, "shift", shift_id, request)


# --- staff --------------------------------------------------------------------------


class StaffIn(BaseModel):
    branch_id: uuid.UUID
    employee_code: str = Field(min_length=1, max_length=50)
    full_name: str = Field(min_length=1, max_length=200)
    shift_id: uuid.UUID | None = None


class StaffUpdate(BaseModel):
    branch_id: uuid.UUID | None = None
    employee_code: str | None = Field(default=None, min_length=1, max_length=50)
    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    shift_id: uuid.UUID | None = None
    status: StaffStatus | None = Field(
        default=None, description="'left' deletes the person's face data"
    )


class StaffOut(BaseModel):
    id: uuid.UUID
    branch_id: uuid.UUID
    employee_code: str
    full_name: str
    shift_id: uuid.UUID | None
    status: StaffStatus
    consent_signed_at: datetime | None
    face_enrolled: bool
    face_model_version: str | None
    created_at: datetime


class ConsentIn(BaseModel):
    signed_at: datetime = Field(description="When the staff member signed the consent form")


def _staff_out(ctx: Context, staff: Staff) -> StaffOut:
    model_version = ctx.db.scalar(
        select(FaceTemplate.model_version).where(FaceTemplate.staff_id == staff.id).limit(1)
    )
    return StaffOut(
        id=staff.id,
        branch_id=staff.branch_id,
        employee_code=staff.employee_code,
        full_name=staff.full_name,
        shift_id=staff.shift_id,
        status=StaffStatus(staff.status),
        consent_signed_at=staff.consent_signed_at,
        face_enrolled=model_version is not None,
        face_model_version=model_version,
        created_at=staff.created_at,
    )


def _staff(ctx: Context, staff_id: uuid.UUID) -> Staff:
    staff = ctx.db.get(Staff, staff_id)
    if staff is None or not ctx.principal.can_access_branch(staff.branch_id):
        raise not_found("staff member not found")
    return staff


def _check_shift(ctx: Context, shift_id: uuid.UUID | None, branch_id: uuid.UUID) -> None:
    if shift_id is not None and _shift(ctx, shift_id).branch_id != branch_id:
        raise HTTPException(422, "the shift belongs to another branch")


@router.get("/staff", response_model=list[StaffOut])
def list_staff(
    ctx: StaffAdmin, branch_id: uuid.UUID | None = None, status: StaffStatus | None = None
) -> list[StaffOut]:
    query = select(Staff).order_by(Staff.full_name)
    if branch_id is not None:
        query = query.where(Staff.branch_id == branch_id)
    if status is not None:
        query = query.where(Staff.status == status.value)
    if ctx.principal.branch_ids is not None:
        query = query.where(Staff.branch_id.in_(ctx.principal.branch_ids))
    enrolled = dict(
        ctx.db.execute(
            select(FaceTemplate.staff_id, func.max(FaceTemplate.model_version)).group_by(
                FaceTemplate.staff_id
            )
        ).all()
    )
    return [
        StaffOut(
            id=s.id,
            branch_id=s.branch_id,
            employee_code=s.employee_code,
            full_name=s.full_name,
            shift_id=s.shift_id,
            status=StaffStatus(s.status),
            consent_signed_at=s.consent_signed_at,
            face_enrolled=s.id in enrolled,
            face_model_version=enrolled.get(s.id),
            created_at=s.created_at,
        )
        for s in ctx.db.scalars(query)
    ]


@router.post("/staff", response_model=StaffOut, status_code=status.HTTP_201_CREATED)
def create_staff(body: StaffIn, ctx: StaffAdmin, request: Request) -> StaffOut:
    accessible_branch(ctx, body.branch_id)
    _check_shift(ctx, body.shift_id, body.branch_id)
    staff = Staff(tenant_id=ctx.principal.tenant_id, **body.model_dump())
    ctx.db.add(staff)
    ctx.db.flush()
    ctx.db.refresh(staff)
    record(ctx.db, ctx.principal, AuditAction.STAFF_CHANGE, "staff", staff.id, request)
    return _staff_out(ctx, staff)


@router.get("/staff/{staff_id}", response_model=StaffOut)
def get_staff(staff_id: uuid.UUID, ctx: StaffAdmin) -> StaffOut:
    return _staff_out(ctx, _staff(ctx, staff_id))


@router.patch("/staff/{staff_id}", response_model=StaffOut)
def update_staff(
    staff_id: uuid.UUID, body: StaffUpdate, ctx: StaffAdmin, request: Request
) -> StaffOut:
    staff = _staff(ctx, staff_id)
    changes = body.model_dump(exclude_unset=True)
    for name in ("branch_id", "employee_code", "full_name", "status"):
        if name in changes and changes[name] is None:
            raise HTTPException(422, f"{name} can't be empty")
    if "branch_id" in changes:
        accessible_branch(ctx, changes["branch_id"])
        if changes["branch_id"] != staff.branch_id and "shift_id" not in changes:
            changes["shift_id"] = None  # shifts belong to a branch
    _check_shift(
        ctx, changes.get("shift_id", staff.shift_id), changes.get("branch_id", staff.branch_id)
    )
    if "status" in changes:
        changes["status"] = changes["status"].value
    affected = {staff.branch_id, changes.get("branch_id", staff.branch_id)}
    recognition_changed = any(
        name in changes and changes[name] != getattr(staff, name)
        for name in ("branch_id", "status")
    )
    for name, value in changes.items():
        setattr(staff, name, value)
    ctx.db.flush()
    if recognition_changed:
        bump_config_version(ctx.db, affected)
    record(
        ctx.db,
        ctx.principal,
        AuditAction.STAFF_CHANGE,
        "staff",
        staff.id,
        request,
        {"fields": sorted(changes)},
    )
    return _staff_out(ctx, staff)


@router.put("/staff/{staff_id}/consent", response_model=StaffOut)
def record_consent(
    staff_id: uuid.UUID, body: ConsentIn, ctx: StaffAdmin, request: Request
) -> StaffOut:
    """Record the signed consent form; face enrolment is refused until this is set."""
    staff = _staff(ctx, staff_id)
    if body.signed_at.tzinfo is None:
        raise HTTPException(422, "signed_at needs a UTC offset")
    if body.signed_at > datetime.now(body.signed_at.tzinfo):
        raise HTTPException(422, "signed_at can't be in the future")
    staff.consent_signed_at = body.signed_at
    ctx.db.flush()
    record(
        ctx.db,
        ctx.principal,
        AuditAction.CONSENT_CHANGE,
        "staff",
        staff.id,
        request,
        {"consent": "recorded", "signed_at": body.signed_at.isoformat()},
    )
    return _staff_out(ctx, staff)


@router.delete("/staff/{staff_id}/consent", response_model=StaffOut)
def withdraw_consent(staff_id: uuid.UUID, ctx: StaffAdmin, request: Request) -> StaffOut:
    """Consent withdrawn: the person's face data is deleted (database trigger)."""
    staff = _staff(ctx, staff_id)
    staff.consent_signed_at = None
    ctx.db.flush()
    bump_config_version(ctx.db, [staff.branch_id])
    record(
        ctx.db,
        ctx.principal,
        AuditAction.CONSENT_CHANGE,
        "staff",
        staff.id,
        request,
        {"consent": "withdrawn"},
    )
    return _staff_out(ctx, staff)


def _enrolment_target(principal: Principal, staff_id: uuid.UUID) -> uuid.UUID:
    """The edge box to embed the photos, after checking the person can be enrolled."""
    with tenant_session(principal.tenant_id, principal.user_id) as db:
        staff = db.get(Staff, staff_id)
        if staff is None or not principal.can_access_branch(staff.branch_id):
            raise not_found("staff member not found")
        if staff.status != StaffStatus.ACTIVE.value:
            raise HTTPException(status.HTTP_409_CONFLICT, "this person has left")
        if staff.consent_signed_at is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "record the signed consent first")
        return online_edge(db, staff.branch_id)


def _store_template(
    principal: Principal,
    staff_id: uuid.UUID,
    embedding: list[float],
    model_version: str,
    photos: int,
    request: Request,
) -> None:
    with tenant_session(principal.tenant_id, principal.user_id) as db:
        # Lock the row: consent could be withdrawn while the box was working.
        staff = db.scalar(select(Staff).where(Staff.id == staff_id).with_for_update())
        if staff is None or staff.consent_signed_at is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "consent was withdrawn")
        db.execute(delete(FaceTemplate).where(FaceTemplate.staff_id == staff_id))
        db.add(
            FaceTemplate(
                tenant_id=staff.tenant_id,
                staff_id=staff_id,
                embedding_encrypted=encrypt_face_embedding(embedding),
                key_id=FACE_KEY_ID,
                model_version=model_version,
            )
        )
        bump_config_version(db, [staff.branch_id])
        record(
            db,
            principal,
            AuditAction.FACE_ENROL,
            "staff",
            staff_id,
            request,
            {"photos": photos, "model_version": model_version},
        )


@router.post("/staff/{staff_id}/face", response_model=StaffOut)
async def enrol_face(
    staff_id: uuid.UUID,
    principal: StaffAdminPrincipal,
    request: Request,
    relay: Annotated[EdgeRelay, Depends(get_relay)],
    photos: Annotated[list[UploadFile], File(description="1-5 JPEG or PNG photos")],
) -> StaffOut:
    """Enrol (or re-enrol) a face from 1-5 photos. The photos are not stored."""
    if not 1 <= len(photos) <= MAX_PHOTOS:
        raise HTTPException(422, f"send 1 to {MAX_PHOTOS} photos")
    images = []
    for photo in photos:
        data = await photo.read(MAX_PHOTO_BYTES + 1)
        if len(data) > MAX_PHOTO_BYTES:
            raise HTTPException(413, "each photo must be at most 5 MB")
        if not data.startswith(_IMAGE_MAGIC):
            raise HTTPException(422, "photos must be JPEG or PNG")
        images.append(base64.b64encode(data).decode())
    device = await run_in_threadpool(_enrolment_target, principal, staff_id)
    reply = await ask_edge(
        relay,
        device,
        FaceEmbeddingRequest(staff_id=staff_id, images_base64=images),
        timeout=get_settings().face_embedding_timeout_seconds,
    )
    if not reply.embedding or not reply.model_version:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "the edge box sent no embedding")
    await run_in_threadpool(
        _store_template,
        principal,
        staff_id,
        reply.embedding,
        reply.model_version,
        len(images),
        request,
    )
    with tenant_session(principal.tenant_id, principal.user_id) as db:
        return _staff_out(Context(principal, db), db.get_one(Staff, staff_id))


@router.delete("/staff/{staff_id}/face", response_model=StaffOut)
def delete_face(staff_id: uuid.UUID, ctx: StaffAdmin, request: Request) -> StaffOut:
    staff = _staff(ctx, staff_id)
    ctx.db.execute(delete(FaceTemplate).where(FaceTemplate.staff_id == staff.id))
    bump_config_version(ctx.db, [staff.branch_id])
    record(ctx.db, ctx.principal, AuditAction.FACE_DELETE, "staff", staff.id, request)
    return _staff_out(ctx, staff)
