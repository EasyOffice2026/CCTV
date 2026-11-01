"""Login, two-factor authentication, sessions and password reset (plan 3.1).

POST /login            email + password -> tokens, or an mfa_token for the second step
POST /mfa/setup        mfa_token -> new TOTP secret (owners/admins enrolling)
POST /mfa/enable       setup_token + code -> tokens
POST /mfa/verify       mfa_token + code -> tokens
POST /refresh          refresh_token -> new tokens (rotated; reuse ends the session)
POST /logout           ends the current session
POST /sso              sign-in token from the client's operational system -> tokens (plan 8.1)

Browsers also get the refresh token as an HttpOnly cookie (path /v1/auth, SameSite=Strict),
so the web app never has to store it; /refresh accepts the cookie instead of a body.
Pages embedded in the operational system (another site) get no cookie: browsers don't send
SameSite=Strict cookies to a frame of another site, so they keep the refresh token in
memory and send it in the body.
GET  /me               the caller
POST /password/forgot  email -> reset link by email (always 202)
POST /password/reset   token + new password
POST /password/change  current + new password
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import text, update
from sqlalchemy.orm import Session

from cctv_cloud.api.deps import get_principal, unauthorized
from cctv_cloud.config import get_settings
from cctv_cloud.db.enums import UserRole
from cctv_cloud.db.models import SsoIntegration, User, UserSession
from cctv_cloud.db.session import tenant_session
from cctv_cloud.rbac import MFA_REQUIRED_ROLES, Principal
from cctv_cloud.security import (
    InvalidTokenError,
    decode_token,
    decrypt_totp_secret,
    encode_token,
    encrypt_totp_secret,
    hash_password,
    new_totp_secret,
    password_fingerprint,
    totp_uri,
    verify_password,
    verify_totp,
)
from cctv_cloud.services import sso
from cctv_cloud.services.audit import AuditAction, client_ip, record
from cctv_cloud.services.mailer import Mailer, get_mailer

router = APIRouter(prefix="/v1/auth", tags=["auth"])

MFA_TOKEN_LIFETIME = timedelta(minutes=5)
REFRESH_COOKIE = "cctv_refresh"
Password = Annotated[str, Field(min_length=10, max_length=200)]


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(max_length=200)


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105 - not a secret
    expires_in: int


class MfaChallenge(BaseModel):
    mfa_required: Literal["verify", "setup"]
    mfa_token: str


class MfaSetup(BaseModel):
    secret: str
    otpauth_uri: str
    setup_token: str


class MfaCode(BaseModel):
    token: str
    code: str = Field(min_length=6, max_length=8)


class TokenRequest(BaseModel):
    token: str


class SsoRequest(BaseModel):
    token: str = Field(max_length=8000)
    embedded: bool = Field(
        default=False,
        description="Inside the operational system's page: no refresh cookie is set; send "
        "the refresh token in the body of /refresh instead",
    )


class Me(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str
    role: UserRole
    tenant_id: uuid.UUID | None
    mfa_enabled: bool
    permissions: list[str]
    branch_ids: list[uuid.UUID] | None


class ForgotRequest(BaseModel):
    email: EmailStr


class ResetRequest(BaseModel):
    token: str
    new_password: Password


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(max_length=200)
    new_password: Password


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _find_user(email: str) -> tuple[uuid.UUID, uuid.UUID | None] | None:
    with tenant_session(None) as session:
        row = session.execute(
            text("SELECT user_id, tenant_id FROM auth_find_user(:email)"), {"email": email}
        ).one_or_none()
    return (row.user_id, row.tenant_id) if row else None


def _issue_tokens(db: Session, user: User, request: Request) -> TokenPair:
    """Start a session for `user` (loaded in `db`, the user's tenant session)."""
    settings = get_settings()
    now = _now()
    session_row = UserSession(
        user_id=user.id,
        refresh_jti=uuid.uuid4().hex,
        last_used_at=now,
        expires_at=now + timedelta(days=settings.session_max_days),
        ip=client_ip(request),
        user_agent=(request.headers.get("user-agent") or "")[:500],
    )
    db.add(session_row)
    db.flush()
    user.last_login_at = now
    return _token_pair(user, session_row)


def _token_pair(user: User, session_row: UserSession) -> TokenPair:
    settings = get_settings()
    claims = {"sub": str(user.id), "tid": str(user.tenant_id) if user.tenant_id else None}
    access = encode_token(
        "access",
        {**claims, "role": user.role, "sid": str(session_row.id)},
        timedelta(minutes=settings.access_token_minutes),
    )
    refresh = encode_token(
        "refresh",
        {**claims, "sid": str(session_row.id), "rti": session_row.refresh_jti},
        session_row.expires_at - _now(),
    )
    return TokenPair(
        access_token=access, refresh_token=refresh, expires_in=settings.access_token_minutes * 60
    )


def _set_refresh_cookie(response: Response, pair: TokenPair) -> TokenPair:
    settings = get_settings()
    response.set_cookie(
        REFRESH_COOKIE,
        pair.refresh_token,
        max_age=settings.session_max_days * 86400,
        path=router.prefix,
        secure=settings.secure_cookies,
        httponly=True,
        samesite="strict",
    )
    return pair


def _clear_refresh_cookie(response: Response) -> None:
    settings = get_settings()
    response.delete_cookie(
        REFRESH_COOKIE,
        path=router.prefix,
        secure=settings.secure_cookies,
        httponly=True,
        samesite="strict",
    )


def _mfa_claims(token: str) -> tuple[uuid.UUID, uuid.UUID | None, dict]:
    try:
        claims = decode_token(token, "mfa")
        return (
            uuid.UUID(claims["sub"]),
            uuid.UUID(claims["tid"]) if claims.get("tid") else None,
            claims,
        )
    except (InvalidTokenError, KeyError, ValueError) as exc:
        raise unauthorized("invalid or expired 2FA token") from exc


def _check_locked(user: User) -> None:
    if user.locked_until and user.locked_until > _now():
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "too many failed logins; try again later"
        )


