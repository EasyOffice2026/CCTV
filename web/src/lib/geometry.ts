/**
 * Zone geometry for the zone editor. Points are normalised to the frame (0..1), the same
 * as the API and the edge (cctv_contracts ZoneConfig), so zones survive resolution changes.
 */
export type Point = [number, number];

export const LINE_TYPES = new Set(["entrance_line"]);

export function clamp01(value: number): number {
  return Math.min(1, Math.max(0, value));
}

/** A pointer position inside `rect` as normalised frame coordinates, rounded to 4 places. */
export function toFrame(clientX: number, clientY: number, rect: { left: number; top: number; width: number; height: number }): Point {
  const round = (v: number) => Math.round(clamp01(v) * 10000) / 10000;
  return [round((clientX - rect.left) / rect.width), round((clientY - rect.top) / rect.height)];
}

/** Index of the point within `radius` (normalised units) of `p`, else -1. */
export function nearestPoint(points: Point[], p: Point, radius: number): number {
  let best = -1;
  let bestDistance = radius;
  points.forEach(([x, y], i) => {
    const distance = Math.hypot(x - p[0], y - p[1]);
    if (distance <= bestDistance) {
      best = i;
      bestDistance = distance;
    }
  });
  return best;
}

function distanceToSegment(p: Point, a: Point, b: Point): number {
  const dx = b[0] - a[0];
  const dy = b[1] - a[1];
  const lengthSquared = dx * dx + dy * dy;
  const t = lengthSquared ? clamp01(((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / lengthSquared) : 0;
  return Math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy));
}

/**
 * Add `p` to a polygon: between the two points of the closest edge, so clicking near an
 * edge refines the shape instead of creating a crossing.
 */
export function insertPoint(points: Point[], p: Point): Point[] {
  if (points.length < 3) return [...points, p];
  let bestEdge = points.length - 1; // the closing edge (last -> first)
  let bestDistance = Infinity;
  points.forEach((a, i) => {
    const b = points[(i + 1) % points.length] as Point;
    const distance = distanceToSegment(p, a, b);
    if (distance < bestDistance) {
      bestDistance = distance;
      bestEdge = i;
    }
  });
  return [...points.slice(0, bestEdge + 1), p, ...points.slice(bestEdge + 1)];
}

function cross(o: Point, a: Point, b: Point): number {
  return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
}

function segmentsCross(a: Point, b: Point, c: Point, d: Point): boolean {
  const d1 = cross(c, d, a);
  const d2 = cross(c, d, b);
  const d3 = cross(a, b, c);
  const d4 = cross(a, b, d);
  return ((d1 > 0 && d2 < 0) || (d1 < 0 && d2 > 0)) && ((d3 > 0 && d4 < 0) || (d3 < 0 && d4 > 0));
}

/** True when two non-adjacent edges of the polygon cross. */
export function selfIntersects(points: Point[]): boolean {
  const n = points.length;
  if (n < 4) return false;
  for (let i = 0; i < n; i++) {
    for (let j = i + 1; j < n; j++) {
      if (j === i + 1 || (i === 0 && j === n - 1)) continue; // adjacent edges share a point
      const a = points[i] as Point;
      const b = points[(i + 1) % n] as Point;
      const c = points[j] as Point;
      const d = points[(j + 1) % n] as Point;
      if (segmentsCross(a, b, c, d)) return true;
    }
  }
  return false;
}

/** The API's shape rule: lines have exactly 2 points, polygons 3 to 64. */
export function validShape(type: string, points: Point[]): boolean {
  if (LINE_TYPES.has(type)) return points.length === 2;
  return points.length >= 3 && points.length <= 64;
}
