"use client";

import { useQuery } from "@tanstack/react-query";

import { Badge, Card, Empty, ErrorText, Spinner, Table, type Tone } from "@/components/ui";
import { api, unwrap, type Schemas } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { intlLocale, useI18n } from "@/lib/i18n";
import type { MessageKey } from "@/lib/messages/en";
import { formatDateTime } from "@/lib/time";

type Incident = Schemas["IncidentOut"];

export const KIND_TONE: Record<Schemas["IncidentKind"], Tone> = {
  device_offline: "red",
  stream_down: "red",
  disk_low: "amber",
  temperature_high: "amber",
  sync_backlog: "amber",
};

export function IncidentTable({ incidents, timeZone, showTenant = false }: { incidents: Incident[]; timeZone: string; showTenant?: boolean }) {
  const { t, locale } = useI18n();
  if (!incidents.length) return <Empty>{t("health.none")}</Empty>;
  return (
    <Table>
      <thead>
        <tr>
          {showTenant && <th>{t("health.client")}</th>}
          <th>{t("common.branch")}</th>
          <th>{t("health.problem")}</th>
          <th>{t("health.detail")}</th>
          <th>{t("health.since")}</th>
          <th>{t("common.status")}</th>
        </tr>
      </thead>
      <tbody>
        {incidents.map((i) => (
          <tr key={i.id} className={i.resolved_at ? "opacity-60" : ""}>
            {showTenant && <td>{i.tenant}</td>}
            <td>{i.branch}</td>
            <td>
              <Badge tone={i.resolved_at ? "slate" : KIND_TONE[i.kind]}>{t(`health.kind.${i.kind}` as MessageKey)}</Badge>
            </td>
            <td className="text-xs" dir="ltr">
              {i.detail}
            </td>
            <td className="text-xs" dir="ltr">
              {formatDateTime(i.opened_at, timeZone, intlLocale(locale))}
            </td>
            <td className="text-xs">
              {i.resolved_at
                ? t("health.resolvedAt", { time: formatDateTime(i.resolved_at, timeZone, intlLocale(locale)) })
                : i.notified_at
                  ? t("health.paged")
                  : t("health.open")}
            </td>
          </tr>
        ))}
      </tbody>
    </Table>
  );
}

/** Open and recent system problems of the tenant's branches (plan 11.4). */
export function Incidents({ branchId, timeZone }: { branchId: string | null; timeZone: string }) {
  const { t } = useI18n();
  const { tenantId } = useAuth();
  const incidents = useQuery({
    queryKey: ["incidents", tenantId, branchId],
    queryFn: () => unwrap(api.GET("/v1/incidents", { params: { query: { branch_id: branchId ?? undefined, limit: 30 } } })),
    refetchInterval: 60_000,
  });
  return (
    <Card className="flex flex-col gap-3 p-4">
      <h2 className="font-semibold">{t("health.title")}</h2>
      <p className="max-w-3xl text-sm text-slate-600">{t("health.help")}</p>
      {incidents.isPending ? (
        <Spinner />
      ) : incidents.isError ? (
        <ErrorText error={incidents.error} />
      ) : (
        <IncidentTable incidents={incidents.data} timeZone={timeZone} />
      )}
    </Card>
  );
}
