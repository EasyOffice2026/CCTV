"""Object storage for clips and snapshots (S3-compatible: MinIO locally, S3 in the cloud).

Keys start with the tenant and branch, so a key handed to a device can be checked against
the device's own tenant and branch before an event may reference it.
"""

import logging
import uuid
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path
from typing import Protocol

import boto3
from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError

from cctv_cloud.config import get_settings
from cctv_contracts.enums import MediaKind

log = logging.getLogger(__name__)

EXTENSIONS = {MediaKind.SNAPSHOT: "jpg", MediaKind.CLIP: "mp4"}


def media_key(
    tenant_id: uuid.UUID, branch_id: uuid.UUID, event_id: uuid.UUID, kind: MediaKind, sha256: str
) -> str:
    return f"{tenant_id}/{branch_id}/{event_id}/{kind.value}-{sha256[:16]}.{EXTENSIONS[kind]}"


def key_belongs_to(
    key: str, tenant_id: uuid.UUID, branch_id: uuid.UUID, event_id: uuid.UUID, kind: MediaKind
) -> bool:
    prefix = f"{tenant_id}/{branch_id}/{event_id}/{kind.value}-"
    return (
        key.startswith(prefix)
        and key.endswith(f".{EXTENSIONS[kind]}")
        and "/" not in key[len(prefix) :]
    )


class ObjectStorage(Protocol):
    def presign_put(self, key: str, content_type: str, expires: int) -> str: ...

    def presign_get(self, key: str, expires: int, download_name: str | None = None) -> str: ...

    def delete(self, keys: Iterable[str]) -> None: ...

    def download(self, key: str, path: Path) -> None: ...


class S3Storage:
    def __init__(
        self,
        bucket: str,
        endpoint_url: str | None,
        region: str,
        access_key: str,
        secret_key: str,
    ) -> None:
        self.bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region,
            # No keys: the AWS credential chain (the ECS task role in the cloud).
            aws_access_key_id=access_key or None,
            aws_secret_access_key=secret_key or None,
            # Path-style works with MinIO; SigV4 is required by both.
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )

    def presign_put(self, key: str, content_type: str, expires: int) -> str:
        return self._client.generate_presigned_url(
            "put_object",
            Params={"Bucket": self.bucket, "Key": key, "ContentType": content_type},
            ExpiresIn=expires,
        )

    def presign_get(self, key: str, expires: int, download_name: str | None = None) -> str:
        params = {"Bucket": self.bucket, "Key": key}
        if download_name:
            params["ResponseContentDisposition"] = f'attachment; filename="{download_name}"'
        return self._client.generate_presigned_url("get_object", Params=params, ExpiresIn=expires)

    def default_encryption(self) -> str | None:
        """The bucket's default server-side encryption ("AES256", "aws:kms", ...), or None
        when there is none (plan 7.2: every clip and snapshot is encrypted at rest)."""
        try:
            config = self._client.get_bucket_encryption(Bucket=self.bucket)
        except ClientError as exc:
            if "ServerSideEncryptionConfiguration" in exc.response.get("Error", {}).get("Code", ""):
                return None
            raise
        rules = config["ServerSideEncryptionConfiguration"]["Rules"]
        return rules[0]["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"] if rules else None

    def download(self, key: str, path: Path) -> None:
        self._client.download_file(self.bucket, key, str(path))

    def delete(self, keys: Iterable[str]) -> None:
        batch = [{"Key": k} for k in keys]
        for start in range(0, len(batch), 1000):  # S3 limit per request
            try:
                self._client.delete_objects(
                    Bucket=self.bucket, Delete={"Objects": batch[start : start + 1000]}
                )
            except (BotoCoreError, ClientError):
                log.exception("deleting %d objects failed", len(batch[start : start + 1000]))
                raise


@lru_cache
def get_storage() -> ObjectStorage:
    s = get_settings()
    return S3Storage(
        s.s3_bucket,
        s.s3_endpoint_url,
        s.s3_region,
        s.s3_access_key.get_secret_value(),
        s.s3_secret_key.get_secret_value(),
    )
