import type { Schemas } from "@/lib/api";

/** Mirrors cloud/cctv_cloud/rbac.py. The API enforces these; the UI only hides what
 * a role can't use. */
export type Permission =
  | "platform_admin"
  | "tenant_admin"
  | "branch_edit"
  | "devices_manage"
  | "settings_manage"
  | "events_view"
  | "events_review"
  | "media_view"
  | "media_export"
  | "live_view"
  | "staff_manage"
  | "reports_view"
  | "kpis_view"
  | "payroll_export"
  | "pos_write";

export type Me = Schemas["Me"];

export function can(me: Me | null | undefined, permission: Permission): boolean {
  return !!me && me.permissions.includes(permission);
}

export function canAny(me: Me | null | undefined, ...permissions: Permission[]): boolean {
  return permissions.some((p) => can(me, p));
}
