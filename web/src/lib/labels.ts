import type { Schemas } from "@/lib/api";
import type { MessageKey } from "@/lib/messages/en";
import type { Tone } from "@/components/ui";

export type EventType = Schemas["EventType"];
export type FeatureKey = Schemas["FeatureKey"];

/** Which feature each event type belongs to (mirrors cctv_contracts EVENT_FEATURE). */
export const EVENT_FEATURE: Record<EventType, FeatureKey> = {
  staff_check_in: "attendance",
  staff_check_out: "attendance",
  phone_use: "staff_activity",
  staff_idle: "staff_activity",
  left_assigned_area: "staff_activity",
  missing_mask: "kitchen_compliance",
  missing_gloves: "kitchen_compliance",
  missing_hair_cap: "kitchen_compliance",
  hygiene_rule_break: "kitchen_compliance",
  drawer_open: "cashier",
  counter_unattended: "cashier",
  occupancy_high: "customer_count",
  queue_long: "queue",
  after_hours_person: "security",
  after_hours_motion: "security",
  restricted_area_entry: "security",
  object_left: "object_left_removed",
  object_removed: "object_left_removed",
};

export const EVENT_TYPES = Object.keys(EVENT_FEATURE) as EventType[];
export const FEATURES: FeatureKey[] = [
  "security",
  "kitchen_compliance",
  "cashier",
  "attendance",
  "staff_activity",
  "customer_count",
  "queue",
  "heatmap",
  "object_left_removed",
];
export const AUDIT_ACTIONS: Schemas["AuditAction"][] = [
  "login",
  "login_failed",
  "view_snapshot",
  "view_clip",
  "view_live",
  "view_playback",
  "view_frame",
  "export_snapshot",
  "export_clip",
  "export_events",
  "export_report",
  "export_audit",
  "review_event",
  "settings_change",
  "user_change",
  "device_change",
  "api_key_change",
  "staff_change",
  "consent_change",
  "face_enrol",
  "face_delete",
  "attendance_change",
];
export const SEVERITIES: Schemas["Severity"][] = ["critical", "warning", "info"];
export const REVIEW_STATUSES: Schemas["ReviewStatus"][] = ["new", "valid", "false_alarm", "reviewed"];
export const ZONE_TYPES: Schemas["ZoneType"][] = [
  "kitchen",
  "counter",
  "cash_drawer",
  "queue",
  "dining",
  "staff_area",
  "restricted",
  "ignore",
  "entrance_line",
];
export const CAMERA_ROLES: Schemas["CameraRole"][] = [
  "entrance",
  "staff_entrance",
  "kitchen",
  "counter",
  "dining",
  "storage",
  "outdoor",
  "other",
];
export const TENANT_ROLES: Exclude<Schemas["UserRole"], "super_admin">[] = [
  "owner",
  "branch_manager",
  "reviewer",
  "installer",
];

export const SEVERITY_TONE: Record<Schemas["Severity"], Tone> = {
  critical: "red",
  warning: "amber",
  info: "blue",
};

export const REVIEW_TONE: Record<Schemas["ReviewStatus"], Tone> = {
  new: "slate",
  valid: "red",
  false_alarm: "green",
  reviewed: "blue",
};

export const HEALTH_TONE: Record<string, Tone> = {
  online: "green",
  degraded: "amber",
  offline: "red",
  pending: "slate",
  retired: "slate",
};

/** Typed message keys for enum values. */
export const key = {
  eventType: (v: string) => `eventType.${v}` as MessageKey,
  feature: (v: string) => `feature.${v}` as MessageKey,
  severity: (v: string) => `severity.${v}` as MessageKey,
  review: (v: string) => `review.${v}` as MessageKey,
  eventStatus: (v: string) => `eventStatus.${v}` as MessageKey,
  zoneType: (v: string) => `zoneType.${v}` as MessageKey,
  role: (v: string) => `role.${v}` as MessageKey,
  cameraRole: (v: string) => `cameraRole.${v}` as MessageKey,
  health: (v: string) => `health.${v}` as MessageKey,
  deviceStatus: (v: string) => `deviceStatus.${v}` as MessageKey,
  weekday: (v: number) => `weekday.${v}` as MessageKey,
  auditAction: (v: string) => `auditAction.${v}` as MessageKey,
  auditTarget: (v: string) => `auditTarget.${v}` as MessageKey,
};
