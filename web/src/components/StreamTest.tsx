"use client";

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { Badge, Card, Empty, ErrorText, Field, Input, Select, Spinner, Table } from "@/components/ui";
import { api, unwrap } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useI18n } from "@/lib/i18n";
import { zonedToUtc } from "@/lib/time";

/** "YYYY-MM-DDTHH:MM" of `at` in the branch's time zone (for a datetime-local input). */
function localInput(at: Date, timeZone: string): string {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(at);
  const get = (type: string) => parts.find((p) => p.type === type)?.value ?? "00";
  return `${get("year")}-${get("month")}-${get("day")}T${get("hour")}:${get("minute")}`;
}

function cell(value: number | null): string {
  if (value == null) return "bg-slate-100";
  if (value >= 1) return "bg-emerald-500";
  if (value > 0) return "bg-amber-400";
  return "bg-red-500";
}

/** The setup test (plan 9.2): every camera streaming, 48 h without drops. */
export function StreamTest({ branchId, timeZone }: { branchId: string; timeZone: string }) {
  const { t } = useI18n();
  const { tenantId } = useAuth();
  // "": from the branch's first heartbeat (install day); else a branch-local time.
  const [since, setSince] = useState("");
  const [hours, setHours] = useState(48);
  const [date, time] = since.split("T");
  const start = since ? zonedToUtc(date ?? "", time ?? "00:00", timeZone).toISOString() : undefined;
  const test = useQuery({
    queryKey: ["stream-test", tenantId, branchId, since, hours],
    queryFn: () =>
      unwrap(
        api.GET("/v1/branches/{branch_id}/stream-test", {
          params: { path: { branch_id: branchId }, query: { since: start, hours } },
        }),
      ),
    refetchInterval: 60_000,
  });
  const verdict = test.data?.passed;
  return (
    <Card className="flex flex-col gap-3 p-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="font-semibold">{t("streamTest.title")}</h2>
          <p className="max-w-2xl text-sm text-slate-600">{t("streamTest.help")}</p>
        </div>
        <div className="flex flex-wrap items-end gap-2">
          <Field label={t("streamTest.since")}>
            {(id) => (
              <Input
                id={id}
                type="datetime-local"
                value={since || (test.data ? localInput(new Date(test.data.since), timeZone) : "")}
                onChange={(e) => setSince(e.target.value)}
              />
            )}
          </Field>
          <Field label={t("streamTest.hours")}>
            {(id) => (
              <Select id={id} value={hours} onChange={(e) => setHours(Number(e.target.value))}>
                {[24, 48, 72, 168].map((h) => (
                  <option key={h} value={h}>
                    {h} h
                  </option>
                ))}
              </Select>
            )}
          </Field>
        </div>
      </div>
      {test.isPending ? (
        <Spinner />
      ) : test.isError ? (
        <ErrorText error={test.error} />
      ) : (
        <>
          <p>
            <Badge tone={verdict === true ? "green" : verdict === false ? "red" : "blue"}>
              {verdict === true ? t("streamTest.passed") : verdict === false ? t("streamTest.failed") : t("streamTest.running")}
            </Badge>
          </p>
          {test.data.cameras.length ? (
            <Table>
              <thead>
                <tr>
                  <th>{t("common.camera")}</th>
                  <th>{t("streamTest.uptime")}</th>
                  <th>{t("streamTest.restarts")}</th>
                  <th>{t("streamTest.offline")}</th>
                  <th>{t("streamTest.missing")}</th>
                  <th>{t("streamTest.longest")}</th>
                  <th>{t("streamTest.byHour")}</th>
                  <th>{t("common.status")}</th>
                </tr>
              </thead>
              <tbody>
                {test.data.cameras.map((c) => (
                  <tr key={c.camera_id}>
                    <td className="font-medium">{c.name}</td>
                    <td className="tabular-nums">{c.uptime_pct == null ? "—" : `${c.uptime_pct}%`}</td>
                    <td className="tabular-nums">{c.restarts}</td>
                    <td className="tabular-nums">{c.offline_minutes}</td>
                    <td className="tabular-nums">{c.missing_minutes}</td>
                    <td className="tabular-nums">{c.longest_outage_minutes} min</td>
                    <td>
                      <div className="flex gap-px" dir="ltr">
                        {c.hours.map((h, i) => (
                          <span key={i} className={`h-4 w-1.5 rounded-sm ${cell(h)}`} />
                        ))}
                      </div>
                    </td>
                    <td>
                      <Badge tone={c.passed === true ? "green" : c.passed === false ? "red" : "blue"}>
                        {c.passed === true ? t("streamTest.passed") : c.passed === false ? t("streamTest.failed") : t("streamTest.running")}
                      </Badge>
                    </td>
                  </tr>
                ))}
              </tbody>
            </Table>
          ) : (
            <Empty>{t("streamTest.noCameras")}</Empty>
          )}
        </>
      )}
    </Card>
  );
}
