"""Edge <-> cloud API, version 1.

Endpoints (all under API_PREFIX, authenticated with the device token except /pair):

    POST {API_PREFIX}/pair                PairRequest         -> PairResponse
    POST {API_PREFIX}/events              EventBatch          -> EventBatchResponse
    POST {API_PREFIX}/media/upload-urls   UploadUrlRequest    -> UploadUrlResponse
    POST {API_PREFIX}/heartbeat           Heartbeat           -> HeartbeatResponse
    GET  {API_PREFIX}/config              (If-None-Match: version) -> EdgeConfig | 304
    GET  {API_PREFIX}/remote?wait=25      (long poll)         -> RemoteRequest | 204
    POST {API_PREFIX}/remote/{id}/reply   RemoteReply         -> 204
"""

from cctv_contracts.v1.common import ContractModel, MediaKey, UtcDatetime
from cctv_contracts.v1.config import (
    CameraConfig,
    ClipSettings,
    EdgeConfig,
    FeatureConfig,
    NvrRef,
    WorkingHours,
    ZoneConfig,
)
from cctv_contracts.v1.events import (
    MAX_EVENTS_PER_BATCH,
    EventBatch,
    EventBatchResponse,
    EventIn,
    EventResult,
)
from cctv_contracts.v1.heartbeat import CameraStatus, Heartbeat, HeartbeatResponse, SystemStats
from cctv_contracts.v1.media import UploadUrlRequest, UploadUrlResponse
from cctv_contracts.v1.pairing import PairRequest, PairResponse
from cctv_contracts.v1.remote import (
    FaceEmbeddingRequest,
    FrameRequest,
    LiveRequest,
    PlaybackRequest,
    RecordingSpan,
    RecordingsRequest,
    RemoteReply,
    RemoteRequest,
)

API_PREFIX = "/v1/edge"

# Top-level request/response bodies; exported as JSON Schema for non-Python clients.
SCHEMA_MODELS: dict[str, type[ContractModel]] = {
    "event_batch": EventBatch,
    "event_batch_response": EventBatchResponse,
    "upload_url_request": UploadUrlRequest,
    "upload_url_response": UploadUrlResponse,
    "heartbeat": Heartbeat,
    "heartbeat_response": HeartbeatResponse,
    "edge_config": EdgeConfig,
    "pair_request": PairRequest,
    "pair_response": PairResponse,
    "remote_request": RemoteRequest,
    "remote_reply": RemoteReply,
}

__all__ = [
    "API_PREFIX",
    "MAX_EVENTS_PER_BATCH",
    "SCHEMA_MODELS",
    "CameraConfig",
    "CameraStatus",
    "ClipSettings",
    "ContractModel",
    "EdgeConfig",
    "EventBatch",
    "EventBatchResponse",
    "EventIn",
    "EventResult",
    "FaceEmbeddingRequest",
    "FeatureConfig",
    "FrameRequest",
    "Heartbeat",
    "HeartbeatResponse",
    "LiveRequest",
    "MediaKey",
    "NvrRef",
    "PairRequest",
    "PairResponse",
    "PlaybackRequest",
    "RecordingSpan",
    "RecordingsRequest",
    "RemoteReply",
    "RemoteRequest",
    "SystemStats",
    "UploadUrlRequest",
    "UploadUrlResponse",
    "UtcDatetime",
    "WorkingHours",
    "ZoneConfig",
]
