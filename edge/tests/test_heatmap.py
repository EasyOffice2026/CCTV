"""Heatmaps (later phase): person-seconds per cell of a grid over the picture, per hour,
without staff; stored in the outbox, merged after a restart, and synced."""

import json
import time
import uuid
from datetime import datetime, timezone

from cctv_contracts.enums import FeatureKey, ZoneType
from cctv_contracts.v1 import HeatmapBucketIn
from cctv_edge.heatmap import HeatmapAggregator
from cctv_edge.outbox import Outbox
from cctv_edge.pipeline import CameraPipeline
from cctv_edge.sync import Syncer

from .conftest import camera, zone
from .test_pipeline import ScriptedDetector
from .test_wave2 import ALWAYS_OPEN, PERSON, STAFF_AREA, at

T0 = 1_800_000_000.0  # a whole hour
CAM = uuid.uuid4()


def frames(agg: HeatmapAggregator, positions, start: float, seconds: float, fps: float = 5):
    """`positions`: (track id, x, y) standing still for `seconds`."""
    finished = []
    for i in range(int(seconds * fps) + 1):
        finished += agg.update([at(t, x, y) for t, x, y in positions], start + i / fps)
    return finished


def test_time_is_added_to_the_cell_people_stand_in():
    agg = HeatmapAggregator(CAM, columns=4, rows=2)
    assert agg.cell((0.0, 0.0)) == 0
    assert agg.cell((1.0, 1.0)) == 7  # the edge belongs to the last cell
    assert agg.cell((0.6, 0.9)) == 6
    frames(agg, [(1, 0.1, 0.4), (2, 0.6, 0.9)], T0, 10)
    frames(agg, [(1, 0.1, 0.4)], T0 + 20, 5)  # 10 s gap: a stream drop, not counted
    [bucket] = agg.flush()
    assert bucket.bucket_start == datetime.fromtimestamp(T0, timezone.utc)
    assert (bucket.columns, bucket.rows, bucket.bucket_seconds) == (4, 2, 3600)
    assert bucket.cells == [(0, 15), (6, 10)]
    assert agg.flush() == []


def test_staff_are_left_out_and_hours_are_split():
    agg = HeatmapAggregator(CAM, staff_areas=[STAFF_AREA], columns=4, rows=2)
    staff_x, staff_y = STAFF_AREA[0][0] + 0.01, STAFF_AREA[0][1] + 0.01
    frames(agg, [(1, staff_x, staff_y)], T0 + 3590, 2)  # seen behind the counter: staff
    finished = frames(agg, [(1, 0.1, 0.4), (2, 0.9, 0.9)], T0 + 3592, 16)
    [first] = finished  # handed over when the next hour's first frame came
    assert first.cells == [(7, 8)]  # only the customer, until the hour ended
    [second] = agg.flush()
    assert second.bucket_start == datetime.fromtimestamp(T0 + 3600, timezone.utc)
    assert second.cells == [(7, 8)]


def test_empty_hours_are_sent_too():
    agg = HeatmapAggregator(CAM)
    frames(agg, [], T0, 3)
    [bucket] = agg.flush()
    assert bucket.cells == []


def test_pipeline_builds_the_heatmap_only_when_switched_on():
    staff = zone(ZoneType.STAFF_AREA, STAFF_AREA)
    on = CameraPipeline(
        camera([FeatureKey.HEATMAP], [staff]), ALWAYS_OPEN, {PERSON: ScriptedDetector}
    )
    assert on.heatmap is not None and len(on.heatmap.staff_areas) == 1
    assert [d.labels for d in on.detectors] == [frozenset({PERSON})]  # people are detected
    off = CameraPipeline(camera([], [staff]), ALWAYS_OPEN, {PERSON: ScriptedDetector})
    assert off.heatmap is None and off.flush_heatmaps() == []


def test_outbox_merges_partial_buckets_and_syncs(tmp_path, cloud, client):
    outbox = Outbox(tmp_path / "outbox.sqlite3")
    start = datetime.fromtimestamp(T0, timezone.utc)

    def bucket(cells):
        return HeatmapBucketIn(camera_id=CAM, bucket_start=start, columns=4, rows=2, cells=cells)

    outbox.add_heatmaps([bucket([(0, 10), (3, 5)])])
    syncer = Syncer(outbox, client)
    assert syncer.sync_heatmaps(time.time()) == 1
    assert syncer.sync_heatmaps(time.time()) == 0  # sent

    # The camera restarted mid-hour: the rest of the hour is added and sent again.
    outbox.add_heatmaps([bucket([(3, 7), (5, 1)])])
    assert syncer.sync_heatmaps(time.time()) == 1
    assert [b["cells"] for b in cloud.heatmaps] == [[[0, 10], [3, 5]], [[0, 10], [3, 12], [5, 1]]]

    # Cloud down: kept and retried later.
    cloud.down = True
    outbox.add_heatmaps([bucket([(1, 1)])])
    assert syncer.sync_heatmaps(time.time()) == 0
    assert outbox.due_heatmaps(time.time() + 3600, 10)[0][1].cells == [
        (0, 10),
        (1, 1),
        (3, 12),
        (5, 1),
    ]

    # Sent buckets are forgotten after a few hours.
    outbox.mark_heatmaps_sent([(str(CAM), T0)])
    outbox.cleanup_aggregates(T0 + 7 * 3600)
    assert json.loads(json.dumps(outbox.due_heatmaps(T0 + 99999, 10))) == []
    outbox.close()
