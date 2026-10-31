import uuid
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from cctv_contracts.enums import RemoteKind
from cctv_contracts.v1 import (
    CameraConfig,
    FrameRequest,
    LiveRequest,
    PlaybackRequest,
    RecordingsRequest,
    RemoteReply,
    RemoteRequest,
)

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)


def test_request_kind_selects_the_payload_model():
    raw = {
        "id": str(uuid.uuid4()),
        "expires_at": NOW.isoformat(),
        "request": {"kind": "live", "camera_id": str(uuid.uuid4()), "sdp_offer": "v=0"},
    }
    request = RemoteRequest.model_validate(raw)
    assert isinstance(request.request, LiveRequest)
    assert request.request.kind is RemoteKind.LIVE

    raw["request"] = {"kind": "frame", "camera_id": str(uuid.uuid4())}
    assert isinstance(RemoteRequest.model_validate(raw).request, FrameRequest)


def test_round_trip_through_json():
    request = RemoteRequest(
        id=uuid.uuid4(),
        expires_at=NOW,
        request=PlaybackRequest(
            camera_id=uuid.uuid4(), start=NOW, end=NOW + timedelta(minutes=5), sdp_offer="v=0"
        ),
    )
    assert RemoteRequest.model_validate_json(request.model_dump_json()) == request


def test_time_ranges_are_checked():
    camera = uuid.uuid4()
    with pytest.raises(ValidationError, match="end must be after start"):
        RecordingsRequest(camera_id=camera, start=NOW, end=NOW)
    with pytest.raises(ValidationError, match="limited"):
        RecordingsRequest(camera_id=camera, start=NOW, end=NOW + timedelta(days=2))
    with pytest.raises(ValidationError, match="limited"):
        PlaybackRequest(camera_id=camera, start=NOW, end=NOW + timedelta(hours=3), sdp_offer="x")


def test_unknown_kind_is_rejected():
    with pytest.raises(ValidationError):
        RemoteRequest.model_validate(
            {"id": str(uuid.uuid4()), "expires_at": NOW.isoformat(), "request": {"kind": "ptz"}}
        )


def test_reply_embedding_size_is_bounded():
    with pytest.raises(ValidationError):
        RemoteReply(ok=True, embedding=[0.1] * 3)
    assert RemoteReply(ok=True, embedding=[0.1] * 512).embedding is not None


def test_camera_nvr_reference_is_optional_and_validated():
    camera = {
        "id": str(uuid.uuid4()),
        "name": "Kitchen",
        "role": "kitchen",
        "rtsp_main": "rtsp://x",
    }
    assert CameraConfig.model_validate(camera).nvr is None
    parsed = CameraConfig.model_validate({**camera, "nvr": {"host": "192.168.1.64", "channel": 3}})
    assert parsed.nvr is not None and parsed.nvr.rtsp_port == 554
    with pytest.raises(ValidationError):
        CameraConfig.model_validate({**camera, "nvr": {"host": "admin:pw@nvr", "channel": 1}})
