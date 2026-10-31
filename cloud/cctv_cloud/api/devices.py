"""NVRs, cameras, zones and edge devices (plan 3.3). Changes bump the branch config version
so the edge picks them up."""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from cctv_cloud.api.admin import ORMModel, accessible_branch, notify_config
from cctv_cloud.api.deps import Context, PublisherDep, context, not_found
from cctv_cloud.config import get_settings
from cctv_cloud.db.enums import DeviceStatus
from cctv_cloud.db.models import Camera, EdgeDevice, Nvr, Zone
from cctv_cloud.rbac import Permission
from cctv_cloud.security import new_pairing_code, sha256_hex
from cctv_cloud.services.audit import AuditAction, record
from cctv_cloud.services.edge_config import bump_config_version
from cctv_contracts.enums import CameraHealth, CameraRole, ZoneType
from cctv_contracts.v1 import ZoneConfig
from cctv_contracts.v1.config import HOST_PATTERN

router = APIRouter(prefix="/v1", tags=["devices"])

AnyMember = Annotated[Context, Depends(context(), scope="function")]
DeviceAdmin = Annotated[Context, Depends(context(Permission.DEVICES_MANAGE), scope="function")]


def _conflict(message: str) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, message)


def _flush(ctx: Context, message: str) -> None:
    try:
        ctx.db.flush()
    except IntegrityError as exc:
        raise _conflict(message) from exc


def _changed(ctx: Context, background: BackgroundTasks, publisher, branch_id: uuid.UUID) -> None:
    notify_config(background, publisher, bump_config_version(ctx.db, [branch_id]))


def _check_rtsp(value: str | None) -> str | None:
    if value is None:
        return value
    if not value.startswith(("rtsp://", "rtsps://")):
        raise ValueError("must be an rtsp:// or rtsps:// URL")
    if "@" in value:
        raise ValueError("must not contain a username or password; use credentials_ref")
    return value


# --- NVRs ---------------------------------------------------------------------------


class NvrIn(BaseModel):
    branch_id: uuid.UUID
    name: str = Field(min_length=1, max_length=100)
    vendor: str = "hikvision"
    model: str | None = Field(default=None, max_length=100)
    host: str = Field(min_length=1, max_length=255, pattern=HOST_PATTERN)
    http_port: int = Field(default=80, ge=1, le=65535)
    rtsp_port: int = Field(default=554, ge=1, le=65535)
    credentials_ref: str | None = Field(default=None, max_length=100)


class NvrOut(ORMModel, NvrIn):
    id: uuid.UUID


def _nvr(ctx: Context, nvr_id: uuid.UUID) -> Nvr:
    nvr = ctx.db.get(Nvr, nvr_id)
    if nvr is None or not ctx.principal.can_access_branch(nvr.branch_id):
        raise not_found("NVR not found")
    return nvr


@router.get("/nvrs", response_model=list[NvrOut])
def list_nvrs(ctx: DeviceAdmin) -> list[Nvr]:
    query = select(Nvr).order_by(Nvr.name)
    if ctx.principal.branch_ids is not None:
        query = query.where(Nvr.branch_id.in_(ctx.principal.branch_ids))
    return list(ctx.db.scalars(query))


@router.post("/nvrs", response_model=NvrOut, status_code=status.HTTP_201_CREATED)
def create_nvr(body: NvrIn, ctx: DeviceAdmin) -> Nvr:
    accessible_branch(ctx, body.branch_id)
    nvr = Nvr(tenant_id=ctx.principal.tenant_id, **body.model_dump())
    ctx.db.add(nvr)
    _flush(ctx, "an NVR with this name exists in the branch")
    return nvr


