"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";

import { EventCard, EventDetail, type EventOut } from "@/components/events";
import { HourChart, Kpi } from "@/components/kpi";
import { Badge, Button, Card, Empty, ErrorText, Spinner } from "@/components/ui";
import { api, ensureOk, rawFetch, unwrap, type Schemas } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useI18n } from "@/lib/i18n";
import { key } from "@/lib/labels";
import type { MessageKey } from "@/lib/messages/en";
import { formatDateTime } from "@/lib/time";

type BranchOverview = Schemas["BranchOverview"];

/** The features with a section on the overview (others have no figures of their own). */
const SECTIONS = ["attendance", "kitchen_compliance", "cashier", "security", "customer_count"] as const;

const pct = (value: number | null | undefined) => (value == null ? "—" : `${value}%`);
const num = (value: number | null | undefined) => (value == null ? "—" : String(value));
const seconds = (ms: number | null | undefined) => (ms == null ? "—" : `${(ms / 1000).toFixed(1)} s`);

async function downloadPdf(reportId: string, lang: string) {
  const response = await ensureOk(await rawFetch(`/v1/reports/${reportId}/file?format=pdf&lang=${lang}`));
  const name = /filename="([^"]+)"/.exec(response.headers.get("content-disposition") ?? "")?.[1] ?? "report.pdf";
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section>
      <h3 className="mb-2 text-sm font-semibold text-slate-700">{title}</h3>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">{children}</div>
    </section>
  );
}

