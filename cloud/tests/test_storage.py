"""Media keys and real S3 URL signing (signing needs no network)."""

import uuid

from cctv_cloud.services.storage import S3Storage, key_belongs_to, media_key
from cctv_contracts.enums import MediaKind

T, B, E = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def test_media_keys_are_bound_to_tenant_branch_and_event():
    key = media_key(T, B, E, MediaKind.CLIP, "ab" * 32)
    assert key == f"{T}/{B}/{E}/clip-abababababababab.mp4"
    assert key_belongs_to(key, T, B, E, MediaKind.CLIP)
    assert not key_belongs_to(key, T, B, E, MediaKind.SNAPSHOT)
    assert not key_belongs_to(key, T, uuid.uuid4(), E, MediaKind.CLIP)
    assert not key_belongs_to(key, uuid.uuid4(), B, E, MediaKind.CLIP)
    assert not key_belongs_to(f"{T}/{B}/{E}/clip-x/../../other.mp4", T, B, E, MediaKind.CLIP)


def test_s3_presigned_urls():
    storage = S3Storage("cctv-media", "http://localhost:9000", "us-east-1", "key", "secret")
    put = storage.presign_put("t/b/e/clip-1.mp4", "video/mp4", 900)
    assert put.startswith("http://localhost:9000/cctv-media/t/b/e/clip-1.mp4?")
    assert "X-Amz-Signature=" in put and "X-Amz-Expires=900" in put
    get = storage.presign_get("t/b/e/clip-1.mp4", 300, download_name="clip.mp4")
    assert "response-content-disposition=attachment" in get


def test_no_keys_means_the_aws_credential_chain(monkeypatch):
    """On AWS the bucket is reached with the task role: no keys are set (infra/terraform)."""
    from cctv_cloud.services import storage

    seen = {}
    monkeypatch.setattr(storage.boto3, "client", lambda *a, **kw: seen.update(kw))
    storage.S3Storage("bucket", "https://s3.me-central-1.amazonaws.com", "me-central-1", "", "")
    assert (seen["aws_access_key_id"], seen["aws_secret_access_key"]) == (None, None)
    storage.S3Storage("bucket", "http://localhost:9000", "us-east-1", "cctv", "dev-secret")
    assert seen["aws_access_key_id"] == "cctv"
