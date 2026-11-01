"""Edge software releases and rollouts (plan 2.10), for our team (super-admins).

A release is a version and the edge image pinned by digest, from the release workflow's
summary. Rolling it out sets the target version of every box of all clients, one client or
one box; the boxes switch with their next heartbeat (services/releases.py)."""

import logging
import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from cctv_cloud.api.deps import not_found, require_platform
from cctv_cloud.db.enums import DeviceStatus
from cctv_cloud.db.models import Branch, EdgeDevice, EdgeRelease, Tenant
from cctv_cloud.db.session import tenant_session
from cctv_cloud.rbac import Principal
from cctv_cloud.services.audit import AuditAction, record
from cctv_cloud.services.releases import GAVE_UP
from cctv_contracts.enums import UpdateState
from cctv_contracts.v1 import IMAGE_REF_PATTERN, SOFTWARE_VERSION_PATTERN

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/platform", tags=["platform"])

PlatformAdmin = Annotated[Principal, Depends(require_platform)]


class ReleaseIn(BaseModel):
    version: str = Field(pattern=SOFTWARE_VERSION_PATTERN, max_length=50, examples=["1.4.0"])
    image: str = Field(
        pattern=IMAGE_REF_PATTERN,
        max_length=330,
        description="From the release workflow's summary: repository@sha256:digest",
    )
    notes: str = Field(default="", max_length=2000)


class ReleaseOut(BaseModel):
    version: str
    image: str
    notes: str
    created_at: datetime
    withdrawn_at: datetime | None
    running: int = Field(description="Boxes on this version")
    updating: int = Field(description="Boxes asked to switch to it and not on it yet")
    failed: int = Field(description="Of those, boxes whose update failed or was rolled back")


class RolloutIn(BaseModel):
    tenant_id: uuid.UUID | None = Field(default=None, description="One client; none: all")
    device_id: uuid.UUID | None = Field(default=None, description="One box of that client")

    @model_validator(mode="after")
    def _device_needs_tenant(self) -> "RolloutIn":
        if self.device_id is not None and self.tenant_id is None:
            raise ValueError("device_id needs tenant_id")
        return self


class RolloutOut(BaseModel):
    devices: int


class PlatformDeviceOut(BaseModel):
    tenant_id: uuid.UUID
    tenant: str
    id: uuid.UUID
    name: str
    branch: str
    status: DeviceStatus
    last_seen_at: datetime | None
    software_version: str | None
    target_version: str | None
    update_version: str | None
    update_state: UpdateState | None
    update_detail: str | None
    update_reported_at: datetime | None


def _gave_up(d: EdgeDevice) -> bool:
    return (
        d.update_version == d.target_version
        and d.update_requested_at == d.target_set_at
        and d.update_state in GAVE_UP
    )


def _all_devices(principal: Principal, tenant: uuid.UUID | None = None):
    """(tenant name, branch name, device) of every box not retired, client by client."""
    from cctv_cloud.worker import tenant_ids

    for tenant_id in [tenant] if tenant else tenant_ids():
        with tenant_session(tenant_id, principal.user_id) as db:
            name = db.scalar(select(Tenant.name))
            rows = db.execute(
                select(EdgeDevice, Branch.name)
                .join(Branch, Branch.id == EdgeDevice.branch_id)
                .where(EdgeDevice.status != DeviceStatus.RETIRED.value)
                .order_by(Branch.name, EdgeDevice.name)
            ).all()
            for device, branch in rows:
                db.expunge(device)
                yield name, branch, device


@router.get("/edge-releases", response_model=list[ReleaseOut])
def list_releases(principal: PlatformAdmin) -> list[ReleaseOut]:
    """Newest first, with how many boxes run each one and how its rollout is going."""
    devices = [d for _, _, d in _all_devices(principal)]
    with tenant_session(None, principal.user_id) as db:
        releases = db.scalars(select(EdgeRelease).order_by(EdgeRelease.created_at.desc())).all()
        result = []
        for r in releases:
            asked = [
                d
                for d in devices
                if d.target_version == r.version and d.software_version != r.version
            ]
            result.append(
                ReleaseOut(
                    version=r.version,
                    image=r.image,
                    notes=r.notes,
                    created_at=r.created_at,
                    withdrawn_at=r.withdrawn_at,
                    running=sum(d.software_version == r.version for d in devices),
                    updating=len(asked),
                    failed=sum(_gave_up(d) for d in asked),
                )
            )
    return result


