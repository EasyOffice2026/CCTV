"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useEffect, useState } from "react";

import { HourChart, Kpi } from "@/components/kpi";
import { RequirePermission } from "@/components/RequirePermission";
import { Badge, Button, Card, Empty, ErrorText, Field, Input, Notice, PageHeader, Select, Spinner, Table } from "@/components/ui";
import { api, ensureOk, rawFetch, unwrap, type Schemas } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useBranches } from "@/lib/branches";
import { intlLocale, useI18n } from "@/lib/i18n";
import { REVIEW_TONE, SEVERITY_TONE, key } from "@/lib/labels";
import type { MessageKey } from "@/lib/messages/en";
import { formatDateTime, localDate } from "@/lib/time";

type Period = Schemas["ReportPeriod"];
type Report = Schemas["ReportOut"];
type Data = Report["data"];

const PERIODS: Period[] = ["day", "week", "month"];

function linkedReport(): string | null {
  if (typeof window === "undefined") return null;
  const id = new URLSearchParams(window.location.search).get("report");
  return id && /^[0-9a-f-]{36}$/i.test(id) ? id : null;
}

const pct = (value: number | null | undefined) => (value == null ? "—" : `${value}%`);
const seconds = (ms: number | null | undefined) => (ms == null ? "—" : `${(ms / 1000).toFixed(1)} s`);
const num = (value: number | null | undefined) => (value == null ? "—" : String(value));

