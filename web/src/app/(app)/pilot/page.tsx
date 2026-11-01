"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { Kpi } from "@/components/kpi";
import { RequirePermission } from "@/components/RequirePermission";
import { Badge, Button, Card, Empty, ErrorText, Field, Input, Notice, PageHeader, Select, Spinner, Table, Textarea } from "@/components/ui";
import { api, unwrap, type Schemas } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useBranches } from "@/lib/branches";
import { useI18n } from "@/lib/i18n";
import { key } from "@/lib/labels";
import type { MessageKey } from "@/lib/messages/en";
import { can } from "@/lib/permissions";
import { localDate } from "@/lib/time";

type Check = Schemas["Check"];
type Kind = Schemas["PilotNoteKind"];

const KINDS: Kind[] = ["check_in", "feedback", "change", "issue", "decision"];
const pct = (value: number | null | undefined, scale = 100) => (value == null ? "—" : `${Math.round(value * scale * 10) / 10}%`);
const sec = (ms: number | null | undefined) => (ms == null ? "—" : `${(ms / 1000).toFixed(1)} s`);

function checkValue(check: Check): string {
  switch (check.key) {
    case "accuracy":
      return pct(check.value);
    case "false_alarms":
      return check.value == null ? "—" : String(check.value);
    case "alert_time":
      return sec(check.value);
    case "uptime":
      return pct(check.value, 1);
    default:
      return check.value == null ? "—" : String(check.value);
  }
}

function checkTarget(check: Check): string {
  switch (check.key) {
    case "accuracy":
      return `≥ ${pct(check.target)}`;
    case "false_alarms":
      return `< ${check.target}`;
    case "alert_time":
      return `< ${sec(check.target)}`;
    case "uptime":
      return `≥ ${check.target}%`;
    default:
      return `≥ ${check.target}`;
  }
}

function Verdict({ met }: { met: boolean | null | undefined }) {
  const { t } = useI18n();
  if (met == null) return <Badge>{t("pilot.notYet")}</Badge>;
  return <Badge tone={met ? "green" : "red"}>{met ? t("pilot.met") : t("pilot.notMet")}</Badge>;
}

function PilotStart({ branch }: { branch: Schemas["BranchOut"] }) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const [day, setDay] = useState(branch.pilot_started_on ?? localDate(new Date(), branch.timezone));
  const save = useMutation({
    mutationFn: (started: string | null) =>
      unwrap(api.PUT("/v1/branches/{branch_id}/pilot", { params: { path: { branch_id: branch.id } }, body: { started_on: started } })),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["branches"] });
      void queryClient.invalidateQueries({ queryKey: ["pilot-kpis"] });
    },
  });
  return (
    <form
      className="flex flex-wrap items-end gap-2"
      onSubmit={(e) => {
        e.preventDefault();
        save.mutate(day);
      }}
    >
      <Field label={t("pilot.startDay")}>{(id) => <Input id={id} type="date" required value={day} onChange={(e) => setDay(e.target.value)} />}</Field>
      <Button type="submit" variant="primary" disabled={save.isPending}>
        {t("common.save")}
      </Button>
      <ErrorText error={save.error} />
    </form>
  );
}

