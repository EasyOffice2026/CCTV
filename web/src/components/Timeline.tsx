"use client";

import { useI18n } from "@/lib/i18n";
import { SEVERITY_TONE, key } from "@/lib/labels";
import type { Schemas } from "@/lib/api";
import { fractionOf } from "@/lib/time";

const MARKER_COLOUR = { red: "bg-red-500", amber: "bg-amber-500", blue: "bg-brand-500", green: "bg-emerald-500", slate: "bg-slate-500" };

/**
 * One day of a camera: what the NVR recorded (green) and events (markers). Clicking picks
 * a time. Time runs left to right in both languages, like a video player.
 */
export function Timeline({
  start,
  end,
  recordings,
  events,
  cursor,
  onPick,
  onEvent,
}: {
  start: Date;
  end: Date;
  recordings: Schemas["RecordingSpan"][];
  events: Schemas["EventOut"][];
  cursor: Date | null;
  onPick: (instant: Date) => void;
  onEvent: (event: Schemas["EventOut"]) => void;
}) {
  const { t } = useI18n();
  const pct = (iso: string | Date) => `${fractionOf(new Date(iso), start, end) * 100}%`;
  const hours = Array.from({ length: 25 }, (_, h) => h);

  return (
    <div dir="ltr" className="select-none">
      <div
        role="slider"
        tabIndex={0}
        aria-label={t("playback.clickTimeline")}
        aria-valuemin={0}
        aria-valuemax={24}
        aria-valuenow={cursor ? fractionOf(cursor, start, end) * 24 : 0}
        className="relative h-12 cursor-pointer rounded bg-slate-200"
        onClick={(e) => {
          const rect = e.currentTarget.getBoundingClientRect();
          const fraction = (e.clientX - rect.left) / rect.width;
          onPick(new Date(start.getTime() + fraction * (end.getTime() - start.getTime())));
        }}
      >
        {recordings.map((span) => (
          <div
            key={span.start}
            className="absolute inset-y-2 bg-emerald-400/80"
            style={{
              left: pct(span.start),
              width: `calc(${pct(span.end)} - ${pct(span.start)})`,
            }}
          />
        ))}
        {events.map((event) => (
          <button
            key={event.id}
            type="button"
            title={t(key.eventType(event.event_type))}
            aria-label={t(key.eventType(event.event_type))}
            onClick={(e) => {
              e.stopPropagation();
              onEvent(event);
            }}
            className={`absolute inset-y-0 w-1 -translate-x-1/2 rounded ${MARKER_COLOUR[SEVERITY_TONE[event.severity]]}`}
            style={{ left: pct(event.started_at) }}
          />
        ))}
        {cursor && <div className="pointer-events-none absolute inset-y-0 w-0.5 bg-slate-900" style={{ left: pct(cursor) }} />}
      </div>
      <div className="relative mt-1 h-4 text-[10px] text-slate-500">
        {hours
          .filter((h) => h % 3 === 0)
          .map((h) => (
            <span key={h} className="absolute -translate-x-1/2" style={{ left: `${(h / 24) * 100}%` }}>
              {String(h).padStart(2, "0")}
            </span>
          ))}
      </div>
    </div>
  );
}
