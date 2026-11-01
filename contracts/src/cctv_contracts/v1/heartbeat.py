from uuid import UUID

from pydantic import Field

from cctv_contracts.enums import CameraHealth, UpdateState
from cctv_contracts.v1.common import ContractModel, UtcDatetime


class CameraStatus(ContractModel):
    camera_id: UUID
    health: CameraHealth
    fps: float = Field(ge=0)
    last_frame_at: UtcDatetime | None = None
    error: str | None = Field(default=None, max_length=500)
    stream_restarts: int = Field(
        default=0,
        ge=0,
        description="Times the stream dropped and was reopened since the service started "
        "(counts up; back to 0 after a restart of the service)",
    )


class SystemStats(ContractModel):
    cpu_percent: float = Field(ge=0, le=100)
    gpu_percent: float | None = Field(default=None, ge=0, le=100)
    temperature_c: float | None = None
    disk_free_bytes: int = Field(ge=0)
    outbox_pending: int = Field(ge=0, description="Events/media waiting to be synced")


# Release versions ("1.4.0", "1.4.0-rc.1") and edge images pinned by digest (plan 2.10).
SOFTWARE_VERSION_PATTERN = r"^\d{1,4}\.\d{1,4}\.\d{1,4}(-[0-9A-Za-z.]{1,30})?$"
IMAGE_REF_PATTERN = r"^[a-z0-9][a-z0-9._/:-]{0,250}@sha256:[0-9a-f]{64}$"


class UpdateReport(ContractModel):
    """What the box's updater last did (plan 2.10). Sent with every heartbeat once there
    has been an update."""

    version: str = Field(max_length=50)
    requested_at: UtcDatetime = Field(description="SoftwareUpdate.requested_at it answers")
    state: UpdateState
    detail: str | None = Field(default=None, max_length=500)
    at: UtcDatetime


class SoftwareUpdate(ContractModel):
    """A release the box should switch to (plan 2.10). The box's updater pulls the image by
    digest, checks its signature, and switches back if the new version isn't healthy."""

    version: str = Field(pattern=SOFTWARE_VERSION_PATTERN, max_length=50)
    image: str = Field(
        pattern=IMAGE_REF_PATTERN,
        max_length=330,
        description="repository@sha256:digest, e.g. ghcr.io/org/cctv-edge@sha256:...",
    )
    requested_at: UtcDatetime = Field(
        description="When the update was asked for; asking again for the same version "
        "(a retry after a failure) gives a new time"
    )


class Heartbeat(ContractModel):
    sent_at: UtcDatetime
    software_version: str = Field(max_length=50)
    config_version_applied: int = Field(ge=0)
    uptime_seconds: int = Field(ge=0)
    cameras: list[CameraStatus]
    system: SystemStats
    update: UpdateReport | None = None


class HeartbeatResponse(ContractModel):
    # The edge compares server_time with its own clock to detect drift.
    server_time: UtcDatetime
    latest_config_version: int = Field(ge=0)
    rotate_token: bool = Field(
        default=False, description="The device token is due for rotation: POST /token"
    )
    update: SoftwareUpdate | None = Field(
        default=None, description="Switch to this release; none: stay on the current one"
    )
