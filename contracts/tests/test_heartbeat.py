"""Plan 2.10: software updates in the heartbeat."""

import pytest
from pydantic import ValidationError

from cctv_contracts.v1 import HeartbeatResponse, SoftwareUpdate

DIGEST = "ghcr.io/org/cctv-edge@sha256:" + "0" * 64
AT = "2026-11-01T09:00:00+00:00"


def test_release_version_and_digest_are_required():
    """Boxes are only ever told to run an image pinned by digest."""
    update = SoftwareUpdate(version="1.4.0-rc.1", image=DIGEST, requested_at=AT)
    assert update.version == "1.4.0-rc.1"
    for version, image in [("1.4.0", "ghcr.io/org/cctv-edge:1.4.0"), ("latest", DIGEST)]:
        with pytest.raises(ValidationError):
            SoftwareUpdate(version=version, image=image, requested_at=AT)


def test_older_cloud_sends_no_update():
    old = HeartbeatResponse.model_validate({"server_time": AT, "latest_config_version": 3})
    assert old.update is None
