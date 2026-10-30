from typing import ClassVar

import numpy as np

from cctv_contracts.enums import EventType, FeatureKey, ZoneType
from cctv_edge.detection import MOTION, PERSON, Detection, IoUTracker, MotionDetector
from cctv_edge.geometry import Box
from cctv_edge.pipeline import CameraPipeline
from cctv_edge.rules import EventOpened
from cctv_edge.schedule import BranchSchedule

from .conftest import camera, zone

CLOSED = BranchSchedule.__new__(BranchSchedule)
CLOSED.is_open = lambda _when: False  # always after hours
FRAME = np.zeros((36, 64, 3), np.uint8)


class ScriptedDetector:
    """Returns whatever boxes the test puts in `boxes`."""

    instances: ClassVar[list["ScriptedDetector"]] = []

    def __init__(self, label: str = PERSON) -> None:
        self.label = label
        self.boxes: list[Box] = []
        self.calls = 0
        ScriptedDetector.instances.append(self)

    def detect(self, frame):
        self.calls += 1
        return [Detection(self.label, b, 0.9) for b in self.boxes]


def test_tracker_keeps_ids_and_needs_two_hits():
    tracker = IoUTracker(min_hits=2)
    box = Box(0.1, 0.1, 0.3, 0.5)
    assert tracker.update([Detection(PERSON, box, 0.9)], 0.0) == []
    tracks = tracker.update([Detection(PERSON, Box(0.12, 0.1, 0.32, 0.5), 0.9)], 0.2)
    assert len(tracks) == 1
    first_id = tracks[0].id
    tracks = tracker.update([Detection(PERSON, Box(0.14, 0.1, 0.34, 0.5), 0.9)], 0.4)
    assert tracks[0].id == first_id
    assert tracks[0].previous_box is not None


def test_tracker_forgets_old_tracks():
    tracker = IoUTracker(min_hits=1, max_age_seconds=1)
    box = Box(0.1, 0.1, 0.3, 0.5)
    first = tracker.update([Detection(PERSON, box, 0.9)], 0.0)[0].id
    assert tracker.update([Detection(PERSON, box, 0.9)], 5.0)[0].id != first


def test_motion_detector():
    detector = MotionDetector(step=1)
    still = np.zeros((40, 40, 3), np.uint8)
    assert detector.detect(still) == []
    assert detector.detect(still) == []
    moved = still.copy()
    moved[10:20, 20:30] = 255
    [motion] = detector.detect(moved)
    assert motion.label == MOTION
    assert motion.box == Box(0.5, 0.25, 0.75, 0.5)


def test_disabled_features_run_no_detectors():
    ScriptedDetector.instances.clear()
    factories = {PERSON: ScriptedDetector, MOTION: lambda: ScriptedDetector(MOTION)}
    off = CameraPipeline(camera([]), CLOSED, factories)
    assert not off.active
    assert ScriptedDetector.instances == []

    on = CameraPipeline(camera([FeatureKey.SECURITY]), CLOSED, factories)
    assert on.active
    assert sorted(d.label for d in on.detectors) == [MOTION, PERSON]


def test_missing_detector_is_reported():
    pipeline = CameraPipeline(camera([FeatureKey.SECURITY]), CLOSED, {MOTION: MotionDetector})
    assert pipeline.missing_labels == {PERSON}


def test_after_hours_person_event():
    pipeline = CameraPipeline(camera([FeatureKey.SECURITY]), CLOSED, {PERSON: ScriptedDetector})
    [detector] = pipeline.detectors
    detector.boxes = [Box(0.4, 0.3, 0.5, 0.8)]
    outputs = []
    for i in range(20):
        outputs += pipeline.process(FRAME, i * 0.2).outputs
    [opened] = outputs
    assert isinstance(opened, EventOpened)
    assert opened.rule.event_type is EventType.AFTER_HOURS_PERSON


def test_ignore_zones_drop_detections():
    tv = zone(ZoneType.IGNORE, [(0.0, 0.0), (0.5, 0.0), (0.5, 1.0), (0.0, 1.0)])
    pipeline = CameraPipeline(
        camera([FeatureKey.SECURITY], [tv]), CLOSED, {PERSON: ScriptedDetector}
    )
    [detector] = pipeline.detectors
    detector.boxes = [Box(0.1, 0.3, 0.2, 0.8)]  # standing in the ignore zone
    for i in range(20):
        result = pipeline.process(FRAME, i * 0.2)
        assert result.outputs == [] and result.observations == []


def test_line_counting():
    door = zone(ZoneType.ENTRANCE_LINE, [(0.0, 0.5), (1.0, 0.5)])
    pipeline = CameraPipeline(
        camera([FeatureKey.CUSTOMER_COUNT], [door]), CLOSED, {PERSON: ScriptedDetector}
    )
    [detector] = pipeline.detectors
    for i, y in enumerate([0.9, 0.8, 0.7, 0.6, 0.45, 0.35]):  # walks up through the door
        detector.boxes = [Box(0.4, y - 0.3, 0.5, y)]
        pipeline.process(FRAME, i * 0.2)
    assert pipeline.line_counter.counts == {door.id: [1, 0]}