def _count_failure(user: User) -> None:
    """A wrong password or 2FA code; too many in a row lock the account for a while."""
    settings = get_settings()
    user.failed_logins += 1
    if user.failed_logins >= settings.login_max_failures:
        user.locked_until = _now() + timedelta(minutes=settings.login_lockout_minutes)
        user.failed_logins = 0


def _audit_login(
    db: Session,
    action: AuditAction,
    tenant_id: uuid.UUID | None,
    user_id: uuid.UUID,
    request: Request,
    details: dict[str, str] | None = None,
) -> None:
    """Sign-ins of super-admins (no tenant) go to the platform audit log."""
    record(
        db, None, action, "user", user_id, request, details, tenant_id=tenant_id, user_id=user_id
    )


@router.post("/login", response_model=TokenPair | MfaChallenge)
def login(body: LoginRequest, request: Request, response: Response) -> TokenPair | MfaChallenge:
    found = _find_user(body.email)
    if found is None:
        verify_password(None, body.password)  # same timing as a real account
        raise unauthorized("wrong email or password")
    user_id, tenant_id = found

    result: TokenPair | MfaChallenge | None = None
    with tenant_session(tenant_id, user_id) as db:
        user = db.get_one(User, user_id)
        _check_locked(user)
        if not user.is_active or not verify_password(user.password_hash, body.password):
            _count_failure(user)
            _audit_login(db, AuditAction.LOGIN_FAILED, tenant_id, user_id, request)
        else:
            # The failure count is only reset once fully signed in (after 2FA), so a
            # password doesn't buy more guesses at the code.
            role = UserRole(user.role)
            if user.totp_secret_encrypted is not None or role in MFA_REQUIRED_ROLES:
                stage = "verify" if user.totp_secret_encrypted is not None else "setup"
                token = encode_token(
                    "mfa",
                    {
                        "sub": str(user.id),
                        "tid": str(tenant_id) if tenant_id else None,
                        "stage": stage,
                    },
                    MFA_TOKEN_LIFETIME,
                )
                result = MfaChallenge(mfa_required=stage, mfa_token=token)
            else:
                user.failed_logins = 0
                _audit_login(db, AuditAction.LOGIN, tenant_id, user_id, request)
                result = _issue_tokens(db, user, request)
    if result is None:  # raised after the block so the failure count is committed
        raise unauthorized("wrong email or password")
    if isinstance(result, TokenPair):
        _set_refresh_cookie(response, result)
    return result


@router.post("/mfa/setup", response_model=MfaSetup)
def mfa_setup(body: TokenRequest) -> MfaSetup:
    user_id, tenant_id, claims = _mfa_claims(body.token)
    if claims.get("stage") != "setup":
        raise unauthorized("2FA is already set up")
    with tenant_session(tenant_id, user_id) as db:
        user = db.get_one(User, user_id)
        email = user.email
    secret = new_totp_secret()
    setup_token = encode_token(
        "mfa",
        {
            "sub": str(user_id),
            "tid": str(tenant_id) if tenant_id else None,
            "stage": "enable",
            "totp": encrypt_totp_secret(secret).decode(),
        },
        MFA_TOKEN_LIFETIME,
    )
    return MfaSetup(secret=secret, otpauth_uri=totp_uri(secret, email), setup_token=setup_token)


