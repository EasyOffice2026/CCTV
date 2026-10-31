"""Event search, export, review and media access (plan 3.6, 3.7, 4.3)."""

import base64
import binascii
import csv
import io
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import ColumnElement, and_, or_, select

from cctv_cloud.api.deps import Context, context, forbidden, not_found
from cctv_cloud.config import get_settings
from cctv_cloud.db.enums import ReviewStatus
from cctv_cloud.db.models import Branch, Camera, Event, Zone
from cctv_cloud.rbac import Permission
from cctv_cloud.services.audit import AuditAction, record
from cctv_cloud.services.storage import ObjectStorage, get_storage
from cctv_contracts.enums import EventStatus, EventType, FeatureKey, MediaKind, Severity

router = APIRouter(prefix="/v1/events", tags=["events"])

Viewer = Annotated[Context, Depends(context(Permission.EVENTS_VIEW))]
Reviewer = Annotated[Context, Depends(context(Permission.EVENTS_VIEW, Permission.EVENTS_REVIEW))]
MediaViewer = Annotated[Context, Depends(context(Permission.MEDIA_VIEW))]
Exporter = Annotated[Context, Depends(context(Permission.EVENTS_VIEW, Permission.MEDIA_EXPORT))]

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


@dataclass
class EventFilters:
    """Search filters (plan 3.6), shared by search and export. All optional."""

    start: Annotated[datetime | None, Query(alias="from")] = None
    end: Annotated[datetime | None, Query(alias="to")] = None
    branch_id: uuid.UUID | None = None
    camera_id: uuid.UUID | None = None
    zone_id: uuid.UUID | None = None
    feature: FeatureKey | None = None
    event_type: Annotated[list[EventType] | None, Query()] = None
    severity: Severity | None = None
    status: EventStatus | None = None
    review_status: ReviewStatus | None = None
    staff_id: uuid.UUID | None = None

    def conditions(self, ctx: Context) -> list[ColumnElement[bool]]:
        """Matching events in branches the caller can access."""
        conditions = []
        for column, value in (
            (Event.branch_id, self.branch_id),
            (Event.camera_id, self.camera_id),
            (Event.zone_id, self.zone_id),
            (Event.feature_key, self.feature.value if self.feature else None),
            (Event.severity, self.severity.value if self.severity else None),
            (Event.status, self.status.value if self.status else None),
            (Event.review_status, self.review_status.value if self.review_status else None),
            (Event.staff_id, self.staff_id),
        ):
            if value is not None:
                conditions.append(column == value)
        if self.event_type:
            conditions.append(Event.event_type.in_([t.value for t in self.event_type]))
        if self.start is not None:
            conditions.append(Event.started_at >= self.start)
        if self.end is not None:
            conditions.append(Event.started_at < self.end)
        if ctx.principal.branch_ids is not None:
            conditions.append(Event.branch_id.in_(ctx.principal.branch_ids))
        return conditions

    def used(self) -> dict[str, Any]:
        """The filters that were set, for the audit log."""
        used: dict[str, Any] = {}
        for name, value in vars(self).items():
            if isinstance(value, list):
                used[name] = [v.value for v in value]
            elif value is not None:
                used[name] = str(getattr(value, "value", value))
        return used


Filters = Annotated[EventFilters, Depends()]


@router.get("", response_model=EventPage)
def search_events(
    ctx: Viewer,
    filters: Filters,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 50,
    cursor: str | None = None,
) -> EventPage:
    """Newest first. Results only include branches the caller can access."""
    conditions = filters.conditions(ctx)
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


EXPORT_COLUMNS = [
    "event_id",
    "started_at",
    "ended_at",
    "branch",
    "camera",
    "zone",
    "feature",
    "event_type",
    "severity",
    "status",
    "confidence",
    "review_status",
    "review_note",
    "staff_id",
]
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _cell(value: object) -> str:
    """Text for a CSV cell. A leading = + - @ would run as a formula in Excel."""
    text = "" if value is None else str(value)
    return f"'{text}" if text.startswith(_FORMULA_START) else text


@router.get(
    "/export",
    response_class=Response,
    responses={200: {"content": {"text/csv": {}}, "description": "Matching events as CSV"}},
)
def export_events(ctx: Exporter, filters: Filters, request: Request) -> Response:
    """The search results as CSV, newest first, up to `event_export_max_rows` rows. Written
    to the audit log with the filters used."""
    rows = ctx.db.execute(
        select(Event, Branch.name, Camera.name, Zone.name)
        .join(Branch, Branch.id == Event.branch_id)
        .join(Camera, Camera.id == Event.camera_id)
        .outerjoin(Zone, Zone.id == Event.zone_id)
        .where(*filters.conditions(ctx))
        .order_by(Event.started_at.desc(), Event.id.desc())
        .limit(get_settings().event_export_max_rows)
    ).all()
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(EXPORT_COLUMNS)
    for event, branch, camera, zone in rows:
        writer.writerow(
            [
                _cell(value)
                for value in (
                    event.id,
                    event.started_at.isoformat(),
                    event.ended_at.isoformat() if event.ended_at else None,
                    branch,
                    camera,
                    zone,
                    event.feature_key,
                    event.event_type,
                    event.severity,
                    event.status,
                    f"{event.confidence:.2f}",
                    event.review_status,
                    event.review_note,
                    event.staff_id,
                )
            ]
        )
    record(
        ctx.db,
        ctx.principal,
        AuditAction.EXPORT_EVENTS,
        "events",
        None,
        request,
        {"rows": len(rows), "filters": filters.used()},
    )
    name = f"events-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.csv"
    return Response(
        # With a BOM, Excel reads the file as UTF-8 (Arabic branch and camera names).
        "﻿" + out.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Cache-Control": "no-store",
        },
    )


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
