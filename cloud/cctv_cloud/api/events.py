"""Event search, review and media access (plan 3.6, 3.7)."""

import base64
import binascii
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import and_, or_, select

from cctv_cloud.api.deps import Context, context, forbidden, not_found
from cctv_cloud.config import get_settings
from cctv_cloud.db.enums import ReviewStatus
from cctv_cloud.db.models import Event
from cctv_cloud.rbac import Permission
from cctv_cloud.services.audit import AuditAction, record
from cctv_cloud.services.storage import ObjectStorage, get_storage
from cctv_contracts.enums import EventStatus, EventType, FeatureKey, MediaKind, Severity

router = APIRouter(prefix="/v1/events", tags=["events"])

Viewer = Annotated[Context, Depends(context(Permission.EVENTS_VIEW))]
Reviewer = Annotated[Context, Depends(context(Permission.EVENTS_VIEW, Permission.EVENTS_REVIEW))]
MediaViewer = Annotated[Context, Depends(context(Permission.MEDIA_VIEW))]

MAX_PAGE = 200


class EventOut(BaseModel):
    id: uuid.UUID
    branch_id: uuid.UUID
    camera_id: uuid.UUID
    zone_id: uuid.UUID | None
    feature: FeatureKey
    event_type: EventType
    severity: Severity
    status: EventStatus
    started_at: datetime
    ended_at: datetime | None
    confidence: float
    staff_id: uuid.UUID | None
    track_id: str | None
    has_snapshot: bool
    has_clip: bool
    attributes: dict[str, Any]
    review_status: ReviewStatus
    reviewed_by: uuid.UUID | None
    reviewed_at: datetime | None
    review_note: str | None
    received_at: datetime


class EventPage(BaseModel):
    items: list[EventOut]
    next_cursor: str | None = Field(description="Pass as `cursor` for the next (older) page")


class ReviewIn(BaseModel):
    review_status: ReviewStatus
    note: str | None = Field(default=None, max_length=2000)


class MediaLink(BaseModel):
    url: str
    expires_at: datetime


def _out(event: Event) -> EventOut:
    return EventOut(
        id=event.id,
        branch_id=event.branch_id,
        camera_id=event.camera_id,
        zone_id=event.zone_id,
        feature=FeatureKey(event.feature_key),
        event_type=EventType(event.event_type),
        severity=Severity(event.severity),
        status=EventStatus(event.status),
        started_at=event.started_at,
        ended_at=event.ended_at,
        confidence=event.confidence,
        staff_id=event.staff_id,
        track_id=event.track_id,
        has_snapshot=event.snapshot_key is not None,
        has_clip=event.clip_key is not None,
        attributes=event.attributes,
        review_status=ReviewStatus(event.review_status),
        reviewed_by=event.reviewed_by,
        reviewed_at=event.reviewed_at,
        review_note=event.review_note,
        received_at=event.received_at,
    )


def _encode_cursor(event: Event) -> str:
    raw = f"{event.started_at.isoformat()}|{event.id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        started, event_id = base64.urlsafe_b64decode(cursor.encode()).decode().split("|")
        return datetime.fromisoformat(started), uuid.UUID(event_id)
    except (ValueError, binascii.Error) as exc:
        raise HTTPException(422, "invalid cursor") from exc


def _visible_event(ctx: Context, event_id: uuid.UUID) -> Event:
    event = ctx.db.get(Event, event_id)
    if event is None or not ctx.principal.can_access_branch(event.branch_id):
        raise not_found("event not found")
    return event


