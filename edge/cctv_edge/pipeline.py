"""Per-camera analysis.

    frame ─► detectors ─► ignore zones ─► tracker ─► zone tests ─┬─► rules ─► events
                                             │                    ├─► PPE (kitchen zones)
                                             ├─► counts           └─► drawer (cash_drawer zones)
                                             └─► heatmap
    frame ─► faces ─► match staff ─► sightings              (attendance cameras only)

Only the stages the enabled features need are built, and only their models run.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field

import numpy as np

from cctv_contracts.enums import LINE_ZONE_TYPES, FeatureKey, ZoneType
from cctv_contracts.v1 import (
    CameraConfig,
    CountBucketIn,
    FeatureConfig,
    HeatmapBucketIn,
    StaffSighting,
)
from cctv_edge.attendance import AttendanceSettings, AttendanceWatcher
from cctv_edge.counts import CountAggregator
from cctv_edge.detection import DRAWER_OPEN, PERSON, DetectorFactory
from cctv_edge.face import FaceAnalyzer, FaceGallery
from cctv_edge.geometry import Box, point_in_polygon
from cctv_edge.heatmap import HeatmapAggregator
from cctv_edge.imaging import crop
from cctv_edge.models.classifier import Classifier, ClassifierFactory
from cctv_edge.models.manifest import ModelTask
from cctv_edge.ppe import ITEM_LABELS, PpeChecker, PpeSettings, Verdict
from cctv_edge.rules import (
    Observation,
    RuleEngine,
    RuleOutput,
    feature_settings,
    rules_for_features,
)
from cctv_edge.schedule import BranchSchedule
from cctv_edge.tracking import ByteTracker, Track

log = logging.getLogger(__name__)

# Labels followed across frames. Others (motion) are used per frame as they are.
TRACKED_LABELS = frozenset({PERSON})
# Features that need a sharper picture than the default analysis size (faces, gloves).
DETAIL_FEATURES = frozenset({FeatureKey.KITCHEN_COMPLIANCE, FeatureKey.ATTENDANCE})
# Area zones whose occupancy is counted, and the feature that turns it on.
OCCUPANCY_ZONES = {
    FeatureKey.CASHIER: ZoneType.COUNTER,
    FeatureKey.QUEUE: ZoneType.QUEUE,
    FeatureKey.CUSTOMER_COUNT: ZoneType.DINING,
}
# Customer counts leave out people seen in these zones (staff), and these zones count
# customers only.
STAFF_ZONES = frozenset({ZoneType.STAFF_AREA, ZoneType.KITCHEN, ZoneType.COUNTER})
CUSTOMER_ZONES = frozenset({ZoneType.QUEUE, ZoneType.DINING})


@dataclass
class FrameResult:
    outputs: list[RuleOutput]
    observations: list[Observation]
    counts: list[CountBucketIn] = field(default_factory=list)
    sightings: list[StaffSighting] = field(default_factory=list)
    heatmaps: list[HeatmapBucketIn] = field(default_factory=list)


def _bounds(points) -> Box:
    return Box(
        min(p[0] for p in points),
        min(p[1] for p in points),
        max(p[0] for p in points),
        max(p[1] for p in points),
    )


class CameraPipeline:
    def __init__(
        self,
        camera: CameraConfig,
        schedule: BranchSchedule,
        detector_factories: Mapping[str, DetectorFactory],
        max_event_seconds: float = 120,
        *,
        classifiers: Mapping[ModelTask, ClassifierFactory] | None = None,
        face_analyzer: FaceAnalyzer | None = None,
        gallery: FaceGallery | None = None,
    ) -> None:
        """`classifiers` maps a model task to a factory returning a `Classifier`."""
        self.camera_id = camera.id
        features: dict[FeatureKey, FeatureConfig] = {f.key: f for f in camera.features}
        classifiers = classifiers or {}
        self.engine = RuleEngine(
            rules_for_features(camera.features), camera.zones, schedule, max_event_seconds
        )
        self._polygons = [
            (z.id, z.points)
            for z in camera.zones
            if z.type not in LINE_ZONE_TYPES and z.type is not ZoneType.IGNORE
        ]
        self._ignore = [z.points for z in camera.zones if z.type is ZoneType.IGNORE]
        self.needs_detail = bool(DETAIL_FEATURES & features.keys())

        # Counts: entrance lines (customer count) and occupancy of counter/queue zones.
        lines = {}
        if FeatureKey.CUSTOMER_COUNT in features:
            lines = {
                z.id: (z.points[0], z.points[1]) for z in camera.zones if z.type in LINE_ZONE_TYPES
            }
        area_types = {t for f, t in OCCUPANCY_ZONES.items() if f in features}
        areas = {z.id: z.points for z in camera.zones if z.type in area_types}
        self.counts = CountAggregator(
            camera.id,
            lines,
            areas,
            customer_areas={
                z.id for z in camera.zones if z.id in areas and z.type in CUSTOMER_ZONES
            },
            queue_areas={z.id for z in camera.zones if z.id in areas and z.type is ZoneType.QUEUE},
            staff_areas=[z.points for z in camera.zones if z.type in STAFF_ZONES],
        )

        # Heatmap: where people (not staff) stand, in a grid over the picture.
        self.heatmap: HeatmapAggregator | None = None
        if FeatureKey.HEATMAP in features:
            self.heatmap = HeatmapAggregator(
                camera.id, staff_areas=[z.points for z in camera.zones if z.type in STAFF_ZONES]
            )

        needed = set(self.engine.labels)
        missing: set[str] = set()
        # Drawer open/closed comes from a classifier on each cash drawer zone.
        self.drawer: Classifier | None = None
        self._drawers = [
            (z.id, _bounds(z.points)) for z in camera.zones if z.type is ZoneType.CASH_DRAWER
        ]
        self._drawer_threshold = 0.6
        if DRAWER_OPEN in needed:
            needed.discard(DRAWER_OPEN)
            factory = classifiers.get(ModelTask.DRAWER_CLASSIFIER)
            if factory is None:
                missing.add(DRAWER_OPEN)
            elif self._drawers:
                self.drawer = factory()
                self._drawer_threshold = feature_settings(features[FeatureKey.CASHIER])[
                    "drawer_open_threshold"
                ]
        # PPE: a classifier on people standing in kitchen zones.
        self.ppe: PpeChecker | None = None
        self._kitchens = [z.id for z in camera.zones if z.type is ZoneType.KITCHEN]
        ppe_labels = needed & set(ITEM_LABELS.values())
        if ppe_labels:
            needed -= ppe_labels
            factory = classifiers.get(ModelTask.PPE_CLASSIFIER)
            if factory is None:
                missing |= ppe_labels
            elif self._kitchens:
                items = [i for i, label in ITEM_LABELS.items() if label in ppe_labels]
                settings = PpeSettings.from_params(
                    feature_settings(features[FeatureKey.KITCHEN_COMPLIANCE])
                )
                self.ppe = PpeChecker(factory(), items, settings)
                needed.add(PERSON)
        if self.counts.active or self.heatmap is not None:
            needed.add(PERSON)

        # Only the detectors the enabled features need are created, so a feature that is
        # switched off costs no inference. A model reporting several labels runs once.
        self.detectors = []
        created: dict[int, object] = {}
        for label in sorted(needed):
            factory = detector_factories.get(label)
            if factory is None:
                continue
            if id(factory) not in created:
                created[id(factory)] = detector = factory()
                self.detectors.append(detector)
        covered = set().union(*(d.labels for d in self.detectors))
        self.missing_labels = (needed - covered) | missing

        # Attendance: faces matched against the enrolled staff of the branch.
        self.attendance: AttendanceWatcher | None = None
        if FeatureKey.ATTENDANCE in features:
            if face_analyzer is None or gallery is None:
                self.missing_labels.add("face")
            else:
                self.attendance = AttendanceWatcher(
                    camera.id,
                    face_analyzer,
                    gallery,
                    AttendanceSettings.from_params(
                        feature_settings(features[FeatureKey.ATTENDANCE])
                    ),
                )

        if self.missing_labels:
            log.warning(
                "camera %s: no model for %s; those features will not fire",
                camera.id,
                sorted(self.missing_labels),
            )
        self.tracker = ByteTracker()

    @property
    def active(self) -> bool:
        return bool(self.detectors or self.drawer or self.attendance)

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
        counts = self.counts.update(tracks, ts)
        heatmaps = self.heatmap.update(tracks, ts) if self.heatmap is not None else []

        observations = [self._observe(t.label, t.box, t.confidence, t.id) for t in tracks] + [
            self._observe(d.label, d.box, d.confidence, None) for d in untracked
        ]
        if self.ppe is not None:
            observations += self._check_ppe(frame, tracks, observations, ts)
        if self.drawer is not None:
            observations += self._check_drawers(frame)
        sightings = self.attendance.process(frame, ts) if self.attendance else []
        return FrameResult(
            self.engine.update(ts, observations), observations, counts, sightings, heatmaps
        )

    def _check_ppe(
        self, frame: np.ndarray, tracks: list[Track], observations: list[Observation], ts: float
    ) -> list[Observation]:
        assert self.ppe is not None  # noqa: S101
        zones_of = {o.track_id: o.zone_ids for o in observations if o.track_id is not None}
        in_kitchen = [
            t
            for t in tracks
            if t.label == PERSON and zones_of.get(t.id, frozenset()) & set(self._kitchens)
        ]
        found = []
        results = self.ppe.check(frame, in_kitchen, ts)
        for track in in_kitchen:
            verdicts, confidence = results[track.id]
            for item, verdict in verdicts.items():
                if verdict is Verdict.MISSING:
                    found.append(
                        Observation(
                            ITEM_LABELS[item], track.box, confidence, track.id, zones_of[track.id]
                        )
                    )
        return found

    def _check_drawers(self, frame: np.ndarray) -> list[Observation]:
        assert self.drawer is not None  # noqa: S101
        found = []
        for zone_id, box in self._drawers:
            p = self.drawer.classify(crop(frame, box)).get("open", 0.0)
            if p >= self._drawer_threshold:
                found.append(Observation(DRAWER_OPEN, box, p, None, frozenset({zone_id})))
        return found

    def flush_counts(self) -> list[CountBucketIn]:
        """The partial count bucket, when the camera stops or its config changes."""
        return self.counts.flush()

    def flush_heatmaps(self) -> list[HeatmapBucketIn]:
        """The partial heatmap bucket, likewise."""
        return self.heatmap.flush() if self.heatmap is not None else []

    def _observe(
        self, label: str, box: Box, confidence: float, track_id: int | None
    ) -> Observation:
        zones = frozenset(zid for zid, poly in self._polygons if point_in_polygon(box.foot, poly))
        return Observation(label, box, confidence, track_id, zones)
