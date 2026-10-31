"""Plan 7.1 and 7.4 on the box: TLS to the cloud, device token rotation, credential files."""

import json
import logging
import os
import time

import pytest
from pydantic import ValidationError

from cctv_edge.credentials import load_credentials
from cctv_edge.outbox import Outbox
from cctv_edge.service import EdgeService
from cctv_edge.settings import EdgeSettings
from cctv_edge.token_store import TokenStore


def test_cloud_url_must_use_tls(tmp_path):
    def settings(**kwargs):
        return EdgeSettings(_env_file=None, data_dir=tmp_path, **kwargs)

    assert settings(cloud_url="https://cloud.example.com")
    assert settings(cloud_url="http://localhost:8000")  # the dev stack on this machine
    with pytest.raises(ValidationError, match="https"):
        settings(cloud_url="http://cloud.example.com")
    assert settings(cloud_url="http://192.168.1.10:8000", allow_insecure_cloud=True)


def test_token_store_prefers_the_rotated_token(tmp_path):
    path = tmp_path / "device_token"
    store = TokenStore(path, "cctv_dev_paired")
    assert store.load() == "cctv_dev_paired"  # nothing rotated yet
    store.save("rotated-1")
    assert store.load() == "rotated-1"
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
    assert "cctv_dev_paired" not in path.read_text()  # only a hash of the paired token

    # Paired again (provision.sh writes a new token to edge.env): the old file is stale.
    assert TokenStore(path, "paired-again").load() == "paired-again"
    path.write_text("not json")
    assert TokenStore(path, "cctv_dev_paired").load() == "cctv_dev_paired"


@pytest.fixture
def service(settings, client, cloud):
    outbox = Outbox(settings.outbox_path)
    tokens = TokenStore(settings.token_path, "test-token")
    svc = EdgeService(
        settings, client, outbox, start_streams=False, detector_factories={}, tokens=tokens
    )
    yield svc
    svc.shutdown()
    outbox.close()


def test_token_rotation_on_heartbeat(service, cloud, settings):
    service.heartbeat_once(time.time())
    assert cloud.issued == []  # not due

    cloud.rotate = True
    service.heartbeat_once(time.time())
    assert cloud.issued == ["token-1"]
    assert json.loads(settings.token_path.read_text())["token"] == "token-1"
    # From now on the new token is used, and the old one is refused.
    cloud.tokens = {"token-1"}
    service.heartbeat_once(time.time())
    assert len(cloud.heartbeats) == 3
    # After a restart the saved token is loaded.
    assert TokenStore(settings.token_path, "test-token").load() == "token-1"


def test_rotation_is_skipped_when_the_token_cannot_be_saved(service, cloud, monkeypatch):
    monkeypatch.setattr(TokenStore, "writable", lambda self: False)
    cloud.rotate = True
    service.heartbeat_once(time.time())
    assert cloud.issued == []  # never asked: a token it can't keep would lock the box out


@pytest.mark.skipif(os.name != "posix", reason="file modes")
def test_world_readable_credentials_are_reported(tmp_path, caplog):
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps({"nvr": {"username": "ai", "password": "secret"}}))
    path.chmod(0o644)
    with caplog.at_level(logging.WARNING):
        assert load_credentials(path)["nvr"].password == "secret"
    assert "chmod 600" in caplog.text
    assert "secret" not in caplog.text
