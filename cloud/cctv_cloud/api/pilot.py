"""Install and shadow mode (plan 9): the stream setup test, shadow mode on/off, and the
review figures for tuning (services/pilot.py). The official pilot (plan 10): its start,
its figures against the success criteria (services/pilot_kpis.py) and the pilot log."""

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import or_, select

from cctv_cloud.api.admin import BranchOut, accessible_branch
from cctv_cloud.api.deps import Context, context, forbidden, not_found
from cctv_cloud.db.enums import PilotNoteKind
from cctv_cloud.db.models import Branch, PilotNote, User
from cctv_cloud.rbac import Permission
from cctv_cloud.services.audit import AuditAction, record
from cctv_cloud.services.pilot import (
    MAX_TEST_HOURS,
    ReviewSummary,
    StreamTest,
    first_heartbeat,
    review_summary,
    stream_test,
)
from cctv_cloud.services.pilot_kpis import PilotKpis, pilot_kpis

router = APIRouter(prefix="/v1", tags=["pilot"])

Installer = Annotated[Context, Depends(context(Permission.DEVICES_MANAGE), scope="function")]
Viewer = Annotated[Context, Depends(context(Permission.EVENTS_VIEW), scope="function")]
PlatformAdmin = Annotated[Context, Depends(context(Permission.PLATFORM_ADMIN), scope="function")]

MAX_REVIEW_DAYS = 92


@router.get("/branches/{branch_id}/stream-test", response_model=StreamTest)
def get_stream_test(
    branch_id: uuid.UUID,
    ctx: Installer,
    since: Annotated[
        datetime | None,
        Query(description="Start of the window; default: the first heartbeat in the last `hours`"),
    ] = None,
    hours: Annotated[int, Query(ge=1, le=MAX_TEST_HOURS)] = 48,
) -> StreamTest:
    """The setup test (plan 9.2): each camera of the branch over `hours` from `since`.
    While the window is still running, cameras without problems so far have `passed: null`.
    Without `since` the window starts at the branch's first heartbeat of the last `hours`,
    so on install day it starts when the box came online."""
    branch = accessible_branch(ctx, branch_id)
    now = datetime.now(timezone.utc)
    start = since or first_heartbeat(ctx.db, branch, now - timedelta(hours=hours)) or now
    if start.tzinfo is None:
        raise HTTPException(422, "since needs a time zone (e.g. 2026-11-01T09:00:00+03:00)")
    return stream_test(ctx.db, branch, start, start + timedelta(hours=hours), now)


class ShadowIn(BaseModel):
    until: datetime | None = Field(description="End of shadow mode; null ends it now")


@router.put("/branches/{branch_id}/shadow", response_model=BranchOut)
def set_shadow_mode(
    branch_id: uuid.UUID, body: ShadowIn, ctx: PlatformAdmin, request: Request
) -> Branch:
    """Shadow mode (plan 9.4), set by our team: until `until`, the branch's alerts go only
    to users marked `shadow_reviewer`, and no webhooks or report links reach the client."""
    branch = accessible_branch(ctx, branch_id)
    if body.until is not None and body.until.tzinfo is None:
        raise HTTPException(422, "until needs a time zone")
    branch.shadow_until = body.until
    record(
        ctx.db,
        ctx.principal,
        AuditAction.SETTINGS_CHANGE,
        "branch",
        branch.id,
        request,
        {"shadow_until": body.until.isoformat() if body.until else None},
    )
    ctx.db.flush()
    ctx.db.refresh(branch)
    return branch


@router.get("/review-summary", response_model=ReviewSummary)
def get_review_summary(
    ctx: Viewer,
    branch_id: uuid.UUID,
    since: date | None = None,
    until: date | None = None,
) -> ReviewSummary:
    """Review results per day, and per event type, camera and zone with precision and
    false alarms per day (plan 9.4, 9.5): where to tune first. Days are branch-local;
    default: the last 7 days."""
    branch = accessible_branch(ctx, branch_id)
    today = datetime.now(timezone.utc).astimezone(ZoneInfo(branch.timezone)).date()
    until = until or today
    since = since or until - timedelta(days=6)
    if since > until or (until - since).days >= MAX_REVIEW_DAYS:
        raise HTTPException(422, f"choose up to {MAX_REVIEW_DAYS} days, since <= until")
    return review_summary(ctx.db, branch, since, until)


# --- the official pilot (plan 10) ----------------------------------------------------------

KpiReader = Annotated[Context, Depends(context(Permission.KPIS_VIEW), scope="function")]
TenantAdmin = Annotated[Context, Depends(context(Permission.TENANT_ADMIN), scope="function")]


class PilotStartIn(BaseModel):
    started_on: date | None = Field(description="Day 1 of the official pilot; null clears it")