@router.post("/edge-releases", response_model=ReleaseOut, status_code=status.HTTP_201_CREATED)
def add_release(body: ReleaseIn, principal: PlatformAdmin) -> ReleaseOut:
    """Adds a release. Nothing changes on any box until it is rolled out."""
    try:
        with tenant_session(None, principal.user_id) as db:
            release = EdgeRelease(
                version=body.version,
                image=body.image,
                notes=body.notes,
                created_by=principal.user_id,
            )
            db.add(release)
            db.flush()
            db.refresh(release)
    except IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, "this version already exists") from None
    log.info("edge release %s added by %s: %s", body.version, principal.user_id, body.image)
    return ReleaseOut(
        version=release.version,
        image=release.image,
        notes=release.notes,
        created_at=release.created_at,
        withdrawn_at=None,
        running=0,
        updating=0,
        failed=0,
    )


@router.post("/edge-releases/{version}/withdraw", status_code=status.HTTP_204_NO_CONTENT)
def withdraw_release(version: str, principal: PlatformAdmin) -> None:
    """Stops offering it: boxes not on it yet stay where they are. Boxes already on it
    keep it; roll out another release to move them."""
    with tenant_session(None, principal.user_id) as db:
        release = db.scalar(select(EdgeRelease).where(EdgeRelease.version == version))
        if release is None:
            raise not_found("release not found")
        release.withdrawn_at = release.withdrawn_at or datetime.now(timezone.utc)
    log.warning("edge release %s withdrawn by %s", version, principal.user_id)


@router.post("/edge-releases/{version}/rollout", response_model=RolloutOut)
def rollout(
    version: str, body: RolloutIn, principal: PlatformAdmin, request: Request
) -> RolloutOut:
    """Sets the boxes' target version (every client, one client or one box). Each box
    switches with its next heartbeat and rolls back by itself if the new version isn't
    healthy. Rolling out again retries boxes whose update failed. Start with one box."""
    from cctv_cloud.worker import tenant_ids

    with tenant_session(None, principal.user_id) as db:
        release = db.scalar(select(EdgeRelease).where(EdgeRelease.version == version))
    if release is None:
        raise not_found("release not found")
    if release.withdrawn_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "this release was withdrawn")
    tenants = tenant_ids()
    if body.tenant_id is not None:
        if body.tenant_id not in tenants:
            raise not_found("client not found")
        tenants = [body.tenant_id]
    # Also identifies this request on the box: rolling out again makes a new one (a retry).
    now = datetime.now(timezone.utc)
    total = 0
    for tenant_id in tenants:
        with tenant_session(tenant_id, principal.user_id) as db:
            query = (
                update(EdgeDevice)
                .where(EdgeDevice.status != DeviceStatus.RETIRED.value)
                .values(target_version=version, target_set_at=now)
            )
            if body.device_id is not None:
                query = query.where(EdgeDevice.id == body.device_id)
            count = db.execute(query).rowcount
            if count:
                details = {"devices": count}
                if body.device_id is not None:
                    details["device_id"] = str(body.device_id)
                record(
                    db,
                    principal,
                    AuditAction.SOFTWARE_UPDATE,
                    "edge_release",
                    version,
                    request,
                    details,
                    tenant_id=tenant_id,
                )
            total += count
    if body.device_id is not None and total == 0:
        raise not_found("edge device not found")
    log.info("edge release %s rolled out to %d boxes by %s", version, total, principal.user_id)
    return RolloutOut(devices=total)


@router.get("/edge-devices", response_model=list[PlatformDeviceOut])
def list_platform_devices(
    principal: PlatformAdmin, tenant_id: uuid.UUID | None = None
) -> list[PlatformDeviceOut]:
    """Every client's boxes (not retired) with their software version and update state."""
    return [
        PlatformDeviceOut(
            tenant_id=d.tenant_id,
            tenant=tenant,
            id=d.id,
            name=d.name,
            branch=branch,
            status=DeviceStatus(d.status),
            last_seen_at=d.last_seen_at,
            software_version=d.software_version,
            target_version=d.target_version,
            update_version=d.update_version,
            update_state=UpdateState(d.update_state) if d.update_state else None,
            update_detail=d.update_detail,
            update_reported_at=d.update_reported_at,
        )
        for tenant, branch, d in _all_devices(principal, tenant_id)
    ]
