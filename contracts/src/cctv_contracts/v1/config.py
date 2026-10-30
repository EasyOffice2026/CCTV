from datetime import time
from typing import Annotated
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, JsonValue, field_validator, model_validator

from cctv_contracts.enums import LINE_ZONE_TYPES, CameraRole, FeatureKey, ZoneType
from cctv_contracts.v1.common import ContractModel, UtcDatetime

# Zone coordinates are normalised to the frame (0..1) so they survive resolution changes.
Coordinate = Annotated[float, Field(ge=0, le=1)]
Point = tuple[Coordinate, Coordinate]


class ZoneConfig(ContractModel):
    id: UUID
    name: str = Field(max_length=100)
    type: ZoneType
    points: list[Point] = Field(min_length=2, max_length=64)

    @model_validator(mode="after")
    def _check_shape(self) -> "ZoneConfig":
        if self.type in LINE_ZONE_TYPES:
            if len(self.points) != 2:
                raise ValueError(f"{self.type.value} zones are lines and need exactly 2 points")
        elif len(self.points) < 3:
            raise ValueError(f"{self.type.value} zones are polygons and need at least 3 points")
        return self


class FeatureConfig(ContractModel):
    key: FeatureKey
    params: dict[str, JsonValue] = Field(default_factory=dict)


class CameraConfig(ContractModel):
    """A camera and the features that are *enabled* for it.

    Feature on/off is already resolved by the cloud (camera > branch > tenant > default),
    so the edge runs exactly the features listed and nothing else.
    """

    id: UUID
    name: str = Field(max_length=100)
    role: CameraRole
    rtsp_main: str
    rtsp_sub: str | None = None
    # Name of a credential stored on the edge device; passwords never travel in config.
    credentials_ref: str | None = Field(default=None, max_length=100)
    features: list[FeatureConfig] = Field(default_factory=list)
    zones: list[ZoneConfig] = Field(default_factory=list)

    @field_validator("rtsp_main", "rtsp_sub")
    @classmethod
    def _check_rtsp(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not value.startswith(("rtsp://", "rtsps://")):
            raise ValueError("must be an rtsp:// or rtsps:// URL")
        if "@" in value:
            raise ValueError("must not contain credentials; use credentials_ref")
        return value

    @model_validator(mode="after")
    def _check_unique(self) -> "CameraConfig":
        if len({f.key for f in self.features}) != len(self.features):
            raise ValueError("duplicate feature key")
        if len({z.id for z in self.zones}) != len(self.zones):
            raise ValueError("duplicate zone id")
        return self


class WorkingHours(ContractModel):
    weekday: int = Field(ge=0, le=6, description="0 = Monday")
    opens_at: time
    closes_at: time = Field(description="If not after opens_at, the branch closes the next day")


class ClipSettings(ContractModel):
    pre_seconds: int = Field(default=10, ge=0, le=30)
    post_seconds: int = Field(default=10, ge=1, le=60)


class EdgeConfig(ContractModel):
    version: int = Field(ge=1)
    generated_at: UtcDatetime
    tenant_id: UUID
    branch_id: UUID
    timezone: str = "Asia/Kuwait"
    working_hours: list[WorkingHours] = Field(default_factory=list)
    clip: ClipSettings = Field(default_factory=ClipSettings)
    cameras: list[CameraConfig] = Field(default_factory=list)

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown time zone {value!r}") from exc
        return value

    @model_validator(mode="after")
    def _check_unique_cameras(self) -> "EdgeConfig":
        if len({c.id for c in self.cameras}) != len(self.cameras):
            raise ValueError("duplicate camera id")
        return self
