"""Detectors.

A detector turns an RGB frame (H x W x 3, uint8) into labelled boxes. Model-based
detectors (people, phones) are in `cctv_edge.models`; `MotionDetector` needs no weights
and drives the after-hours motion rule. Tracking is in `cctv_edge.tracking`.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from cctv_edge.geometry import Box

PERSON = "person"
MOTION = "motion"
# Labels produced by classifiers on crops rather than by detectors.
DRAWER_OPEN = "drawer_open"
NO_MASK = "no_mask"
NO_GLOVES = "no_gloves"
NO_HAIR_CAP = "no_hair_cap"


@dataclass(frozen=True)
class Detection:
    label: str
    box: Box
    confidence: float


class Detector(Protocol):
    labels: frozenset[str]  # what it can report; one model can find several things

    def detect(self, frame: np.ndarray) -> list[Detection]: ...


class MotionDetector:
    """Frame differencing on a downscaled grey image. Reports one box around all change."""

    labels = frozenset({MOTION})

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
