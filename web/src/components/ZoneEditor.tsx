"use client";

import { useRef, useState } from "react";

import { LINE_TYPES, insertPoint, nearestPoint, toFrame, type Point } from "@/lib/geometry";

export type ZoneShape = { id?: string; name: string; type: string; points: Point[] };

const HANDLE_RADIUS = 0.02; // normalised; how close a click must be to grab a point

/**
 * Draws zones over a camera frame. Coordinates are normalised (0..1). The selected zone is
 * editable: click adds a point, dragging moves one, right-click removes one.
 */
export function ZoneEditor({
  imageUrl,
  zones,
  selected,
  onChange,
  readOnly = false,
}: {
  imageUrl: string | null;
  zones: ZoneShape[];
  selected: ZoneShape | null;
  onChange: (points: Point[]) => void;
  readOnly?: boolean;
}) {
  const area = useRef<HTMLDivElement>(null);
  const [dragging, setDragging] = useState<number | null>(null);

  const pointAt = (e: React.PointerEvent | React.MouseEvent): Point =>
    toFrame(e.clientX, e.clientY, area.current!.getBoundingClientRect());

  const isLine = selected ? LINE_TYPES.has(selected.type) : false;
  const editable = !readOnly && !!selected;

  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!editable || e.button !== 0) return;
    const p = pointAt(e);
    const hit = nearestPoint(selected.points, p, HANDLE_RADIUS);
    if (hit >= 0) {
      setDragging(hit);
      e.currentTarget.setPointerCapture(e.pointerId);
      return;
    }
    if (isLine) {
      // A line has two points: the third click starts it again.
      onChange(selected.points.length >= 2 ? [p] : [...selected.points, p]);
    } else if (selected.points.length < 64) {
      onChange(insertPoint(selected.points, p));
    }
  };

  const onPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    if (dragging === null || !selected) return;
    const p = pointAt(e);
    onChange(selected.points.map((q, i) => (i === dragging ? p : q)));
  };

  const onContextMenu = (e: React.MouseEvent<HTMLDivElement>) => {
    if (!editable) return;
    const hit = nearestPoint(selected.points, pointAt(e), HANDLE_RADIUS);
    if (hit >= 0) {
      e.preventDefault();
      onChange(selected.points.filter((_, i) => i !== hit));
    }
  };

  const shape = (zone: ZoneShape, active: boolean) => {
    const points = zone.points.map(([x, y]) => `${x},${y}`).join(" ");
    const stroke = active ? "#2f6fde" : zone.type === "ignore" ? "#64748b" : "#f59e0b";
    const common = { stroke, strokeWidth: active ? 3 : 2, vectorEffect: "non-scaling-stroke" as const };
    if (LINE_TYPES.has(zone.type) || zone.points.length < 3) {
      return <polyline key={zone.id ?? "draft"} points={points} fill="none" {...common} />;
    }
    return (
      <polygon
        key={zone.id ?? "draft"}
        points={points}
        fill={active ? "rgba(47,111,222,0.25)" : zone.type === "ignore" ? "rgba(100,116,139,0.35)" : "rgba(245,158,11,0.15)"}
        {...common}
      />
    );
  };

  return (
    <div
      ref={area}
      dir="ltr"
      className={`video-frame relative w-full touch-none overflow-hidden rounded bg-slate-300 select-none ${editable ? "cursor-crosshair" : ""}`}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={() => setDragging(null)}
      onContextMenu={onContextMenu}
    >
      {imageUrl && (
        // eslint-disable-next-line @next/next/no-img-element -- local object URL of the frame
        <img src={imageUrl} alt="" draggable={false} className="pointer-events-none absolute inset-0 size-full object-fill" />
      )}
      <svg viewBox="0 0 1 1" preserveAspectRatio="none" className="pointer-events-none absolute inset-0 size-full">
        {zones.filter((z) => z.id !== selected?.id).map((z) => shape(z, false))}
        {selected && shape(selected, true)}
      </svg>
      {zones
        .filter((z) => z.id !== selected?.id && z.points.length)
        .map((z) => {
          const [x, y] = z.points[0]!;
          return (
            <span
              key={`label-${z.id}`}
              className="pointer-events-none absolute rounded bg-slate-900/70 px-1 text-[10px] text-white"
              style={{ left: `${x * 100}%`, top: `${y * 100}%` }}
            >
              {z.name}
            </span>
          );
        })}
      {editable &&
        selected.points.map(([x, y], i) => (
          <span
            key={i}
            className="pointer-events-none absolute size-3 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-white bg-brand-600 shadow"
            style={{ left: `${x * 100}%`, top: `${y * 100}%` }}
          />
        ))}
    </div>
  );
}
