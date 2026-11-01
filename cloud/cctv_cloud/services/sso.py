"""Sign-in from the client's operational system (plan 8.1).

The operational system's server signs a short-lived JWT for the user signed in there and
hands it to the browser (in the URL fragment of the dashboard link or the embedded page,
or by postMessage). The dashboard swaps it at POST /v1/auth/sso for a normal session of
the user with the same email in the integration's tenant. Claims:

    iss    the integration's issuer; finds the tenant and the key
    aud    CCTV_SSO_AUDIENCE (default: the dashboard URL, CCTV_WEB_URL)
    email  an active user of that tenant (invited beforehand; roles and branches are ours)
    iat, exp  at most CCTV_SSO_MAX_TOKEN_SECONDS apart
    jti    unique; each token works once
    sub    the user's id in the operational system (written to the audit log)
    amr    for owners, when the integration requires it: the methods used to sign in,
           including a second factor (RFC 8176, e.g. ["pwd", "otp"])

Keys: a shared secret we generate (HS256), or the operational system's RSA or P-256
public key (RS256, ES256). The full guide for their developers is
docs/integration/ops-system.md.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from cryptography.fernet import InvalidToken
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from cctv_cloud.config import get_settings
from cctv_cloud.db.enums import SsoAlgorithm, UserRole
from cctv_cloud.db.models import SsoIntegration, SsoTokenUse, User
from cctv_cloud.db.session import tenant_session
from cctv_cloud.rbac import MFA_REQUIRED_ROLES
from cctv_cloud.security import InvalidTokenError, _fernet, new_secret

# RFC 8176 methods that are a second factor (or stronger than a password on their own).
SECOND_FACTORS = frozenset(
    {"mfa", "otp", "hwk", "swk", "sms", "tel", "fpt", "face", "iris", "retina", "vbm", "sc"}
)


class SsoError(Exception):
    """The token can't sign anyone in; the message says why (for the audit log only)."""


@dataclass(frozen=True)
class Integration:
    id: uuid.UUID
    tenant_id: uuid.UUID


def new_shared_secret() -> str:
    return new_secret(32)


def encrypt_secret(secret: str) -> bytes:
    return _fernet(b"sso-secret").encrypt(secret.encode())


def decrypt_secret(encrypted: bytes) -> str:
    try:
        return _fernet(b"sso-secret").decrypt(encrypted).decode()
    except InvalidToken as exc:
        raise InvalidTokenError("SSO secret cannot be decrypted (secret key changed?)") from exc


def check_public_key(algorithm: SsoAlgorithm, pem: str) -> str:
    """The PEM, if it is a usable public key for `algorithm`; ValueError otherwise."""
    try:
        key = load_pem_public_key(pem.strip().encode())
    except ValueError as exc:
        raise ValueError("not a PEM public key") from exc
    if algorithm is SsoAlgorithm.RS256:
        if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 2048:
            raise ValueError("RS256 needs an RSA public key of at least 2048 bits")
    elif algorithm is SsoAlgorithm.ES256:
        if not isinstance(key, ec.EllipticCurvePublicKey) or key.curve.name != "secp256r1":
            raise ValueError("ES256 needs a P-256 (secp256r1) public key")
    else:
        raise ValueError("HS256 uses a shared secret, not a public key")
    return pem.strip() + "\n"


def find_integration(token: str) -> Integration:
    """The enabled integration named by the token's issuer (not yet verified)."""
    try:
        issuer = jwt.decode(token, options={"verify_signature": False}).get("iss")
    except jwt.PyJWTError as exc:
        raise SsoError("malformed token") from exc
    if not isinstance(issuer, str) or not issuer:
        raise SsoError("token has no issuer")
    return find_issuer(issuer)


def find_issuer(issuer: str) -> Integration:
    with tenant_session(None) as db:
        row = db.execute(
            text("SELECT integration_id, tenant_id FROM auth_find_sso_integration(:issuer)"),
            {"issuer": issuer},
        ).one_or_none()
    if row is None:
        raise SsoError("unknown or disabled issuer")
    return Integration(row.integration_id, row.tenant_id)


def verify(
    db: Session, integration: SsoIntegration, token: str, now: datetime | None = None
) -> dict[str, Any]:
    """The token's claims, if it is valid for `integration` and not used before. Marks it
    used. `db` is the integration's tenant session."""
    settings = get_settings()
    now = now or datetime.now(timezone.utc)
    skew = settings.sso_clock_skew_seconds
    if integration.algorithm == SsoAlgorithm.HS256.value:
        assert integration.secret_encrypted is not None  # noqa: S101 - database check
        key: str = decrypt_secret(integration.secret_encrypted)
    else:
        assert integration.public_key is not None  # noqa: S101 - database check
        key = integration.public_key
    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=[integration.algorithm],  # never the algorithm the token names
            audience=settings.sso_expected_audience,
            issuer=integration.issuer,
            leeway=skew,
            options={"require": ["iss", "aud", "exp", "iat", "jti", "email"]},
        )
    except jwt.PyJWTError as exc:
        raise SsoError(f"invalid token: {exc}") from exc
    issued = datetime.fromtimestamp(claims["iat"], timezone.utc)
    expires = datetime.fromtimestamp(claims["exp"], timezone.utc)
    if issued > now + timedelta(seconds=skew):
        raise SsoError("token issued in the future")
    if (expires - issued).total_seconds() > settings.sso_max_token_seconds:
        raise SsoError(f"token lives longer than {settings.sso_max_token_seconds} s")
    if not isinstance(claims["email"], str):
        raise SsoError("email is not a string")

    db.execute(
        delete(SsoTokenUse).where(
            SsoTokenUse.integration_id == integration.id, SsoTokenUse.expires_at < now
        )
    )
    first_use = db.scalar(
        insert(SsoTokenUse)
        .values(
            tenant_id=integration.tenant_id,
            integration_id=integration.id,
            jti=str(claims["jti"])[:200],
            expires_at=expires + timedelta(seconds=skew),
        )
        .on_conflict_do_nothing()
        .returning(SsoTokenUse.jti)
    )
    if first_use is None:
        raise SsoError("token already used")
    return claims


def find_user(db: Session, integration: SsoIntegration, claims: dict[str, Any]) -> User:
    """The active user the token signs in. Roles and branch access are managed here, not
    in the token."""
    user = db.scalar(select(User).where(func.lower(User.email) == claims["email"].lower()))
    if user is None or not user.is_active or user.tenant_id != integration.tenant_id:
        raise SsoError("no active user with that email")
    role = UserRole(user.role)
    if integration.require_mfa and role in MFA_REQUIRED_ROLES:
        methods = claims.get("amr")
        if not isinstance(methods, list) or not SECOND_FACTORS & {str(m) for m in methods}:
            raise SsoError("owners must sign in to the operational system with 2FA (amr)")
    return user


def sign_in_token(
    integration: SsoIntegration, email: str, lifetime: timedelta = timedelta(minutes=2)
) -> str:
    """An HS256 token as the operational system would make it (testing and the CLI)."""
    now = datetime.now(timezone.utc)
    assert integration.secret_encrypted is not None  # noqa: S101
    return jwt.encode(
        {
            "iss": integration.issuer,
            "aud": get_settings().sso_expected_audience,
            "sub": email,
            "email": email,
            "amr": ["pwd", "otp"],
            "iat": now,
            "exp": now + lifetime,
            "jti": uuid.uuid4().hex,
        },
        decrypt_secret(integration.secret_encrypted),
        algorithm="HS256",
    )
