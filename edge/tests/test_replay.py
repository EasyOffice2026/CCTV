"""The offline replay tool: the real pipeline over a video file, JSONL + coverage out."""

import csv
import json
import subprocess

import pytest

from cctv_contracts.enums import FeatureKey
from cctv_edge.replay import main

from .conftest import camera

# Monday 03:00 in Kuwait; the branch only opens on Thursdays, so it is after hours.
START = "2026-11-02T03:00:00+03:00"
HOURS = [{"weekday": 3, "opens_at": "09:00:00", "closes_at": "10:00:00"}]


@pytest.fixture(scope="module")
def moving_video(ffmpeg_bin, tmp_path_factory):
    """10 s of a white box moving across a black frame: motion all the way through."""
    path = tmp_path_factory.mktemp("replay") / "moving.mp4"
    subprocess.run(  # noqa: S603
        [
            ffmpeg_bin,
            *("-hide_banner", "-loglevel", "error"),
            *("-f", "lavfi", "-i", "color=c=black:s=320x240:r=10"),
            *("-f", "lavfi", "-i", "color=c=white:s=40x80:r=10"),
            *("-filter_complex", "[0][1]overlay=x='mod(t*80,280)':y=80"),
            *("-t", "10", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)),
        ],
        check=True,
    )
    return path


def test_replay_writes_events_and_coverage(tmp_path, ffmpeg_bin, moving_video):
    cam = camera([FeatureKey.SECURITY])
    (tmp_path / "camera.json").write_text(cam.model_dump_json())
    (tmp_path / "hours.json").write_text(json.dumps(HOURS))
    out, coverage = tmp_path / "out" / "events.jsonl", tmp_path / "coverage.csv"
    args = [
        str(moving_video),
        *("--camera", str(tmp_path / "camera.json")),
        *("--start", START),
        *("--hours", str(tmp_path / "hours.json")),
        *("--models-dir", str(tmp_path / "no-models")),
        *("--ffmpeg", ffmpeg_bin),
        *("--out", str(out), "--coverage", str(coverage)),
    ]
    assert main(args) == 0

    # Motion all the time: one after-hours motion event for the whole video.
    [event] = [json.loads(line) for line in out.read_text().splitlines()]
    assert set(event) == {
        "event_id",
        "camera_id",
        "feature",
        "event_type",
        "zone_id",
        "track_id",
        "started_at",
        "ended_at",
        "confidence",
        "attributes",
    }
    assert event["camera_id"] == str(cam.id)
    assert (event["feature"], event["event_type"]) == ("security", "after_hours_motion")
    assert event["started_at"] == "2026-11-02T00:00:00.200000+00:00"  # motion needs 2 frames
    assert event["ended_at"] > event["started_at"]

    rows = list(csv.reader(coverage.open()))
    assert rows[0] == ["camera_id", "started_at", "ended_at"]
    assert rows[1] == [str(cam.id), "2026-11-02T00:00:00+00:00", "2026-11-02T00:00:10+00:00"]

    assert main(args) == 0  # a second video appends, with no second header
    assert len(out.read_text().splitlines()) == 2
    assert len(list(csv.reader(coverage.open()))) == 3
