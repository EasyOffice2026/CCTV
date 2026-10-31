"use client";

import { Notice } from "@/components/ui";
import { useAuth } from "@/lib/auth";
import { useI18n } from "@/lib/i18n";
import { canAny, type Permission } from "@/lib/permissions";

/** Shows the page only to users holding one of `anyOf` (the API checks again). */
export function RequirePermission({ anyOf, children }: { anyOf: Permission[]; children: React.ReactNode }) {
  const { me } = useAuth();
  const { t } = useI18n();
  if (!canAny(me, ...anyOf)) return <Notice tone="amber">{t("common.forbidden")}</Notice>;
  return <>{children}</>;
}
