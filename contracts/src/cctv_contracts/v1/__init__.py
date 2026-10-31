"""Edge <-> cloud API, version 1.

Endpoints (all under API_PREFIX, authenticated with the device token except /pair):

    POST {API_PREFIX}/pair                PairRequest         -> PairResponse
    POST {API_PREFIX}/events              EventBatch          -> EventBatchResponse
    POST {API_PREFIX}/media/upload-urls   UploadUrlRequest    -> UploadUrlResponse
    POST {API_PREFIX}/heartbeat           Heartbeat           -> HeartbeatResponse
    POST {API_PREFIX}/token               (no body)           -> TokenResponse (rotation)
    GET  {API_PREFIX}/config              (If-None-Match: version) -> EdgeConfig | 304
    GET  {API_PREFIX}/remote?wait=25      (long poll)         -> RemoteRequest | 204
    POST {API_PREFIX}/remote/{id}/reply   RemoteReply         -> 204
    GET  {API_PREFIX}/face-templates      (If-None-Match: etag) -> FaceTemplateList | 304
    POST {API_PREFIX}/attendance          SightingBatch       -> BatchResult
    POST {API_PREFIX}/counts              CountBatch          -> BatchResult
"""

from cctv_contracts.v1.attendance import (
    MAX_SIGHTINGS_PER_BATCH,
    BatchResult,
    FaceTemplate,
    FaceTemplateList,
    RejectedItem,
    SightingBatch,
    StaffSighting,
)
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
from cctv_contracts.v1.counts import MAX_BUCKETS_PER_BATCH, CountBatch, CountBucketIn
from cctv_contracts.v1.events import (
    MAX_EVENTS_PER_BATCH,
    EventBatch,
    EventBatchResponse,
    EventIn,
    EventResult,
)
from cctv_contracts.v1.heartbeat import CameraStatus, Heartbeat, HeartbeatResponse, SystemStats
from cctv_contracts.v1.media import UploadUrlRequest, UploadUrlResponse
from cctv_contracts.v1.pairing import PairRequest, PairResponse, TokenResponse
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
    "token_response": TokenResponse,
    "remote_request": RemoteRequest,
    "remote_reply": RemoteReply,
    "face_template_list": FaceTemplateList,
    "sighting_batch": SightingBatch,
    "count_batch": CountBatch,
    "batch_result": BatchResult,
}

__all__ = [
    "API_PREFIX",
    "MAX_BUCKETS_PER_BATCH",
    "MAX_EVENTS_PER_BATCH",
    "MAX_SIGHTINGS_PER_BATCH",
    "SCHEMA_MODELS",
    "BatchResult",
    "CameraConfig",
    "CameraStatus",
    "ClipSettings",
    "ContractModel",
    "CountBatch",
    "CountBucketIn",
    "EdgeConfig",
    "EventBatch",
    "EventBatchResponse",
    "EventIn",
    "EventResult",
    "FaceEmbeddingRequest",
    "FaceTemplate",
    "FaceTemplateList",
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
    "RejectedItem",
    "RemoteReply",
    "RemoteRequest",
    "SightingBatch",
    "StaffSighting",
    "SystemStats",
    "TokenResponse",
    "UploadUrlRequest",
    "UploadUrlResponse",
    "UtcDatetime",
    "WorkingHours",
    "ZoneConfig",
]