function Figures({ branchId }: { branchId: string }) {
  const { t } = useI18n();
  const { tenantId } = useAuth();
  const kpis = useQuery({
    queryKey: ["pilot-kpis", tenantId, branchId],
    queryFn: () => unwrap(api.GET("/v1/pilot/kpis", { params: { query: { branch_id: branchId } } })),
    refetchInterval: 5 * 60_000,
  });
  if (kpis.isPending) return <Spinner />;
  if (kpis.isError) return <ErrorText error={kpis.error} />;
  const { summary, features, days, checks, pilot_day: pilotDay } = kpis.data;
  return (
    <>
      <Card className="flex flex-col gap-3 p-4">
        <h2 className="font-semibold">
          {pilotDay == null ? t("pilot.notStarted") : t("pilot.day", { day: pilotDay })} · {kpis.data.since} – {kpis.data.until}
        </h2>
        <Table>
          <thead>
            <tr>
              <th>{t("pilot.check")}</th>
              <th>{t("pilot.value")}</th>
              <th>{t("pilot.target")}</th>
              <th>{t("common.status")}</th>
            </tr>
          </thead>
          <tbody>
            {checks.map((c) => (
              <tr key={c.key}>
                <td>{t(`pilot.check.${c.key}` as MessageKey)}</td>
                <td className="tabular-nums" dir="ltr">
                  {checkValue(c)}
                </td>
                <td className="tabular-nums" dir="ltr">
                  {checkTarget(c)}
                </td>
                <td>
                  <Verdict met={c.met} />
                </td>
              </tr>
            ))}
            <tr>
              <td>{t("pilot.check.search")}</td>
              <td>—</td>
              <td dir="ltr">&lt; 30 s</td>
              <td>
                <Badge>{t("pilot.byHand")}</Badge>
              </td>
            </tr>
            <tr>
              <td>{t("pilot.check.feedback")}</td>
              <td>—</td>
              <td>{t("pilot.signOff")}</td>
              <td>
                <Badge>{t("pilot.byHand")}</Badge>
              </td>
            </tr>
          </tbody>
        </Table>
        <p className="text-xs text-slate-500">{t("pilot.recallNote")}</p>
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <Kpi label={t("reports.kpi.events")} value={String(summary.events)} />
          <Kpi label={t("pilot.reviewed")} value={summary.events ? `${Math.round((100 * summary.reviewed) / summary.events)}%` : "—"} />
          <Kpi label={t("reports.kpi.alertsSent")} value={String(summary.alerts_sent)} />
          <Kpi label={t("pilot.boxUptime")} value={pct(summary.box_uptime_pct, 1)} />
        </div>
      </Card>

      <Card className="flex flex-col gap-3 p-4">
        <h2 className="font-semibold">{t("pilot.perFeature")}</h2>
        {features.length ? (
          <Table>
            <thead>
              <tr>
                <th>{t("pilot.feature")}</th>
                <th>{t("reports.kpi.events")}</th>
                <th>{t("review.valid")}</th>
                <th>{t("review.falseAlarms")}</th>
                <th>{t("review.precision")}</th>
                <th>{t("review.perDay")}</th>
                <th>{t("common.status")}</th>
              </tr>
            </thead>
            <tbody>
              {features.map((f) => (
                <tr key={f.feature}>
                  <td>{t(key.feature(f.feature))}</td>
                  <td className="tabular-nums">{f.events}</td>
                  <td className="tabular-nums">{f.valid}</td>
                  <td className="tabular-nums">{f.false_alarm}</td>
                  <td className="tabular-nums">{pct(f.precision)}</td>
                  <td className="tabular-nums">{f.false_alarms_per_day ?? "—"}</td>
                  <td>
                    <Verdict met={f.meets_target} />
                  </td>
                </tr>
              ))}
            </tbody>
          </Table>
        ) : (
          <Empty />
        )}
        <p className="text-xs text-slate-500">{t("pilot.featureNote")}</p>
      </Card>

      <Card className="flex flex-col gap-3 p-4">
        <h2 className="font-semibold">{t("pilot.byDay")}</h2>
        <Table>
          <thead>
            <tr>
              <th>{t("common.date")}</th>
              <th>{t("reports.kpi.events")}</th>
              <th>{t("pilot.reviewed")}</th>
              <th>{t("review.falseAlarms")}</th>
              <th>{t("review.precision")}</th>
              <th>{t("reports.kpi.alertP95")}</th>
              <th>{t("pilot.cameraUptime")}</th>
              <th>{t("pilot.boxUptime")}</th>
            </tr>
          </thead>
          <tbody>
            {[...days].reverse().map((d) => (
              <tr key={d.date}>
                <td dir="ltr" className="text-start">
                  {d.date}
                </td>
                <td className="tabular-nums">{d.events}</td>
                <td className="tabular-nums">{d.events ? `${d.reviewed}/${d.events}` : "—"}</td>
                <td className={`tabular-nums ${d.false_alarm >= 5 ? "font-semibold text-red-700" : ""}`}>{d.false_alarm}</td>
                <td className="tabular-nums">{pct(d.precision)}</td>
                <td className={`tabular-nums ${(d.alert_p95_ms ?? 0) >= 10_000 ? "text-red-700" : ""}`}>{sec(d.alert_p95_ms)}</td>
                <td className={`tabular-nums ${d.camera_uptime_pct != null && d.camera_uptime_pct < 99 ? "text-red-700" : ""}`}>
                  {pct(d.camera_uptime_pct, 1)}
                </td>
                <td className="tabular-nums">{pct(d.box_uptime_pct, 1)}</td>
              </tr>
            ))}
          </tbody>
        </Table>
      </Card>
    </>
  );
}

