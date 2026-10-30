"""Edge <-> cloud API, version 1.

Endpoints (all under API_PREFIX, authenticated with the device token):

    POST {API_PREFIX}/events              EventBatch          -> EventBatchResponse
    POST {API_PREFIX}/media/upload-urls   UploadUrlRequest    -> UploadUrlResponse
    POST {API_PREFIX}/heartbeat           Heartbeat           -> HeartbeatResponse
    GET  {API_PREFIX}/config              (If-None-Match: version) -> EdgeConfig | 304
"""

from cctv_contracts.v1.common import ContractModel, MediaKey, UtcDatetime
from cctv_contracts.v1.config import (
    CameraConfig,
    ClipSettings,
    EdgeConfig,
    FeatureConfig,
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
    "FeatureConfig",
    "Heartbeat",
    "HeartbeatResponse",
    "MediaKey",
    "SystemStats",
    "UploadUrlRequest",
    "UploadUrlResponse",
    "UtcDatetime",
    "WorkingHours",
    "ZoneConfig",
]