@router.delete("/nvrs/{nvr_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_nvr(nvr_id: uuid.UUID, ctx: DeviceAdmin) -> None:
    ctx.db.delete(_nvr(ctx, nvr_id))
    _flush(ctx, "cameras still use this NVR")


# --- cameras ------------------------------------------------------------------------


class CameraIn(BaseModel):
    branch_id: uuid.UUID
    name: str = Field(min_length=1, max_length=100)
    role: CameraRole = CameraRole.OTHER
    nvr_id: uuid.UUID | None = None
    channel: int | None = Field(default=None, ge=1, le=256)
    rtsp_main: str = Field(max_length=500)
    rtsp_sub: str | None = Field(default=None, max_length=500)
    credentials_ref: str | None = Field(default=None, max_length=100)
    enabled: bool = True

    _rtsp = field_validator("rtsp_main", "rtsp_sub")(_check_rtsp)


class CameraUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    role: CameraRole | None = None
    nvr_id: uuid.UUID | None = None
    channel: int | None = Field(default=None, ge=1, le=256)
    rtsp_main: str | None = Field(default=None, max_length=500)
    rtsp_sub: str | None = Field(default=None, max_length=500)
    credentials_ref: str | None = Field(default=None, max_length=100)
    enabled: bool | None = None

    _rtsp = field_validator("rtsp_main", "rtsp_sub")(_check_rtsp)


class CameraOut(ORMModel, CameraIn):
    id: uuid.UUID
    health: CameraHealth
    last_seen_at: datetime | None


def accessible_camera(ctx: Context, camera_id: uuid.UUID) -> Camera:
    camera = ctx.db.get(Camera, camera_id)
    if camera is None or not ctx.principal.can_access_branch(camera.branch_id):
        raise not_found("camera not found")
    return camera


@router.get("/cameras", response_model=list[CameraOut])
def list_cameras(ctx: AnyMember, branch_id: uuid.UUID | None = None) -> list[Camera]:
    query = select(Camera).order_by(Camera.name)
    if branch_id is not None:
        query = query.where(Camera.branch_id == branch_id)
    if ctx.principal.branch_ids is not None:
        query = query.where(Camera.branch_id.in_(ctx.principal.branch_ids))
    return list(ctx.db.scalars(query))


@router.post("/cameras", response_model=CameraOut, status_code=status.HTTP_201_CREATED)
def create_camera(
    body: CameraIn, ctx: DeviceAdmin, background: BackgroundTasks, publisher: PublisherDep
) -> Camera:
    accessible_branch(ctx, body.branch_id)
    if body.nvr_id is not None and _nvr(ctx, body.nvr_id).branch_id != body.branch_id:
        raise HTTPException(422, "the NVR is in another branch")
    camera = Camera(tenant_id=ctx.principal.tenant_id, **body.model_dump(mode="json"))
    ctx.db.add(camera)
    _flush(ctx, "a camera with this name or NVR channel exists")
    _changed(ctx, background, publisher, camera.branch_id)
    ctx.db.refresh(camera)
    return camera


@router.get("/cameras/{camera_id}", response_model=CameraOut)
def get_camera(camera_id: uuid.UUID, ctx: AnyMember) -> Camera:
    return accessible_camera(ctx, camera_id)


@router.patch("/cameras/{camera_id}", response_model=CameraOut)
def update_camera(
    camera_id: uuid.UUID,
    body: CameraUpdate,
    ctx: DeviceAdmin,
    background: BackgroundTasks,
    publisher: PublisherDep,
) -> Camera:
    camera = accessible_camera(ctx, camera_id)
    changes = body.model_dump(exclude_unset=True, mode="json")
    if changes.get("rtsp_main", "x") is None:
        raise HTTPException(422, "rtsp_main can't be empty")
    if (
        changes.get("nvr_id")
        and _nvr(ctx, uuid.UUID(changes["nvr_id"])).branch_id != camera.branch_id
    ):
        raise HTTPException(422, "the NVR is in another branch")
    for key, value in changes.items():
        setattr(camera, key, value)
    _flush(ctx, "a camera with this name or NVR channel exists")
    _changed(ctx, background, publisher, camera.branch_id)
    ctx.db.refresh(camera)
    return camera


@router.delete("/cameras/{camera_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_camera(
    camera_id: uuid.UUID, ctx: DeviceAdmin, background: BackgroundTasks, publisher: PublisherDep
) -> None:
    camera = accessible_camera(ctx, camera_id)
    ctx.db.delete(camera)
    _flush(ctx, "the camera has events; disable it instead")
    _changed(ctx, background, publisher, camera.branch_id)


# --- zones --------------------------------------------------------------------------


class ZoneIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    type: ZoneType
    points: list[tuple[float, float]]


class ZoneOut(ZoneIn):
    id: uuid.UUID
    camera_id: uuid.UUID


def _validated_zone(zone_id: uuid.UUID, body: ZoneIn) -> ZoneConfig:
    """The same rules the edge applies (0..1 coordinates, lines vs polygons)."""
    try:
        return ZoneConfig(id=zone_id, name=body.name, type=body.type, points=body.points)
    except ValidationError as exc:
        raise HTTPException(
            422, exc.errors(include_url=False, include_context=False, include_input=False)
        ) from exc


def _zone_out(zone: Zone) -> ZoneOut:
    return ZoneOut(
        id=zone.id,
        camera_id=zone.camera_id,
        name=zone.name,
        type=ZoneType(zone.zone_type),
        points=[tuple(p) for p in zone.points],
    )


@router.get("/cameras/{camera_id}/zones", response_model=list[ZoneOut])
def list_zones(camera_id: uuid.UUID, ctx: AnyMember) -> list[ZoneOut]:
    accessible_camera(ctx, camera_id)
    zones = ctx.db.scalars(select(Zone).where(Zone.camera_id == camera_id).order_by(Zone.name))
    return [_zone_out(z) for z in zones]


@router.post(
    "/cameras/{camera_id}/zones", response_model=ZoneOut, status_code=status.HTTP_201_CREATED
)
def create_zone(
    camera_id: uuid.UUID,
    body: ZoneIn,
    ctx: DeviceAdmin,
    background: BackgroundTasks,
    publisher: PublisherDep,
) -> ZoneOut:
    camera = accessible_camera(ctx, camera_id)
    config = _validated_zone(uuid.uuid4(), body)
    zone = Zone(
        id=config.id,
        tenant_id=ctx.principal.tenant_id,
        camera_id=camera.id,
        name=config.name,
        zone_type=config.type.value,
        points=[list(p) for p in config.points],
    )
    ctx.db.add(zone)
    _flush(ctx, "a zone with this name exists on the camera")
    _changed(ctx, background, publisher, camera.branch_id)
    return _zone_out(zone)


def _zone(ctx: Context, zone_id: uuid.UUID) -> tuple[Zone, Camera]:
    zone = ctx.db.get(Zone, zone_id)
    if zone is None:
        raise not_found("zone not found")
    return zone, accessible_camera(ctx, zone.camera_id)


@router.put("/zones/{zone_id}", response_model=ZoneOut)
def replace_zone(
    zone_id: uuid.UUID,
    body: ZoneIn,
    ctx: DeviceAdmin,
    background: BackgroundTasks,
    publisher: PublisherDep,
) -> ZoneOut:
    zone, camera = _zone(ctx, zone_id)
    config = _validated_zone(zone.id, body)
    zone.name = config.name
    zone.zone_type = config.type.value
    zone.points = [list(p) for p in config.points]
    zone.updated_at = datetime.now(timezone.utc)
    _flush(ctx, "a zone with this name exists on the camera")
    _changed(ctx, background, publisher, camera.branch_id)
    return _zone_out(zone)


@router.delete("/zones/{zone_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_zone(
    zone_id: uuid.UUID, ctx: DeviceAdmin, background: BackgroundTasks, publisher: PublisherDep
) -> None:
    zone, camera = _zone(ctx, zone_id)
    ctx.db.delete(zone)
    _flush(ctx, "events refer to this zone")
    _changed(ctx, background, publisher, camera.branch_id)


# --- edge devices -------------------------------------------------------------------


class EdgeDeviceIn(BaseModel):
    branch_id: uuid.UUID
    name: str = Field(min_length=1, max_length=100)
    serial: str = Field(min_length=1, max_length=100)


class EdgeDeviceOut(ORMModel):
    id: uuid.UUID
    branch_id: uuid.UUID
    name: str
    serial: str
    status: DeviceStatus
    software_version: str | None
    config_version_applied: int | None
    last_seen_at: datetime | None
    paired: bool


class PairingCode(BaseModel):
    device: EdgeDeviceOut
    pairing_code: str = Field(description="Shown once. Typed in when installing the box.")
    expires_at: datetime


def _device_out(device: EdgeDevice) -> EdgeDeviceOut:
    return EdgeDeviceOut(
        id=device.id,
        branch_id=device.branch_id,
        name=device.name,
        serial=device.serial,
        status=DeviceStatus(device.status),
        software_version=device.software_version,
        config_version_applied=device.config_version_applied,
        last_seen_at=device.last_seen_at,
        paired=device.token_hash is not None,
    )


def _new_pairing_code(device: EdgeDevice) -> PairingCode:
    code = new_pairing_code()
    device.pairing_code_hash = sha256_hex(code)
    device.pairing_expires_at = datetime.now(timezone.utc) + timedelta(
        hours=get_settings().pairing_code_hours
    )
    return PairingCode(
        device=_device_out(device), pairing_code=code, expires_at=device.pairing_expires_at
    )


def _device(ctx: Context, device_id: uuid.UUID) -> EdgeDevice:
    device = ctx.db.get(EdgeDevice, device_id)
    if device is None or not ctx.principal.can_access_branch(device.branch_id):
        raise not_found("edge device not found")
    return device


@router.get("/edge-devices", response_model=list[EdgeDeviceOut])
def list_devices(ctx: DeviceAdmin, branch_id: Annotated[uuid.UUID | None, Query()] = None):
    query = select(EdgeDevice).order_by(EdgeDevice.name)
    if branch_id is not None:
        query = query.where(EdgeDevice.branch_id == branch_id)
    if ctx.principal.branch_ids is not None:
        query = query.where(EdgeDevice.branch_id.in_(ctx.principal.branch_ids))
    return [_device_out(d) for d in ctx.db.scalars(query)]


@router.post("/edge-devices", response_model=PairingCode, status_code=status.HTTP_201_CREATED)
def register_device(body: EdgeDeviceIn, ctx: DeviceAdmin, request: Request) -> PairingCode:
    accessible_branch(ctx, body.branch_id)
    device = EdgeDevice(tenant_id=ctx.principal.tenant_id, **body.model_dump())
    ctx.db.add(device)
    _flush(ctx, "a device with this serial number exists")
    record(
        ctx.db,
        ctx.principal,
        AuditAction.DEVICE_CHANGE,
        "edge_device",
        device.id,
        request,
        {"registered": body.serial},
    )
    device.status = DeviceStatus.PENDING.value
    return _new_pairing_code(device)


@router.post("/edge-devices/{device_id}/pairing-code", response_model=PairingCode)
def renew_pairing_code(device_id: uuid.UUID, ctx: DeviceAdmin, request: Request) -> PairingCode:
    """New code, e.g. to replace a broken box. The old box keeps working until the new
    one pairs."""
    device = _device(ctx, device_id)
    if device.status == DeviceStatus.RETIRED.value:
        raise _conflict("the device is retired")
    record(
        ctx.db,
        ctx.principal,
        AuditAction.DEVICE_CHANGE,
        "edge_device",
        device.id,
        request,
        {"pairing_code": "renewed"},
    )
    return _new_pairing_code(device)


@router.delete("/edge-devices/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
def retire_device(device_id: uuid.UUID, ctx: DeviceAdmin, request: Request) -> None:
    """The device token stops working immediately."""
    device = _device(ctx, device_id)
    device.status = DeviceStatus.RETIRED.value
    device.token_hash = None
    device.previous_token_hash = None
    device.pairing_code_hash = None
    record(
        ctx.db,
        ctx.principal,
        AuditAction.DEVICE_CHANGE,
        "edge_device",
        device.id,
        request,
        {"retired": device.serial},
    )
