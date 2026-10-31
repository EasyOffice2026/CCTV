"""Phase 5 features through the camera pipeline, with scripted detections and fake
classifiers: kitchen compliance, cash drawer, unattended counter, counts."""

import uuid
from datetime import datetime, timezone

import numpy as np
import pytest

from cctv_contracts.enums import EventType, FeatureKey, ZoneType
from cctv_contracts.v1 import CountBucketIn, FeatureConfig
from cctv_edge.counts import CountAggregator
from cctv_edge.detection import DRAWER_OPEN, NO_GLOVES, NO_HAIR_CAP, NO_MASK, PERSON
from cctv_edge.geometry import Box
from cctv_edge.models.manifest import ModelTask
from cctv_edge.outbox import Outbox
from cctv_edge.pipeline import CameraPipeline
from cctv_edge.ppe import PpeChecker, PpeSettings, Verdict
from cctv_edge.rules import EventClosed, EventOpened
from cctv_edge.schedule import BranchSchedule
from cctv_edge.sync import Syncer
from cctv_edge.tracking import Track

from .conftest import camera, zone
from .test_pipeline import ScriptedDetector

ALWAYS_OPEN = BranchSchedule([], "Asia/Kuwait")
RNG = np.random.default_rng(1)
SHARP = RNG.integers(0, 255, (360, 640, 3), dtype=np.uint8)  # plenty of detail
BLURRED = np.full((360, 640, 3), 120, np.uint8)
WHOLE = [(0, 0), (1, 0), (1, 1), (0, 1)]
PERSON_BOX = Box(0.4, 0.2, 0.5, 0.9)  # 252 px tall at 360p


class FakeClassifier:
    def __init__(self, result: dict[str, float]) -> None:
        self.classes = tuple(result)
        self.result = result
        self.calls = 0
        self.crops: list[tuple[int, int]] = []

    def classify(self, crop):
        self.calls += 1
        self.crops.append(crop.shape[:2])
        return dict(self.result)


def run(pipeline, frame, seconds, start=0.0, fps=5):
    outputs = []
    for i in range(int(seconds * fps)):
        outputs += pipeline.process(frame, start + i / fps).outputs
    return outputs


def opened(outputs):
    return [o for o in outputs if isinstance(o, EventOpened)]


# --- kitchen compliance -----------------------------------------------------------------


def kitchen_pipeline(result, params=None, zones=None):
    classifier = FakeClassifier(result)
    feature = FeatureConfig(key=FeatureKey.KITCHEN_COMPLIANCE, params=params or {})
    pipeline = CameraPipeline(
        camera([feature], zones if zones is not None else [zone(ZoneType.KITCHEN, WHOLE)]),
        ALWAYS_OPEN,
        {PERSON: ScriptedDetector},
        classifiers={ModelTask.PPE_CLASSIFIER: lambda: classifier},
    )
    [detector] = pipeline.detectors
    detector.boxes = [PERSON_BOX]
    return pipeline, classifier


def test_missing_gloves_event_after_it_persists():
    pipeline, classifier = kitchen_pipeline({"mask": 0.9, "gloves": 0.1, "hair_cap": 0.5})
    assert pipeline.needs_detail
    assert opened(run(pipeline, SHARP, 4)) == []  # 5 s persistence not reached
    [event] = opened(run(pipeline, SHARP, 4, start=4))
    assert event.rule.event_type is EventType.MISSING_GLOVES  # mask worn, hair cap unsure
    assert event.track_id is not None and event.confidence == pytest.approx(0.9)
    assert dict(event.rule.attributes)["checked_items"] == ("gloves", "hair_cap", "mask")
    # Rate-limited: about once a second, not every frame.
    assert 6 <= classifier.calls <= 9


def test_small_or_blurred_people_are_never_violations():
    far = Box(0.45, 0.8, 0.5, 0.9)  # 36 px tall
    pipeline, classifier = kitchen_pipeline({"mask": 0.0, "gloves": 0.0, "hair_cap": 0.0})
    pipeline.detectors[0].boxes = [far]
    assert opened(run(pipeline, SHARP, 10)) == []
    pipeline, classifier = kitchen_pipeline({"mask": 0.0, "gloves": 0.0, "hair_cap": 0.0})
    assert opened(run(pipeline, BLURRED, 10)) == []
    assert classifier.calls == 0  # the quality gate runs before the model


def test_only_people_in_kitchen_zones_are_checked():
    left = zone(ZoneType.KITCHEN, [(0, 0), (0.3, 0), (0.3, 1), (0, 1)])
    pipeline, classifier = kitchen_pipeline({"gloves": 0.0}, zones=[left])
    assert opened(run(pipeline, SHARP, 10)) == []
    assert classifier.calls == 0


