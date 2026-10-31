import { describe, expect, it } from "vitest";

import { dayRange, formatDuration, fractionOf, localDate, tzOffsetMinutes, zonedToUtc } from "./time";

describe("branch time zones", () => {
  it("knows Kuwait is UTC+3 all year", () => {
    expect(tzOffsetMinutes(new Date("2026-01-15T12:00:00Z"), "Asia/Kuwait")).toBe(180);
    expect(tzOffsetMinutes(new Date("2026-07-15T12:00:00Z"), "Asia/Kuwait")).toBe(180);
  });

  it("converts a Kuwait wall-clock time to UTC", () => {
    expect(zonedToUtc("2026-10-31", "10:55", "Asia/Kuwait").toISOString()).toBe("2026-10-31T07:55:00.000Z");
  });

  it("handles daylight saving zones", () => {
    // London switches from BST (+1) to GMT on 2026-10-25.
    expect(zonedToUtc("2026-10-24", "12:00", "Europe/London").toISOString()).toBe("2026-10-24T11:00:00.000Z");
    expect(zonedToUtc("2026-10-26", "12:00", "Europe/London").toISOString()).toBe("2026-10-26T12:00:00.000Z");
  });

  it("gives the local day around an instant", () => {
    // 22:30 UTC is already the next day in Kuwait.
    expect(localDate(new Date("2026-10-30T22:30:00Z"), "Asia/Kuwait")).toBe("2026-10-31");
    const { start, end } = dayRange("2026-10-31", "Asia/Kuwait");
    expect(start.toISOString()).toBe("2026-10-30T21:00:00.000Z");
    expect(end.toISOString()).toBe("2026-10-31T21:00:00.000Z");
  });
});

describe("timeline helpers", () => {
  it("formats durations", () => {
    expect(formatDuration(8)).toBe("0:08");
    expect(formatDuration(125)).toBe("2:05");
    expect(formatDuration(3725)).toBe("1:02:05");
  });

  it("places instants on a range", () => {
    const start = new Date("2026-10-31T00:00:00Z");
    const end = new Date("2026-10-31T10:00:00Z");
    expect(fractionOf(new Date("2026-10-31T05:00:00Z"), start, end)).toBe(0.5);
    expect(fractionOf(new Date("2026-10-30T05:00:00Z"), start, end)).toBe(0);
    expect(fractionOf(new Date("2026-11-01T05:00:00Z"), start, end)).toBe(1);
  });
});
