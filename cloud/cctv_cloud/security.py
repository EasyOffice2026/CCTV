"""Passwords, signed tokens, TOTP and opaque secrets."""

import base64
import hashlib
import hmac
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Literal

import jwt
import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet, InvalidToken

from cctv_cloud.config import get_settings

TokenType = Literal["access", "refresh", "mfa", "reset"]
ISSUER = "cctv-cloud"
TOTP_ISSUER = "CCTV AI"

_hasher = PasswordHasher()


class InvalidTokenError(Exception):
    pass


# --- passwords ----------------------------------------------------------------------


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    if password_hash is None:
        # Same work as a real check, so response time doesn't reveal unknown accounts.
        password_hash = _dummy_hash()
        password = password + "x"
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


@lru_cache
def _dummy_hash() -> str:
    return _hasher.hash(secrets.token_urlsafe(16))


# --- opaque secrets (device tokens, pairing codes, API keys) -------------------------


def new_secret(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def sha256_hex(value: str) -> str:
    """For high-entropy secrets only; passwords use Argon2."""
    return hashlib.sha256(value.encode()).hexdigest()


def new_pairing_code() -> str:
    """8 characters from an unambiguous alphabet, shown as XXXX-XXXX for typing."""
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    raw = "".join(secrets.choice(alphabet) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def normalise_pairing_code(code: str) -> str:
    raw = code.replace("-", "").replace(" ", "").upper()
    return f"{raw[:4]}-{raw[4:]}"


# --- signed tokens ------------------------------------------------------------------


def _key() -> str:
    return get_settings().secret_key.get_secret_value()


def encode_token(typ: TokenType, claims: dict[str, Any], lifetime: timedelta) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        **claims,
        "typ": typ,
        "iss": ISSUER,
        "iat": now,
        "exp": now + lifetime,
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, _key(), algorithm="HS256")


def decode_token(token: str, typ: TokenType) -> dict[str, Any]:
    try:
        claims = jwt.decode(
            token,
            _key(),
            algorithms=["HS256"],
            issuer=ISSUER,
            options={"require": ["exp", "iat", "typ", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise InvalidTokenError(str(exc)) from exc
    if claims["typ"] != typ:
        raise InvalidTokenError(f"expected a {typ} token")
    return claims


def password_fingerprint(password_hash: str | None, changed_at: datetime | None) -> str:
    """Embedded in reset links: changes when the password does, so a link works once."""
    material = f"{password_hash}|{changed_at.isoformat() if changed_at else ''}"
    return hmac.new(_key().encode(), material.encode(), hashlib.sha256).hexdigest()[:32]


# --- TOTP (2FA) ---------------------------------------------------------------------


def _fernet() -> Fernet:
    digest = hashlib.sha256(b"totp-secret:" + _key().encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def new_totp_secret() -> str:
    return pyotp.random_base32()


def encrypt_totp_secret(secret: str) -> bytes:
    return _fernet().encrypt(secret.encode())


def decrypt_totp_secret(encrypted: bytes) -> str:
    try:
        return _fernet().decrypt(encrypted).decode()
    except InvalidToken as exc:
        raise InvalidTokenError("TOTP secret cannot be decrypted (secret key changed?)") from exc


def totp_uri(secret: str, email: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=TOTP_ISSUER)


def verify_totp(secret: str, code: str, last_step: int | None, now: float | None = None) -> int:
    """Returns the matched time step, or 0 if the code is wrong or was already used.
    One step of clock drift either way is accepted."""
    totp = pyotp.TOTP(secret)
    current = int((now or time.time()) // totp.interval)
    code = code.strip().replace(" ", "")
    for step in (current - 1, current, current + 1):
        if hmac.compare_digest(totp.generate_otp(step), code):
            return step if last_step is None or step > last_step else 0
    return 0