@router.put("/branches/{branch_id}/pilot", response_model=BranchOut)
def set_pilot_start(
    branch_id: uuid.UUID, body: PilotStartIn, ctx: PlatformAdmin, request: Request
) -> Branch:
    """Day 1 of the branch's official pilot (plan 10), set by our team when shadow mode
    ends; the pilot figures and the 30-day check count from it."""
    branch = accessible_branch(ctx, branch_id)
    branch.pilot_started_on = body.started_on
    record(
        ctx.db,
        ctx.principal,
        AuditAction.SETTINGS_CHANGE,
        "branch",
        branch.id,
        request,
        {"pilot_started_on": body.started_on.isoformat() if body.started_on else None},
    )
    ctx.db.flush()
    ctx.db.refresh(branch)
    return branch


@router.get("/pilot/kpis", response_model=PilotKpis)
def get_pilot_kpis(
    ctx: KpiReader,
    branch_id: uuid.UUID,
    since: date | None = None,
    until: date | None = None,
) -> PilotKpis:
    """The pilot's figures per day and in total against the success criteria (plan 10.3,
    10.5). Default: from the pilot start (or the last 30 days) to today."""
    branch = accessible_branch(ctx, branch_id)
    today = datetime.now(timezone.utc).astimezone(ZoneInfo(branch.timezone)).date()
    until = until or today
    since = since or branch.pilot_started_on or until - timedelta(days=29)
    if since > until or (until - since).days >= MAX_REVIEW_DAYS:
        raise HTTPException(422, f"choose up to {MAX_REVIEW_DAYS} days, since <= until")
    return pilot_kpis(ctx.db, branch, since, until)


class PilotNoteIn(BaseModel):
    branch_id: uuid.UUID | None = None
    noted_on: date
    kind: PilotNoteKind
    body: str = Field(min_length=1, max_length=5000)


class PilotNoteOut(BaseModel):
    id: uuid.UUID
    branch_id: uuid.UUID | None
    noted_on: date
    kind: PilotNoteKind
    body: str
    author_id: uuid.UUID | None
    author_name: str | None
    created_at: datetime


def _note_out(note: PilotNote, name: str | None) -> PilotNoteOut:
    return PilotNoteOut(
        id=note.id,
        branch_id=note.branch_id,
        noted_on=note.noted_on,
        kind=PilotNoteKind(note.kind),
        body=note.body,
        author_id=note.author_id,
        author_name=name,
        created_at=note.created_at,
    )


@router.get("/pilot/log", response_model=list[PilotNoteOut])
def list_pilot_notes(
    ctx: KpiReader,
    branch_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> list[PilotNoteOut]:
    """The pilot log (plan 10.4), newest first: weekly check-ins, the owner's feedback,
    changes made, issues and decisions. With `branch_id`: that branch and client-wide
    entries."""
    query = select(PilotNote, User.full_name).outerjoin(User, User.id == PilotNote.author_id)
    if branch_id is not None:
        accessible_branch(ctx, branch_id)
        query = query.where(or_(PilotNote.branch_id == branch_id, PilotNote.branch_id.is_(None)))
    allowed = ctx.principal.branch_ids
    if allowed is not None:
        query = query.where(or_(PilotNote.branch_id.in_(allowed), PilotNote.branch_id.is_(None)))
    rows = ctx.db.execute(
        query.order_by(PilotNote.noted_on.desc(), PilotNote.created_at.desc()).limit(limit)
    )
    return [_note_out(note, name) for note, name in rows]


@router.post("/pilot/log", response_model=PilotNoteOut, status_code=201)
def add_pilot_note(body: PilotNoteIn, ctx: TenantAdmin) -> PilotNoteOut:
    if body.branch_id is not None:
        accessible_branch(ctx, body.branch_id)
    note = PilotNote(
        tenant_id=ctx.principal.tenant_id,
        branch_id=body.branch_id,
        noted_on=body.noted_on,
        kind=body.kind.value,
        body=body.body,
        author_id=ctx.principal.user_id,
    )
    ctx.db.add(note)
    ctx.db.flush()
    ctx.db.refresh(note)
    name = ctx.db.scalar(select(User.full_name).where(User.id == note.author_id))
    return _note_out(note, name)


@router.delete("/pilot/log/{note_id}", status_code=204)
def delete_pilot_note(note_id: uuid.UUID, ctx: TenantAdmin) -> None:
    """Only the author (or our team) removes an entry."""
    note = ctx.db.get(PilotNote, note_id)
    if note is None:
        raise not_found("entry not found")
    if note.author_id != ctx.principal.user_id and not ctx.principal.can(Permission.PLATFORM_ADMIN):
        raise forbidden("only the author can remove an entry")
    ctx.db.delete(note)