@router.post("/mfa/enable", response_model=TokenPair)
def mfa_enable(body: MfaCode, request: Request, response: Response) -> TokenPair:
    user_id, tenant_id, claims = _mfa_claims(body.token)
    if claims.get("stage") != "enable":
        raise unauthorized("invalid 2FA setup token")
    encrypted = claims["totp"].encode()
    step = verify_totp(decrypt_totp_secret(encrypted), body.code, None)
    if not step:
        raise unauthorized("wrong code")
    with tenant_session(tenant_id, user_id) as db:
        user = db.get_one(User, user_id)
        if user.totp_secret_encrypted is not None:
            raise unauthorized("2FA is already set up")
        user.totp_secret_encrypted = encrypted
        user.totp_last_step = step
        user.failed_logins = 0
        _audit_login(db, AuditAction.LOGIN, tenant_id, user_id, request, {"mfa": "enabled"})
        return _set_refresh_cookie(response, _issue_tokens(db, user, request))


@router.post("/mfa/verify", response_model=TokenPair)
def mfa_verify(body: MfaCode, request: Request, response: Response) -> TokenPair:
    user_id, tenant_id, claims = _mfa_claims(body.token)
    if claims.get("stage") != "verify":
        raise unauthorized("invalid 2FA token")
    result: TokenPair | None = None
    with tenant_session(tenant_id, user_id) as db:
        user = db.get_one(User, user_id)
        if not user.is_active or user.totp_secret_encrypted is None:
            raise unauthorized()
        _check_locked(user)
        secret = decrypt_totp_secret(user.totp_secret_encrypted)
        step = verify_totp(secret, body.code, user.totp_last_step)
        if not step:
            # Wrong codes count like wrong passwords, so codes can't be guessed.
            _count_failure(user)
            _audit_login(
                db, AuditAction.LOGIN_FAILED, tenant_id, user_id, request, {"stage": "2fa"}
            )
        else:
            user.failed_logins = 0
            user.totp_last_step = step
            _audit_login(db, AuditAction.LOGIN, tenant_id, user_id, request)
            result = _issue_tokens(db, user, request)
    if result is None:  # raised after the block so the failure count is committed
        raise unauthorized("wrong or already used code")
    return _set_refresh_cookie(response, result)


@router.post("/sso", response_model=TokenPair)
def sso_sign_in(body: SsoRequest, request: Request, response: Response) -> TokenPair:
    """Swap a sign-in token from the client's operational system for a session of the
    user with the same email (services/sso.py). No 2FA step here: the operational system
    signed the user in (owners need its 2FA, see `require_mfa`). Each token works once."""
    try:
        found = sso.find_integration(body.token)
    except sso.SsoError as exc:
        raise unauthorized("invalid sign-in token") from exc
    user_id: uuid.UUID | None = None
    with tenant_session(found.tenant_id) as db:
        integration = db.get_one(SsoIntegration, found.id)
        details = {"via": "sso", "integration": integration.name}
        if body.embedded:
            details["embedded"] = "yes"
        try:
            claims = sso.verify(db, integration, body.token)
            details["subject"] = str(claims.get("sub", ""))[:100]
            user_id = sso.find_user(db, integration, claims).id
        except sso.SsoError as exc:
            record(
                db,
                None,
                AuditAction.LOGIN_FAILED,
                "sso_integration",
                integration.id,
                request,
                {**details, "reason": str(exc)[:200]},
                tenant_id=found.tenant_id,
            )
        else:
            integration.last_used_at = _now()
    if user_id is None:  # raised after the block so the audit entry is committed
        raise unauthorized("invalid sign-in token")
    # Sessions are only visible to their own user (row-level security).
    with tenant_session(found.tenant_id, user_id) as db:
        user = db.get_one(User, user_id)
        _audit_login(db, AuditAction.LOGIN, found.tenant_id, user_id, request, details)
        result = _issue_tokens(db, user, request)
    return result if body.embedded else _set_refresh_cookie(response, result)


@router.post("/refresh", response_model=TokenPair)
def refresh(
    response: Response,
    body: TokenRequest | None = None,
    cookie: Annotated[str | None, Cookie(alias=REFRESH_COOKIE)] = None,
) -> TokenPair:
    """Send the refresh token in the body, or rely on the cookie set at login."""
    settings = get_settings()
    token = body.token if body is not None else cookie
    if not token:
        raise unauthorized("refresh token required")
    try:
        claims = decode_token(token, "refresh")
        user_id = uuid.UUID(claims["sub"])
        tenant_id = uuid.UUID(claims["tid"]) if claims.get("tid") else None
        session_id = uuid.UUID(claims["sid"])
        refresh_id = claims["rti"]
    except (InvalidTokenError, KeyError, ValueError) as exc:
        raise unauthorized("invalid or expired refresh token") from exc

    result: TokenPair | None = None
    with tenant_session(tenant_id, user_id) as db:
        session_row = db.get(UserSession, session_id)
        user = db.get(User, user_id)
        now = _now()
        if session_row is None or user is None or not user.is_active:
            raise unauthorized("session ended")
        if session_row.revoked_at is not None or session_row.expires_at <= now:
            raise unauthorized("session ended")
        idle = now - session_row.last_used_at > timedelta(minutes=settings.session_idle_minutes)
        # A replayed (already rotated) refresh token means it may have been stolen.
        if session_row.refresh_jti != refresh_id or idle:
            session_row.revoked_at = now
        else:
            session_row.refresh_jti = uuid.uuid4().hex
            session_row.last_used_at = now
            result = _token_pair(user, session_row)
    if result is None:  # raised after the block so the revocation is committed
        raise unauthorized("session ended")
    return _set_refresh_cookie(response, result)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(principal: Annotated[Principal, Depends(get_principal)], response: Response) -> None:
    _clear_refresh_cookie(response)
    if principal.session_id is None:
        return
    with tenant_session(principal.tenant_id, principal.user_id) as db:
        db.execute(
            update(UserSession)
            .where(UserSession.id == principal.session_id)
            .values(revoked_at=_now())
        )


