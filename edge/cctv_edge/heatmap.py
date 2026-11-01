"""Heatmaps: where people stand, per camera, in hourly buckets
(`cctv_contracts.v1.HeatmapBucketIn`).

Every frame, each person's foot point adds the time since the previous frame to its cell
of a columns x rows grid over the picture. Staff (seen in a staff zone of the camera) are
left out, as in customer counts. Ignore zones are already applied to the detections.

A bucket is handed over when the first frame of the next bucket arrives, or by `flush()`
when the camera stops (the outbox adds a partial bucket to what it already has).
"""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID

from cctv_contracts.v1 import HEATMAP_COLUMNS, HEATMAP_ROWS, HeatmapBucketIn
from cctv_edge.counts import MAX_FRAME_GAP_SECONDS, StaffMarker
from cctv_edge.detection import PERSON
from cctv_edge.geometry import Point
from cctv_edge.tracking import Track

BUCKET_SECONDS = 3600


@dataclass
class HeatmapAggregator:
    camera_id: UUID
    staff_areas: Sequence[Sequence[Point]] = ()
    columns: int = HEATMAP_COLUMNS
    rows: int = HEATMAP_ROWS
    bucket_seconds: int = BUCKET_SECONDS
    _bucket: int | None = field(default=None, init=False)
    _seconds: dict[int, float] = field(default_factory=lambda: defaultdict(float), init=False)
    _last_ts: float | None = field(default=None, init=False)
    _staff: StaffMarker = field(init=False)

    def __post_init__(self) -> None:
        self._staff = StaffMarker(self.staff_areas)

    def cell(self, point: Point) -> int:
        x, y = point
        column = min(self.columns - 1, max(0, int(x * self.columns)))
        row = min(self.rows - 1, max(0, int(y * self.rows)))
        return row * self.columns + column

    def update(self, tracks: Sequence[Track], ts: float) -> list[HeatmapBucketIn]:
        """Add this frame; returns buckets that just finished."""
        finished: list[HeatmapBucketIn] = []
        bucket = int(ts // self.bucket_seconds) * self.bucket_seconds
        if self._bucket is not None and bucket != self._bucket:
            finished = self.flush()
        if self._bucket is None:
            self._bucket = bucket
        dt = 0.0 if self._last_ts is None else ts - self._last_ts
        self._last_ts = ts
        people = [t for t in tracks if t.label == PERSON]
        staff = self._staff.mark(people, ts)
        if 0 < dt <= MAX_FRAME_GAP_SECONDS:  # a stream gap isn't time spent standing
            for track in people:
                if track.id not in staff:
                    self._seconds[self.cell(track.box.foot)] += dt
        return finished

    def flush(self) -> list[HeatmapBucketIn]:
        """The current (possibly partial) bucket, and start afresh. Empty buckets (nobody
        in view) are sent too, so an hour without anyone shows as such."""
        if self._bucket is None:
            return []
        bucket = HeatmapBucketIn(
            camera_id=self.camera_id,
            bucket_start=datetime.fromtimestamp(self._bucket, timezone.utc),
            bucket_seconds=self.bucket_seconds,
            columns=self.columns,
            rows=self.rows,
            cells=sorted((i, round(s)) for i, s in self._seconds.items() if round(s) >= 1),
        )
        self._bucket, self._last_ts = None, None
        self._seconds = defaultdict(float)
        return [bucket]
