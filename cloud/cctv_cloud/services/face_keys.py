"""Encryption of face templates (plan 7.2): biometric data, stored only encrypted.

Each template is encrypted with AES-256-GCM. The tenant and staff member are bound in as
associated data, so a template copied onto another person's row doesn't decrypt. The key
comes from `CCTV_FACE_KEY`:

* ``aws-kms:<key ARN or alias>`` - envelope encryption: KMS makes a data key per template
  and only its encrypted form is stored; decrypting needs KMS (and is logged there).
* ``local:<id>`` - a 32-byte key from ``CCTV_FACE_KEYS`` (a secrets manager, not git).
* empty - a key derived from ``CCTV_SECRET_KEY``; dev and test only (refused in production).

``key_id`` on each row names the key, so keys can be rotated: set a new one, then run
``python -m cctv_cloud.cli rotate-face-keys`` to re-encrypt the old rows.
"""

import base64
import hashlib
import os
import struct
import uuid
from collections import OrderedDict
from functools import lru_cache
from typing import Any

import boto3
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select

from cctv_cloud.config import get_settings
from cctv_cloud.db.models import FaceTemplate
from cctv_cloud.db.session import tenant_session
from cctv_cloud.security import InvalidTokenError, _fernet

# The original format (Fernet with a key derived from secret_key, no associated data).
LEGACY_KEY_ID = "app-secret:v1"
DERIVED_KEY_ID = "app-secret:v2"
_DIRECT = b"\x02"  # AES-GCM with the key itself
_ENVELOPE = b"\x03"  # AES-GCM with a KMS data key stored in front (2-byte length)
_NONCE = 12


def current_key_id() -> str:
    return get_settings().face_key or DERIVED_KEY_ID


def new_local_key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


def _pack(embedding: list[float]) -> bytes:
    return struct.pack(f"<{len(embedding)}f", *embedding)


def _unpack(raw: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(raw) // 4}f", raw))


def _aad(tenant_id: uuid.UUID, staff_id: uuid.UUID) -> bytes:
    return f"face-template:{tenant_id}:{staff_id}".encode()


def _local_key(key_id: str) -> bytes:
    if key_id == DERIVED_KEY_ID:
        secret = get_settings().secret_key.get_secret_value()
        return hashlib.sha256(b"face-template-v2:" + secret.encode()).digest()
    stored = get_settings().face_keys.get(key_id.removeprefix("local:"))
    if stored is None:
        raise InvalidTokenError(f"face key {key_id!r} is not configured")
    key = base64.b64decode(stored.get_secret_value())
    if len(key) != 32:
        raise InvalidTokenError(f"face key {key_id!r} must be 32 bytes")
    return key


@lru_cache
def _kms() -> Any:
    return boto3.client("kms", region_name=get_settings().kms_region)


class _DataKeyCache:
    """Plaintext data keys by their encrypted form, so the edge's template fetch doesn't
    call KMS for every template every time."""

    def __init__(self, size: int = 2048) -> None:
        self._size = size
        self._keys: OrderedDict[bytes, bytes] = OrderedDict()

    def get(self, encrypted: bytes, context: dict[str, str]) -> bytes:
        key = self._keys.get(encrypted)
        if key is None:
            key = _kms().decrypt(CiphertextBlob=encrypted, EncryptionContext=context)["Plaintext"]
            self._keys[encrypted] = key
            if len(self._keys) > self._size:
                self._keys.popitem(last=False)
        self._keys.move_to_end(encrypted)
        return key


_data_keys = _DataKeyCache()


def _kms_context(tenant_id: uuid.UUID) -> dict[str, str]:
    return {"purpose": "cctv-face-template", "tenant_id": str(tenant_id)}


def seal_embedding(
    embedding: list[float], tenant_id: uuid.UUID, staff_id: uuid.UUID
) -> tuple[bytes, str]:
    """(ciphertext, key id) for a face embedding (float32, little-endian)."""
    key_id = current_key_id()
    nonce = os.urandom(_NONCE)
    aad = _aad(tenant_id, staff_id)
    if key_id.startswith("aws-kms:"):
        data_key = _kms().generate_data_key(
            KeyId=key_id.removeprefix("aws-kms:"),
            KeySpec="AES_256",
            EncryptionContext=_kms_context(tenant_id),
        )
        wrapped = data_key["CiphertextBlob"]
        sealed = AESGCM(data_key["Plaintext"]).encrypt(nonce, _pack(embedding), aad)
        return _ENVELOPE + struct.pack(">H", len(wrapped)) + wrapped + nonce + sealed, key_id
    sealed = AESGCM(_local_key(key_id)).encrypt(nonce, _pack(embedding), aad)
    return _DIRECT + nonce + sealed, key_id


def open_embedding(
    blob: bytes, key_id: str, tenant_id: uuid.UUID, staff_id: uuid.UUID
) -> list[float]:
    if key_id == LEGACY_KEY_ID:
        try:
            return _unpack(_fernet(b"face-template").decrypt(blob))
        except Exception as exc:
            raise InvalidTokenError("face template cannot be decrypted (key changed?)") from exc
    try:
        if blob[:1] == _ENVELOPE:
            (size,) = struct.unpack(">H", blob[1:3])
            wrapped, rest = blob[3 : 3 + size], blob[3 + size :]
            key = _data_keys.get(wrapped, _kms_context(tenant_id))
        elif blob[:1] == _DIRECT:
            key, rest = _local_key(key_id), blob[1:]
        else:
            raise InvalidTokenError("unknown face template format")
        raw = AESGCM(key).decrypt(rest[:_NONCE], rest[_NONCE:], _aad(tenant_id, staff_id))
    except InvalidTokenError:
        raise
    except Exception as exc:  # wrong key, wrong person, tampered, or KMS refused
        raise InvalidTokenError(f"face template cannot be decrypted with {key_id!r}") from exc
    return _unpack(raw)


def rotate_templates(tenant_ids: list[uuid.UUID], batch: int = 200) -> int:
    """Re-encrypt every template not under the current key; returns how many."""

    key_id = current_key_id()
    done = 0
    for tenant_id in tenant_ids:
        while True:
            with tenant_session(tenant_id) as db:
                rows = db.scalars(
                    select(FaceTemplate)
                    .where(FaceTemplate.key_id != key_id)
                    .limit(batch)
                    .with_for_update(skip_locked=True)
                ).all()
                for row in rows:
                    embedding = open_embedding(
                        row.embedding_encrypted, row.key_id, tenant_id, row.staff_id
                    )
                    row.embedding_encrypted, row.key_id = seal_embedding(
                        embedding, tenant_id, row.staff_id
                    )
            done += len(rows)
            if len(rows) < batch:
                break
    return done
