"""Live view, NVR playback, recording lists and camera frames, through the branch's edge
box (plan 4.4, 4.5, 4.6).

The browser sends a WebRTC offer; the cloud passes it to the box (see services/relay.py)
and returns the box's answer, after which video flows from the box to the browser
directly or through a TURN server, never through this API. Every view is written to the
audit log before the box is asked.
"""

import base64
import binascii
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from cctv_cloud.api.deps import not_found, require, require_any
from cctv_cloud.config import get_settings
from cctv_cloud.db.enums import DeviceStatus
from cctv_cloud.db.models import Camera, EdgeDevice
from cctv_cloud.db.session import tenant_session
from cctv_cloud.rbac import Permission, Principal
from cctv_cloud.services.audit import AuditAction, record
from cctv_cloud.services.relay import EdgeRelay, get_relay
from cctv_contracts.v1 import (
    FrameRequest,
    LiveRequest,
    PlaybackRequest,
    RecordingSpan,
    RecordingsRequest,
    RemoteReply,
    RemoteRequest,
)
from cctv_contracts.v1.remote import MAX_PLAYBACK, MAX_RECORDINGS_RANGE, RemotePayload, Sdp

router = APIRouter(prefix="/v1", tags=["live view"])

Viewer = Annotated[Principal, Depends(require(Permission.LIVE_VIEW))]
FrameViewer = Annotated[
    Principal, Depends(require_any(Permission.LIVE_VIEW, Permission.DEVICES_MANAGE))
]
Relay = Annotated[EdgeRelay, Depends(get_relay)]


class IceServers(BaseModel):
    ice_servers: list[dict[str, Any]]


class LiveIn(BaseModel):
    sdp_offer: Sdp


class PlaybackIn(LiveIn):
    start: datetime
    end: datetime


class SdpAnswer(BaseModel):
    sdp_answer: str


def online_edge(db: Session, branch_id: uuid.UUID) -> uuid.UUID:
    """The branch's edge box that reported most recently, if it is online."""
    stale = datetime.now(timezone.utc) - timedelta(seconds=get_settings().device_offline_seconds)
    device_id = db.scalar(
        select(EdgeDevice.id)
        .where(
            EdgeDevice.branch_id == branch_id,
            EdgeDevice.status == DeviceStatus.ONLINE.value,
            EdgeDevice.last_seen_at > stale,
        )
        .order_by(EdgeDevice.last_seen_at.desc())
        .limit(1)
    )
    if device_id is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "the branch's edge box is offline")
    return device_id


def edge_for_camera(
    principal: Principal,
    camera_id: uuid.UUID,
    request: Request,
    action: AuditAction | None = None,
    details: dict[str, Any] | None = None,
) -> uuid.UUID:
    """The online edge box of the camera's branch. Writes the audit entry (if any) first."""
    with tenant_session(principal.tenant_id, principal.user_id) as db:
        camera = db.get(Camera, camera_id)
        if camera is None or not principal.can_access_branch(camera.branch_id):
            raise not_found("camera not found")
        device_id = online_edge(db, camera.branch_id)
        if action is not None:
            record(db, principal, action, "camera", camera_id, request, details)
    return device_id


async def ask_edge(
    relay: EdgeRelay, device_id: uuid.UUID, payload: RemotePayload, timeout: float | None = None
) -> RemoteReply:
    timeout = timeout or get_settings().remote_timeout_seconds
    remote = RemoteRequest(
        id=uuid.uuid4(),
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=timeout),
        request=payload,
    )
    await relay.submit(device_id, remote)
    reply = await relay.wait_reply(remote.id, timeout)
    if reply is None:
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, "the edge box did not answer in time")
    if not reply.ok:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, reply.error or "the edge box failed")
    return reply


def _check_range(start: datetime, end: datetime, limit: timedelta) -> None:
    if start.tzinfo is None or end.tzinfo is None:
        raise HTTPException(422, "times need a UTC offset")
    if end <= start or end - start > limit:
        raise HTTPException(422, f"the time range must be positive and at most {limit}")


@router.get("/live/ice-servers", response_model=IceServers)
def ice_servers(_: FrameViewer) -> IceServers:
    """STUN/TURN servers for the browser's RTCPeerConnection."""
    return IceServers(ice_servers=get_settings().webrtc_ice_servers)


@router.post("/cameras/{camera_id}/live", response_model=SdpAnswer)
async def start_live(
    camera_id: uuid.UUID, body: LiveIn, principal: Viewer, request: Request, relay: Relay
) -> SdpAnswer:
    """Exchange a WebRTC offer for the box's answer (camera sub-stream). The browser closes
    the connection when the viewer leaves or is inactive."""
    device = await run_in_threadpool(
        edge_for_camera, principal, camera_id, request, AuditAction.VIEW_LIVE
    )
    reply = await ask_edge(
        relay, device, LiveRequest(camera_id=camera_id, sdp_offer=body.sdp_offer)
    )
    if not reply.sdp_answer:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "the edge box sent no answer")
    return SdpAnswer(sdp_answer=reply.sdp_answer)


@router.post("/cameras/{camera_id}/playback", response_model=SdpAnswer)
async def start_playback(
    camera_id: uuid.UUID, body: PlaybackIn, principal: Viewer, request: Request, relay: Relay
) -> SdpAnswer:
    """Play the NVR's recording from `start` to `end` (at most 2 hours) over WebRTC."""
    _check_range(body.start, body.end, MAX_PLAYBACK)
    details = {"start": body.start.isoformat(), "end": body.end.isoformat()}
    device = await run_in_threadpool(
        edge_for_camera, principal, camera_id, request, AuditAction.VIEW_PLAYBACK, details
    )
    reply = await ask_edge(
        relay,
        device,
        PlaybackRequest(
            camera_id=camera_id, start=body.start, end=body.end, sdp_offer=body.sdp_offer
        ),
    )
    if not reply.sdp_answer:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "the edge box sent no answer")
    return SdpAnswer(sdp_answer=reply.sdp_answer)


@router.get("/cameras/{camera_id}/recordings", response_model=list[RecordingSpan])
async def list_recordings(
    camera_id: uuid.UUID,
    principal: Viewer,
    request: Request,
    relay: Relay,
    start: Annotated[datetime, Query(alias="from")],
    end: Annotated[datetime, Query(alias="to")],
) -> list[RecordingSpan]:
    """What the NVR recorded for this camera (at most one day per call), for the timeline."""
    _check_range(start, end, MAX_RECORDINGS_RANGE)
    device = await run_in_threadpool(edge_for_camera, principal, camera_id, request)
    reply = await ask_edge(
        relay, device, RecordingsRequest(camera_id=camera_id, start=start, end=end)
    )
    return reply.recordings or []


@router.get(
    "/cameras/{camera_id}/frame",
    response_class=Response,
    responses={200: {"content": {"image/jpeg": {}}}},
)
async def camera_frame(
    camera_id: uuid.UUID, principal: FrameViewer, request: Request, relay: Relay
) -> Response:
    """A current frame from the camera (e.g. to draw zones on). Not stored anywhere."""
    device = await run_in_threadpool(
        edge_for_camera, principal, camera_id, request, AuditAction.VIEW_FRAME
    )
    reply = await ask_edge(relay, device, FrameRequest(camera_id=camera_id))
    try:
        jpeg = base64.b64decode(reply.jpeg_base64 or "", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "the edge box sent a bad image") from exc
    if not jpeg.startswith(b"\xff\xd8"):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "the edge box sent no image")
    return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})
