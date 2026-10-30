from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from cctv_contracts.enums import MediaKind
from cctv_contracts.v1.common import ContractModel, MediaKey, UtcDatetime

MAX_SNAPSHOT_BYTES = 5 * 1024 * 1024
MAX_CLIP_BYTES = 100 * 1024 * 1024

_CONTENT_TYPES = {MediaKind.SNAPSHOT: "image/jpeg", MediaKind.CLIP: "video/mp4"}
_MAX_BYTES = {MediaKind.SNAPSHOT: MAX_SNAPSHOT_BYTES, MediaKind.CLIP: MAX_CLIP_BYTES}


class UploadUrlRequest(ContractModel):
    """Ask the cloud where to upload a snapshot or clip for an event."""

    event_id: UUID
    kind: MediaKind
    content_type: Literal["image/jpeg", "video/mp4"]
    size_bytes: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _check_kind(self) -> "UploadUrlRequest":
        if self.content_type != _CONTENT_TYPES[self.kind]:
            raise ValueError(f"{self.kind.value} must be {_CONTENT_TYPES[self.kind]}")
        if self.size_bytes > _MAX_BYTES[self.kind]:
            raise ValueError(f"{self.kind.value} larger than {_MAX_BYTES[self.kind]} bytes")
        return self


class UploadUrlResponse(ContractModel):
    """Pre-signed upload target. The edge PUTs the file, then sends `media_key` in the event."""

    media_key: MediaKey
    upload_url: str
    method: Literal["PUT"] = "PUT"
    headers: dict[str, str] = Field(default_factory=dict)
    expires_at: UtcDatetime
