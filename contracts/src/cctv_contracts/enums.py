"""Enums shared by edge and cloud.

Values are stored in the database and sent over the wire: add new values freely,
but never rename or remove an existing one.
"""

from enum import Enum


class FeatureKey(str, Enum):
    ATTENDANCE = "attendance"
    STAFF_ACTIVITY = "staff_activity"
    KITCHEN_COMPLIANCE = "kitchen_compliance"
    CASHIER = "cashier"
    CUSTOMER_COUNT = "customer_count"
    QUEUE = "queue"
    SECURITY = "security"
    HEATMAP = "heatmap"
    OBJECT_LEFT_REMOVED = "object_left_removed"


class EventType(str, Enum):
    # attendance
    STAFF_CHECK_IN = "staff_check_in"
    STAFF_CHECK_OUT = "staff_check_out"
    # staff activity
    PHONE_USE = "phone_use"
    STAFF_IDLE = "staff_idle"
    LEFT_ASSIGNED_AREA = "left_assigned_area"
    # kitchen compliance
    MISSING_MASK = "missing_mask"
    MISSING_GLOVES = "missing_gloves"
    MISSING_HAIR_CAP = "missing_hair_cap"
    HYGIENE_RULE_BREAK = "hygiene_rule_break"
    # cashier
    DRAWER_OPEN = "drawer_open"
    COUNTER_UNATTENDED = "counter_unattended"
    # customer count (in/out counts are sent as aggregates, not events)
    OCCUPANCY_HIGH = "occupancy_high"
    # queue
    QUEUE_LONG = "queue_long"
    # security
    AFTER_HOURS_PERSON = "after_hours_person"
    AFTER_HOURS_MOTION = "after_hours_motion"
    RESTRICTED_AREA_ENTRY = "restricted_area_entry"
    # object left / removed (later phase)
    OBJECT_LEFT = "object_left"
    OBJECT_REMOVED = "object_removed"


EVENT_FEATURE: dict[EventType, FeatureKey] = {
    EventType.STAFF_CHECK_IN: FeatureKey.ATTENDANCE,
    EventType.STAFF_CHECK_OUT: FeatureKey.ATTENDANCE,
    EventType.PHONE_USE: FeatureKey.STAFF_ACTIVITY,
    EventType.STAFF_IDLE: FeatureKey.STAFF_ACTIVITY,
    EventType.LEFT_ASSIGNED_AREA: FeatureKey.STAFF_ACTIVITY,
    EventType.MISSING_MASK: FeatureKey.KITCHEN_COMPLIANCE,
    EventType.MISSING_GLOVES: FeatureKey.KITCHEN_COMPLIANCE,
    EventType.MISSING_HAIR_CAP: FeatureKey.KITCHEN_COMPLIANCE,
    EventType.HYGIENE_RULE_BREAK: FeatureKey.KITCHEN_COMPLIANCE,
    EventType.DRAWER_OPEN: FeatureKey.CASHIER,
    EventType.COUNTER_UNATTENDED: FeatureKey.CASHIER,
    EventType.OCCUPANCY_HIGH: FeatureKey.CUSTOMER_COUNT,
    EventType.QUEUE_LONG: FeatureKey.QUEUE,
    EventType.AFTER_HOURS_PERSON: FeatureKey.SECURITY,
    EventType.AFTER_HOURS_MOTION: FeatureKey.SECURITY,
    EventType.RESTRICTED_AREA_ENTRY: FeatureKey.SECURITY,
    EventType.OBJECT_LEFT: FeatureKey.OBJECT_LEFT_REMOVED,
    EventType.OBJECT_REMOVED: FeatureKey.OBJECT_LEFT_REMOVED,
}


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class EventStatus(str, Enum):
    OPEN = "open"  # still happening; the edge will send an update with the same event_id
    CLOSED = "closed"


class MediaKind(str, Enum):
    SNAPSHOT = "snapshot"
    CLIP = "clip"


class CameraRole(str, Enum):
    ENTRANCE = "entrance"
    STAFF_ENTRANCE = "staff_entrance"
    KITCHEN = "kitchen"
    COUNTER = "counter"
    DINING = "dining"
    STORAGE = "storage"
    OUTDOOR = "outdoor"
    OTHER = "other"


class CameraHealth(str, Enum):
    ONLINE = "online"
    DEGRADED = "degraded"  # streaming, but below target fps or with decode errors
    OFFLINE = "offline"


class ZoneType(str, Enum):
    KITCHEN = "kitchen"
    COUNTER = "counter"
    CASH_DRAWER = "cash_drawer"
    QUEUE = "queue"
    DINING = "dining"
    STAFF_AREA = "staff_area"
    RESTRICTED = "restricted"
    IGNORE = "ignore"  # detections inside are dropped (TV screens, reflections, street)
    ENTRANCE_LINE = "entrance_line"  # 2-point line for in/out counting


LINE_ZONE_TYPES = frozenset({ZoneType.ENTRANCE_LINE})


class RemoteKind(str, Enum):
    """Requests the cloud passes to an edge box (see cctv_contracts.v1.remote)."""

    LIVE = "live"
    PLAYBACK = "playback"
    RECORDINGS = "recordings"
    FRAME = "frame"
    FACE_EMBEDDING = "face_embedding"


class UpdateState(str, Enum):
    """What the box's updater last did with a software update (plan 2.10)."""

    UPDATING = "updating"  # downloading, checking the signature or waiting for a heartbeat
    UPDATED = "updated"
    FAILED = "failed"  # not switched: download or signature check failed
    ROLLED_BACK = "rolled_back"  # switched, not healthy in time, back on the previous version