@router.get("", response_model=EventPage)
def search_events(
    ctx: Viewer,
    start: Annotated[datetime | None, Query(alias="from")] = None,
    end: Annotated[datetime | None, Query(alias="to")] = None,
    branch_id: uuid.UUID | None = None,
    camera_id: uuid.UUID | None = None,
    zone_id: uuid.UUID | None = None,
    feature: FeatureKey | None = None,
    event_type: Annotated[list[EventType] | None, Query()] = None,
    severity: Severity | None = None,
    status: EventStatus | None = None,
    review_status: ReviewStatus | None = None,
    staff_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 50,
    cursor: str | None = None,
) -> EventPage:
    """Newest first. Every filter is optional; results only include branches the caller
    can access."""
    conditions = []
    for column, value in (
        (Event.branch_id, branch_id),
        (Event.camera_id, camera_id),
        (Event.zone_id, zone_id),
        (Event.feature_key, feature.value if feature else None),
        (Event.severity, severity.value if severity else None),
        (Event.status, status.value if status else None),
        (Event.review_status, review_status.value if review_status else None),
        (Event.staff_id, staff_id),
    ):
        if value is not None:
            conditions.append(column == value)
    if event_type:
        conditions.append(Event.event_type.in_([t.value for t in event_type]))
    if start is not None:
        conditions.append(Event.started_at >= start)
    if end is not None:
        conditions.append(Event.started_at < end)
    if ctx.principal.branch_ids is not None:
        conditions.append(Event.branch_id.in_(ctx.principal.branch_ids))
    if cursor:
        started, event_id = _decode_cursor(cursor)
        conditions.append(
            or_(
                Event.started_at < started,
                and_(Event.started_at == started, Event.id < event_id),
            )
        )
    rows = ctx.db.scalars(
        select(Event)
        .where(*conditions)
        .order_by(Event.started_at.desc(), Event.id.desc())
        .limit(limit + 1)
    ).all()
    page = rows[:limit]
    next_cursor = _encode_cursor(page[-1]) if len(rows) > limit else None
    return EventPage(items=[_out(e) for e in page], next_cursor=next_cursor)


@router.get("/{event_id}", response_model=EventOut)
def get_event(event_id: uuid.UUID, ctx: Viewer) -> EventOut:
    return _out(_visible_event(ctx, event_id))


@router.patch("/{event_id}/review", response_model=EventOut)
def review_event(event_id: uuid.UUID, body: ReviewIn, ctx: Reviewer, request: Request) -> EventOut:
    event = _visible_event(ctx, event_id)
    event.review_status = body.review_status.value
    event.review_note = body.note
    event.reviewed_by = ctx.principal.user_id
    event.reviewed_at = datetime.now(timezone.utc)
    record(
        ctx.db,
        ctx.principal,
        AuditAction.REVIEW_EVENT,
        "event",
        event.id,
        request,
        {"review_status": body.review_status.value},
    )
    return _out(event)


@router.get("/{event_id}/media/{kind}", response_model=MediaLink)
def media_link(
    event_id: uuid.UUID,
    kind: MediaKind,
    ctx: MediaViewer,
    request: Request,
    storage: Annotated[ObjectStorage, Depends(get_storage)],
    download: bool = False,
) -> MediaLink:
    """A short-lived link to the snapshot or clip. Every call is written to the audit log."""
    if download and not ctx.principal.can(Permission.MEDIA_EXPORT):
        raise forbidden("downloading needs export rights")
    event = _visible_event(ctx, event_id)
    key = event.snapshot_key if kind is MediaKind.SNAPSHOT else event.clip_key
    if key is None:
        raise not_found(f"this event has no {kind.value}")
    seconds = get_settings().media_url_seconds
    name = f"{event.event_type}-{event.started_at:%Y%m%d-%H%M%S}.{key.rsplit('.', 1)[-1]}"
    url = storage.presign_get(key, seconds, download_name=name if download else None)
    action = {
        (MediaKind.SNAPSHOT, False): AuditAction.VIEW_SNAPSHOT,
        (MediaKind.CLIP, False): AuditAction.VIEW_CLIP,
        (MediaKind.SNAPSHOT, True): AuditAction.EXPORT_SNAPSHOT,
        (MediaKind.CLIP, True): AuditAction.EXPORT_CLIP,
    }[(kind, download)]
    record(ctx.db, ctx.principal, action, "event", event.id, request)
    return MediaLink(url=url, expires_at=datetime.now(timezone.utc) + timedelta(seconds=seconds))
