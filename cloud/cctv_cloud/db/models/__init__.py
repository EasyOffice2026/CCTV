"""All ORM models; importing this module registers every table on Base.metadata."""

from cctv_cloud.db.models.alerts import (
    AlertDelivery,
    AlertSubscription,
    AuditLog,
    PushSubscription,
)
from cctv_cloud.db.models.devices import (
    Camera,
    CameraHealthMinute,
    EdgeDevice,
    EdgeRelease,
    HealthIncident,
    Nvr,
    Zone,
)
from cctv_cloud.db.models.events import AttendanceRecord, CountBucket, Event, HeatmapBucket
from cctv_cloud.db.models.features import Feature, FeatureSetting
from cctv_cloud.db.models.integrations import (
    PosSale,
    SsoIntegration,
    SsoTokenUse,
    Webhook,
    WebhookDelivery,
)
from cctv_cloud.db.models.people import (
    ApiKey,
    FaceTemplate,
    Staff,
    User,
    UserBranchAccess,
    UserSession,
)
from cctv_cloud.db.models.reports import PilotNote, Report
from cctv_cloud.db.models.tenancy import Branch, Shift, Tenant, WorkingHours

__all__ = [
    "AlertDelivery",
    "AlertSubscription",
    "ApiKey",
    "AttendanceRecord",
    "AuditLog",
    "Branch",
    "Camera",
    "CameraHealthMinute",
    "CountBucket",
    "EdgeDevice",
    "EdgeRelease",
    "Event",
    "FaceTemplate",
    "Feature",
    "FeatureSetting",
    "HealthIncident",
    "HeatmapBucket",
    "Nvr",
    "PilotNote",
    "PosSale",
    "PushSubscription",
    "Report",
    "Shift",
    "SsoIntegration",
    "SsoTokenUse",
    "Staff",
    "Tenant",
    "User",
    "UserBranchAccess",
    "UserSession",
    "Webhook",
    "WebhookDelivery",
    "WorkingHours",
    "Zone",
]
