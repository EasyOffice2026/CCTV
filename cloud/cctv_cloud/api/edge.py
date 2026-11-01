"""Edge device API (`cctv_contracts.v1`; see contracts/README.md).

Devices authenticate with their token (`Authorization: Bearer ...`). The token identifies
the tenant and branch, so nothing in a request body can point at another tenant's data:
every camera, zone, staff member and media key is checked against the device's branch.
"""

import logging
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Header,
    HTTPException,
    Query,
    Response,
    status,
)
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cctv_cloud.api.deps import PublisherDep, unauthorized
from cctv_cloud.config import get_settings
from cctv_cloud.db.enums import DeviceStatus, StaffStatus
from cctv_cloud.db.models import Branch, Camera, EdgeDevice, Event, FaceTemplate, Staff, Zone
from cctv_cloud.db.session import tenant_session
from cctv_cloud.security import new_secret, normalise_pairing_code, sha256_hex
from cctv_cloud.services import pilot, releases, webhooks
from cctv_cloud.services.alerts import Change, Senders, dispatch, get_senders, route_event
from cctv_cloud.services.attendance import apply_sightings
from cctv_cloud.services.counts import apply_counts
from cctv_cloud.services.edge_config import build_edge_config
from cctv_cloud.services.face_keys import open_embedding
from cctv_cloud.services.heatmaps import apply_heatmaps
from cctv_cloud.services.relay import EdgeRelay, get_relay
from cctv_cloud.services.storage import ObjectStorage, get_storage, key_belongs_to, media_key
from cctv_contracts.enums import EventStatus, MediaKind
from cctv_contracts.v1 import (
    API_PREFIX,
    BatchResult,
    CountBatch,
    EdgeConfig,
    EventBatch,
    EventBatchResponse,
    EventIn,
    EventResult,
    FaceTemplateList,
    Heartbeat,
    HeartbeatResponse,
    HeatmapBatch,
    PairRequest,
    PairResponse,
    RemoteReply,
    RemoteRequest,
    SightingBatch,
    TokenResponse,
    UploadUrlRequest,
    UploadUrlResponse,
)
from cctv_contracts.v1 import FaceTemplate as EdgeFaceTemplate

log = logging.getLogger(__name__)

router = APIRouter(prefix=API_PREFIX, tags=["edge"])
_bearer = HTTPBearer(auto_error=False)


@dataclass
class DeviceContext:
    device: EdgeDevice
    db: Session
    token_hash: str  # of the token this request came with


def _find_device(
    credentials: HTTPAuthorizationCredentials | None,
) -> tuple[uuid.UUID, uuid.UUID, str]:
    """(device id, tenant id, token hash) for a device token."""
    if credentials is None:
        raise unauthorized("device token required")
    token_hash = sha256_hex(credentials.credentials)
    with tenant_session(None) as db:
        found = db.execute(
            text("SELECT device_id, tenant_id FROM auth_find_device(:hash)"), {"hash": token_hash}
        ).one_or_none()
    if found is None:
        raise unauthorized("unknown or retired device")
    return found.device_id, found.tenant_id, token_hash


def _token_valid(device: EdgeDevice | None, token_hash: str) -> bool:
    """The current token, or the one it replaced while the grace period lasts."""
    if device is None or device.status == DeviceStatus.RETIRED.value:
        return False
    if device.token_hash == token_hash:
        return True
    return (
        device.previous_token_hash == token_hash
        and device.previous_token_expires_at is not None
        and device.previous_token_expires_at > datetime.now(timezone.utc)
    )


