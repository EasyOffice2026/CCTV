"use client";

import { usePathname, useRouter } from "next/navigation";
import { useEffect } from "react";

import { AppShell } from "@/components/AppShell";
import { Notice, Spinner } from "@/components/ui";
import { useAuth } from "@/lib/auth";
import { useI18n } from "@/lib/i18n";

/** Pages a super-admin can use before choosing a client (they cover every client). */
const TENANTLESS = ["/admin/tenants", "/admin/health", "/admin/software", "/account"];

export default function SignedInLayout({ children }: { children: React.ReactNode }) {
  const { status, me, tenantId } = useAuth();
  const { t } = useI18n();
  const router = useRouter();
  const pathname = usePathname();

  useEffect(() => {
    // Keep the query (e.g. ?event=... from an alert link) for after sign-in.
    if (status === "anonymous") router.replace(`/login/?next=${encodeURIComponent(pathname + window.location.search)}`);
  }, [status, router, pathname]);

  if (status !== "signed-in" || !me) return <Spinner />;
  const needsTenant = !tenantId && !TENANTLESS.some((p) => pathname.startsWith(p));
  return <AppShell>{needsTenant ? <Notice tone="amber">{t("shell.tenantNeeded")}</Notice> : children}</AppShell>;
}
