/**
 * Times are shown in the branch's time zone (Asia/Kuwait by default), never the
 * viewer's: a manager abroad must see the same times as the restaurant.
 */

/** Offset of `timeZone` from UTC at `instant`, in minutes (Kuwait: +180). */
export function tzOffsetMinutes(instant: Date, timeZone: string): number {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone,
    hourCycle: "h23",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).formatToParts(instant);
  const get = (type: string) => Number(parts.find((p) => p.type === type)?.value);
  const asUtc = Date.UTC(get("year"), get("month") - 1, get("day"), get("hour"), get("minute"), get("second"));
  return Math.round((asUtc - Math.floor(instant.getTime() / 1000) * 1000) / 60000);
}

/** The instant when the wall clock in `timeZone` shows the given local date and time. */
export function zonedToUtc(
  date: string,
  time: string,
  timeZone: string,
): Date {
  const [y, m, d] = date.split("-").map(Number) as [number, number, number];
  const [hh, mm] = time.split(":").map(Number) as [number, number];
  const guess = Date.UTC(y, m - 1, d, hh, mm);
  let result = guess - tzOffsetMinutes(new Date(guess), timeZone) * 60000;
  // Second pass in case the offset differs at the result (DST changes).
  result = guess - tzOffsetMinutes(new Date(result), timeZone) * 60000;
  return new Date(result);
}

/** "YYYY-MM-DD" of `instant` in `timeZone`. */
export function localDate(instant: Date, timeZone: string): string {
  return new Intl.DateTimeFormat("en-CA", { timeZone, year: "numeric", month: "2-digit", day: "2-digit" }).format(
    instant,
  );
}

/** Start and end (exclusive) of a local calendar day. */
export function dayRange(date: string, timeZone: string): { start: Date; end: Date } {
  const start = zonedToUtc(date, "00:00", timeZone);
  const [y, m, d] = date.split("-").map(Number) as [number, number, number];
  const next = new Date(Date.UTC(y, m - 1, d + 1)).toISOString().slice(0, 10);
  return { start, end: zonedToUtc(next, "00:00", timeZone) };
}

export function formatDateTime(iso: string | Date, timeZone: string, locale: string): string {
  return new Intl.DateTimeFormat(locale, {
    timeZone,
    dateStyle: "medium",
    timeStyle: "medium",
    hourCycle: "h23",
  }).format(new Date(iso));
}

export function formatTime(iso: string | Date, timeZone: string, locale: string): string {
  return new Intl.DateTimeFormat(locale, { timeZone, timeStyle: "medium", hourCycle: "h23" }).format(
    new Date(iso),
  );
}

export function formatDuration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const rest = s % 60;
  if (h) return `${h}:${String(m).padStart(2, "0")}:${String(rest).padStart(2, "0")}`;
  return `${m}:${String(rest).padStart(2, "0")}`;
}

/** Position of `instant` within [start, end) as a fraction 0..1 (clamped). */
export function fractionOf(instant: Date, start: Date, end: Date): number {
  const span = end.getTime() - start.getTime();
  if (span <= 0) return 0;
  return Math.min(1, Math.max(0, (instant.getTime() - start.getTime()) / span));
}