def test_items_switched_off_are_not_checked():
    pipeline, _ = kitchen_pipeline(
        {"mask": 0.0, "gloves": 0.0, "hair_cap": 0.0},
        params={"missing_mask": {"enabled": False}, "missing_hair_cap": {"enabled": False}},
    )
    events = opened(run(pipeline, SHARP, 8))
    assert [e.rule.event_type for e in events] == [EventType.MISSING_GLOVES]


def test_no_ppe_model_means_no_kitchen_events():
    pipeline = CameraPipeline(
        camera([FeatureKey.KITCHEN_COMPLIANCE], [zone(ZoneType.KITCHEN, WHOLE)]),
        ALWAYS_OPEN,
        {PERSON: ScriptedDetector},
    )
    assert pipeline.ppe is None
    assert pipeline.missing_labels == {NO_MASK, NO_GLOVES, NO_HAIR_CAP}


def test_ppe_verdicts_and_reuse():
    classifier = FakeClassifier({"mask": 0.95, "gloves": 0.45, "hair_cap": 0.2})
    checker = PpeChecker(classifier, ["mask", "gloves", "hair_cap"], PpeSettings())
    track = Track(7, PERSON, PERSON_BOX, 0.9, 0.0, 0.0)
    [(verdicts, confidence)] = checker.check(SHARP, [track], 0.0).values()
    assert verdicts == {
        "mask": Verdict.WORN,
        "gloves": Verdict.UNKNOWN,
        "hair_cap": Verdict.MISSING,
    }
    assert confidence == pytest.approx(0.8)
    checker.check(SHARP, [track], 0.5)  # within the interval: last verdict reused
    assert classifier.calls == 1
    # Blurred for longer than the verdict lasts: back to unknown.
    for ts in (1.0, 1.5, 2.0, 2.5):
        [(verdicts, _)] = checker.check(BLURRED, [track], ts).values()
    assert set(verdicts.values()) == {Verdict.UNKNOWN}


# --- cashier ----------------------------------------------------------------------------


def test_drawer_openings_are_logged():
    drawer_zone = zone(ZoneType.CASH_DRAWER, [(0.6, 0.6), (0.8, 0.6), (0.8, 0.8), (0.6, 0.8)])
    classifier = FakeClassifier({"closed": 0.1, "open": 0.9})
    pipeline = CameraPipeline(
        camera([FeatureKey.CASHIER], [drawer_zone]),
        ALWAYS_OPEN,
        {},
        classifiers={ModelTask.DRAWER_CLASSIFIER: lambda: classifier},
    )
    assert pipeline.active and pipeline.detectors == []  # no counter zone: no person model
    out = run(pipeline, SHARP, 3)  # open
    classifier.result = {"closed": 0.9, "open": 0.1}
    out += run(pipeline, SHARP, 10, start=3)  # closed
    classifier.result = {"closed": 0.2, "open": 0.8}
    out += run(pipeline, SHARP, 3, start=13)  # opened again
    events = opened(out)
    assert [e.rule.event_type for e in events] == [EventType.DRAWER_OPEN] * 2
    assert {e.zone_id for e in events} == {drawer_zone.id}
    assert classifier.crops[0] == (72, 128)  # the zone's bounding box
    assert any(isinstance(o, EventClosed) for o in out)


def test_drawer_threshold_param():
    drawer_zone = zone(ZoneType.CASH_DRAWER, [(0.6, 0.6), (0.8, 0.6), (0.8, 0.8)])
    classifier = FakeClassifier({"closed": 0.3, "open": 0.7})
    pipeline = CameraPipeline(
        camera(
            [FeatureConfig(key=FeatureKey.CASHIER, params={"drawer_open_threshold": 0.8})],
            [drawer_zone],
        ),
        ALWAYS_OPEN,
        {},
        classifiers={ModelTask.DRAWER_CLASSIFIER: lambda: classifier},
    )
    assert opened(run(pipeline, SHARP, 5)) == []
    assert DRAWER_OPEN not in pipeline.missing_labels


def test_unattended_counter():
    counter = zone(ZoneType.COUNTER, [(0.3, 0.5), (0.7, 0.5), (0.7, 1.0), (0.3, 1.0)])
    fast = FeatureConfig(
        key=FeatureKey.CASHIER, params={"counter_unattended": {"min_duration_s": 5}}
    )
    pipeline = CameraPipeline(camera([fast], [counter]), ALWAYS_OPEN, {PERSON: ScriptedDetector})
    [detector] = pipeline.detectors
    detector.boxes = [PERSON_BOX]  # foot point (0.45, 0.9) is in the counter zone
    assert opened(run(pipeline, SHARP, 10)) == []
    detector.boxes = []
    [event] = opened(run(pipeline, SHARP, 10, start=10))
    assert event.rule.event_type is EventType.COUNTER_UNATTENDED and event.zone_id == counter.id
    detector.boxes = [PERSON_BOX]
    closed = [o for o in run(pipeline, SHARP, 5, start=20) if isinstance(o, EventClosed)]
    assert [c.event_id for c in closed] == [event.event_id]


