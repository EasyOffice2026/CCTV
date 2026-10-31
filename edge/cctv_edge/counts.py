"""People counts per camera, in time buckets (`cctv_contracts.v1.CountBucketIn`).

- Entrance lines: tracks whose foot point crosses the line, in and out.
- Area zones (counter, queue): the most people seen in the zone at once, and how long at
  least one person was there (`occupied_seconds`, e.g. how long the counter was staffed).

A bucket is handed over when the first frame of the next bucket arrives, or by `flush()`
when the camera stops (the outbox adds a partial bucket to what it already has).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID

from cctv_contracts.v1 import CountBucketIn
from cctv_edge.detection import PERSON
from cctv_edge.geometry import Point, line_crossing, point_in_polygon
from cctv_edge.tracking import Track

BUCKET_SECONDS = 300
# Longer gaps between frames (stream down) don't count as occupied time.
MAX_FRAME_GAP_SECONDS = 2.0


@dataclass
class _Totals:
    in_count: int = 0
    out_count: int = 0
    max_occupancy: int = 0
    occupied_seconds: float = 0.0


@dataclass
class CountAggregator:
    camera_id: UUID
    lines: Mapping[UUID, tuple[Point, Point]] = field(default_factory=dict)
    areas: Mapping[UUID, Sequence[Point]] = field(default_factory=dict)
    bucket_seconds: int = BUCKET_SECONDS
    _bucket: int | None = field(default=None, init=False)
    _totals: dict[UUID, _Totals] = field(default_factory=dict, init=False)
    _last_ts: float | None = field(default=None, init=False)

    @property
    def active(self) -> bool:
        return bool(self.lines or self.areas)

    def update(self, tracks: Sequence[Track], ts: float) -> list[CountBucketIn]:
        """Count this frame; returns buckets that just finished."""
        if not self.active:
            return []
        finished: list[CountBucketIn] = []
        bucket = int(ts // self.bucket_seconds) * self.bucket_seconds
        if self._bucket is not None and bucket != self._bucket:
            finished = self.flush()
        if self._bucket is None:
            self._bucket = bucket
            self._totals = {zone_id: _Totals() for zone_id in (*self.lines, *self.areas)}
        dt = 0.0 if self._last_ts is None else ts - self._last_ts
        self._last_ts = ts
        people = [t for t in tracks if t.label == PERSON]

        for zone_id, (a, b) in self.lines.items():
            totals = self._totals[zone_id]
            for track in people:
                if track.previous_box is None:
                    continue
                direction = line_crossing(track.previous_box.foot, track.box.foot, a, b)
                if direction > 0:
                    totals.in_count += 1
                elif direction < 0:
                    totals.out_count += 1
        for zone_id, polygon in self.areas.items():
            totals = self._totals[zone_id]
            inside = sum(point_in_polygon(t.box.foot, polygon) for t in people)
            totals.max_occupancy = max(totals.max_occupancy, inside)
            if inside and 0 < dt <= MAX_FRAME_GAP_SECONDS:
                totals.occupied_seconds += dt
        return finished

    def flush(self) -> list[CountBucketIn]:
        """The current (possibly partial) bucket, and start afresh."""
        if self._bucket is None:
            return []
        start = datetime.fromtimestamp(self._bucket, timezone.utc)
        buckets = []
        for zone_id, totals in self._totals.items():
            if zone_id in self.lines:
                buckets.append(
                    CountBucketIn(
                        camera_id=self.camera_id,
                        zone_id=zone_id,
                        bucket_start=start,
                        bucket_seconds=self.bucket_seconds,
                        in_count=totals.in_count,
                        out_count=totals.out_count,
                    )
                )
            else:
                buckets.append(
                    CountBucketIn(
                        camera_id=self.camera_id,
                        zone_id=zone_id,
                        bucket_start=start,
                        bucket_seconds=self.bucket_seconds,
                        max_occupancy=totals.max_occupancy,
                        occupied_seconds=min(round(totals.occupied_seconds), self.bucket_seconds),
                    )
                )
        self._bucket, self._totals, self._last_ts = None, {}, None
        return buckets
