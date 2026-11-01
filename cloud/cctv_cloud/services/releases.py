"""Edge software updates (plan 2.10): what each box is told with its heartbeat.

Our team adds a release (version + image pinned by digest) and rolls it out to boxes,
which sets their target version. A box not on its target is offered the release with every
heartbeat until it reports back. The box's updater checks the image's signature, switches,
and rolls back by itself if the new version isn't healthy; a failed or rolled-back update
isn't offered again until it's rolled out again (a new requested_at).
"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from cctv_cloud.db.models import EdgeDevice, EdgeRelease
from cctv_contracts.enums import UpdateState
from cctv_contracts.v1 import SoftwareUpdate, UpdateReport

GAVE_UP = (UpdateState.FAILED.value, UpdateState.ROLLED_BACK.value)


def record_report(device: EdgeDevice, report: UpdateReport | None, now: datetime) -> None:
    if report is None:
        return
    device.update_version = report.version
    device.update_state = report.state.value
    device.update_detail = report.detail
    device.update_requested_at = report.requested_at
    device.update_reported_at = now


def offer(db: Session, device: EdgeDevice, running_version: str) -> SoftwareUpdate | None:
    target, requested_at = device.target_version, device.target_set_at
    if target is None or requested_at is None or target == running_version:
        return None
    if (
        device.update_version == target
        and device.update_requested_at == requested_at
        and device.update_state in GAVE_UP
    ):
        return None
    release = db.scalar(
        select(EdgeRelease).where(EdgeRelease.version == target, EdgeRelease.withdrawn_at.is_(None))
    )
    if release is None:
        return None
    return SoftwareUpdate(version=release.version, image=release.image, requested_at=requested_at)
