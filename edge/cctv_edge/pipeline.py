"""Per-camera analysis: detect -> track -> zone tests / line crossing -> rules."""

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from uuid import UUID

import numpy as np

from cctv_contracts.enums import LINE_ZONE_TYPES, FeatureKey, ZoneType
from cctv_contracts.v1 import CameraConfig
from cctv_edge.detection import PERSON, DetectorFactory, IoUTracker, Track
from cctv_edge.geometry import Box, Point, line_crossing, point_in_polygon
from cctv_edge.rules import Observation, RuleEngine, RuleOutput, rules_for_features
from cctv_edge.schedule import BranchSchedule

log = logging.getLogger(__name__)

# Labels followed across frames. Others (motion) are used per frame as they are.
TRACKED_LABELS = frozenset({PERSON})


@dataclass
class LineCounter:
    """In/out counts per entrance line. Sent as aggregates once the count endpoint exists."""

    lines: dict[UUID, tuple[Point, Point]]
    counts: dict[UUID, list[int]] = field(default_factory=dict)

    def update(self, tracks: list[Track]) -> None:
        for track in tracks:
            if track.previous_box is None:
                continue
            for zone_id, (a, b) in self.lines.items():
                direction = line_crossing(track.previous_box.foot, track.box.foot, a, b)
                if direction:
                    pair = self.counts.setdefault(zone_id, [0, 0])
                    pair[0 if direction > 0 else 1] += 1


@dataclass
class FrameResult:
    outputs: list[RuleOutput]
    observations: list[Observation]


class CameraPipeline:
    def __init__(
        self,
        camera: CameraConfig,
        schedule: BranchSchedule,
        detector_factories: Mapping[str, DetectorFactory],
        max_event_seconds: float = 120,
    ) -> None:
        self.camera_id = camera.id
        self.engine = RuleEngine(
            rules_for_features(camera.features), camera.zones, schedule, max_event_seconds
        )
        self._polygons = [
            (z.id, z.points)
            for z in camera.zones
            if z.type not in LINE_ZONE_TYPES and z.type is not ZoneType.IGNORE
        ]
        self._ignore = [z.points for z in camera.zones if z.type is ZoneType.IGNORE]
        counting = any(f.key is FeatureKey.CUSTOMER_COUNT for f in camera.features)
        self.line_counter = LineCounter(
            {z.id: (z.points[0], z.points[1]) for z in camera.zones if z.type in LINE_ZONE_TYPES}
            if counting
            else {}
        )

        needed = set(self.engine.labels)
        if self.line_counter.lines:
            needed.add(PERSON)
        # Only the detectors the enabled features need are created, so a feature that is
        # switched off costs no inference.
        self.detectors = [
            detector_factories[label]() for label in sorted(needed) if label in detector_factories
        ]
        self.missing_labels = needed - {d.label for d in self.detectors}
        if self.missing_labels:
            log.warning(
                "camera %s: no detector for %s; those rules will not fire",
                camera.id,
                sorted(self.missing_labels),
            )
        self.tracker = IoUTracker()

    @property
    def active(self) -> bool:
        return bool(self.detectors)

    def process(self, frame: np.ndarray, ts: float) -> FrameResult:
        detections = [
            d
            for detector in self.detectors
            for d in detector.detect(frame)
            if not any(point_in_polygon(d.box.foot, zone) for zone in self._ignore)
        ]
        tracked = [d for d in detections if d.label in TRACKED_LABELS]
        untracked = [d for d in detections if d.label not in TRACKED_LABELS]
        tracks = self.tracker.update(tracked, ts)
        self.line_counter.update(tracks)

        observations = [self._observe(t.label, t.box, t.confidence, t.id) for t in tracks] + [
            self._observe(d.label, d.box, d.confidence, None) for d in untracked
        ]
        return FrameResult(self.engine.update(ts, observations), observations)

    def _observe(
        self, label: str, box: Box, confidence: float, track_id: int | None
    ) -> Observation:
        zones = frozenset(zid for zid, poly in self._polygons if point_in_polygon(box.foot, poly))
        return Observation(label, box, confidence, track_id, zones)
