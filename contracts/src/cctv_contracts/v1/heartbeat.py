from uuid import UUID

from pydantic import Field

from cctv_contracts.enums import CameraHealth
from cctv_contracts.v1.common import ContractModel, UtcDatetime


class CameraStatus(ContractModel):
    camera_id: UUID
    health: CameraHealth
    fps: float = Field(ge=0)
    last_frame_at: UtcDatetime | None = None
    error: str | None = Field(default=None, max_length=500)


class SystemStats(ContractModel):
    cpu_percent: float = Field(ge=0, le=100)
    gpu_percent: float | None = Field(default=None, ge=0, le=100)
    temperature_c: float | None = None
    disk_free_bytes: int = Field(ge=0)
    outbox_pending: int = Field(ge=0, description="Events/media waiting to be synced")


class Heartbeat(ContractModel):
    sent_at: UtcDatetime
    software_version: str = Field(max_length=50)
    config_version_applied: int = Field(ge=0)
    uptime_seconds: int = Field(ge=0)
    cameras: list[CameraStatus]
    system: SystemStats


class HeartbeatResponse(ContractModel):
    # The edge compares server_time with its own clock to detect drift.
    server_time: UtcDatetime
    latest_config_version: int = Field(ge=0)
