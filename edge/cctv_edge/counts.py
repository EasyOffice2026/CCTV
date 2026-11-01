"""People counts per camera, in time buckets (`cctv_contracts.v1.CountBucketIn`).

- Entrance lines: tracks whose foot point crosses the line, in and out.
- Area zones (counter, queue, dining): the most people seen in the zone at once, and how
  long at least one person was there (`occupied_seconds`, e.g. how long the counter was
  staffed).
- Staff are left out of customer counts (plan 10.2): a track seen in a staff zone of the
  camera (staff area, kitchen, behind the counter) is staff from then on, and isn't
  counted at entrance lines or in customer zones (queue, dining). The crossings left out
  are reported (`staff_crossings`). Staff who never step into a staff zone in view are
  still counted; a uniform classifier or face-matched staff would catch them (later).
- Queue zones: how long each person's track stayed in the zone. A wait ends when the
  person has been out of the zone for `LEAVE_GRACE_SECONDS`; waits shorter than
  `MIN_WAIT_SECONDS` (passing through) are ignored. A track that is lost and found again
  under a new id splits a wait in two, so waits are a lower bound.

A bucket is handed over when the first frame of the next bucket arrives, or by `flush()`
when the camera stops (the outbox adds a partial bucket to what it already has).
"""

from collections.abc import Collection, Mapping, Sequence
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
LEAVE_GRACE_SECONDS = 3.0
MIN_WAIT_SECONDS = 5.0
# Staff tracks are forgotten after this long unseen (tracks don't come back after that).
FORGET_SECONDS = 600.0


@dataclass
class _Totals:
    in_count: int = 0
    out_count: int = 0
    max_occupancy: int = 0
    occupied_seconds: float = 0.0
    staff_crossings: int = 0
    waits: int = 0
    wait_total: float = 0.0
    wait_max: float = 0.0


@dataclass
class StaffMarker:
    """Tracks seen in a staff zone (staff area, kitchen, behind the counter) are staff from
    then on, until unseen for FORGET_SECONDS. Shared by counts and heatmaps."""

    staff_areas: Sequence[Sequence[Point]] = ()
    _staff: dict[int, float] = field(default_factory=dict, init=False)  # track -> last seen

    def mark(self, people: Sequence[Track], ts: float) -> set[int]:
        """Ids of the staff tracks, after looking at this frame's people."""
        for track in people:
            if track.id in self._staff or any(
                point_in_polygon(track.box.foot, area) for area in self.staff_areas
            ):
                self._staff[track.id] = ts
        for track_id, seen in list(self._staff.items()):
            if ts - seen > FORGET_SECONDS:
                del self._staff[track_id]
        return set(self._staff)


@dataclass
class _Wait:
    zone_id: UUID
    entered: float
    last_inside: float


@dataclass
class CountAggregator:
    camera_id: UUID
    lines: Mapping[UUID, tuple[Point, Point]] = field(default_factory=dict)
    areas: Mapping[UUID, Sequence[Point]] = field(default_factory=dict)
    # Areas where staff are left out (queue, dining) and areas where waits are timed (queue).
    customer_areas: Collection[UUID] = ()
    queue_areas: Collection[UUID] = ()
    # Where only staff go: a person seen here is staff.
    staff_areas: Sequence[Sequence[Point]] = ()
    bucket_seconds: int = BUCKET_SECONDS
    _bucket: int | None = field(default=None, init=False)
    _totals: dict[UUID, _Totals] = field(default_factory=dict, init=False)
    _last_ts: float | None = field(default=None, init=False)
    _staff: StaffMarker = field(init=False)
    _waits: dict[int, _Wait] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self._staff = StaffMarker(self.staff_areas)

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
        staff = self._staff.mark(people, ts)

        for zone_id, (a, b) in self.lines.items():
            totals = self._totals[zone_id]
            for track in people:
                if track.previous_box is None:
                    continue
                direction = line_crossing(track.previous_box.foot, track.box.foot, a, b)
                if direction and track.id in staff:
                    totals.staff_crossings += 1
                elif direction > 0:
                    totals.in_count += 1
                elif direction < 0:
                    totals.out_count += 1
        for zone_id, polygon in self.areas.items():
            totals = self._totals[zone_id]
            customers_only = zone_id in self.customer_areas
            inside = [
                t
                for t in people
                if point_in_polygon(t.box.foot, polygon) and not (customers_only and t.id in staff)
            ]
            totals.max_occupancy = max(totals.max_occupancy, len(inside))
            if inside and 0 < dt <= MAX_FRAME_GAP_SECONDS:
                totals.occupied_seconds += dt
            if zone_id in self.queue_areas:
                for track in inside:
                    wait = self._waits.get(track.id)
                    if wait is None or wait.zone_id != zone_id:
                        self._waits[track.id] = _Wait(zone_id, ts, ts)
                    else:
                        wait.last_inside = ts
        self._end_waits(ts)
        return finished

    def _end_waits(self, ts: float, force: bool = False) -> None:
        for track_id, wait in list(self._waits.items()):
            if not force and ts - wait.last_inside <= LEAVE_GRACE_SECONDS:
                continue
            del self._waits[track_id]
            seconds = wait.last_inside - wait.entered
            totals = self._totals.get(wait.zone_id)
            if seconds < MIN_WAIT_SECONDS or totals is None:
                continue
            totals.waits += 1
            totals.wait_total += seconds
            totals.wait_max = max(totals.wait_max, seconds)

    def flush(self) -> list[CountBucketIn]:
        """The current (possibly partial) bucket, and start afresh. People still in a
        queue are counted in the bucket they leave it."""
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
                        staff_crossings=totals.staff_crossings,
                    )
                )
            else:
                queue = zone_id in self.queue_areas
                buckets.append(
                    CountBucketIn(
                        camera_id=self.camera_id,
                        zone_id=zone_id,
                        bucket_start=start,
                        bucket_seconds=self.bucket_seconds,
                        max_occupancy=totals.max_occupancy,
                        occupied_seconds=min(round(totals.occupied_seconds), self.bucket_seconds),
                        waits=totals.waits if queue else None,
                        wait_total_seconds=round(totals.wait_total) if queue else None,
                        wait_max_seconds=round(totals.wait_max) if queue else None,
                    )
                )
        self._bucket, self._totals, self._last_ts = None, {}, None
        return buckets
