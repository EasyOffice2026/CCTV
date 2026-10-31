"""All ORM models; importing this module registers every table on Base.metadata."""

from cctv_cloud.db.models.alerts import AlertDelivery, AlertSubscription, AuditLog
from cctv_cloud.db.models.devices import Camera, EdgeDevice, Nvr, Zone
from cctv_cloud.db.models.events import AttendanceRecord, CountBucket, Event
from cctv_cloud.db.models.features import Feature, FeatureSetting
from cctv_cloud.db.models.people import (
    ApiKey,
    FaceTemplate,
    Staff,
    User,
    UserBranchAccess,
    UserSession,
)
from cctv_cloud.db.models.tenancy import Branch, Shift, Tenant, WorkingHours

__all__ = [
    "AlertDelivery",
    "AlertSubscription",
    "ApiKey",
    "AttendanceRecord",
    "AuditLog",
    "Branch",
    "Camera",
    "CountBucket",
    "EdgeDevice",
    "Event",
    "FaceTemplate",
    "Feature",
    "FeatureSetting",
    "Nvr",
    "Shift",
    "Staff",
    "Tenant",
    "User",
    "UserBranchAccess",
    "UserSession",
    "WorkingHours",
    "Zone",
]
