"""End-to-end tests of the edge service against the fake cloud (Phase 2 exit criteria)."""

import time

import numpy as np
import pytest

from cctv_contracts.enums import FeatureKey, ZoneType
from cctv_contracts.v1 import ClipSettings, FeatureConfig
from cctv_edge.detection import PERSON
from cctv_edge.geometry import Box
from cctv_edge.outbox import Outbox
from cctv_edge.ringbuffer import SegmentRecorder, list_segments
from cctv_edge.service import EdgeService

from .conftest import camera, edge_config, zone
from .test_pipeline import ScriptedDetector

FRAME = np.zeros((36, 64, 3), np.uint8)
FAST = FeatureConfig(
    key=FeatureKey.SECURITY,
    params={"after_hours_person": {"min_duration_s": 1, "max_gap_s": 1}},
)


@pytest.fixture
def service(settings, client, cloud):
    outbox = Outbox(settings.outbox_path)
    svc = EdgeService(
        settings, client, outbox, detector_factories={PERSON: ScriptedDetector}, start_streams=False
    )
    yield svc
    svc.shutdown()
    outbox.close()


def feed(worker, boxes, start, seconds, fps=5.0):
    """Push frames with timestamps start .. start+seconds through the camera's pipeline."""
    worker.pipeline.detectors[0].boxes = boxes
    n = int(seconds * fps)
    for i in range(n + 1):
        worker.process(FRAME, start + i / fps)
    return start + n / fps


def test_after_hours_person_reaches_cloud_with_clip(
    service, cloud, settings, ffmpeg_bin, sample_video
):
    cam = camera([FAST])
    config = edge_config([cam]).model_copy(
        update={"clip": ClipSettings(pre_seconds=1, post_seconds=1)}
    )
    cloud.config = config
    service.poll_config()
    worker = service.workers[cam.id]

    recorder = SegmentRecorder(
        ffmpeg_bin, str(sample_video), settings.ring_dir / str(cam.id), 1, 600, ["-re"]
    )
    recorder.start()
    try:
        while len(list_segments(settings.ring_dir / str(cam.id))) < 2:
            time.sleep(0.2)
        first_seen = time.time()
        last = feed(worker, [Box(0.4, 0.3, 0.5, 0.8)], first_seen, 2)
        service.sync_once(time.time())
        [event] = cloud.events.values()
        assert event["event_type"] == "after_hours_person"
        assert event["status"] == "open"
        assert time.time() - first_seen < 10  # brief: event in the cloud within 10 s
        assert cloud.objects[event["snapshot_key"]][:2] == b"\xff\xd8"  # JPEG

        feed(worker, [], last + 0.2, 2)  # person leaves
        due = last + 1 + 2 * settings.segment_seconds
        while time.time() < due + 1.5:
            time.sleep(0.2)
        assert service.run_clip_jobs(time.time()) == 1
    finally:
        recorder.stop()

    service.sync_once(time.time())
    [event] = cloud.events.values()
    assert event["status"] == "closed"
    assert "clip_fallback" not in event["attributes"]
    clip = cloud.objects[event["clip_key"]]
    assert clip[4:8] == b"ftyp"  # MP4
    assert len(clip) > 1000


def test_event_survives_network_cut(service, cloud, settings, ffmpeg_bin):
    cam = camera([FAST])
    cloud.config = edge_config([cam])
    service.poll_config()
    worker = service.workers[cam.id]

    cloud.down = True
    now = time.time()
    last = feed(worker, [Box(0.4, 0.3, 0.5, 0.8)], now, 2)
    feed(worker, [], last + 0.2, 2)
    # No ring-buffer footage here: the clip falls back to the snapshot.
    assert service.run_clip_jobs(time.time() + 60) == 1
    assert service.sync_once(time.time()) == 0
    assert cloud.events == {}
    assert service.outbox.pending_count() == 1

    cloud.down = False
    assert service.sync_once(time.time() + 600) == 1
    [event] = cloud.events.values()
    assert event["status"] == "closed"
    assert event["attributes"]["clip_fallback"] == "snapshot"
    assert service.outbox.pending_count() == 0


def test_config_changes_apply_without_restart(service, cloud):
    cam = camera([FAST])
    idle = camera([])  # no features enabled: not streamed or analysed
    cloud.config = edge_config([cam, idle])
    service.poll_config()
    assert set(service.workers) == {cam.id}
    worker = service.workers[cam.id]
    assert service.store.path.exists()  # kept for offline restarts

    # New zones: same worker (stream keeps running), new pipeline.
    restricted = zone(ZoneType.RESTRICTED, [(0, 0), (1, 0), (1, 1)])
    cloud.config = edge_config([cam.model_copy(update={"zones": [restricted]}), idle], version=2)
    old_pipeline = worker.pipeline
    service.poll_config()
    assert service.workers[cam.id] is worker
    assert worker.pipeline is not old_pipeline

    # New stream URL: the worker is replaced.
    moved = cam.model_copy(update={"rtsp_sub": "rtsp://nvr.local:554/Streaming/Channels/202"})
    cloud.config = edge_config([moved, idle], version=3)
    service.poll_config()
    assert service.workers[cam.id] is not worker

    # Feature switched off: the camera stops, and its open event is closed.
    worker = service.workers[cam.id]
    feed(worker, [Box(0.4, 0.3, 0.5, 0.8)], time.time(), 2)
    assert service.outbox.pending_count() == 1
    cloud.config = edge_config([moved.model_copy(update={"features": []}), idle], version=4)
    service.poll_config()
    assert service.workers == {}
    assert len(service.outbox.clips_due(time.time() + 600)) == 1


def test_config_survives_restart(service, cloud, settings, client):
    cam = camera([FAST])
    cloud.config = edge_config([cam])
    service.poll_config()

    cloud.down = True  # rebooted with no internet
    outbox = Outbox(settings.data_dir / "other.sqlite3")
    fresh = EdgeService(
        settings, client, outbox, detector_factories={PERSON: ScriptedDetector}, start_streams=False
    )
    try:
        fresh.start()
        assert set(fresh.workers) == {cam.id}
    finally:
        fresh.shutdown()
        outbox.close()


def test_heartbeat(service, cloud):
    cam = camera([FAST])
    cloud.config = edge_config([cam])
    service.poll_config()
    service.heartbeat_once(time.time())
    [beat] = cloud.heartbeats
    assert beat["config_version_applied"] == 1
    assert beat["cameras"][0]["camera_id"] == str(cam.id)
    assert beat["cameras"][0]["health"] == "offline"  # no stream in this test
    assert beat["system"]["outbox_pending"] == 0
    assert beat["system"]["disk_free_bytes"] > 0

    cloud.config = edge_config([cam], version=2)
    service.heartbeat_once(time.time())
    assert service._config_wanted.is_set()  # newer config: fetch it now


def test_stalled_loops_are_detected(service):
    service._ticks = {"sync": (1000.0, 2.0), "heartbeat": (100.0, 30.0)}
    assert service.stalled_loops(now=1000.0) == []
    assert service.stalled_loops(now=1500.0) == ["heartbeat"]  # 1400 s in one step
