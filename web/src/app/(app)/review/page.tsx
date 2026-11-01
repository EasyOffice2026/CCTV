"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useEffect, useState } from "react";

import { EventPanel, type EventOut } from "@/components/events";
import { RequirePermission } from "@/components/RequirePermission";
import { Badge, Button, Card, Empty, ErrorText, Field, Input, Notice, PageHeader, Spinner, Table } from "@/components/ui";
import { api, unwrap } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useBranches } from "@/lib/branches";
import { useI18n } from "@/lib/i18n";
import { key } from "@/lib/labels";
import { dayRange, localDate } from "@/lib/time";

const pct = (value: number | null | undefined) => (value == null ? "—" : `${Math.round(value * 100)}%`);

/** The day's events not reviewed yet, one at a time, oldest first (plan 9.4). */
function Queue({ branchId, day, timeZone }: { branchId: string; day: string; timeZone: string }) {
  const { t } = useI18n();
  const { tenantId } = useAuth();
  const { start, end } = dayRange(day, timeZone);
  const events = useQuery({
    queryKey: ["events", "review-queue", tenantId, branchId, day],
    queryFn: () =>
      unwrap(
        api.GET("/v1/events", {
          params: {
            query: { branch_id: branchId, review_status: "new", from: start.toISOString(), to: end.toISOString(), limit: 200 },
          },
        }),
      ),
  });
  const [done, setDone] = useState<string[]>([]);
  const [index, setIndex] = useState(0);
  const queue: EventOut[] = (events.data?.items ?? []).filter((e) => !done.includes(e.id)).reverse();
  const position = Math.min(index, Math.max(queue.length - 1, 0));
  const current = queue[position];

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLElement && e.target.closest("input, textarea, select")) return;
      if (e.code === "KeyS" || e.code === "ArrowDown") {
        e.preventDefault();
        setIndex((i) => i + 1);
      } else if (e.code === "KeyB" || e.code === "ArrowUp") {
        e.preventDefault();
        setIndex((i) => Math.max(i - 1, 0));
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  if (events.isPending) return <Spinner />;
  if (events.isError) return <ErrorText error={events.error} />;
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <Badge tone={queue.length ? "amber" : "green"}>{t("review.left", { count: queue.length })}</Badge>
        {done.length > 0 && <Badge tone="green">{t("review.done", { count: done.length })}</Badge>}
        {(events.data?.next_cursor ?? null) && <span className="text-xs text-slate-500">{t("review.more")}</span>}
        <span className="ms-auto text-xs text-slate-500">{t("review.keys")}</span>
      </div>
      {current ? (
        <Card className="p-4">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <h2 className="font-semibold">
              {t(key.eventType(current.event_type))} · {position + 1} / {queue.length}
            </h2>
            <div className="flex gap-1">
              <Button size="sm" variant="ghost" disabled={position === 0} onClick={() => setIndex(position - 1)}>
                {t("review.previous")}
              </Button>
              <Button size="sm" variant="ghost" disabled={position >= queue.length - 1} onClick={() => setIndex(position + 1)}>
                {t("review.skip")}
              </Button>
            </div>
          </div>
          <EventPanel key={current.id} event={current} shortcuts onReviewed={(e) => setDone((d) => [...d, e.id])} />
        </Card>
      ) : (
        <Empty>{t("review.allDone")}</Empty>
      )}
    </div>
  );
}

