"""Tests that run the real ffmpeg (skipped when it is not installed)."""

import subprocess
import time
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from cctv_edge.geometry import Box
from cctv_edge.media import build_clip, save_snapshot, still_clip
from cctv_edge.ringbuffer import SegmentRecorder, list_segments, prune, segments_between
from cctv_edge.streams import FrameReader, StreamHealth


def duration_seconds(ffmpeg_bin: str, path: Path) -> float:
    """Decode the whole file; returns its length from the decoded frame count."""
    result = subprocess.run(  # noqa: S603
        [ffmpeg_bin, "-hide_banner", "-i", str(path), "-map", "0:v", "-f", "null", "-"],
        capture_output=True,
        text=True,
        check=True,
    )
    time_field = result.stderr.rsplit("time=", 1)[1].split()[0]
    h, m, s = time_field.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def test_frame_reader(ffmpeg_bin, sample_video):
    reader = FrameReader(ffmpeg_bin, str(sample_video), fps=5, width=64, height=36)
    frames = []
    for ts, frame in reader.frames():
        frames.append((ts, frame))
        if len(frames) == 5:
            break
    assert len(frames) == 5
    assert frames[0][1].shape == (36, 64, 3)
    assert frames[0][1].dtype == np.uint8


def test_frame_reader_bad_url(ffmpeg_bin, tmp_path):
    reader = FrameReader(ffmpeg_bin, str(tmp_path / "missing.mp4"), 5, 64, 36)
    assert list(reader.frames()) == []
    time.sleep(0.2)  # stderr is collected in the background
    assert reader.stderr_tail


def test_stream_health():
    health = StreamHealth(target_fps=5, stale_after=10)
    assert health.report(100).health.value == "offline"
    for i in range(50):
        health.frame(100 + i * 0.2)
    assert health.report(110).health.value == "online"
    assert health.report(110).fps == pytest.approx(5, rel=0.1)
    for i in range(10):
        health.frame(111 + i)  # 1 fps
    assert health.report(120).health.value == "degraded"
    assert health.report(140).health.value == "offline"


def test_ring_buffer_records_and_prunes(ffmpeg_bin, sample_video, tmp_path):
    ring = tmp_path / "ring"
    recorder = SegmentRecorder(
        ffmpeg_bin,
        str(sample_video),
        ring,
        segment_seconds=1,
        keep_seconds=600,
        extra_input_args=["-re"],  # play the file at real speed, like a live camera
    )
    started = time.time()
    recorder.start()
    try:
        deadline = time.time() + 20
        while len(list_segments(ring)) < 5 and time.time() < deadline:
            time.sleep(0.5)
    finally:
        recorder.stop()
    segments = list_segments(ring)
    assert len(segments) >= 5
    # Names are UTC start times, so they line up with event timestamps.
    assert started - 2 <= segments[0].start <= started + 3

    end = segments[-1].start
    chosen = segments_between(segments, segments[1].start + 0.5, end)
    assert chosen[0] == segments[1]
    assert segments[-1] not in chosen  # still being written

    clip = tmp_path / "clip.mp4"
    build_clip(ffmpeg_bin, chosen, segments[1].start + 0.5, segments[3].start, clip)
    assert 1 <= duration_seconds(ffmpeg_bin, clip) <= 2.6

    assert prune(ring, keep_seconds=0, now=time.time() + 60) == len(segments) - 1
    assert len(list_segments(ring)) == 1


def test_snapshot_draws_boxes(tmp_path):
    frame = np.zeros((100, 200, 3), np.uint8)
    path = tmp_path / "snap.jpg"
    save_snapshot(frame, [Box(0.25, 0.2, 0.75, 0.8)], path)
    image = np.asarray(Image.open(path))
    assert image.shape == (100, 200, 3)
    assert image[20, 100, 0] > 150  # red box edge
    assert image[50, 100].max() < 40  # inside stays black


def test_still_clip(ffmpeg_bin, tmp_path):
    snap = tmp_path / "snap.jpg"
    save_snapshot(np.zeros((101, 201, 3), np.uint8), [], snap)  # odd size on purpose
    clip = tmp_path / "clip.mp4"
    still_clip(ffmpeg_bin, snap, clip)
    assert duration_seconds(ffmpeg_bin, clip) == pytest.approx(2, abs=0.3)
