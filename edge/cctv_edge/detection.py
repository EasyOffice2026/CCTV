"""Detectors and tracking.

A detector turns an RGB frame (H x W x 3, uint8) into labelled boxes. The person detector
arrives with its model in Phase 5; `MotionDetector` needs no weights and drives the
after-hours motion rule.
"""

import itertools
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np

from cctv_edge.geometry import Box

PERSON = "person"
MOTION = "motion"


@dataclass(frozen=True)
class Detection:
    label: str
    box: Box
    confidence: float


class Detector(Protocol):
    label: str

    def detect(self, frame: np.ndarray) -> list[Detection]: ...


class MotionDetector:
    """Frame differencing on a downscaled grey image. Reports one box around all change."""

    label = MOTION

    def __init__(self, threshold: int = 25, min_fraction: float = 0.005, step: int = 4) -> None:
        self.threshold = threshold
        self.min_fraction = min_fraction
        self.step = step
        self._previous: np.ndarray | None = None

    def detect(self, frame: np.ndarray) -> list[Detection]:
        grey = frame[:: self.step, :: self.step].mean(axis=2, dtype=np.float32)
        previous, self._previous = self._previous, grey
        if previous is None or previous.shape != grey.shape:
            return []
        changed = np.abs(grey - previous) > self.threshold
        fraction = float(changed.mean())
        if fraction < self.min_fraction:
            return []
        rows = np.flatnonzero(changed.any(axis=1))
        cols = np.flatnonzero(changed.any(axis=0))
        h, w = changed.shape
        box = Box(cols[0] / w, rows[0] / h, (cols[-1] + 1) / w, (rows[-1] + 1) / h)
        return [Detection(MOTION, box, min(1.0, fraction * 10))]


DetectorFactory = Callable[[], Detector]

BUILTIN_DETECTORS: dict[str, DetectorFactory] = {MOTION: MotionDetector}


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


class IoUTracker:
    """Greedy IoU tracker giving each object a stable id across frames.

    Simple on purpose: it is enough for zone dwell and line crossing at 5 fps. Replace with
    ByteTrack (same `update` interface) when the person model lands in Phase 5.
    """

    def __init__(
        self, iou_threshold: float = 0.3, max_age_seconds: float = 2.0, min_hits: int = 2
    ) -> None:
        self.iou_threshold = iou_threshold
        self.max_age_seconds = max_age_seconds
        self.min_hits = min_hits
        self._tracks: list[Track] = []
        self._ids = itertools.count(1)

    def update(self, detections: Sequence[Detection], ts: float) -> list[Track]:
        """Returns confirmed tracks that were seen in this frame."""
        self._tracks = [t for t in self._tracks if ts - t.last_seen <= self.max_age_seconds]
        pairs = sorted(
            (
                (track.box.iou(det.box), ti, di)
                for ti, track in enumerate(self._tracks)
                for di, det in enumerate(detections)
                if track.label == det.label
            ),
            reverse=True,
        )
        used_tracks: set[int] = set()
        used_dets: set[int] = set()
        seen: list[Track] = []
        for iou, ti, di in pairs:
            if iou < self.iou_threshold:
                break
            if ti in used_tracks or di in used_dets:
                continue
            used_tracks.add(ti)
            used_dets.add(di)
            track, det = self._tracks[ti], detections[di]
            track.previous_box = track.box
            track.box = det.box
            track.confidence = det.confidence
            track.last_seen = ts
            track.hits += 1
            seen.append(track)
        for di, det in enumerate(detections):
            if di not in used_dets:
                track = Track(next(self._ids), det.label, det.box, det.confidence, ts, ts)
                self._tracks.append(track)
                seen.append(track)
        return [t for t in seen if t.hits >= self.min_hits]
