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


class SsoAlgorithm(str, Enum):
    """How the operational system signs its sign-in tokens (plan 8.1)."""

    HS256 = "HS256"  # shared secret
    RS256 = "RS256"  # RSA key pair; we hold the public key
    ES256 = "ES256"  # EC P-256 key pair; we hold the public key


class WebhookTopic(str, Enum):
    EVENT_NEW = "event.new"
    EVENT_CLOSED = "event.closed"
    REPORT_READY = "report.ready"


class PilotNoteKind(str, Enum):
    """Pilot log entries (plan 10.4)."""

    CHECK_IN = "check_in"  # weekly meeting with the owner
    FEEDBACK = "feedback"  # what the client said
    CHANGE = "change"  # a tuning or set-up change, and why
    ISSUE = "issue"  # something wrong on site (camera moved, internet down, ...)
    DECISION = "decision"  # e.g. a feature passes, or its pilot is extended
