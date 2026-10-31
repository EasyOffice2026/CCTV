"""Feature on/off resolution and the config each edge device pulls (`EdgeConfig`).

Resolution per camera and feature (plan §3): camera setting, otherwise branch, otherwise
tenant, otherwise the catalogue default. `params` merge in the same order, so a threshold
set for the whole tenant can be overridden for one camera.
"""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from cctv_cloud.db.enums import FeatureScope
from cctv_cloud.db.models import (
    Branch,
    Camera,
    EdgeDevice,
    Feature,
    FeatureSetting,
    Nvr,
    WorkingHours,
    Zone,
)
from cctv_contracts.enums import CameraRole, FeatureKey, ZoneType
from cctv_contracts.v1 import (
    CameraConfig,
    EdgeConfig,
    FeatureConfig,
    NvrRef,
    ZoneConfig,
)
from cctv_contracts.v1 import WorkingHours as WorkingHoursConfig


@dataclass
class ResolvedFeature:
    key: str
    enabled: bool
    source: str  # camera | branch | tenant | default
    params: dict[str, Any] = field(default_factory=dict)


def resolve_features(
    session: Session, branch_id: uuid.UUID, camera_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, dict[str, ResolvedFeature]]:
    camera_ids = list(camera_ids)
    defaults = dict(session.execute(select(Feature.key, Feature.default_enabled)).all())
    settings = session.scalars(
        select(FeatureSetting).where(
            or_(
                FeatureSetting.scope == FeatureScope.TENANT.value,
                FeatureSetting.branch_id == branch_id,
                FeatureSetting.camera_id.in_(camera_ids),
            )
        )
    ).all()
    tenant_level = {s.feature_key: s for s in settings if s.scope == FeatureScope.TENANT.value}
    branch_level = {s.feature_key: s for s in settings if s.scope == FeatureScope.BRANCH.value}
    camera_level = {
        (s.camera_id, s.feature_key): s for s in settings if s.scope == FeatureScope.CAMERA.value
    }

    result: dict[uuid.UUID, dict[str, ResolvedFeature]] = {}
    for camera_id in camera_ids:
        per_camera = {}
        for key, default in defaults.items():
            resolved = ResolvedFeature(key, default, "default")
            for source, setting in (
                ("tenant", tenant_level.get(key)),
                ("branch", branch_level.get(key)),
                ("camera", camera_level.get((camera_id, key))),
            ):
                if setting is not None:
                    resolved.enabled = setting.enabled
                    resolved.source = source
                    resolved.params = {**resolved.params, **setting.params}
            per_camera[key] = resolved
        result[camera_id] = per_camera
    return result


def build_edge_config(session: Session, device: EdgeDevice) -> EdgeConfig:
    branch = session.get_one(Branch, device.branch_id)
    hours = session.scalars(
        select(WorkingHours)
        .where(WorkingHours.branch_id == branch.id)
        .order_by(WorkingHours.weekday, WorkingHours.opens_at)
    ).all()
    cameras = session.scalars(
        select(Camera)
        .where(Camera.branch_id == branch.id, Camera.enabled.is_(True))
        .order_by(Camera.name)
    ).all()
    camera_ids = [c.id for c in cameras]
    nvrs = {n.id: n for n in session.scalars(select(Nvr).where(Nvr.branch_id == branch.id))}
    zones: dict[uuid.UUID, list[Zone]] = {}
    for zone in session.scalars(
        select(Zone).where(Zone.camera_id.in_(camera_ids)).order_by(Zone.name)
    ):
        zones.setdefault(zone.camera_id, []).append(zone)
    features = resolve_features(session, branch.id, camera_ids)

    return EdgeConfig(
        version=branch.config_version,
        generated_at=datetime.now(timezone.utc),
        tenant_id=branch.tenant_id,
        branch_id=branch.id,
        timezone=branch.timezone,
        working_hours=[
            WorkingHoursConfig(weekday=h.weekday, opens_at=h.opens_at, closes_at=h.closes_at)
            for h in hours
        ],
        cameras=[
            CameraConfig(
                id=camera.id,
                name=camera.name,
                role=CameraRole(camera.role),
                rtsp_main=camera.rtsp_main,
                rtsp_sub=camera.rtsp_sub,
                credentials_ref=camera.credentials_ref
                or (nvrs[camera.nvr_id].credentials_ref if camera.nvr_id else None),
                features=[
                    FeatureConfig(key=FeatureKey(f.key), params=f.params)
                    for f in features[camera.id].values()
                    if f.enabled and f.key in FeatureKey._value2member_map_
                ],
                zones=[
                    ZoneConfig(id=z.id, name=z.name, type=ZoneType(z.zone_type), points=z.points)
                    for z in zones.get(camera.id, [])
                ],
                nvr=_nvr_ref(nvrs.get(camera.nvr_id), camera.channel),
            )
            for camera in cameras
        ],
    )


def _nvr_ref(nvr: Nvr | None, channel: int | None) -> NvrRef | None:
    if nvr is None or channel is None:
        return None
    return NvrRef(
        vendor=nvr.vendor,
        host=nvr.host,
        http_port=nvr.http_port,
        rtsp_port=nvr.rtsp_port,
        channel=channel,
        credentials_ref=nvr.credentials_ref,
    )


def bump_config_version(
    session: Session, branch_ids: Iterable[uuid.UUID] | None = None
) -> list[uuid.UUID]:
    """Mark the edge config of these branches (default: all of the tenant's) as changed.
    Returns the affected branch ids, to notify after commit."""
    statement = update(Branch).values(config_version=Branch.config_version + 1)
    if branch_ids is not None:
        statement = statement.where(Branch.id.in_(list(branch_ids)))
    return list(session.scalars(statement.returning(Branch.id)))
