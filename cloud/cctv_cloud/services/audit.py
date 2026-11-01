"""Audit log: who viewed, exported or changed what (plan §7.3).

Entries are written in the same transaction as the action, so a view that is recorded
is a view that happened, and the app role can only insert and read them.
"""

import ipaddress
from enum import Enum
from typing import Any

from fastapi import Request
from sqlalchemy.orm import Session

from cctv_cloud.db.models import AuditLog
from cctv_cloud.rbac import Principal


def client_ip(request: Request | None) -> str | None:
    """The caller's IP, or None when it isn't one (e.g. a test client or a unix socket)."""
    host = request.client.host if request is not None and request.client else None
    try:
        return str(ipaddress.ip_address(host)) if host else None
    except ValueError:
        return None


class AuditAction(str, Enum):
    LOGIN = "login"
    LOGIN_FAILED = "login_failed"
    VIEW_SNAPSHOT = "view_snapshot"
    VIEW_CLIP = "view_clip"
    EXPORT_SNAPSHOT = "export_snapshot"
    EXPORT_CLIP = "export_clip"
    REVIEW_EVENT = "review_event"
    SETTINGS_CHANGE = "settings_change"
    USER_CHANGE = "user_change"
    DEVICE_CHANGE = "device_change"
    API_KEY_CHANGE = "api_key_change"
    VIEW_LIVE = "view_live"
    VIEW_PLAYBACK = "view_playback"
    VIEW_FRAME = "view_frame"
    EXPORT_EVENTS = "export_events"
    STAFF_CHANGE = "staff_change"
    CONSENT_CHANGE = "consent_change"
    FACE_ENROL = "face_enrol"
    FACE_DELETE = "face_delete"
    ATTENDANCE_CHANGE = "attendance_change"
    EXPORT_REPORT = "export_report"
    EXPORT_AUDIT = "export_audit"
    SSO_CHANGE = "sso_change"
    WEBHOOK_CHANGE = "webhook_change"
    SOFTWARE_UPDATE = "software_update"


def record(
    session: Session,
    principal: Principal | None,
    action: AuditAction,
    target_type: str | None = None,
    target_id: object = None,
    request: Request | None = None,
    details: dict[str, Any] | None = None,
    *,
    tenant_id=None,
    user_id=None,
) -> None:
    session.add(
        AuditLog(
            tenant_id=tenant_id or (principal.tenant_id if principal else None),
            user_id=user_id or (principal.user_id if principal else None),
            action=action.value,
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            ip=client_ip(request),
            user_agent=(request.headers.get("user-agent") or "")[:500] if request else None,
            details={
                **(details or {}),
                **(
                    {"api_key_id": str(principal.api_key_id)}
                    if principal and principal.api_key_id
                    else {}
                ),
            },
        )
    )
