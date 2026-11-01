"""Plan 10.2: customer counts without staff, queue waits, long queues and full rooms."""

import sqlite3
import uuid
from datetime import datetime, timezone

from cctv_contracts.enums import EventType, FeatureKey, ZoneType
from cctv_contracts.v1 import CountBucketIn, FeatureConfig
from cctv_edge.counts import CountAggregator
from cctv_edge.detection import PERSON
from cctv_edge.geometry import Box
from cctv_edge.outbox import Outbox
from cctv_edge.pipeline import CameraPipeline
from cctv_edge.rules import EventOpened, RuleEngine, rules_for_features
from cctv_edge.schedule import BranchSchedule
from cctv_edge.tracking import Track

from .conftest import camera, zone
from .test_pipeline import ScriptedDetector
from .test_rules import person, run

ALWAYS_OPEN = BranchSchedule([], "Asia/Kuwait")
T0 = 1_800_000_000.0  # a multiple of 300
LINE = ((0.0, 0.5), (1.0, 0.5))
STAFF_AREA = [(0.8, 0.0), (1.0, 0.0), (1.0, 0.3), (0.8, 0.3)]
QUEUE = [(0.0, 0.6), (0.5, 0.6), (0.5, 1.0), (0.0, 1.0)]


def at(track_id, x, y, prev=None):
    track = Track(track_id, PERSON, Box(x - 0.05, y - 0.3, x + 0.05, y), 0.9, 0.0, 0.0)
    if prev is not None:
        track.previous_box = Box(prev[0] - 0.05, prev[1] - 0.3, prev[0] + 0.05, prev[1])
    return track


def test_staff_are_left_out_of_customer_counts():
    line_id, queue_id = uuid.uuid4(), uuid.uuid4()
    counts = CountAggregator(
        uuid.uuid4(),
        {line_id: LINE},
        {queue_id: QUEUE},
        customer_areas={queue_id},
        staff_areas=[STAFF_AREA],
    )
    # Track 1 comes from the staff area behind the counter, then walks out the door;
    # track 2 is a customer walking in.
    counts.update([at(1, 0.9, 0.2), at(2, 0.3, 0.7)], T0)
    counts.update([at(1, 0.6, 0.45, prev=(0.6, 0.55)), at(2, 0.3, 0.45, prev=(0.3, 0.7))], T0 + 1)
    # Both stand in the queue zone: only the customer counts.
    counts.update([at(1, 0.2, 0.8), at(3, 0.3, 0.9)], T0 + 2)
    by_zone = {b.zone_id: b for b in counts.update([], T0 + 300)}
    line = by_zone[line_id]
    assert (line.in_count, line.out_count, line.staff_crossings) == (1, 0, 1)
    assert by_zone[queue_id].max_occupancy == 1


def test_queue_waits():
    queue_id = uuid.uuid4()
    counts = CountAggregator(
        uuid.uuid4(), {}, {queue_id: QUEUE}, customer_areas={queue_id}, queue_areas={queue_id}
    )
    for s in range(0, 41):  # track 1 waits 40 s; track 2 only passes through (2 s)
        tracks = [at(1, 0.2, 0.8)]
        if s in (10, 11, 12):
            tracks.append(at(2, 0.4, 0.7))
        counts.update(tracks, T0 + s)
    for s in range(41, 50):  # both gone
        counts.update([], T0 + s)
    [bucket] = counts.update([], T0 + 300)
    assert (bucket.waits, bucket.wait_total_seconds, bucket.wait_max_seconds) == (1, 40, 40)
    assert bucket.max_occupancy == 2


def test_long_queue_and_full_room_rules():
    queue, dining = zone(ZoneType.QUEUE, QUEUE), zone(ZoneType.DINING, [(0, 0), (1, 0), (1, 1)])
    rules = rules_for_features(
        [
            FeatureConfig(key=FeatureKey.QUEUE, params={"queue_long": {"min_count": 3}}),
            FeatureConfig(key=FeatureKey.CUSTOMER_COUNT),
        ]
    )
    by_type = {r.event_type: r for r in rules}
    assert by_type[EventType.QUEUE_LONG].min_count == 3
    assert by_type[EventType.OCCUPANCY_HIGH].min_count == 30
    engine = RuleEngine([by_type[EventType.QUEUE_LONG]], [queue, dining], ALWAYS_OPEN)

    def frames(people, start, end):
        return {
            start + s: [person(i, {queue.id}) for i in range(people)]
            for s in range(0, end - start + 1, 5)
        }

    # Two people for two minutes: not a long queue.
    assert run(engine, frames(2, 0, 120)) == []
    # Three people for a minute: one event for the zone.
    opened = [o for o in run(engine, frames(3, 200, 270)) if isinstance(o, EventOpened)]
    assert len(opened) == 1 and opened[0].zone_id == queue.id and len(opened[0].boxes) == 3


def test_pipeline_counts_dining_and_queue_for_customers():
    door = zone(ZoneType.ENTRANCE_LINE, list(LINE))
    queue, dining = zone(ZoneType.QUEUE, QUEUE), zone(ZoneType.DINING, STAFF_AREA)
    staff, counter = zone(ZoneType.STAFF_AREA, STAFF_AREA), zone(ZoneType.COUNTER, QUEUE)
    pipeline = CameraPipeline(
        camera(
            [FeatureKey.CUSTOMER_COUNT, FeatureKey.QUEUE, FeatureKey.CASHIER],
            [door, queue, dining, staff, counter],
        ),
        ALWAYS_OPEN,
        {PERSON: ScriptedDetector},
    )
    counts = pipeline.counts
    assert set(counts.areas) == {queue.id, dining.id, counter.id}
    assert set(counts.customer_areas) == {queue.id, dining.id}  # the counter counts staff
    assert set(counts.queue_areas) == {queue.id}
    assert len(counts.staff_areas) == 2  # staff area and counter


def test_outbox_keeps_and_merges_the_new_count_fields(tmp_path):
    path = tmp_path / "outbox.sqlite3"
    # An outbox from before these columns existed.
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE counts (camera_id TEXT NOT NULL, zone_id TEXT NOT NULL DEFAULT '',"
        " bucket_start REAL NOT NULL, bucket_seconds INTEGER NOT NULL,"
        " in_count INTEGER NOT NULL DEFAULT 0, out_count INTEGER NOT NULL DEFAULT 0,"
        " max_occupancy INTEGER, occupied_seconds INTEGER, sent INTEGER NOT NULL DEFAULT 0,"
        " attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL NOT NULL DEFAULT 0,"
        " PRIMARY KEY (camera_id, zone_id, bucket_start))"
    )
    old.commit()
    old.close()
    outbox = Outbox(path)
    cam, zone_id = uuid.uuid4(), uuid.uuid4()
    start = datetime.fromtimestamp(T0, timezone.utc)

    def bucket(**values):
        return CountBucketIn(camera_id=cam, zone_id=zone_id, bucket_start=start, **values)

    outbox.add_counts([bucket(waits=2, wait_total_seconds=100, wait_max_seconds=70)])
    outbox.add_counts([bucket(waits=1, wait_total_seconds=30, wait_max_seconds=30)])
    [(_, merged)] = outbox.due_counts(T0 + 1000, 10)
    assert (merged.waits, merged.wait_total_seconds, merged.wait_max_seconds) == (3, 130, 70)
    assert merged.staff_crossings is None