def device_context(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Iterator[DeviceContext]:
    device_id, tenant_id, token_hash = _find_device(credentials)
    with tenant_session(tenant_id) as db:
        device = db.get(EdgeDevice, device_id)
        if not _token_valid(device, token_hash):
            raise unauthorized("unknown or retired device")
        yield DeviceContext(device, db, token_hash)


def device_id(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> uuid.UUID:
    """Authenticates the device without holding a database session (for long polls)."""
    found_id, tenant_id, token_hash = _find_device(credentials)
    with tenant_session(tenant_id) as db:
        if not _token_valid(db.get(EdgeDevice, found_id), token_hash):
            raise unauthorized("unknown or retired device")
    return found_id


Device = Annotated[DeviceContext, Depends(device_context, scope="function")]
DeviceId = Annotated[uuid.UUID, Depends(device_id)]
Relay = Annotated[EdgeRelay, Depends(get_relay)]


@router.post("/pair", response_model=PairResponse)
def pair(body: PairRequest) -> PairResponse:
    """Swap a one-time pairing code (from the admin panel) for the device token."""
    code_hash = sha256_hex(normalise_pairing_code(body.pairing_code))
    with tenant_session(None) as db:
        found = db.execute(
            text("SELECT device_id, tenant_id FROM auth_find_pairing(:hash)"), {"hash": code_hash}
        ).one_or_none()
    if found is None:
        raise unauthorized("invalid or expired pairing code")
    token = f"cctv_dev_{new_secret()}"
    with tenant_session(found.tenant_id) as db:
        device = db.get_one(EdgeDevice, found.device_id)
        if body.serial and body.serial != device.serial:
            raise HTTPException(status.HTTP_409_CONFLICT, "this code belongs to another serial")
        device.token_hash = sha256_hex(token)
        device.token_issued_at = datetime.now(timezone.utc)
        device.previous_token_hash = None  # a replaced box stops working
        device.previous_token_expires_at = None
        device.pairing_code_hash = None  # single use
        device.pairing_expires_at = None
        device.software_version = body.software_version or device.software_version
        return PairResponse(
            device_id=device.id,
            tenant_id=device.tenant_id,
            branch_id=device.branch_id,
            device_token=token,
        )


@router.get("/config", response_model=EdgeConfig, responses={304: {"description": "Unchanged"}})
def get_config(
    ctx: Device,
    response: Response,
    if_none_match: Annotated[str | None, Header()] = None,
) -> EdgeConfig | Response:
    version = ctx.db.scalar(select(Branch.config_version).where(Branch.id == ctx.device.branch_id))
    etag = f'"{version}"'
    if if_none_match == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": etag})
    response.headers["ETag"] = etag
    return build_edge_config(ctx.db, ctx.device)


@router.post("/heartbeat", response_model=HeartbeatResponse)
def heartbeat(body: Heartbeat, ctx: Device) -> HeartbeatResponse:
    now = datetime.now(timezone.utc)
    device = ctx.device
    device.status = DeviceStatus.ONLINE.value
    device.last_seen_at = now
    device.software_version = body.software_version
    device.config_version_applied = body.config_version_applied
    device.uptime_seconds = body.uptime_seconds
    device.cpu_percent = body.system.cpu_percent
    device.gpu_percent = body.system.gpu_percent
    device.temperature_c = body.system.temperature_c
    device.disk_free_bytes = body.system.disk_free_bytes
    device.outbox_pending = body.system.outbox_pending
    releases.record_report(device, body.update, now)
    # Only this device's branch: a device can't report on other cameras.
    pilot.record_health(ctx.db, device.tenant_id, device.branch_id, body.cameras, now)
    version = ctx.db.scalar(select(Branch.config_version).where(Branch.id == device.branch_id))
    max_age = timedelta(days=get_settings().device_token_days)
    return HeartbeatResponse(
        server_time=now,
        latest_config_version=version or 0,
        rotate_token=device.token_issued_at is None or device.token_issued_at < now - max_age,
        update=releases.offer(ctx.db, device, body.software_version),
    )


@router.post("/token", response_model=TokenResponse)
def rotate_token(ctx: Device) -> TokenResponse:
    """A new device token (plan 7.1). The token used for this call keeps working for a
    short grace period, so a box that restarts before saving the new one isn't locked out.
    Calling again with that old token (within the grace period) replaces the new one."""
    now = datetime.now(timezone.utc)
    device = ctx.device
    token = f"cctv_dev_{new_secret()}"
    if ctx.token_hash == device.token_hash:
        device.previous_token_hash = device.token_hash
        device.previous_token_expires_at = now + timedelta(
            minutes=get_settings().device_token_grace_minutes
        )
    device.token_hash = sha256_hex(token)
    device.token_issued_at = now
    log.info("device %s rotated its token", device.id)
    return TokenResponse(device_token=token)


@router.post("/media/upload-urls", response_model=UploadUrlResponse)
def upload_url(
    body: UploadUrlRequest,
    ctx: Device,
    storage: Annotated[ObjectStorage, Depends(get_storage)],
) -> UploadUrlResponse:
    seconds = get_settings().upload_url_seconds
    key = media_key(
        ctx.device.tenant_id, ctx.device.branch_id, body.event_id, body.kind, body.sha256
    )
    return UploadUrlResponse(
        media_key=key,
        upload_url=storage.presign_put(key, body.content_type, seconds),
        headers={"Content-Type": body.content_type},
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=seconds),
    )


class _RejectedError(Exception):
    pass


def _check(ctx: DeviceContext, event: EventIn) -> None:
    device = ctx.device
    camera = ctx.db.get(Camera, event.camera_id)
    if camera is None or camera.branch_id != device.branch_id:
        raise _RejectedError("unknown camera")
    if event.zone_id is not None:
        zone = ctx.db.get(Zone, event.zone_id)
        if zone is None or zone.camera_id != camera.id:
            raise _RejectedError("unknown zone")
    if event.staff_id is not None and ctx.db.get(Staff, event.staff_id) is None:
        raise _RejectedError("unknown staff member")
    for key, kind in ((event.snapshot_key, MediaKind.SNAPSHOT), (event.clip_key, MediaKind.CLIP)):
        if key is not None and not key_belongs_to(
            key, device.tenant_id, device.branch_id, event.event_id, kind
        ):
            raise _RejectedError(f"{kind.value} key was not issued for this event")


def _store(ctx: DeviceContext, event: EventIn, now: datetime) -> tuple[Event, Change] | None:
    """Insert or update; returns None when nothing changed (a stale retry)."""
    existing = ctx.db.get(Event, event.event_id)
    if existing is None:
        stored = Event(
            id=event.event_id,
            tenant_id=ctx.device.tenant_id,
            branch_id=ctx.device.branch_id,
            camera_id=event.camera_id,
            zone_id=event.zone_id,
            edge_device_id=ctx.device.id,
            feature_key=event.feature.value,
            event_type=event.event_type.value,
            severity=event.severity.value,
            status=event.status.value,
            started_at=event.started_at,
            ended_at=event.ended_at,
            confidence=event.confidence,
            staff_id=event.staff_id,
            track_id=event.track_id,
            snapshot_key=event.snapshot_key,
            clip_key=event.clip_key,
            attributes=event.attributes,
        )
        ctx.db.add(stored)
        ctx.db.flush()
        return stored, Change.NEW
    if existing.camera_id != event.camera_id or existing.event_type != event.event_type.value:
        raise _RejectedError("event_id already used for another event")
    if existing.status == EventStatus.CLOSED.value and event.status is EventStatus.OPEN:
        return None  # an older "open" retry arriving after "closed": keep the newer state
    closing = existing.status == EventStatus.OPEN.value and event.status is EventStatus.CLOSED
    existing.status = event.status.value
    existing.ended_at = event.ended_at
    existing.severity = event.severity.value
    existing.confidence = max(existing.confidence, event.confidence)
    existing.zone_id = event.zone_id
    existing.staff_id = event.staff_id
    existing.snapshot_key = event.snapshot_key
    existing.clip_key = event.clip_key or existing.clip_key
    existing.attributes = {**existing.attributes, **event.attributes}
    existing.updated_at = now
    return existing, Change.CLOSED if closing else Change.UPDATED


@router.post("/events", response_model=EventBatchResponse)
def ingest_events(
    body: EventBatch,
    ctx: Device,
    background: BackgroundTasks,
    publisher: PublisherDep,
    senders: Annotated[Senders, Depends(get_senders)],
    poster: Annotated[webhooks.WebhookPoster, Depends(webhooks.get_webhook_poster)],
) -> EventBatchResponse:
    """Idempotent by event_id; each event is accepted or rejected on its own. New events
    are routed to alert rules and webhooks; both are sent once the batch is committed."""
    now = datetime.now(timezone.utc)
    results = []
    deliveries: list[int] = []
    hook_messages: list[int] = []
    for event in body.events:
        try:
            with ctx.db.begin_nested():  # one bad event doesn't undo the others
                _check(ctx, event)
                changed = _store(ctx, event, now)
        except _RejectedError as exc:
            results.append(EventResult(event_id=event.event_id, accepted=False, error=str(exc)))
            continue
        except IntegrityError:
            log.warning("event %s rejected by the database", event.event_id)
            results.append(
                EventResult(event_id=event.event_id, accepted=False, error="conflicting event")
            )
            continue
        results.append(EventResult(event_id=event.event_id, accepted=True))
        if changed:
            try:
                with ctx.db.begin_nested():  # an alerting bug must not lose the event
                    deliveries += route_event(ctx.db, *changed, now)
            except Exception:
                log.exception("alert routing failed for event %s", event.event_id)
            try:
                with ctx.db.begin_nested():
                    hook_messages += webhooks.queue_event(ctx.db, *changed)
            except Exception:
                log.exception("webhook queueing failed for event %s", event.event_id)
            background.add_task(
                publisher.publish,
                f"events:{ctx.device.tenant_id}",
                {
                    "event_id": str(event.event_id),
                    "status": event.status.value,
                    "event_type": event.event_type.value,
                    "severity": event.severity.value,
                    "branch_id": str(ctx.device.branch_id),
                    "camera_id": str(event.camera_id),
                    "started_at": event.started_at.isoformat(),
                },
            )
    ctx.device.last_seen_at = now
    if deliveries:
        background.add_task(dispatch, ctx.device.tenant_id, senders, deliveries)
    if hook_messages:
        background.add_task(webhooks.dispatch, ctx.device.tenant_id, poster, hook_messages)
    return EventBatchResponse(results=results)


@router.get(
    "/remote",
    response_model=RemoteRequest,
    responses={204: {"description": "Nothing to do within `wait` seconds"}},
)
async def next_remote_request(
    device: DeviceId, relay: Relay, wait: Annotated[int, Query(ge=0, le=55)] = 25
) -> RemoteRequest | Response:
    """Long poll: the next request for this box (live view, playback, ...), if any."""
    request = await relay.next_request(device, wait)
    if request is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return request


@router.post("/remote/{request_id}/reply", status_code=status.HTTP_204_NO_CONTENT)
async def reply_remote(
    request_id: uuid.UUID, body: RemoteReply, device: DeviceId, relay: Relay
) -> None:
    """Answer a request; only the box it was sent to can, and only once."""
    if not await relay.reply(device, request_id, body):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown or expired request")


@router.get(
    "/face-templates",
    response_model=FaceTemplateList,
    responses={304: {"description": "Unchanged"}},
)
def face_templates(
    ctx: Device,
    response: Response,
    if_none_match: Annotated[str | None, Header()] = None,
) -> FaceTemplateList | Response:
    """Templates of the branch's active staff with recorded consent, for recognition on
    the box. The box keeps them in memory only."""
    rows = ctx.db.execute(
        select(FaceTemplate.id, FaceTemplate.staff_id, FaceTemplate.model_version)
        .join(Staff, Staff.id == FaceTemplate.staff_id)
        .where(
            Staff.branch_id == ctx.device.branch_id,
            Staff.status == StaffStatus.ACTIVE.value,
            Staff.consent_signed_at.is_not(None),
        )
        .order_by(FaceTemplate.id)
    ).all()
    # Re-enrolment creates a new template id, so the ids identify the content.
    etag = '"' + sha256_hex(",".join(str(r.id) for r in rows))[:32] + '"'
    if if_none_match == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": etag})
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "no-store"
    encrypted = {
        r.id: r
        for r in ctx.db.execute(
            select(FaceTemplate.id, FaceTemplate.embedding_encrypted, FaceTemplate.key_id).where(
                FaceTemplate.id.in_([r.id for r in rows])
            )
        )
    }
    return FaceTemplateList(
        templates=[
            EdgeFaceTemplate(
                staff_id=r.staff_id,
                model_version=r.model_version,
                embedding=open_embedding(
                    encrypted[r.id].embedding_encrypted,
                    encrypted[r.id].key_id,
                    ctx.device.tenant_id,
                    r.staff_id,
                ),
            )
            for r in rows
        ]
    )


@router.post("/attendance", response_model=BatchResult)
def report_sightings(body: SightingBatch, ctx: Device) -> BatchResult:
    """Staff recognised by the box; each item is accepted or rejected on its own."""
    result = apply_sightings(ctx.db, ctx.device.tenant_id, ctx.device.branch_id, body.sightings)
    if result.rejected:
        log.warning(
            "device %s: %d sightings rejected (first: %s)",
            ctx.device.id,
            len(result.rejected),
            result.rejected[0].error,
        )
    return result


@router.post("/counts", response_model=BatchResult)
def report_counts(body: CountBatch, ctx: Device) -> BatchResult:
    """Finished count buckets; a bucket sent again replaces the stored one."""
    return apply_counts(ctx.db, ctx.device.tenant_id, ctx.device.branch_id, body.buckets)


@router.post("/heatmaps", response_model=BatchResult)
def report_heatmaps(body: HeatmapBatch, ctx: Device) -> BatchResult:
    """Finished heatmap buckets; a bucket sent again replaces the stored one."""
    return apply_heatmaps(ctx.db, ctx.device.tenant_id, ctx.device.branch_id, body.buckets)
