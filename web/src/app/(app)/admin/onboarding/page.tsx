"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";

import { RequirePermission } from "@/components/RequirePermission";
import { Badge, Card, ErrorText, Notice, PageHeader, Spinner } from "@/components/ui";
import { api, unwrap } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useBranches } from "@/lib/branches";
import { useI18n } from "@/lib/i18n";
import type { MessageKey } from "@/lib/messages/en";

/** Where each step is done in the dashboard. */
const WHERE: Record<string, string> = {
  branch: "/settings/hours/",
  cameras: "/admin/cameras/",
  edge_box: "/admin/devices/",
  streaming: "/live/",
  setup_test: "/admin/devices/",
  zones: "/settings/zones/",
  features: "/settings/features/",
  staff: "/staff/",
  alerts: "/alerts/",
  shadow_mode: "/admin/branches/",
  go_live: "/pilot/",
};

function Steps({ branchId }: { branchId: string }) {
  const { t } = useI18n();
  const { tenantId } = useAuth();
  const steps = useQuery({
    queryKey: ["onboarding", tenantId, branchId],
    queryFn: () => unwrap(api.GET("/v1/branches/{branch_id}/onboarding", { params: { path: { branch_id: branchId } } })),
    refetchInterval: 60_000,
  });
  if (steps.isPending) return <Spinner />;
  if (steps.isError) return <ErrorText error={steps.error} />;
  const next = steps.data.find((s) => s.done === false);
  const done = steps.data.filter((s) => s.done).length;
  const counted = steps.data.filter((s) => s.done !== null).length;
  return (
    <div className="flex flex-col gap-3">
      <p className="text-sm font-medium">{t("onboarding.progress", { done, total: counted })}</p>
      <ol className="flex flex-col gap-2">
        {steps.data.map((s, index) => (
          <li key={s.key}>
            <Card className={`flex flex-wrap items-start gap-3 p-3 ${s.key === next?.key ? "ring-2 ring-brand-500" : ""}`}>
              <span className="w-6 text-center font-semibold text-slate-500 tabular-nums">{index + 1}</span>
              <div className="min-w-0 flex-1">
                <p className="font-medium">{t(`onboarding.step.${s.key}` as MessageKey)}</p>
                <p className="text-sm text-slate-600">{t(`onboarding.help.${s.key}` as MessageKey)}</p>
              </div>
              <div className="flex flex-col items-end gap-1 text-sm">
                {s.done === null ? (
                  <Badge>{t("onboarding.notNeeded")}</Badge>
                ) : s.done ? (
                  <Badge tone="green">{t("onboarding.done")}</Badge>
                ) : (
                  <Badge tone="amber">{t("onboarding.todo")}</Badge>
                )}
                {s.total != null && s.done_count != null && (
                  <span className="text-xs text-slate-500 tabular-nums">
                    {s.done_count} / {s.total}
                  </span>
                )}
                {s.done !== null && (
                  <Link className="text-xs text-brand-700 hover:underline" href={WHERE[s.key] ?? "/"}>
                    {t("onboarding.open")}
                  </Link>
                )}
              </div>
            </Card>
          </li>
        ))}
      </ol>
    </div>
  );
}

function Onboarding() {
  const { t } = useI18n();
  const { branchId } = useBranches();
  return (
    <>
      <PageHeader title={t("onboarding.title")} />
      <p className="mb-4 max-w-3xl text-sm text-slate-600">{t("onboarding.help")}</p>
      {branchId ? <Steps key={branchId} branchId={branchId} /> : <Notice tone="amber">{t("onboarding.chooseBranch")}</Notice>}
    </>
  );
}

export default function OnboardingPage() {
  return (
    <RequirePermission anyOf={["devices_manage"]}>
      <Onboarding />
    </RequirePermission>
  );
}
