"""Request authentication and the per-request database session.

Every authenticated request runs in one transaction bound to the caller's tenant via
row-level security; permission and branch checks happen on top of that.
"""

import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated

from fastapi import BackgroundTasks, Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from cctv_cloud.db.enums import UserRole
from cctv_cloud.db.models import ApiKey, User, UserBranchAccess, UserSession
from cctv_cloud.db.session import tenant_session
from cctv_cloud.rbac import (
    ALL_BRANCH_ROLES,
    API_KEY_SCOPES,
    ROLE_PERMISSIONS,
    Permission,
    Principal,
)
from cctv_cloud.security import InvalidTokenError, decode_token, sha256_hex
from cctv_cloud.services.notify import Publisher, get_publisher

API_KEY_HEADER = "X-API-Key"
TENANT_HEADER = "X-Tenant-Id"

_bearer = HTTPBearer(auto_error=False)


def unauthorized(detail: str = "not authenticated") -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED, detail, headers={"WWW-Authenticate": "Bearer"}
    )


def forbidden(detail: str = "not allowed") -> HTTPException:
    return HTTPException(status.HTTP_403_FORBIDDEN, detail)


def not_found(what: str = "not found") -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, what)


def _user_principal(token: str, tenant_header: uuid.UUID | None) -> Principal:
    try:
        claims = decode_token(token, "access")
        user_id = uuid.UUID(claims["sub"])
        session_id = uuid.UUID(claims["sid"])
        token_tenant = uuid.UUID(claims["tid"]) if claims.get("tid") else None
        role = UserRole(claims["role"])
    except (InvalidTokenError, KeyError, ValueError) as exc:
        raise unauthorized("invalid or expired token") from exc

    with tenant_session(token_tenant, user_id) as session:
        row = session.execute(
            select(User.is_active, User.role, UserSession.revoked_at, UserSession.expires_at)
            .join(UserSession, UserSession.user_id == User.id)
            .where(User.id == user_id, UserSession.id == session_id)
        ).one_or_none()
        if row is None or not row.is_active or row.revoked_at is not None:
            raise unauthorized("session ended")
        if row.expires_at <= datetime.now(timezone.utc):
            raise unauthorized("session expired")
        role = UserRole(row.role)  # a role change applies immediately
        branch_ids = None
        if role not in ALL_BRANCH_ROLES:
            branch_ids = frozenset(
                session.scalars(
                    select(UserBranchAccess.branch_id).where(UserBranchAccess.user_id == user_id)
                )
            )

    tenant_id = token_tenant
    if role is UserRole.SUPER_ADMIN:
        tenant_id = tenant_header  # our team picks the tenant to act on per request
    elif tenant_header is not None and tenant_header != token_tenant:
        raise forbidden("not a member of that tenant")
    return Principal(
        tenant_id=tenant_id,
        permissions=ROLE_PERMISSIONS[role],
        branch_ids=branch_ids,
        user_id=user_id,
        role=role,
        session_id=session_id,
    )


def _api_key_principal(key: str) -> Principal:
    # Format: cctv_<prefix>_<secret>
    parts = key.split("_", 2)  # the secret part may itself contain "_"
    if len(parts) != 3 or parts[0] != "cctv":
        raise unauthorized("invalid API key")
    prefix = parts[1]
    with tenant_session(None) as session:
        found = session.execute(
            text("SELECT api_key_id, tenant_id FROM auth_find_api_key(:prefix)"), {"prefix": prefix}
        ).one_or_none()
    if found is None:
        raise unauthorized("invalid API key")
    with tenant_session(found.tenant_id) as session:
        api_key = session.get(ApiKey, found.api_key_id)
        if api_key is None or api_key.key_hash != sha256_hex(key) or api_key.revoked_at:
            raise unauthorized("invalid API key")
        api_key.last_used_at = func.now()
        permissions = frozenset().union(
            *(API_KEY_SCOPES.get(s, frozenset()) for s in api_key.scopes)
        )
    return Principal(
        tenant_id=found.tenant_id,
        permissions=permissions,
        branch_ids=None,
        api_key_id=found.api_key_id,
    )


def get_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    api_key: Annotated[str | None, Header(alias=API_KEY_HEADER)] = None,
    tenant_header: Annotated[uuid.UUID | None, Header(alias=TENANT_HEADER)] = None,
) -> Principal:
    if api_key:
        return _api_key_principal(api_key)
    if credentials is None:
        raise unauthorized()
    return _user_principal(credentials.credentials, tenant_header)


def require(*permissions: Permission) -> Callable[..., Principal]:
    """Dependency: the caller must hold every listed permission and have a tenant."""

    def check(principal: Annotated[Principal, Depends(get_principal)]) -> Principal:
        if not all(principal.can(p) for p in permissions):
            raise forbidden()
        if principal.tenant_id is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"choose a tenant with the {TENANT_HEADER} header"
            )
        return principal

    return check


def require_platform(principal: Annotated[Principal, Depends(get_principal)]) -> Principal:
    if not principal.can(Permission.PLATFORM_ADMIN):
        raise forbidden()
    return principal


@dataclass
class Context:
    principal: Principal
    db: Session


def context(*permissions: Permission) -> Callable[..., Iterator[Context]]:
    """Dependency: the caller (holding every permission in `permissions`) and a database
    session bound to their tenant. The session commits after the endpoint returns."""
    checker = require(*permissions)

    def dependency(principal: Annotated[Principal, Depends(checker)]) -> Iterator[Context]:
        with tenant_session(principal.tenant_id, principal.user_id) as session:
            yield Context(principal, session)

    return dependency


def notify_after_commit(
    background: BackgroundTasks, publisher: Publisher, channel: str, message: dict
) -> None:
    """Background tasks run after the response, i.e. after the request transaction commits."""
    background.add_task(publisher.publish, channel, message)


PublisherDep = Annotated[Publisher, Depends(get_publisher)]
