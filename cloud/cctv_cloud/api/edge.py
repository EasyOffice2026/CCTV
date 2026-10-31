"""Edge device API (`cctv_contracts.v1`; see contracts/README.md).

Devices authenticate with their token (`Authorization: Bearer ...`). The token identifies
the tenant and branch, so nothing in a request body can point at another tenant's data:
every camera, zone, staff member and media key is checked against the device's branch.
"""

import logging
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
    Response,
    status,
)
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cctv_cloud.api.deps import PublisherDep, unauthorized
from cctv_cloud.config import get_settings
from cctv_cloud.db.enums import DeviceStatus
from cctv_cloud.db.models import Branch, Camera, EdgeDevice, Event, Staff, Zone
from cctv_cloud.db.session import tenant_session
from cctv_cloud.security import new_secret, normalise_pairing_code, sha256_hex
from cctv_cloud.services.edge_config import build_edge_config
from cctv_cloud.services.storage import ObjectStorage, get_storage, key_belongs_to, media_key
from cctv_contracts.enums import EventStatus, MediaKind
from cctv_contracts.v1 import (
    API_PREFIX,
    EdgeConfig,
    EventBatch,
    EventBatchResponse,
    EventIn,
    EventResult,
    Heartbeat,
    HeartbeatResponse,
    PairRequest,
    PairResponse,
    UploadUrlRequest,
    UploadUrlResponse,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix=API_PREFIX, tags=["edge"])
_bearer = HTTPBearer(auto_error=False)


@dataclass
class DeviceContext:
    device: EdgeDevice
    db: Session


def device_context(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Iterator[DeviceContext]:
    if credentials is None:
        raise unauthorized("device token required")
    token_hash = sha256_hex(credentials.credentials)
    with tenant_session(None) as db:
        found = db.execute(
            text("SELECT device_id, tenant_id FROM auth_find_device(:hash)"), {"hash": token_hash}
        ).one_or_none()
    if found is None:
        raise unauthorized("unknown or retired device")
    with tenant_session(found.tenant_id) as db:
        device = db.get(EdgeDevice, found.device_id)
        if device is None or device.token_hash != token_hash:
            raise unauthorized("unknown or retired device")
        yield DeviceContext(device, db)


Device = Annotated[DeviceContext, Depends(device_context)]


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
    # Only this device's branch: a device can't report on other cameras.
    for camera in body.cameras:
        ctx.db.execute(
            update(Camera)
            .where(Camera.id == camera.camera_id, Camera.branch_id == device.branch_id)
            .values(
                health=camera.health.value,
                last_seen_at=camera.last_frame_at or Camera.last_seen_at,
            )
        )
    version = ctx.db.scalar(select(Branch.config_version).where(Branch.id == device.branch_id))
    return HeartbeatResponse(server_time=now, latest_config_version=version or 0)


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


def _store(ctx: DeviceContext, event: EventIn, now: datetime) -> bool:
    """Insert or update; returns False when nothing changed (a stale retry)."""
    existing = ctx.db.get(Event, event.event_id)
    if existing is None:
        ctx.db.add(
            Event(
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
        )
        return True
    if existing.camera_id != event.camera_id or existing.event_type != event.event_type.value:
        raise _RejectedError("event_id already used for another event")
    if existing.status == EventStatus.CLOSED.value and event.status is EventStatus.OPEN:
        return False  # an older "open" retry arriving after "closed": keep the newer state
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
    return True


@router.post("/events", response_model=EventBatchResponse)
def ingest_events(
    body: EventBatch, ctx: Device, background: BackgroundTasks, publisher: PublisherDep
) -> EventBatchResponse:
    """Idempotent by event_id; each event is accepted or rejected on its own."""
    now = datetime.now(timezone.utc)
    results = []
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
    return EventBatchResponse(results=results)