# --- counts -----------------------------------------------------------------------------


def walker(track_id, x, y, prev_y=None):
    track = Track(track_id, PERSON, Box(x, y - 0.3, x + 0.1, y), 0.9, 0.0, 0.0)
    if prev_y is not None:
        track.previous_box = Box(x, prev_y - 0.3, x + 0.1, prev_y)
    return track


def test_count_buckets():
    line_id, area_id, cam = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    counts = CountAggregator(
        cam, {line_id: ((0.0, 0.5), (1.0, 0.5))}, {area_id: [(0, 0.6), (1, 0.6), (1, 1), (0, 1)]}
    )
    t0 = 1_800_000_000.0  # a multiple of 300
    assert counts.update([walker(1, 0.1, 0.7), walker(2, 0.5, 0.8)], t0) == []
    # 1 in; person 2 still in the area for 1 s
    assert counts.update([walker(1, 0.1, 0.45, prev_y=0.7), walker(2, 0.5, 0.8)], t0 + 1) == []
    assert counts.update([walker(3, 0.3, 0.55, prev_y=0.4)], t0 + 10) == []  # 1 out
    finished = counts.update([], t0 + 300)
    by_zone = {b.zone_id: b for b in finished}
    line, area = by_zone[line_id], by_zone[area_id]
    assert (line.in_count, line.out_count, line.max_occupancy) == (1, 1, None)
    assert (area.max_occupancy, area.occupied_seconds) == (2, 1)  # gap of 9 s not counted
    assert line.bucket_start == datetime.fromtimestamp(t0, timezone.utc)
    [*partial] = counts.flush()
    assert {b.bucket_start.timestamp() for b in partial} == {t0 + 300}


def test_counts_follow_the_enabled_features():
    door = zone(ZoneType.ENTRANCE_LINE, [(0.0, 0.5), (1.0, 0.5)])
    counter = zone(ZoneType.COUNTER, WHOLE)
    queue = zone(ZoneType.QUEUE, WHOLE)
    zones = [door, counter, queue]
    only_security = CameraPipeline(camera([FeatureKey.SECURITY], zones), ALWAYS_OPEN, {})
    assert not only_security.counts.active
    counting = CameraPipeline(
        camera([FeatureKey.CUSTOMER_COUNT, FeatureKey.CASHIER], zones),
        ALWAYS_OPEN,
        {PERSON: ScriptedDetector},
    )
    assert set(counting.counts.lines) == {door.id}
    assert set(counting.counts.areas) == {counter.id}


def test_outbox_merges_partial_buckets_and_syncs(tmp_path, client, cloud):
    outbox = Outbox(tmp_path / "outbox.sqlite3")
    cam, zone_id = uuid.uuid4(), uuid.uuid4()
    start = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)

    def bucket(**values):
        return CountBucketIn(camera_id=cam, zone_id=zone_id, bucket_start=start, **values)

    outbox.add_counts([bucket(max_occupancy=2, occupied_seconds=200)])
    syncer = Syncer(outbox, client)
    assert syncer.sync_counts(0) == 1
    assert syncer.sync_counts(0) == 0  # nothing left to send
    # The camera restarted mid-bucket: the rest is added and the totals sent again.
    outbox.add_counts([bucket(max_occupancy=3, occupied_seconds=150)])
    outbox.add_counts([CountBucketIn(camera_id=cam, bucket_start=start, in_count=4)])
    assert syncer.sync_counts(0) == 2
    sent = {
        (c["zone_id"], c["max_occupancy"], c["occupied_seconds"], c["in_count"])
        for c in cloud.counts
    }
    assert (str(zone_id), 3, 300, 0) in sent  # capped at the bucket length
    assert (None, None, None, 4) in sent

    cloud.down = True
    outbox.add_counts([bucket(max_occupancy=1, occupied_seconds=1)])
    assert syncer.sync_counts(10) == 0
    assert outbox.due_counts(10, 10) == []  # backing off
    cloud.down = False
    assert syncer.sync_counts(10_000) == 1

    outbox.cleanup_aggregates(start.timestamp() + 7 * 3600)
    assert outbox._query("SELECT COUNT(*) AS n FROM counts")[0]["n"] == 0
    outbox.close()