function BranchCard({ data, embedded, onOpen }: { data: BranchOverview; embedded: boolean; onOpen: (e: EventOut) => void }) {
  const { t, locale } = useI18n();
  const off = SECTIONS.filter((f) => !data.features[f]);
  const { attendance, cashier, customers, alerts, system } = data;
  return (
    <Card className="flex flex-col gap-5 p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-lg font-semibold text-slate-900">{data.branch_name}</h2>
        <p className="text-xs text-slate-500">
          {data.partial && <Badge tone="blue">{t("reports.partial")}</Badge>}{" "}
          {t("overview.updated", { time: formatDateTime(data.generated_at, data.timezone, locale) })}
        </p>
      </div>

      <Section title={t("overview.events")}>
        <Kpi label={t("reports.kpi.events")} value={num(data.events.total)} />
        <Kpi label={t("reports.kpi.critical")} value={num(data.events.critical)} tone={data.events.critical ? "red" : undefined} />
        <Kpi label={t("overview.unreviewed")} value={num(data.events.unreviewed)} tone={data.events.unreviewed ? "amber" : undefined} />
        <Kpi label={t("reports.kpi.falseAlarms")} value={num(data.events.false_alarms)} />
      </Section>

      {(data.security != null || data.kitchen_compliance != null || cashier) && (
        <Section title={t("overview.issues")}>
          {data.security != null && (
            <Kpi label={t("feature.security")} value={num(data.security)} tone={data.security ? "red" : "green"} />
          )}
          {data.kitchen_compliance != null && (
            <Kpi
              label={t("feature.kitchen_compliance")}
              value={num(data.kitchen_compliance)}
              tone={data.kitchen_compliance ? "amber" : "green"}
            />
          )}
          {cashier && (
            <>
              <Kpi label={t("feature.cashier")} value={num(cashier.issues)} tone={cashier.issues ? "amber" : "green"} />
              <Kpi label={t("reports.kpi.counterStaffed")} value={pct(cashier.counter_staffed_pct)} />
            </>
          )}
        </Section>
      )}

      {attendance && (
        <Section title={t("feature.attendance")}>
          <Kpi label={t("attendance.status.on_time")} value={num(attendance.on_time)} tone="green" />
          <Kpi label={t("attendance.status.late")} value={num(attendance.late)} tone={attendance.late ? "amber" : undefined} />
          <Kpi label={t("attendance.status.absent")} value={num(attendance.absent)} tone={attendance.absent ? "red" : undefined} />
          <Kpi label={t("reports.kpi.attendanceRate")} value={pct(attendance.attendance_pct)} />
        </Section>
      )}

      {customers && (
        <div className="flex flex-col gap-3">
          <Section title={t("feature.customer_count")}>
            <Kpi label={t("reports.kpi.customers")} value={num(customers.customers_in)} />
            <Kpi
              label={t("reports.kpi.peakHour")}
              value={customers.peak_hour == null ? "—" : `${String(customers.peak_hour).padStart(2, "0")}:00`}
            />
            <Kpi label={t("overview.peakOccupancy")} value={num(customers.peak_occupancy)} />
          </Section>
          {customers.hours && <HourChart hours={customers.hours} />}
        </div>
      )}

      <Section title={t("overview.system")}>
        <Kpi
          label={t("overview.devicesOnline")}
          value={`${system.devices_online} / ${system.devices_online + system.devices_offline}`}
          tone={system.devices_offline ? "red" : "green"}
        />
        <Kpi
          label={t("overview.camerasOnline")}
          value={`${system.cameras_online} / ${system.cameras}`}
          tone={system.cameras_online < system.cameras ? "amber" : "green"}
        />
        <Kpi label={t("reports.kpi.alertsSent")} value={num(alerts.sent)} />
        <Kpi label={t("reports.kpi.alertP95")} value={seconds(alerts.p95_ms)} />
      </Section>

      {off.length > 0 && (
        <p className="text-xs text-slate-500">
          {t("overview.off", { features: off.map((f) => t(key.feature(f))).join(", ") })}
        </p>
      )}

      {data.recent_events && (
        <section>
          <h3 className="mb-2 text-sm font-semibold text-slate-700">{t("overview.recent")}</h3>
          {data.recent_events.length ? (
            <div className="grid gap-2 sm:grid-cols-2">
              {data.recent_events.map((event) => (
                <EventCard key={event.id} event={event} onOpen={onOpen} />
              ))}
            </div>
          ) : (
            <Empty>{t("overview.noEvents")}</Empty>
          )}
        </section>
      )}

      {data.reports && data.reports.length > 0 && (
        <section>
          <h3 className="mb-2 text-sm font-semibold text-slate-700">{t("nav.reports")}</h3>
          <ul className="flex flex-wrap gap-2">
            {data.reports.map((report) => {
              const label = `${t(`reports.period.${report.period}` as MessageKey)} · ${report.period_start}`;
              return (
                <li key={report.id}>
                  {embedded ? (
                    <Button size="sm" variant="secondary" onClick={() => void downloadPdf(report.id, locale)}>
                      {label} (PDF)
                    </Button>
                  ) : (
                    <Link className="text-sm text-brand-700 hover:underline" href={`/reports/?report=${report.id}`}>
                      {label}
                    </Link>
                  )}
                </li>
              );
            })}
          </ul>
        </section>
      )}
    </Card>
  );
}

/** Each branch's day at a glance (plan 8.2): the Overview page, and the page embedded in
 * the operational system. Sections follow the feature switches (8.3). */
export function OwnerOverview({ branchId, embedded = false }: { branchId: string | null; embedded?: boolean }) {
  const { t } = useI18n();
  const { tenantId, status } = useAuth();
  const [open, setOpen] = useState<EventOut | null>(null);
  const query = useQuery({
    queryKey: ["overview", tenantId, branchId],
    queryFn: () =>
      unwrap(api.GET("/v1/overview", { params: { query: { branch_id: branchId ?? undefined, recent: 6 } } })),
    enabled: status === "signed-in" && !!tenantId,
    refetchInterval: 60_000,
  });
  if (query.isPending) return <Spinner />;
  if (query.isError) return <ErrorText error={query.error} />;
  return (
    <div className="flex flex-col gap-4">
      {query.data.length ? (
        query.data.map((branch) => <BranchCard key={branch.branch_id} data={branch} embedded={embedded} onOpen={setOpen} />)
      ) : (
        <Empty>{t("overview.noBranches")}</Empty>
      )}
      <EventDetail event={open} onClose={() => setOpen(null)} />
    </div>
  );
}
