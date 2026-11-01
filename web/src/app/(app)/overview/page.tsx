"use client";

import { OwnerOverview } from "@/components/OwnerOverview";
import { RequirePermission } from "@/components/RequirePermission";
import { PageHeader } from "@/components/ui";
import { useBranches } from "@/lib/branches";
import { useI18n } from "@/lib/i18n";

export default function OverviewPage() {
  const { t } = useI18n();
  const { branchId } = useBranches();
  return (
    <RequirePermission anyOf={["kpis_view"]}>
      <PageHeader title={t("overview.title")} />
      <p className="mb-4 max-w-3xl text-sm text-slate-600">{t("overview.help")}</p>
      <OwnerOverview branchId={branchId} />
    </RequirePermission>
  );
}
