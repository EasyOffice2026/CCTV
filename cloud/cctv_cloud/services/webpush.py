"""Web Push (plan 6.2): encrypted messages to browsers through their push service.

Implements RFC 8291 (message encryption, aes128gcm) and RFC 8292 (VAPID) with
`cryptography` and `httpx`, so no extra native dependency is needed. Browsers subscribe
with the VAPID public key (`GET /v1/push/config`) and send us their endpoint and keys.

Endpoints come from browsers, so only well-known push services are contacted (the server
would otherwise POST to any URL a user supplies).
"""

import base64
import json
import os
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from cctv_cloud.config import get_settings

# Push services of Chrome/Edge/Android, Firefox, Windows and Safari.
PUSH_HOSTS = (
    "fcm.googleapis.com",
    "updates.push.services.mozilla.com",
    ".notify.windows.com",
    ".push.apple.com",
)
RECORD_SIZE = 4096
TTL_SECONDS = 600  # an alert that can't be delivered within 10 minutes is dropped
TIMEOUT_SECONDS = 10


def b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def allowed_endpoint(endpoint: str) -> bool:
    parts = urlsplit(endpoint)
    host = (parts.hostname or "").lower()
    return (
        parts.scheme == "https"
        and parts.port in (None, 443)
        and any(host == h or (h.startswith(".") and host.endswith(h)) for h in PUSH_HOSTS)
    )


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def _public_bytes(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )


def encrypt(
    payload: bytes, p256dh: str, auth: str, *, salt: bytes | None = None, private_key=None
) -> bytes:
    """aes128gcm body for one subscription (RFC 8291 section 3.4, a single record)."""
    ua_public = b64url_decode(p256dh)
    auth_secret = b64url_decode(auth)
    salt = salt or os.urandom(16)
    as_private = private_key or ec.generate_private_key(ec.SECP256R1())
    as_public = _public_bytes(as_private.public_key())
    shared = as_private.exchange(
        ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    )
    ikm = _hkdf(auth_secret, shared, b"WebPush: info\x00" + ua_public + as_public, 32)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    if len(payload) > RECORD_SIZE - 16 - 1 - 86:
        raise ValueError("push payload too large")
    ciphertext = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)  # 0x02: last record
    header = salt + RECORD_SIZE.to_bytes(4, "big") + bytes([len(as_public)]) + as_public
    return header + ciphertext


@dataclass(frozen=True)
class Vapid:
    private_key: ec.EllipticCurvePrivateKey
    subject: str

    @classmethod
    def from_private(cls, value: str, subject: str) -> "Vapid":
        number = int.from_bytes(b64url_decode(value.strip()), "big")
        return cls(ec.derive_private_key(number, ec.SECP256R1()), subject)

    @property
    def public_key(self) -> str:
        """For the browser's `applicationServerKey`."""
        return b64url_encode(_public_bytes(self.private_key.public_key()))

    def authorization(self, endpoint: str) -> str:
        parts = urlsplit(endpoint)
        claims = {
            "aud": f"{parts.scheme}://{parts.netloc}",
            "exp": int(time.time()) + 12 * 3600,
            "sub": self.subject,
        }
        token = jwt.encode(claims, self.private_key, algorithm="ES256")
        return f"vapid t={token}, k={self.public_key}"


def new_vapid_private_key() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    return b64url_encode(key.private_numbers().private_value.to_bytes(32, "big"))


class PushGoneError(Exception):
    """The subscription expired or was removed; delete it."""


class PushSender(Protocol):
    public_key: str | None

    def send(self, endpoint: str, p256dh: str, auth: str, message: dict[str, Any]) -> None: ...


class WebPushSender:
    def __init__(self, vapid: Vapid | None) -> None:
        self._vapid = vapid
        self.public_key = vapid.public_key if vapid else None

    def send(self, endpoint: str, p256dh: str, auth: str, message: dict[str, Any]) -> None:
        if self._vapid is None:
            raise RuntimeError("Web Push is not configured (CCTV_VAPID_PRIVATE_KEY)")
        if not allowed_endpoint(endpoint):
            raise PushGoneError("not a known push service")
        body = encrypt(json.dumps(message, ensure_ascii=False).encode(), p256dh, auth)
        response = httpx.post(
            endpoint,
            content=body,
            headers={
                "Authorization": self._vapid.authorization(endpoint),
                "Content-Encoding": "aes128gcm",
                "Content-Type": "application/octet-stream",
                "TTL": str(TTL_SECONDS),
                "Urgency": "high",
            },
            timeout=TIMEOUT_SECONDS,
        )
        if response.status_code in (404, 410):
            raise PushGoneError(f"push service answered {response.status_code}")
        response.raise_for_status()


@lru_cache
def get_push_sender() -> PushSender:
    settings = get_settings()
    key = settings.vapid_private_key.get_secret_value()
    return WebPushSender(Vapid.from_private(key, settings.vapid_subject) if key else None)
