"""Roles and permissions (plan §1 #2). Row-level security still limits every query to the
caller's tenant; these checks decide what the caller may do inside it."""

import uuid
from dataclasses import dataclass
from enum import Enum

from cctv_cloud.db.enums import UserRole


class Permission(str, Enum):
    PLATFORM_ADMIN = "platform_admin"  # tenants; our team only
    TENANT_ADMIN = "tenant_admin"  # tenant settings, branches, users, API keys, audit log
    BRANCH_EDIT = "branch_edit"  # working hours of accessible branches
    DEVICES_MANAGE = "devices_manage"  # NVRs, cameras, zones, edge devices
    SETTINGS_MANAGE = "settings_manage"  # feature on/off and thresholds
    EVENTS_VIEW = "events_view"
    EVENTS_REVIEW = "events_review"
    MEDIA_VIEW = "media_view"
    MEDIA_EXPORT = "media_export"  # download clips/snapshots, export event lists
    LIVE_VIEW = "live_view"  # live view and NVR playback (through the edge box)
    STAFF_MANAGE = "staff_manage"  # staff, shifts, consent and face enrolment


_ALL = frozenset(Permission)

ROLE_PERMISSIONS: dict[UserRole, frozenset[Permission]] = {
    UserRole.SUPER_ADMIN: _ALL,
    UserRole.OWNER: _ALL - {Permission.PLATFORM_ADMIN},
    UserRole.BRANCH_MANAGER: frozenset(
        {
            Permission.BRANCH_EDIT,
            Permission.SETTINGS_MANAGE,
            Permission.EVENTS_VIEW,
            Permission.EVENTS_REVIEW,
            Permission.MEDIA_VIEW,
            Permission.MEDIA_EXPORT,
            Permission.LIVE_VIEW,
            Permission.STAFF_MANAGE,
        }
    ),
    UserRole.REVIEWER: frozenset(
        {Permission.EVENTS_VIEW, Permission.EVENTS_REVIEW, Permission.MEDIA_VIEW}
    ),
    UserRole.INSTALLER: frozenset({Permission.DEVICES_MANAGE}),
}

# Roles that see every branch of the tenant; others only branches in user_branch_access.
ALL_BRANCH_ROLES = frozenset({UserRole.SUPER_ADMIN, UserRole.OWNER})
# Roles that must use two-factor authentication (plan 3.1).
MFA_REQUIRED_ROLES = frozenset({UserRole.SUPER_ADMIN, UserRole.OWNER})

# Scopes an integration API key can have.
API_KEY_SCOPES: dict[str, frozenset[Permission]] = {
    "events:read": frozenset({Permission.EVENTS_VIEW}),
}


@dataclass(frozen=True)
class Principal:
    """Who is calling, for which tenant, and what they may do."""

    tenant_id: uuid.UUID | None  # the tenant acted on (a super-admin picks one per request)
    permissions: frozenset[Permission]
    branch_ids: frozenset[uuid.UUID] | None  # None = every branch of the tenant
    user_id: uuid.UUID | None = None
    role: UserRole | None = None
    session_id: uuid.UUID | None = None
    api_key_id: uuid.UUID | None = None

    def can(self, permission: Permission) -> bool:
        return permission in self.permissions

    def can_access_branch(self, branch_id: uuid.UUID) -> bool:
        return self.branch_ids is None or branch_id in self.branch_ids
