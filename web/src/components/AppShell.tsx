"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";

import { AlertToasts } from "@/components/AlertToasts";
import { Select } from "@/components/ui";
import { useAuth } from "@/lib/auth";
import { useBranches } from "@/lib/branches";
import { useI18n } from "@/lib/i18n";
import type { MessageKey } from "@/lib/messages/en";
import { can, canAny, type Permission } from "@/lib/permissions";

type NavItem = { href: string; label: MessageKey; anyOf: Permission[] };

const NAV: { group: MessageKey; items: NavItem[] }[] = [
  {
    group: "nav.monitoring",
    items: [
      { href: "/overview/", label: "nav.overview", anyOf: ["kpis_view"] },
      { href: "/events/", label: "nav.events", anyOf: ["events_view"] },
      { href: "/search/", label: "nav.search", anyOf: ["events_view"] },
      { href: "/live/", label: "nav.live", anyOf: ["live_view"] },
      { href: "/playback/", label: "nav.playback", anyOf: ["live_view"] },
      { href: "/reports/", label: "nav.reports", anyOf: ["reports_view"] },
    ],
  },
  {
    group: "nav.settings",
    items: [
      { href: "/settings/zones/", label: "nav.zones", anyOf: ["devices_manage", "settings_manage"] },
      { href: "/settings/features/", label: "nav.features", anyOf: ["settings_manage"] },
      { href: "/settings/hours/", label: "nav.hours", anyOf: ["branch_edit"] },
      { href: "/alerts/", label: "nav.alerts", anyOf: ["events_view"] },
    ],
  },
  {
    group: "nav.people",
    items: [
      { href: "/staff/", label: "nav.staff", anyOf: ["staff_manage"] },
      { href: "/attendance/", label: "nav.attendance", anyOf: ["staff_manage"] },
    ],
  },
  {
    group: "nav.admin",
    items: [
      { href: "/admin/tenants/", label: "nav.tenants", anyOf: ["platform_admin"] },
      { href: "/admin/branches/", label: "nav.branches", anyOf: ["tenant_admin"] },
      { href: "/admin/cameras/", label: "nav.cameras", anyOf: ["devices_manage"] },
      { href: "/admin/devices/", label: "nav.devices", anyOf: ["devices_manage"] },
      { href: "/admin/users/", label: "nav.users", anyOf: ["tenant_admin"] },
      { href: "/admin/integrations/", label: "nav.integrations", anyOf: ["tenant_admin"] },
      { href: "/audit/", label: "nav.audit", anyOf: ["tenant_admin"] },
    ],
  },
];

/** The first page this user may open (after login). */
export function homePath(me: Parameters<typeof canAny>[0]): string {
  for (const { items } of NAV) {
    const item = items.find((i) => canAny(me, ...i.anyOf));
    if (item) return item.href;
  }
  return "/account/";
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const { me, signOut, tenantId } = useAuth();
  const { branches, branchId, setBranchId } = useBranches();
  const { t, locale, setLocale } = useI18n();
  const pathname = usePathname();
  const [menuOpen, setMenuOpen] = useState(false);

  const nav = (
    <nav className="flex flex-col gap-5 p-3">
      {NAV.map(({ group, items }) => {
        const visible = items.filter((i) => canAny(me, ...i.anyOf));
        if (!visible.length) return null;
        return (
          <div key={group}>
            <p className="px-2 pb-1 text-xs font-semibold tracking-wide text-slate-400 uppercase">{t(group)}</p>
            <ul className="flex flex-col gap-0.5">
              {visible.map((item) => {
                const active = pathname === item.href || pathname === item.href.slice(0, -1);
                return (
                  <li key={item.href}>
                    <Link
                      href={item.href}
                      onClick={() => setMenuOpen(false)}
                      aria-current={active ? "page" : undefined}
                      className={`block rounded-md px-2 py-1.5 text-sm ${active ? "bg-brand-50 font-medium text-brand-700" : "text-slate-700 hover:bg-slate-100"}`}
                    >
                      {t(item.label)}
                    </Link>
                  </li>
                );
              })}
            </ul>
          </div>
        );
      })}
    </nav>
  );

  return (
    <div className="flex min-h-screen">
      <aside
        className={`fixed inset-y-0 start-0 z-30 w-60 shrink-0 border-e border-slate-200 bg-white transition-transform md:static md:translate-x-0 md:rtl:translate-x-0 ${menuOpen ? "translate-x-0" : "-translate-x-full rtl:translate-x-full"}`}
      >
        <div className="border-b border-slate-200 px-5 py-4">
          <p className="font-semibold text-slate-900">{t("app.name")}</p>
          <p className="text-xs text-slate-500">{t("app.tagline")}</p>
        </div>
        {nav}
      </aside>
      {menuOpen && (
        <button
          className="fixed inset-0 z-20 bg-slate-900/30 md:hidden"
          aria-label={t("common.close")}
          onClick={() => setMenuOpen(false)}
        />
      )}

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-10 flex flex-wrap items-center gap-3 border-b border-slate-200 bg-white px-4 py-2">
          <button
            className="rounded-md px-2 py-1 text-slate-600 hover:bg-slate-100 md:hidden"
            onClick={() => setMenuOpen(true)}
            aria-label={t("shell.menu")}
          >
            ☰
          </button>
          {tenantId && branches.length > 0 && (
            <Select
              aria-label={t("common.branch")}
              className="w-auto max-w-56"
              value={branchId ?? ""}
              onChange={(e) => setBranchId(e.target.value || null)}
            >
              <option value="">{t("shell.allBranches")}</option>
              {branches.map((b) => (
                <option key={b.id} value={b.id}>
                  {b.name}
                </option>
              ))}
            </Select>
          )}
          <div className="ms-auto flex items-center gap-3 text-sm">
            <button
              className="rounded-md px-2 py-1 text-slate-600 hover:bg-slate-100"
              onClick={() => setLocale(locale === "en" ? "ar" : "en")}
            >
              {t("shell.language")}
            </button>
            <Link href="/account/" className="hidden text-slate-700 hover:underline sm:inline">
              {me?.full_name}
            </Link>
            <button className="rounded-md px-2 py-1 text-slate-600 hover:bg-slate-100" onClick={() => void signOut()}>
              {t("auth.signOut")}
            </button>
          </div>
        </header>
        <main className="mx-auto w-full max-w-7xl flex-1 p-4 md:p-6">{children}</main>
      </div>
      <AlertToasts enabled={!!tenantId && can(me, "events_view")} />
    </div>
  );
}
