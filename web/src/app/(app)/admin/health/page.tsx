"use client";

import { useQuery } from "@tanstack/react-query";

import { IncidentTable } from "@/components/Incidents";
import { RequirePermission } from "@/components/RequirePermission";
import { Card, ErrorText, PageHeader, Spinner } from "@/components/ui";
import { api, unwrap } from "@/lib/api";
import { DEFAULT_TIMEZONE } from "@/lib/branches";
import { useI18n } from "@/lib/i18n";

/** Every client's open system problems, for our on-call team (plan 11.4). */
function PlatformHealth() {
  const { t } = useI18n();
  const incidents = useQuery({
    queryKey: ["platform-incidents"],
    queryFn: () => unwrap(api.GET("/v1/platform/incidents")),
    refetchInterval: 60_000,
  });
  return (
    <>
      <PageHeader title={t("health.platformTitle")} />
      <p className="mb-4 max-w-3xl text-sm text-slate-600">{t("health.platformHelp")}</p>
      <Card className="p-4">
        {incidents.isPending ? (
          <Spinner />
        ) : incidents.isError ? (
          <ErrorText error={incidents.error} />
        ) : (
          <IncidentTable incidents={incidents.data} timeZone={DEFAULT_TIMEZONE} showTenant />
        )}
      </Card>
    </>
  );
}

export default function PlatformHealthPage() {
  return (
    <RequirePermission anyOf={["platform_admin"]}>
      <PlatformHealth />
    </RequirePermission>
  );
}
