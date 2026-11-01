"""Install and shadow mode (plan 9): the stream setup test, shadow mode on/off, and the
review figures for tuning (services/pilot.py)."""

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from cctv_cloud.api.admin import BranchOut, accessible_branch
from cctv_cloud.api.deps import Context, context
from cctv_cloud.db.models import Branch
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