/** Where the false alarms come from (plan 9.5): the place to start tuning. */
function Tuning({ branchId }: { branchId: string }) {
  const { t } = useI18n();
  const { tenantId } = useAuth();
  const summary = useQuery({
    queryKey: ["review-summary", tenantId, branchId],
    queryFn: () => unwrap(api.GET("/v1/review-summary", { params: { query: { branch_id: branchId } } })),
  });
  if (summary.isPending) return <Spinner />;
  if (summary.isError) return <ErrorText error={summary.error} />;
  const { days, groups } = summary.data;
  return (
    <Card className="flex flex-col gap-3 p-4">
      <h2 className="font-semibold">{t("review.tuning")}</h2>
      <p className="text-sm text-slate-600">{t("review.tuningHelp")}</p>
      <div className="flex flex-wrap gap-2" dir="ltr">
        {days.map((d) => (
          <div key={d.date} className="min-w-20 rounded border border-slate-200 p-2 text-center text-xs">
            <p className="text-slate-500">{d.date.slice(5)}</p>
            <p className="font-semibold tabular-nums">
              {d.reviewed}/{d.total}
            </p>
            <p className={d.reviewed < d.total ? "text-amber-700" : "text-emerald-700"}>
              {d.total ? `${Math.round((100 * d.reviewed) / d.total)}%` : "—"}
            </p>
          </div>
        ))}
      </div>
      {groups.length ? (
        <Table>
          <thead>
            <tr>
              <th>{t("review.type")}</th>
              <th>{t("common.camera")}</th>
              <th>{t("common.zone")}</th>
              <th>{t("reports.total")}</th>
              <th>{t("review.valid")}</th>
              <th>{t("review.falseAlarms")}</th>
              <th>{t("review.notReviewed")}</th>
              <th>{t("review.precision")}</th>
              <th>{t("review.perDay")}</th>
              <th>{t("review.confidence")}</th>
            </tr>
          </thead>
          <tbody>
            {groups.map((g) => (
              <tr key={`${g.event_type}-${g.camera_id}-${g.zone_id ?? ""}`}>
                <td>{t(key.eventType(g.event_type))}</td>
                <td>{g.camera}</td>
                <td>{g.zone ?? "—"}</td>
                <td className="tabular-nums">{g.total}</td>
                <td className="tabular-nums">{g.valid}</td>
                <td className={`tabular-nums ${g.false_alarm ? "font-semibold text-red-700" : ""}`}>{g.false_alarm}</td>
                <td className="tabular-nums">{g.unreviewed}</td>
                <td className="tabular-nums">{pct(g.precision)}</td>
                <td className="tabular-nums">{g.false_alarms_per_day}</td>
                <td className="text-xs tabular-nums" dir="ltr">
                  {pct(g.mean_confidence_valid)} / {pct(g.mean_confidence_false)}
                </td>
              </tr>
            ))}
          </tbody>
        </Table>
      ) : (
        <Empty />
      )}
      <p className="text-xs text-slate-500">
        {t("review.tuneWhere")}{" "}
        <Link className="text-brand-700 hover:underline" href="/settings/features/">
          {t("nav.features")}
        </Link>{" "}
        ·{" "}
        <Link className="text-brand-700 hover:underline" href="/settings/zones/">
          {t("nav.zones")}
        </Link>
      </p>
    </Card>
  );
}

function Review() {
  const { t } = useI18n();
  const { branchId, timezoneOf } = useBranches();
  const timeZone = timezoneOf(branchId);
  const [day, setDay] = useState(() => localDate(new Date(), timeZone));
  return (
    <>
      <PageHeader title={t("review.title")}>
        <Field label={t("common.date")}>
          {(id) => <Input id={id} type="date" value={day} max={localDate(new Date(), timeZone)} onChange={(e) => setDay(e.target.value)} />}
        </Field>
      </PageHeader>
      <p className="mb-4 max-w-3xl text-sm text-slate-600">{t("review.help")}</p>
      {branchId ? (
        <div className="flex flex-col gap-4">
          <Queue key={`${branchId}-${day}`} branchId={branchId} day={day} timeZone={timeZone} />
          <Tuning branchId={branchId} />
        </div>
      ) : (
        <Notice tone="amber">{t("review.chooseBranch")}</Notice>
      )}
    </>
  );
}

export default function ReviewPage() {
  return (
    <RequirePermission anyOf={["events_review"]}>
      <Review />
    </RequirePermission>
  );
}
