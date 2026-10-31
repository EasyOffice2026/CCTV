"""Feature on/off and thresholds per tenant, branch or camera (plan 3.4).

Tenant-wide settings need tenant-admin rights; branch and camera settings need settings
rights on that branch. Every change bumps the affected branches' config version and
notifies their edge devices.
"""

import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, JsonValue, model_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from cctv_cloud.api.admin import ORMModel, accessible_branch, notify_config
from cctv_cloud.api.deps import Context, PublisherDep, context, forbidden, not_found
from cctv_cloud.api.devices import accessible_camera
from cctv_cloud.db.enums import FeatureScope
from cctv_cloud.db.models import Camera, Feature, FeatureSetting
from cctv_cloud.rbac import Permission
from cctv_cloud.services.audit import AuditAction, record
from cctv_cloud.services.edge_config import bump_config_version, resolve_features

router = APIRouter(prefix="/v1", tags=["features"])

AnyMember = Annotated[Context, Depends(context(), scope="function")]
SettingsAdmin = Annotated[Context, Depends(context(Permission.SETTINGS_MANAGE), scope="function")]


class FeatureOut(ORMModel):
    key: str
    name: str
    category: str
    availability: str
    default_enabled: bool


class FeatureSettingIn(BaseModel):
    scope: FeatureScope
    branch_id: uuid.UUID | None = None
    camera_id: uuid.UUID | None = None
    feature_key: str
    enabled: bool
    params: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_target(self) -> "FeatureSettingIn":
        expected = {
            FeatureScope.TENANT: (False, False),
            FeatureScope.BRANCH: (True, False),
            FeatureScope.CAMERA: (False, True),
        }[self.scope]
        if (self.branch_id is not None, self.camera_id is not None) != expected:
            raise ValueError(
                "tenant scope takes no ids, branch scope a branch_id, camera scope a camera_id"
            )
        return self


class FeatureSettingOut(ORMModel):
    id: uuid.UUID
    scope: FeatureScope
    branch_id: uuid.UUID | None
    camera_id: uuid.UUID | None
    feature_key: str
    enabled: bool
    params: dict[str, Any]
    updated_at: datetime


class ResolvedFeatureOut(BaseModel):
    key: str
    enabled: bool
    source: str = Field(description="Where the value comes from: camera, branch, tenant, default")
    params: dict[str, Any]


@router.get("/features", response_model=list[FeatureOut])
def list_features(ctx: AnyMember) -> list[Feature]:
    return list(ctx.db.scalars(select(Feature).order_by(Feature.category, Feature.key)))


def _affected_branches(ctx: Context, body: FeatureSettingIn) -> list[uuid.UUID] | None:
    """Checks the caller may change this scope; returns the branches it affects
    (None = all of the tenant's)."""
    if body.scope is FeatureScope.TENANT:
        if not ctx.principal.can(Permission.TENANT_ADMIN):
            raise forbidden("tenant-wide settings need the owner role")
        return None
    if body.scope is FeatureScope.BRANCH:
        assert body.branch_id is not None  # noqa: S101 - validated
        return [accessible_branch(ctx, body.branch_id).id]
    assert body.camera_id is not None  # noqa: S101
    return [accessible_camera(ctx, body.camera_id).branch_id]


@router.get("/feature-settings", response_model=list[FeatureSettingOut])
def list_feature_settings(
    ctx: SettingsAdmin, branch_id: uuid.UUID | None = None, camera_id: uuid.UUID | None = None
) -> list[FeatureSetting]:
    query = select(FeatureSetting).order_by(FeatureSetting.feature_key, FeatureSetting.scope)
    if branch_id is not None:
        accessible_branch(ctx, branch_id)
        query = query.where(FeatureSetting.branch_id == branch_id)
    if camera_id is not None:
        accessible_camera(ctx, camera_id)
        query = query.where(FeatureSetting.camera_id == camera_id)
    rows = ctx.db.scalars(query).all()
    allowed = ctx.principal.branch_ids
    if allowed is None:
        return list(rows)
    # Camera-scope rows have no branch_id: check the camera's branch instead.
    camera_branch = dict(
        ctx.db.execute(
            select(Camera.id, Camera.branch_id).where(
                Camera.id.in_([r.camera_id for r in rows if r.camera_id is not None])
            )
        ).all()
    )
    return [
        r
        for r in rows
        if r.scope == FeatureScope.TENANT.value
        or r.branch_id in allowed
        or camera_branch.get(r.camera_id) in allowed
    ]


@router.put("/feature-settings", response_model=FeatureSettingOut)
def upsert_feature_setting(
    body: FeatureSettingIn,
    ctx: SettingsAdmin,
    request: Request,
    background: BackgroundTasks,
    publisher: PublisherDep,
) -> FeatureSetting:
    if ctx.db.get(Feature, body.feature_key) is None:
        raise HTTPException(422, f"unknown feature {body.feature_key!r}")
    branches = _affected_branches(ctx, body)
    values = {
        "tenant_id": ctx.principal.tenant_id,
        **body.model_dump(mode="json"),
        "updated_at": datetime.now(timezone.utc),
        "updated_by": ctx.principal.user_id,
    }
    setting_id = ctx.db.scalar(
        insert(FeatureSetting)
        .values(**values)
        .on_conflict_do_update(
            constraint="uq_feature_settings_tenant_id_feature_key_branch_id_camera_id",
            set_={k: values[k] for k in ("enabled", "params", "updated_at", "updated_by")},
        )
        .returning(FeatureSetting.id)
    )
    record(
        ctx.db,
        ctx.principal,
        AuditAction.SETTINGS_CHANGE,
        "feature_setting",
        setting_id,
        request,
        body.model_dump(mode="json"),
    )
    notify_config(background, publisher, bump_config_version(ctx.db, branches))
    return ctx.db.get_one(FeatureSetting, setting_id, populate_existing=True)


@router.delete("/feature-settings/{setting_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_feature_setting(
    setting_id: uuid.UUID,
    ctx: SettingsAdmin,
    request: Request,
    background: BackgroundTasks,
    publisher: PublisherDep,
) -> None:
    """Back to inheriting from the level above."""
    setting = ctx.db.get(FeatureSetting, setting_id)
    if setting is None:
        raise not_found("setting not found")
    branches = _affected_branches(
        ctx,
        FeatureSettingIn(
            scope=FeatureScope(setting.scope),
            branch_id=setting.branch_id,
            camera_id=setting.camera_id,
            feature_key=setting.feature_key,
            enabled=setting.enabled,
        ),
    )
    ctx.db.delete(setting)
    record(
        ctx.db,
        ctx.principal,
        AuditAction.SETTINGS_CHANGE,
        "feature_setting",
        setting_id,
        request,
        {"deleted": True},
    )
    notify_config(background, publisher, bump_config_version(ctx.db, branches))


@router.get("/cameras/{camera_id}/features", response_model=list[ResolvedFeatureOut])
def camera_features(camera_id: uuid.UUID, ctx: AnyMember) -> list[ResolvedFeatureOut]:
    """What actually runs on this camera, and which level decided it."""
    camera = accessible_camera(ctx, camera_id)
    resolved = resolve_features(ctx.db, camera.branch_id, [camera.id])[camera.id]
    return [ResolvedFeatureOut(**vars(f)) for f in sorted(resolved.values(), key=lambda f: f.key)]
