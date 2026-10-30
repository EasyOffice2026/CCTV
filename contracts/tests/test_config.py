from uuid import uuid4

import pytest
from pydantic import ValidationError

from cctv_contracts.v1 import CameraConfig, EdgeConfig, ZoneConfig


def make_camera(**overrides):
    data = {
        "id": str(uuid4()),
        "name": "Kitchen 1",
        "role": "kitchen",
        "rtsp_main": "rtsp://192.168.1.64:554/Streaming/Channels/101",
        "rtsp_sub": "rtsp://192.168.1.64:554/Streaming/Channels/102",
        "credentials_ref": "nvr-main",
        "features": [{"key": "kitchen_compliance", "params": {"min_violation_seconds": 5}}],
        "zones": [
            {
                "id": str(uuid4()),
                "name": "Prep line",
                "type": "kitchen",
                "points": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9]],
            }
        ],
    }
    data.update(overrides)
    return CameraConfig.model_validate(data)


def test_valid_edge_config():
    config = EdgeConfig.model_validate(
        {
            "version": 3,
            "generated_at": "2026-10-30T10:00:00Z",
            "tenant_id": str(uuid4()),
            "branch_id": str(uuid4()),
            "working_hours": [{"weekday": 4, "opens_at": "11:00", "closes_at": "02:00"}],
            "cameras": [make_camera().model_dump(mode="json")],
        }
    )
    assert config.timezone == "Asia/Kuwait"
    assert config.clip.pre_seconds == 10
    assert config.cameras[0].features[0].key.value == "kitchen_compliance"


def test_rtsp_credentials_rejected():
    with pytest.raises(ValidationError, match="must not contain credentials"):
        make_camera(rtsp_main="rtsp://admin:secret@192.168.1.64:554/Streaming/Channels/101")


def test_non_rtsp_url_rejected():
    with pytest.raises(ValidationError, match="rtsp"):
        make_camera(rtsp_main="http://192.168.1.64/")


def test_unknown_timezone_rejected():
    with pytest.raises(ValidationError, match="unknown time zone"):
        EdgeConfig.model_validate(
            {
                "version": 1,
                "generated_at": "2026-10-30T10:00:00Z",
                "tenant_id": str(uuid4()),
                "branch_id": str(uuid4()),
                "timezone": "Asia/Nowhere",
            }
        )


def test_duplicate_features_and_cameras_rejected():
    with pytest.raises(ValidationError, match="duplicate feature key"):
        make_camera(features=[{"key": "cashier"}, {"key": "cashier"}])
    camera = make_camera().model_dump(mode="json")
    with pytest.raises(ValidationError, match="duplicate camera id"):
        EdgeConfig.model_validate(
            {
                "version": 1,
                "generated_at": "2026-10-30T10:00:00Z",
                "tenant_id": str(uuid4()),
                "branch_id": str(uuid4()),
                "cameras": [camera, camera],
            }
        )


@pytest.mark.parametrize(
    ("zone_type", "points", "ok"),
    [
        ("entrance_line", [[0.1, 0.5], [0.9, 0.5]], True),
        ("entrance_line", [[0.1, 0.5], [0.9, 0.5], [0.5, 0.9]], False),
        ("counter", [[0.1, 0.5], [0.9, 0.5]], False),
        ("counter", [[0.1, 0.1], [0.9, 0.1], [0.5, 0.9]], True),
        ("counter", [[0.1, 0.1], [1.5, 0.1], [0.5, 0.9]], False),
    ],
)
def test_zone_shapes(zone_type, points, ok):
    data = {"id": str(uuid4()), "name": "z", "type": zone_type, "points": points}
    if ok:
        ZoneConfig.model_validate(data)
    else:
        with pytest.raises(ValidationError):
            ZoneConfig.model_validate(data)
