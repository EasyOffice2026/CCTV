"""Requests from the cloud to an edge box, for what only the box can reach on the branch
LAN: live view and NVR playback (WebRTC through go2rtc on the box), the NVR's list of
recordings, a current camera frame, and face embeddings for staff enrolment.

The box long-polls `GET /remote`; the cloud answers with one `RemoteRequest`, or 204
when nothing arrived within `wait` seconds. The box answers each request once with
`POST /remote/{id}/reply`. A request the box picks up after `expires_at` is dropped: the
user has stopped waiting.

Video never passes through the cloud: the WebRTC answer lets the browser connect to the
box directly (or through a TURN relay).
"""

from datetime import timedelta
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator

from cctv_contracts.enums import RemoteKind
from cctv_contracts.v1.common import ContractModel, UtcDatetime

MAX_SDP_LENGTH = 32_000
MAX_PLAYBACK = timedelta(hours=2)
MAX_RECORDINGS_RANGE = timedelta(days=1)
# Base64 of a 5 MB image.
MAX_IMAGE_BASE64 = 7_000_000

Sdp = Annotated[str, Field(min_length=1, max_length=MAX_SDP_LENGTH)]


class _TimeRange(ContractModel):
    start: UtcDatetime
    end: UtcDatetime

    @model_validator(mode="after")
    def _check_order(self) -> "_TimeRange":
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self


class LiveRequest(ContractModel):
    """Start WebRTC from the camera's sub-stream (main stream if it has none)."""

    kind: Literal[RemoteKind.LIVE] = RemoteKind.LIVE
    camera_id: UUID
    sdp_offer: Sdp


class PlaybackRequest(_TimeRange):
    """Start WebRTC from the NVR's recording of the camera, from `start` to `end`."""

    kind: Literal[RemoteKind.PLAYBACK] = RemoteKind.PLAYBACK
    camera_id: UUID
    sdp_offer: Sdp

    @model_validator(mode="after")
    def _check_length(self) -> "PlaybackRequest":
        if self.end - self.start > MAX_PLAYBACK:
            raise ValueError(f"playback is limited to {MAX_PLAYBACK} per request")
        return self


class RecordingsRequest(_TimeRange):
    """List what the NVR has recorded for the camera between `start` and `end`."""

    kind: Literal[RemoteKind.RECORDINGS] = RemoteKind.RECORDINGS
    camera_id: UUID

    @model_validator(mode="after")
    def _check_length(self) -> "RecordingsRequest":
        if self.end - self.start > MAX_RECORDINGS_RANGE:
            raise ValueError(f"the range is limited to {MAX_RECORDINGS_RANGE}")
        return self


class FrameRequest(ContractModel):
    """One current JPEG frame, e.g. as the background for drawing zones."""

    kind: Literal[RemoteKind.FRAME] = RemoteKind.FRAME
    camera_id: UUID


ImageBase64 = Annotated[str, Field(min_length=1, max_length=MAX_IMAGE_BASE64)]


class FaceEmbeddingRequest(ContractModel):
    """Embed enrolment photos with the box's face model. The box must not keep the photos;
    the cloud stores only the (encrypted) embedding."""

    kind: Literal[RemoteKind.FACE_EMBEDDING] = RemoteKind.FACE_EMBEDDING
    staff_id: UUID
    images_base64: list[ImageBase64] = Field(min_length=1, max_length=5)


RemotePayload = Annotated[
    LiveRequest | PlaybackRequest | RecordingsRequest | FrameRequest | FaceEmbeddingRequest,
    Field(discriminator="kind"),
]


class RemoteRequest(ContractModel):
    id: UUID
    expires_at: UtcDatetime
    request: RemotePayload


class RecordingSpan(_TimeRange):
    pass


class RemoteReply(ContractModel):
    """`ok=False` with `error` when the box can't do it (camera offline, NVR unreachable,
    no face model yet). Otherwise the field for the request's kind is set."""

    ok: bool
    error: str | None = Field(default=None, max_length=500)
    sdp_answer: Sdp | None = None  # live, playback
    recordings: list[RecordingSpan] | None = Field(default=None, max_length=2000)
    jpeg_base64: ImageBase64 | None = None  # frame
    embedding: list[float] | None = Field(default=None, min_length=64, max_length=4096)
    model_version: str | None = Field(default=None, max_length=50)  # face_embedding
