"use client";

import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import { RequirePermission } from "@/components/RequirePermission";
import { Button, Card, ErrorText, Field, Notice, PageHeader, Select, Spinner } from "@/components/ui";
import { api, ensureOk, rawFetch, unwrap } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useBranches } from "@/lib/branches";
import { useI18n } from "@/lib/i18n";
import type { MessageKey } from "@/lib/messages/en";
import { canAny } from "@/lib/permissions";
import { useCameras } from "@/lib/queries";
import { dayRange, localDate } from "@/lib/time";

// Days in each period, ending today (yesterday: ending yesterday).
const PERIODS = { today: 1, yesterday: 1, week: 7, month: 30, quarter: 90 } as const;
type Period = keyof typeof PERIODS;

// Sequential: one hue, light (little time) to dark (most time); empty cells stay clear.
const RAMP = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"];

function shade(share: number): { color: string; opacity: number } {
  // Square root, so a few very busy cells don't wash the rest out.
  const t = Math.sqrt(share);
  return { color: RAMP[Math.min(RAMP.length - 1, Math.floor(t * RAMP.length))] ?? RAMP[0]!, opacity: 0.3 + 0.45 * t };
}

/** The calendar day `days` before `date` ("YYYY-MM-DD"). */
function daysBefore(date: string, days: number): string {
  const [y, m, d] = date.split("-").map(Number) as [number, number, number];
  return new Date(Date.UTC(y, m - 1, d - days)).toISOString().slice(0, 10);
}

/** The period in the branch's time zone: whole local days, up to the end of today. */
function range(period: Period, timeZone: string): { from: string; to: string } {
  const today = localDate(new Date(), timeZone);
  const last = period === "yesterday" ? daysBefore(today, 1) : today;
  const first = daysBefore(last, PERIODS[period] - 1);
  return { from: dayRange(first, timeZone).start.toISOString(), to: dayRange(last, timeZone).end.toISOString() };
}

const THIRDS = [
  ["top", "middle", "bottom"],
  ["left", "centre", "right"],
] as const;

/** Which ninth of the picture (3 x 3 areas, 0 = top left) a cell is in. */
function area(index: number, columns: number, rows: number): number {
  const row = Math.min(2, Math.floor(((Math.floor(index / columns) + 0.5) / rows) * 3));
  const column = Math.min(2, Math.floor((((index % columns) + 0.5) / columns) * 3));
  return row * 3 + column;
}

function areaName(ninth: number): MessageKey {
  return `heatmap.place.${THIRDS[0][Math.floor(ninth / 3)]}.${THIRDS[1][ninth % 3]}` as MessageKey;
}

function minutes(seconds: number): number {
  return Math.round(seconds / 60);
}

