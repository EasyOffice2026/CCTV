"""Onboarding checklist for a branch (plan 11.1): survey → camera check → connect → zones
→ go live. Each step is worked out from what is already in the system, so the admin
panel's wizard always shows where a branch stands; nothing is ticked by hand.
docs/runbooks/onboarding.md is the same list with the work behind each step.
"""

from datetime import datetime, timedelta, timezone

from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from cctv_cloud.db.enums import DeviceStatus, StaffStatus
from cctv_cloud.db.models import (
    AlertSubscription,
    Branch,
    Camera,
    EdgeDevice,
    FaceTemplate,
    Staff,
    WorkingHours,
    Zone,
)
from cctv_cloud.services.edge_config import resolve_features
from cctv_cloud.services.pilot import first_heartbeat, stream_test
from cctv_contracts.enums import CameraHealth, FeatureKey, ZoneType

STEPS = [
    "branch",
    "cameras",
    "edge_box",
    "streaming",
    "setup_test",
    "zones",
    "features",
    "staff",
    "alerts",
    "shadow_mode",
    "go_live",
]


class Step(BaseModel):
    key: str
    done: bool | None  # None: doesn't apply to this branch (e.g. no attendance)
    done_count: int | None = None  # e.g. cameras with zones
    total: int | None = None


def onboarding(db: Session, branch: Branch, now: datetime | None = None) -> list[Step]:
    now = now or datetime.now(timezone.utc)
    cameras = db.scalars(select(Camera).where(Camera.branch_id == branch.id, Camera.enabled)).all()
    camera_ids = [c.id for c in cameras]
    devices = db.scalars(
        select(EdgeDevice).where(
            EdgeDevice.branch_id == branch.id, EdgeDevice.status != DeviceStatus.RETIRED.value
        )
    ).all()
    hours = db.scalar(select(func.count()).where(WorkingHours.branch_id == branch.id)) or 0
    steps = [Step(key="branch", done=hours > 0)]
    steps.append(Step(key="cameras", done=bool(cameras), done_count=len(cameras)))

    online = [d for d in devices if d.status == DeviceStatus.ONLINE.value]
    steps.append(
        Step(key="edge_box", done=bool(online), done_count=len(online), total=len(devices))
    )
    streaming = sum(c.health != CameraHealth.OFFLINE.value for c in cameras)
    steps.append(
        Step(
            key="streaming",
            done=bool(cameras) and streaming == len(cameras),
            done_count=streaming,
            total=len(cameras),
        )
    )

    # The 48-hour setup test from the first heartbeat (plan 9.2).
    first = first_heartbeat(db, branch, now - timedelta(days=60))
    passed = None
    if first is not None:
        passed = stream_test(db, branch, first, first + timedelta(hours=48), now).passed
    steps.append(Step(key="setup_test", done=passed is True))

    zoned = set(
        db.scalars(
            select(Zone.camera_id).where(
                Zone.camera_id.in_(camera_ids), Zone.zone_type != ZoneType.IGNORE.value
            )
        )
    )
    steps.append(
        Step(
            key="zones",
            done=bool(cameras) and len(zoned) == len(cameras),
            done_count=len(zoned),
            total=len(cameras),
        )
    )
    resolved = resolve_features(db, branch.id, camera_ids)
    with_features = [cid for cid, f in resolved.items() if any(x.enabled for x in f.values())]
    steps.append(
        Step(
            key="features",
            done=bool(cameras) and len(with_features) == len(cameras),
            done_count=len(with_features),
            total=len(cameras),
        )
    )

    attendance = any(
        f[FeatureKey.ATTENDANCE.value].enabled
        for f in resolved.values()
        if FeatureKey.ATTENDANCE.value in f
    )
    if attendance:
        consented = db.scalars(
            select(Staff.id).where(
                Staff.branch_id == branch.id,
                Staff.status == StaffStatus.ACTIVE.value,
                Staff.consent_signed_at.is_not(None),
            )
        ).all()
        enrolled = (
            db.scalar(
                select(func.count(func.distinct(FaceTemplate.staff_id))).where(
                    FaceTemplate.staff_id.in_(consented)
                )
            )
            or 0
        )
        steps.append(
            Step(
                key="staff",
                done=bool(consented) and enrolled == len(consented),
                done_count=enrolled,
                total=len(consented),
            )
        )
    else:
        steps.append(Step(key="staff", done=None))

    rules = db.scalar(
        select(func.count()).where(
            AlertSubscription.enabled,
            or_(AlertSubscription.branch_id.is_(None), AlertSubscription.branch_id == branch.id),
        )
    )
    steps.append(Step(key="alerts", done=bool(rules), done_count=rules))
    steps.append(Step(key="shadow_mode", done=branch.shadow_until is not None))
    steps.append(Step(key="go_live", done=branch.pilot_started_on is not None))
    return steps
