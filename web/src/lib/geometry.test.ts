import { describe, expect, it } from "vitest";

import { insertPoint, nearestPoint, selfIntersects, toFrame, validShape, type Point } from "./geometry";

const square: Point[] = [
  [0.1, 0.1],
  [0.5, 0.1],
  [0.5, 0.5],
  [0.1, 0.5],
];

describe("zone geometry", () => {
  it("maps pointer positions into the frame and clamps", () => {
    const rect = { left: 100, top: 50, width: 400, height: 200 };
    expect(toFrame(300, 150, rect)).toEqual([0.5, 0.5]);
    expect(toFrame(0, 1000, rect)).toEqual([0, 1]);
  });

  it("finds the point under the pointer", () => {
    expect(nearestPoint(square, [0.51, 0.49], 0.03)).toBe(2);
    expect(nearestPoint(square, [0.3, 0.3], 0.03)).toBe(-1);
  });

  it("inserts new points into the closest edge", () => {
    const result = insertPoint(square, [0.3, 0.09]); // near the top edge
    expect(result).toEqual([[0.1, 0.1], [0.3, 0.09], [0.5, 0.1], [0.5, 0.5], [0.1, 0.5]]);
    expect(insertPoint(square.slice(0, 2), [0.3, 0.3])).toHaveLength(3);
  });

  it("detects crossing edges", () => {
    expect(selfIntersects(square)).toBe(false);
    const bowTie: Point[] = [
      [0.1, 0.1],
      [0.5, 0.5],
      [0.5, 0.1],
      [0.1, 0.5],
    ];
    expect(selfIntersects(bowTie)).toBe(true);
  });

  it("applies the API's shape rules", () => {
    expect(validShape("kitchen", square)).toBe(true);
    expect(validShape("kitchen", square.slice(0, 2))).toBe(false);
    expect(validShape("entrance_line", square.slice(0, 2))).toBe(true);
    expect(validShape("entrance_line", square)).toBe(false);
  });
});
