"""Multi-object tracking: gives each person a stable id across frames.

ByteTrack-style association, written for this project:

1. Every track's box is predicted forward with its recent velocity.
2. Confident detections are matched to the predicted boxes (IoU).
3. Tracks still unmatched get a second chance with the *low*-confidence detections, at a
   stricter IoU. A person partly hidden behind the counter scores low but is usually
   still where the track expects them, so the track survives instead of breaking in two.
4. Unmatched confident detections start new tracks; unmatched low ones are dropped.

Matching is greedy by IoU rather than optimal (Hungarian). With the few people in a
restaurant camera's view the result is the same in practice.
"""

import itertools
from collections.abc import Sequence
from dataclasses import dataclass, field

from cctv_edge.detection import Detection
from cctv_edge.geometry import Box

# Prediction never extrapolates further than this, so a stopped person isn't "thrown".
MAX_PREDICT_SECONDS = 1.0


@dataclass
class Track:
    id: int
    label: str
    box: Box
    confidence: float
    first_seen: float
    last_seen: float
    hits: int = 1
    previous_box: Box | None = field(default=None, repr=False)
    # Change of (x1, y1, x2, y2) per second, smoothed.
    velocity: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)

    def predict(self, ts: float) -> Box:
        dt = min(max(ts - self.last_seen, 0.0), MAX_PREDICT_SECONDS)
        b, v = self.box, self.velocity
        return Box(b.x1 + v[0] * dt, b.y1 + v[1] * dt, b.x2 + v[2] * dt, b.y2 + v[3] * dt)


def _greedy(
    pairs: list[tuple[float, int, int]], threshold: float, tracks: set[int], dets: set[int]
) -> list[tuple[int, int]]:
    """Match (iou, track, detection) pairs best first; removes matched ones from the sets."""
    matches = []
    for iou, ti, di in sorted(pairs, reverse=True):
        if iou < threshold:
            break
        if ti in tracks and di in dets:
            tracks.discard(ti)
            dets.discard(di)
            matches.append((ti, di))
    return matches


class ByteTracker:
    def __init__(
        self,
        high_threshold: float = 0.5,
        low_threshold: float = 0.1,
        new_track_threshold: float = 0.6,
        match_iou: float = 0.2,
        low_match_iou: float = 0.5,
        max_age_seconds: float = 2.0,
        min_hits: int = 2,
        smoothing: float = 0.6,
    ) -> None:
        self.high_threshold = high_threshold
        self.low_threshold = low_threshold
        self.new_track_threshold = new_track_threshold
        self.match_iou = match_iou
        self.low_match_iou = low_match_iou
        self.max_age_seconds = max_age_seconds
        self.min_hits = min_hits
        self.smoothing = smoothing
        self._tracks: list[Track] = []
        self._ids = itertools.count(1)

    @property
    def tracks(self) -> list[Track]:
        return list(self._tracks)

    def update(self, detections: Sequence[Detection], ts: float) -> list[Track]:
        """Returns confirmed tracks that were seen in this frame."""
        self._tracks = [t for t in self._tracks if ts - t.last_seen <= self.max_age_seconds]
        predicted = [t.predict(ts) for t in self._tracks]

        def pairs(dets: set[int], tracks: set[int]) -> list[tuple[float, int, int]]:
            return [
                (predicted[ti].iou(detections[di].box), ti, di)
                for ti in tracks
                for di in dets
                if self._tracks[ti].label == detections[di].label
            ]

        high = {i for i, d in enumerate(detections) if d.confidence >= self.high_threshold}
        low = {
            i
            for i, d in enumerate(detections)
            if self.low_threshold <= d.confidence < self.high_threshold
        }
        free = set(range(len(self._tracks)))
        matches = _greedy(pairs(high, free), self.match_iou, free, high)
        matches += _greedy(pairs(low, free), self.low_match_iou, free, low)

        seen = []
        for ti, di in matches:
            track = self._tracks[ti]
            self._apply(track, detections[di], ts)
            seen.append(track)
        for di in sorted(high):  # confident detections nobody matched: new people
            det = detections[di]
            if det.confidence >= self.new_track_threshold:
                track = Track(next(self._ids), det.label, det.box, det.confidence, ts, ts)
                self._tracks.append(track)
                seen.append(track)
        return [t for t in seen if t.hits >= self.min_hits]

    def _apply(self, track: Track, det: Detection, ts: float) -> None:
        dt = ts - track.last_seen
        if dt > 0:
            old, new = track.box, det.box
            measured = (
                (new.x1 - old.x1) / dt,
                (new.y1 - old.y1) / dt,
                (new.x2 - old.x2) / dt,
                (new.y2 - old.y2) / dt,
            )
            s = self.smoothing if track.hits > 1 else 0.0
            track.velocity = tuple(  # type: ignore[assignment]
                s * v + (1 - s) * m for v, m in zip(track.velocity, measured, strict=False)
            )
        track.previous_box = track.box
        track.box = det.box
        track.confidence = det.confidence
        track.last_seen = ts
        track.hits += 1
