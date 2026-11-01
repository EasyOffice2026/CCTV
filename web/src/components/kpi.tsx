"use client";

import { useI18n } from "@/lib/i18n";

/** Figures shared by the reports and the owner overview. */
export function Kpi({ label, value, tone }: { label: string; value: string; tone?: "red" | "amber" | "green" }) {
  const color = tone === "red" ? "text-red-700" : tone === "amber" ? "text-amber-700" : tone === "green" ? "text-emerald-700" : "text-slate-900";
  return (
    <div className="rounded-md border border-slate-200 bg-white p-3">
      <p className="text-xs text-slate-500">{label}</p>
      <p className={`mt-1 text-xl font-semibold tabular-nums ${color}`} dir="ltr">
        {value}
      </p>
    </div>
  );
}

export function HourChart({ hours }: { hours: number[] }) {
  const { t } = useI18n();
  const max = Math.max(...hours, 1);
  return (
    <div>
      <div className="flex h-32 items-end gap-0.5" dir="ltr" role="img" aria-label={t("reports.byHour")}>
        {hours.map((count, hour) => (
          <div key={hour} className="flex h-full flex-1 flex-col justify-end" title={`${String(hour).padStart(2, "0")}:00 · ${count}`}>
            <div className="rounded-t bg-brand-600" style={{ height: `${(count / max) * 100}%`, minHeight: count ? 2 : 0 }} />
          </div>
        ))}
      </div>
      <div className="mt-1 flex gap-0.5 text-[10px] text-slate-400" dir="ltr">
        {hours.map((_, hour) => (
          <span key={hour} className="flex-1 text-center">
            {hour % 3 === 0 ? String(hour).padStart(2, "0") : ""}
          </span>
        ))}
      </div>
    </div>
  );
}