async function download(report: Report, format: "pdf" | "xlsx", lang: string) {
  const response = await ensureOk(await rawFetch(`/v1/reports/${report.id}/file?format=${format}&lang=${lang}`));
  const name = /filename="([^"]+)"/.exec(response.headers.get("content-disposition") ?? "")?.[1] ?? `report.${format}`;
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function ReportView({ report }: { report: Report }) {
  const { t, locale } = useI18n();
  const [error, setError] = useState<unknown>(null);
  const data: Data = report.data;
  const totals = data.totals;
  const tz = data.timezone;

  const save = async (format: "pdf" | "xlsx") => {
    setError(null);
    try {
      await download(report, format, locale);
    } catch (err) {
      setError(err);
    }
  };

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <div className="me-auto">
          <h2 className="text-lg font-semibold">
            {t(`reports.period.${data.period}` as MessageKey)}: {data.branch_name}
          </h2>
          <p className="text-sm text-slate-600">
            <span dir="ltr">{data.start === data.end ? data.start : `${data.start} → ${data.end}`}</span> ·{" "}
            {t("reports.generated", { time: formatDateTime(data.generated_at, tz, intlLocale(locale)) })}
          </p>
        </div>
        {data.partial && <Badge tone="amber">{t("reports.partial")}</Badge>}
        <Button size="sm" onClick={() => void save("pdf")}>
          {t("reports.downloadPdf")}
        </Button>
        <Button size="sm" onClick={() => void save("xlsx")}>
          {t("reports.downloadExcel")}
        </Button>
      </div>
      <ErrorText error={error} />

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
        <Kpi label={t("reports.kpi.events")} value={num(totals.events)} />
        <Kpi label={t("reports.kpi.critical")} value={num(totals.critical)} tone={totals.critical ? "red" : undefined} />
        <Kpi label={t("reports.kpi.security")} value={num(totals.security)} />
        <Kpi label={t("reports.kpi.compliance")} value={num(totals.compliance)} tone={totals.compliance ? "amber" : undefined} />
        <Kpi label={t("reports.kpi.cashier")} value={num(totals.cashier)} />
        <Kpi label={t("reports.kpi.falseAlarms")} value={num(totals.false_alarms)} />
        <Kpi label={t("reports.kpi.attendanceRate")} value={pct(totals.attendance_pct)} />
        <Kpi label={t("reports.kpi.punctuality")} value={pct(totals.punctuality_pct)} />
        <Kpi label={t("reports.kpi.customers")} value={num(totals.customers_in)} />
        <Kpi label={t("reports.kpi.peakHour")} value={totals.peak_hour == null ? "—" : `${String(totals.peak_hour).padStart(2, "0")}:00`} />
        <Kpi label={t("reports.kpi.counterStaffed")} value={pct(totals.counter_staffed_pct)} />
        <Kpi
          label={t("reports.kpi.alertP95")}
          value={seconds(data.alerts.p95_ms)}
          tone={data.alerts.p95_ms == null ? undefined : data.alerts.p95_ms <= 10_000 ? "green" : "red"}
        />
      </div>

      {data.hours && (
        <Card className="p-4">
          <h3 className="mb-3 font-medium">{t("reports.byHour")}</h3>
          <HourChart hours={data.hours} />
        </Card>
      )}

      {data.days.length > 1 && (
        <Card>
          <h3 className="px-4 pt-3 font-medium">{t("reports.byDay")}</h3>
          <Table>
            <thead>
              <tr>
                <th>{t("common.date")}</th>
                <th>{t("reports.kpi.events")}</th>
                <th>{t("reports.kpi.critical")}</th>
                <th>{t("reports.kpi.compliance")}</th>
                <th>{t("attendance.status.on_time")}</th>
                <th>{t("attendance.status.late")}</th>
                <th>{t("attendance.status.absent")}</th>
                <th>{t("reports.kpi.customers")}</th>
              </tr>
            </thead>
            <tbody className="tabular-nums">
              {data.days.map((day) => (
                <tr key={day.date}>
                  <td dir="ltr" className="text-start">
                    {day.date}
                  </td>
                  <td>{day.events}</td>
                  <td>{day.critical}</td>
                  <td>{day.compliance}</td>
                  <td>{day.on_time}</td>
                  <td>{day.late}</td>
                  <td>{day.absent}</td>
                  <td>{num(day.customers_in)}</td>
                </tr>
              ))}
            </tbody>
          </Table>
        </Card>
      )}

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <h3 className="px-4 pt-3 font-medium">{t("reports.byType")}</h3>
          {data.types.length ? (
            <Table>
              <thead>
                <tr>
                  <th>{t("reports.type")}</th>
                  <th>{t("reports.total")}</th>
                  <th>{t("review.valid")}</th>
                  <th>{t("review.false_alarm")}</th>
                  <th>{t("review.new")}</th>
                </tr>
              </thead>
              <tbody className="tabular-nums">
                {data.types.map((row) => (
                  <tr key={row.event_type}>
                    <td>{t(key.eventType(row.event_type))}</td>
                    <td>{row.total}</td>
                    <td>{row.valid}</td>
                    <td>{row.false_alarm}</td>
                    <td>{row.unreviewed}</td>
                  </tr>
                ))}
              </tbody>
            </Table>
          ) : (
            <Empty>{t("reports.noData")}</Empty>
          )}
        </Card>

        <Card>
          <h3 className="px-4 pt-3 font-medium">{t("reports.securityEvents")}</h3>
          {data.security_events.length ? (
            <ul className="divide-y divide-slate-100 text-sm">
              {data.security_events.map((e) => (
                <li key={e.event_id} className="flex flex-wrap items-center gap-2 px-4 py-2">
                  <span className="text-slate-500 tabular-nums">
                    {formatDateTime(e.started_at, tz, intlLocale(locale))}
                  </span>
                  <Link href={`/events/?event=${e.event_id}`} className="text-brand-700 hover:underline">
                    {t(key.eventType(e.event_type))}
                  </Link>
                  <span className="text-slate-500">{e.camera}</span>
                  <span className="ms-auto flex gap-1">
                    <Badge tone={SEVERITY_TONE[e.severity as Schemas["Severity"]]}>{t(key.severity(e.severity))}</Badge>
                    <Badge tone={REVIEW_TONE[e.review_status as Schemas["ReviewStatus"]]}>{t(key.review(e.review_status))}</Badge>
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <Empty>{t("reports.noData")}</Empty>
          )}
        </Card>
      </div>

      <Card>
        <h3 className="px-4 pt-3 font-medium">{t("reports.staff")}</h3>
        <p className="px-4 text-xs text-slate-500">{t("reports.staffHelp")}</p>
        {data.staff.length ? (
          <Table>
            <thead>
              <tr>
                <th>{t("attendance.staff")}</th>
                <th>{t("attendance.status.on_time")}</th>
                <th>{t("attendance.status.late")}</th>
                <th>{t("attendance.status.absent")}</th>
                <th>{t("reports.kpi.attendanceRate")}</th>
                <th>{t("reports.kpi.punctuality")}</th>
                <th>{t("reports.kpi.compliance")}</th>
              </tr>
            </thead>
            <tbody className="tabular-nums">
              {data.staff.map((s) => (
                <tr key={s.staff_id}>
                  <td>
                    <p className="font-medium">{s.name}</p>
                    <p className="text-xs text-slate-500">{s.employee_code}</p>
                  </td>
                  <td>{s.on_time}</td>
                  <td>{s.late}</td>
                  <td>{s.absent}</td>
                  <td>{pct(s.attendance_pct)}</td>
                  <td>{pct(s.punctuality_pct)}</td>
                  <td>{s.compliance_issues}</td>
                </tr>
              ))}
            </tbody>
          </Table>
        ) : (
          <Empty>{t("reports.noData")}</Empty>
        )}
      </Card>

      <Card className="p-4 text-sm">
        <h3 className="mb-2 font-medium">{t("reports.alerts")}</h3>
        <p className="mb-3 text-xs text-slate-500">{t("reports.alertsHelp")}</p>
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-5">
          <Kpi label={t("reports.kpi.alertsSent")} value={num(data.alerts.sent)} />
          <Kpi label={t("reports.kpi.alertP50")} value={seconds(data.alerts.p50_ms)} />
          <Kpi label={t("reports.kpi.alertP95")} value={seconds(data.alerts.p95_ms)} />
          <Kpi label={t("reports.kpi.within10s")} value={pct(data.alerts.within_10s_pct)} />
          <Kpi label={t("reports.kpi.alertsFailed")} value={num(data.alerts.failed)} tone={data.alerts.failed ? "amber" : undefined} />
        </div>
      </Card>
    </div>
  );
}

function Reports({ branchId }: { branchId: string }) {
  const { t, locale } = useI18n();
  const { tenantId } = useAuth();
  const { timezoneOf } = useBranches();
  const queryClient = useQueryClient();
  const today = localDate(new Date(), timezoneOf(branchId));
  const [period, setPeriod] = useState<Period>("day");
  const [day, setDay] = useState(() => {
    const yesterday = new Date(`${today}T12:00:00Z`);
    yesterday.setUTCDate(yesterday.getUTCDate() - 1);
    return yesterday.toISOString().slice(0, 10);
  });
  // Links in report messages: /reports/?report=<id>. (Only rendered after sign-in, so
  // never during the static prerender.)
  const [linked, setLinked] = useState<string | null>(linkedReport);

  const list = useQuery({
    queryKey: ["reports", tenantId, branchId, period],
    queryFn: () => unwrap(api.GET("/v1/reports", { params: { query: { branch_id: branchId, period, limit: 200 } } })),
  });
  const match = list.data?.find((r) => r.period_start <= day && day <= r.period_end);
  const reportId = linked ?? match?.id ?? null;
  const report = useQuery({
    queryKey: ["report", tenantId, reportId],
    queryFn: () => unwrap(api.GET("/v1/reports/{report_id}", { params: { path: { report_id: reportId! } } })),
    enabled: !!reportId,
  });
  const generate = useMutation({
    mutationFn: () => unwrap(api.POST("/v1/reports", { body: { branch_id: branchId, period, day } })),
    onSuccess: (made) => {
      setLinked(null);
      queryClient.setQueryData(["report", tenantId, made.id], made);
      void queryClient.invalidateQueries({ queryKey: ["reports", tenantId, branchId] });
    },
  });

  return (
    <div className="grid gap-4 lg:grid-cols-[16rem_1fr]">
      <div className="flex flex-col gap-3">
        <Card className="flex flex-col gap-3 p-4">
          <Field label={t("reports.periodLabel")}>
            {(id) => (
              <Select
                id={id}
                value={period}
                onChange={(e) => {
                  setLinked(null);
                  setPeriod(e.target.value as Period);
                }}
              >
                {PERIODS.map((p) => (
                  <option key={p} value={p}>
                    {t(`reports.period.${p}` as MessageKey)}
                  </option>
                ))}
              </Select>
            )}
          </Field>
          <Field label={t("common.date")}>
            {(id) => (
              <Input
                id={id}
                type="date"
                max={today}
                value={day}
                onChange={(e) => {
                  if (!e.target.value) return;
                  setLinked(null);
                  setDay(e.target.value);
                }}
              />
            )}
          </Field>
          <Button variant={match ? "secondary" : "primary"} disabled={generate.isPending} onClick={() => generate.mutate()}>
            {t(match ? "reports.regenerate" : "reports.generate")}
          </Button>
          <ErrorText error={generate.error} />
        </Card>
        <Card className="p-2">
          <p className="px-2 py-1 text-xs font-semibold tracking-wide text-slate-400 uppercase">{t("reports.recent")}</p>
          {list.isLoading ? (
            <Spinner />
          ) : !list.data?.length ? (
            <p className="px-2 py-1 text-sm text-slate-500">{t("reports.none")}</p>
          ) : (
            <ul className="flex flex-col">
              {list.data.slice(0, 14).map((r) => (
                <li key={r.id}>
                  <button
                    type="button"
                    onClick={() => {
                      setLinked(null);
                      setDay(r.period_start);
                    }}
                    className={`w-full rounded px-2 py-1 text-start text-sm hover:bg-slate-100 ${r.id === reportId ? "bg-brand-50 font-medium text-brand-700" : "text-slate-700"}`}
                  >
                    <span dir="ltr">{r.period_start === r.period_end ? r.period_start : `${r.period_start} → ${r.period_end}`}</span>
                    {r.partial && <span className="text-xs text-amber-700"> · {t("reports.partial")}</span>}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>
      <div>
        {report.isLoading || list.isLoading ? (
          <Spinner />
        ) : report.data ? (
          <ReportView key={`${report.data.id}-${locale}`} report={report.data} />
        ) : (
          <Empty>{t("reports.noneForPeriod")}</Empty>
        )}
        <ErrorText error={report.error ?? list.error} />
      </div>
    </div>
  );
}

function ReportsContent() {
  const { t } = useI18n();
  const { branchId, setBranchId } = useBranches();

  // A report link opens that report's branch.
  useEffect(() => {
    const id = linkedReport();
    if (!id) return;
    void unwrap(api.GET("/v1/reports/{report_id}", { params: { path: { report_id: id } } }))
      .then((report) => setBranchId(report.branch_id))
      .catch(() => undefined);
  }, [setBranchId]);

  if (!branchId) return <Notice>{t("reports.chooseBranch")}</Notice>;
  return <Reports key={branchId} branchId={branchId} />;
}

export default function ReportsPage() {
  const { t } = useI18n();
  return (
    <RequirePermission anyOf={["reports_view"]}>
      <PageHeader title={t("reports.title")} />
      <p className="mb-4 max-w-3xl text-sm text-slate-600">{t("reports.help")}</p>
      <ReportsContent />
    </RequirePermission>
  );
}