@router.get("/me", response_model=Me)
def me(principal: Annotated[Principal, Depends(get_principal)]) -> Me:
    if principal.user_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "API keys have no user")
    with tenant_session(principal.tenant_id, principal.user_id) as db:
        user = db.get_one(User, principal.user_id)
        return Me(
            id=user.id,
            email=user.email,
            full_name=user.full_name,
            role=UserRole(user.role),
            tenant_id=user.tenant_id,
            mfa_enabled=user.totp_secret_encrypted is not None,
            permissions=sorted(p.value for p in principal.permissions),
            branch_ids=sorted(principal.branch_ids) if principal.branch_ids is not None else None,
        )


def send_password_link(user: User, mailer: Mailer, purpose: str = "reset") -> str:
    """Email a set-password link (reset or invitation). Returns the token (for tests)."""
    settings = get_settings()
    token = encode_token(
        "reset",
        {
            "sub": str(user.id),
            "tid": str(user.tenant_id) if user.tenant_id else None,
            "fp": password_fingerprint(user.password_hash, user.password_changed_at),
        },
        timedelta(minutes=settings.password_reset_minutes if purpose == "reset" else 72 * 60),
    )
    link = f"{settings.web_url}/set-password?token={token}"
    subject = "Reset your password" if purpose == "reset" else "You have been invited"
    mailer.send(user.email, subject, f"Open this link to set your password:\n{link}\n")
    return token


@router.post("/password/forgot", status_code=status.HTTP_202_ACCEPTED)
def forgot_password(
    body: ForgotRequest, mailer: Annotated[Mailer, Depends(get_mailer)]
) -> dict[str, str]:
    found = _find_user(body.email)
    if found is not None:
        with tenant_session(found[1], found[0]) as db:
            user = db.get_one(User, found[0])
            if user.is_active:
                send_password_link(user, mailer)
    # Same answer either way, so the endpoint can't be used to find accounts.
    return {"status": "if the account exists, an email has been sent"}


@router.post("/password/reset", status_code=status.HTTP_204_NO_CONTENT)
def reset_password(body: ResetRequest) -> None:
    try:
        claims = decode_token(body.token, "reset")
        user_id = uuid.UUID(claims["sub"])
        tenant_id = uuid.UUID(claims["tid"]) if claims.get("tid") else None
    except (InvalidTokenError, KeyError, ValueError) as exc:
        raise unauthorized("invalid or expired link") from exc
    with tenant_session(tenant_id, user_id) as db:
        user = db.get(User, user_id)
        if user is None or not user.is_active:
            raise unauthorized("invalid or expired link")
        if claims.get("fp") != password_fingerprint(user.password_hash, user.password_changed_at):
            raise unauthorized("this link has already been used")
        _set_password(db, user, body.new_password)


@router.post("/password/change", status_code=status.HTTP_204_NO_CONTENT)
def change_password(
    body: ChangePasswordRequest, principal: Annotated[Principal, Depends(get_principal)]
) -> None:
    if principal.user_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "API keys have no password")
    with tenant_session(principal.tenant_id, principal.user_id) as db:
        user = db.get_one(User, principal.user_id)
        if not verify_password(user.password_hash, body.current_password):
            raise unauthorized("wrong password")
        _set_password(db, user, body.new_password, keep_session=principal.session_id)


def _set_password(
    db: Session, user: User, password: str, keep_session: uuid.UUID | None = None
) -> None:
    now = _now()
    user.password_hash = hash_password(password)
    user.password_changed_at = now
    user.failed_logins = 0
    user.locked_until = None
    # Sign out everywhere else.
    statement = update(UserSession).where(
        UserSession.user_id == user.id, UserSession.revoked_at.is_(None)
    )
    if keep_session is not None:
        statement = statement.where(UserSession.id != keep_session)
    db.execute(statement.values(revoked_at=now))