function Log({ branchId }: { branchId: string }) {
  const { t } = useI18n();
  const { me, tenantId } = useAuth();
  const { timezoneOf } = useBranches();
  const queryClient = useQueryClient();
  const log = useQuery({
    queryKey: ["pilot-log", tenantId, branchId],
    queryFn: () => unwrap(api.GET("/v1/pilot/log", { params: { query: { branch_id: branchId } } })),
  });
  const [form, setForm] = useState({ noted_on: localDate(new Date(), timezoneOf(branchId)), kind: "check_in" as Kind, body: "", all: false });
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["pilot-log"] });
  const add = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/v1/pilot/log", {
          body: { noted_on: form.noted_on, kind: form.kind, body: form.body, branch_id: form.all ? null : branchId },
        }),
      ),
    onSuccess: () => {
      setForm({ ...form, body: "" });
      void refresh();
    },
  });
  const remove = useMutation({
    mutationFn: (id: string) => unwrap(api.DELETE("/v1/pilot/log/{note_id}", { params: { path: { note_id: id } } })),
    onSuccess: () => void refresh(),
  });
  const canWrite = can(me, "tenant_admin");
  return (
    <Card className="flex flex-col gap-3 p-4">
      <h2 className="font-semibold">{t("pilot.log")}</h2>
      <p className="text-sm text-slate-600">{t("pilot.logHelp")}</p>
      {canWrite && (
        <form
          className="flex flex-col gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            add.mutate();
          }}
        >
          <div className="flex flex-wrap items-end gap-2">
            <Field label={t("common.date")}>
              {(id) => <Input id={id} type="date" required value={form.noted_on} onChange={(e) => setForm({ ...form, noted_on: e.target.value })} />}
            </Field>
            <Field label={t("pilot.kind")}>
              {(id) => (
                <Select id={id} value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value as Kind })}>
                  {KINDS.map((k) => (
                    <option key={k} value={k}>
                      {t(`pilot.kind.${k}` as MessageKey)}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
            <label className="flex items-center gap-2 pb-2 text-sm">
              <input type="checkbox" checked={form.all} onChange={(e) => setForm({ ...form, all: e.target.checked })} />
              {t("pilot.allBranches")}
            </label>
          </div>
          <Textarea aria-label={t("pilot.note")} placeholder={t("pilot.note")} required rows={3} maxLength={5000} value={form.body} onChange={(e) => setForm({ ...form, body: e.target.value })} />
          <div>
            <Button type="submit" variant="primary" disabled={add.isPending || !form.body.trim()}>
              {t("common.add")}
            </Button>
          </div>
          <ErrorText error={add.error} />
        </form>
      )}
      {log.isPending ? (
        <Spinner />
      ) : log.data?.length ? (
        <ul className="flex flex-col divide-y divide-slate-100">
          {log.data.map((n) => (
            <li key={n.id} className="flex flex-col gap-1 py-2">
              <div className="flex flex-wrap items-center gap-2 text-xs text-slate-500">
                <span dir="ltr">{n.noted_on}</span>
                <Badge>{t(`pilot.kind.${n.kind}` as MessageKey)}</Badge>
                {!n.branch_id && <Badge tone="blue">{t("pilot.allBranches")}</Badge>}
                <span>{n.author_name ?? t("audit.support")}</span>
                {(n.author_id === me?.id || can(me, "platform_admin")) && (
                  <Button size="sm" variant="ghost" onClick={() => window.confirm(t("pilot.deleteConfirm")) && remove.mutate(n.id)}>
                    {t("common.delete")}
                  </Button>
                )}
              </div>
              <p className="text-sm whitespace-pre-wrap">{n.body}</p>
            </li>
          ))}
        </ul>
      ) : (
        <Empty>{t("pilot.logEmpty")}</Empty>
      )}
      <ErrorText error={log.error ?? remove.error} />
    </Card>
  );
}

function Pilot() {
  const { t } = useI18n();
  const { me } = useAuth();
  const { branch, branchId } = useBranches();
  return (
    <>
      <PageHeader title={t("pilot.title")}>{branch && can(me, "platform_admin") && <PilotStart key={branch.id} branch={branch} />}</PageHeader>
      <p className="mb-4 max-w-3xl text-sm text-slate-600">{t("pilot.help")}</p>
      {branchId ? (
        <div className="flex flex-col gap-4">
          <Figures branchId={branchId} />
          <Log branchId={branchId} />
        </div>
      ) : (
        <Notice tone="amber">{t("pilot.chooseBranch")}</Notice>
      )}
    </>
  );
}

export default function PilotPage() {
  return (
    <RequirePermission anyOf={["kpis_view"]}>
      <Pilot />
    </RequirePermission>
  );
}