/** Where people (not staff) stood in a camera's view (later phase: heatmaps). */
function Heatmap() {
  const { t, locale } = useI18n();
  const { me } = useAuth();
  const { branchId, timezoneOf } = useBranches();
  const cameras = useCameras(branchId);
  const [chosen, setChosen] = useState("");
  const [period, setPeriod] = useState<Period>("week");
  const [hover, setHover] = useState<number | null>(null);
  const [hoverArea, setHoverArea] = useState<number | null>(null);
  const [frame, setFrame] = useState<{ camera: string; url: string } | null>(null);
  const [frameError, setFrameError] = useState<unknown>(null);
  const cameraId = chosen || cameras.data?.[0]?.id || "";
  const camera = cameras.data?.find((c) => c.id === cameraId);
  const timeZone = timezoneOf(camera?.branch_id ?? branchId);
  const { from, to } = range(period, timeZone);

  const heatmap = useQuery({
    queryKey: ["heatmap", cameraId, from, to],
    queryFn: () => unwrap(api.GET("/v1/heatmaps", { params: { query: { camera_id: cameraId, from, to } } })),
    enabled: !!cameraId,
  });
  const grid = heatmap.data ?? null;
  const peak = grid ? Math.max(0, ...grid.seconds) : 0;
  // The text version of the map: time per ninth of the picture, busiest first.
  const areas = useMemo(() => {
    if (!grid) return [];
    const totals = Array.from({ length: 9 }, (_, ninth) => ({ ninth, seconds: 0 }));
    grid.seconds.forEach((seconds, index) => {
      totals[area(index, grid.columns, grid.rows)]!.seconds += seconds;
    });
    return totals.filter((a) => a.seconds > 0).sort((a, b) => b.seconds - a.seconds);
  }, [grid]);
  const total = areas.reduce((sum, a) => sum + a.seconds, 0);

  const showFrame = async () => {
    setFrameError(null);
    try {
      const blob = await (await ensureOk(await rawFetch(`/v1/cameras/${cameraId}/frame`))).blob();
      setFrame((old) => {
        if (old) URL.revokeObjectURL(old.url);
        return { camera: cameraId, url: URL.createObjectURL(blob) };
      });
    } catch (e) {
      setFrameError(e);
    }
  };
  const picture = frame?.camera === cameraId ? frame.url : null;

  return (
    <>
      <PageHeader title={t("heatmap.title")} />
      <p className="mb-4 max-w-3xl text-sm text-slate-600">{t("heatmap.help")}</p>
      <div className="mb-4 flex flex-wrap items-end gap-3">
        <Field label={t("common.camera")}>
          {(id) => (
            <Select id={id} value={cameraId} onChange={(e) => setChosen(e.target.value)}>
              {(cameras.data ?? []).map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </Select>
          )}
        </Field>
        <Field label={t("heatmap.period")}>
          {(id) => (
            <Select id={id} value={period} onChange={(e) => setPeriod(e.target.value as Period)}>
              {(Object.keys(PERIODS) as Period[]).map((p) => (
                <option key={p} value={p}>
                  {t(`heatmap.period.${p}` as MessageKey)}
                </option>
              ))}
            </Select>
          )}
        </Field>
        {cameraId && canAny(me, "live_view", "devices_manage") && (
          <Button onClick={() => void showFrame()}>{t(picture ? "heatmap.refreshPicture" : "heatmap.showPicture")}</Button>
        )}
      </div>
      <ErrorText error={cameras.error ?? heatmap.error ?? frameError} />

      {heatmap.isLoading ? (
        <Spinner />
      ) : !grid ? (
        cameraId && <Notice tone="slate">{t("heatmap.none")}</Notice>
      ) : (
        <div className="grid gap-4 lg:grid-cols-[1fr_18rem]">
          <Card className="p-3">
            <div
              className="video-frame relative w-full overflow-hidden rounded bg-slate-700"
              role="img"
              aria-label={t("heatmap.aria", { hours: Math.round(grid.hours), minutes: minutes(peak) })}
              onPointerLeave={() => setHover(null)}
              onPointerMove={(e) => {
                const box = e.currentTarget.getBoundingClientRect();
                const column = Math.min(grid.columns - 1, Math.floor(((e.clientX - box.left) / box.width) * grid.columns));
                const row = Math.min(grid.rows - 1, Math.floor(((e.clientY - box.top) / box.height) * grid.rows));
                setHover(row * grid.columns + column);
              }}
            >
              {/* eslint-disable-next-line @next/next/no-img-element -- local object URL of the frame */}
              {picture && <img src={picture} alt="" className="absolute inset-0 h-full w-full object-cover" />}
              <div
                className="absolute inset-0 grid"
                style={{ gridTemplateColumns: `repeat(${grid.columns}, 1fr)`, gridTemplateRows: `repeat(${grid.rows}, 1fr)` }}
                dir="ltr"
              >
                {grid.seconds.map((seconds, index) => {
                  const { color, opacity } = seconds > 0 ? shade(seconds / peak) : { color: "transparent", opacity: 0 };
                  return (
                    <div key={index} style={{ backgroundColor: color, opacity }} className={index === hover ? "outline-2 -outline-offset-2 outline-white" : ""} />
                  );
                })}
              </div>
              {hoverArea !== null && (
                <div
                  className="pointer-events-none absolute border-2 border-white"
                  style={{ left: `${(hoverArea % 3) * 33.333}%`, top: `${Math.floor(hoverArea / 3) * 33.333}%`, width: "33.333%", height: "33.333%" }}
                  dir="ltr"
                />
              )}
              {hover !== null && (
                <div
                  className="pointer-events-none absolute rounded bg-white px-2 py-1 text-xs shadow"
                  style={{
                    left: `${(((hover % grid.columns) + 0.5) / grid.columns) * 100}%`,
                    top: `${(Math.floor(hover / grid.columns) / grid.rows) * 100}%`,
                    transform: "translate(-50%, -110%)",
                  }}
                >
                  <strong className="text-slate-900">{t("heatmap.minutes", { minutes: minutes(grid.seconds[hover] ?? 0) })}</strong>{" "}
                  <span className="text-slate-500">
                    {peak > 0 ? t("heatmap.ofBusiest", { percent: Math.round(((grid.seconds[hover] ?? 0) / peak) * 100) }) : ""}
                  </span>
                </div>
              )}
            </div>
            <div className="mt-3 flex items-center gap-2 text-xs text-slate-600">
              <span>{t("heatmap.less")}</span>
              {/* From "less" to "more" in reading order. */}
              <span className="h-2.5 w-40 rounded-sm" style={{ background: `linear-gradient(${locale === "ar" ? "to left" : "to right"}, ${RAMP.join(", ")})` }} />
              <span>{t("heatmap.more", { minutes: minutes(peak) })}</span>
            </div>
          </Card>
          <Card className="p-4 text-sm">
            <h2 className="mb-2 font-semibold text-slate-800">{t("heatmap.busiest")}</h2>
            {areas.length === 0 ? (
              <p className="text-slate-500">{t("heatmap.nobody")}</p>
            ) : (
              <ol className="flex flex-col gap-1">
                {areas.map((a) => (
                  <li
                    key={a.ninth}
                    className={`flex items-baseline gap-2 rounded px-1 ${hoverArea === a.ninth ? "bg-slate-100" : ""}`}
                    onPointerEnter={() => setHoverArea(a.ninth)}
                    onPointerLeave={() => setHoverArea(null)}
                  >
                    <span className="flex-1 text-slate-600">{t(areaName(a.ninth))}</span>
                    <strong className="text-slate-900">{t("heatmap.minutes", { minutes: minutes(a.seconds) })}</strong>
                    <span className="w-10 text-end text-xs text-slate-500">{Math.round((a.seconds / total) * 100)}%</span>
                  </li>
                ))}
              </ol>
            )}
            <p className="mt-4 text-xs text-slate-500">{t("heatmap.covered", { hours: Math.round(grid.hours) })}</p>
          </Card>
        </div>
      )}
    </>
  );
}

export default function HeatmapPage() {
  return (
    <RequirePermission anyOf={["events_view"]}>
      <Heatmap />
    </RequirePermission>
  );
}
