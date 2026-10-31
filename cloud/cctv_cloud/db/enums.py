"""Cloud-only enums (shared edge/cloud enums live in cctv_contracts.enums)."""

from enum import Enum


class TenantStatus(str, Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


class UserRole(str, Enum):
    SUPER_ADMIN = "super_admin"  # our team; not bound to a tenant
    OWNER = "owner"
    BRANCH_MANAGER = "branch_manager"
    REVIEWER = "reviewer"
    INSTALLER = "installer"


class DeviceStatus(str, Enum):
    PENDING = "pending"  # registered, not yet paired
    ONLINE = "online"
    OFFLINE = "offline"
    RETIRED = "retired"


class FeatureScope(str, Enum):
    TENANT = "tenant"
    BRANCH = "branch"
    CAMERA = "camera"


class FeatureAvailability(str, Enum):
    PILOT = "pilot"
    LATER = "later"


class StaffStatus(str, Enum):
    ACTIVE = "active"
    LEFT = "left"


class ReviewStatus(str, Enum):
    NEW = "new"
    VALID = "valid"
    FALSE_ALARM = "false_alarm"
    REVIEWED = "reviewed"


class AttendanceStatus(str, Enum):
    ON_TIME = "on_time"
    LATE = "late"
    ABSENT = "absent"
    DAY_OFF = "day_off"


class AttendanceSource(str, Enum):
    FACE = "face"
    MANUAL = "manual"


class AlertChannel(str, Enum):
    WEB = "web"
    WHATSAPP = "whatsapp"
    EMAIL = "email"


class DeliveryStatus(str, Enum):
    QUEUED = "queued"
    SENT = "sent"  # accepted by the channel (published, push service, WhatsApp API)
    DELIVERED = "delivered"  # the channel confirmed delivery to the phone (WhatsApp webhook)
    FAILED = "failed"


class AlertKind(str, Enum):
    ALERT = "alert"
    RESOLVED = "resolved"  # the event ended (optional, per alert rule)


class Locale(str, Enum):
    EN = "en"
    AR = "ar"


class ReportPeriod(str, Enum):
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
