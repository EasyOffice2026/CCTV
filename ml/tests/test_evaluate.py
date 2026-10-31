import csv
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from cctv_contracts.enums import EventType
from cctv_ml import evaluate as ev
from cctv_ml.io import InputError

T0 = datetime(2026, 10, 31, 0, 0, tzinfo=timezone.utc)
PERSON = EventType.AFTER_HOURS_PERSON
DRAWER = EventType.DRAWER_OPEN


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def truth(start, end, camera="cam", event_type=PERSON):
    return ev.Truth(camera, event_type, at(start), at(end))


def pred(start, end, camera="cam", event_type=PERSON, confidence=0.9, event_id=""):
    return ev.Prediction(camera, event_type, at(start), at(end), confidence, event_id)


def day_coverage(*cameras, hours=24):
    return {c: [(T0, T0 + timedelta(hours=hours))] for c in cameras}


def test_overlap_tolerance_and_types():
    matching = ev.match(
        [truth(100, 110), truth(200, 210), truth(300, 310)],
        [
            pred(105, 120),  # overlaps
            pred(211.5, 215),  # 1.5 s after the end: within 2 s tolerance
            pred(313, 320),  # 3 s after: too far
            pred(100, 110, event_type=DRAWER),  # other type
            pred(100, 110, camera="other"),  # other camera
        ],
        tolerance=2,
    )
    assert len(matching.matched) == 2
    assert len(matching.misses) == 1 and matching.misses[0].start == at(300)
    assert len(matching.false_positives) == 3


def test_split_event_parts_are_duplicates_not_false_alarms():
    matching = ev.match(
        [truth(0, 300)], [pred(0, 120), pred(120, 240), pred(240, 300)], tolerance=2
    )
    assert len(matching.matched) == 1
    assert len(matching.duplicates) == 3 - 1
    assert matching.false_positives == []


def test_each_truth_matched_once_greedy_by_overlap():
    # One long prediction spanning two labelled events matches the bigger overlap.
    matching = ev.match([truth(0, 10), truth(20, 60)], [pred(5, 60)], tolerance=0)
    assert [t.start for t, _ in matching.matched] == [at(20)]
    assert [t.start for t in matching.misses] == [at(0)]


def test_point_prediction_inside_truth_matches():
    matching = ev.match([truth(0, 10)], [pred(5, 5)], tolerance=0)
    assert len(matching.matched) == 1


def test_coverage_filtering_and_false_alarms_per_day():
    coverage = {
        "cam": [(T0, T0 + timedelta(hours=12))],
        "cam2": [(T0 + timedelta(hours=6), T0 + timedelta(hours=18))],
    }
    result = ev.evaluate(
        [truth(10, 20), truth(13 * 3600, 13 * 3600 + 5)],  # second is outside "cam" coverage
        [
            pred(10, 20),
            pred(500, 510),
            pred(7 * 3600, 7 * 3600 + 5, camera="cam2"),
            pred(20 * 3600, 20 * 3600 + 5, camera="cam2"),
        ],  # last outside coverage
        coverage,
    )
    assert result.ignored_truths == 1
    assert result.ignored_predictions == 1
    assert result.stats.camera_hours == pytest.approx(24)
    assert result.stats.wall_hours == pytest.approx(18)
    counts = result.by_feature()["security"]
    assert (counts.tp, counts.fp, counts.fn) == (1, 2, 0)
    data = ev.to_json(result)
    assert data["features"]["security"]["false_alarms_per_day"] == pytest.approx(2 / 0.75)
    assert data["features"]["security"]["false_alarms_per_camera_day"] == pytest.approx(2.0)


def test_metrics_status_and_delay():
    truths = [truth(i * 100, i * 100 + 10) for i in range(10)]
    preds = [pred(i * 100 + 2, i * 100 + 12) for i in range(10)] + [pred(5000, 5010)]
    result = ev.evaluate(truths, preds, day_coverage("cam"))
    counts = result.by_feature()["security"]
    assert counts.precision == pytest.approx(10 / 11)
    assert counts.recall == 1.0
    assert counts.median_delay == pytest.approx(2.0)
    assert result.feature_status(counts) == "pass"

    result = ev.evaluate(truths, preds[:8], day_coverage("cam"))
    assert result.feature_status(result.by_feature()["security"]) == "fail"
    assert result.feature_status(ev.Counts()) == "not enough data"


def test_threshold_sweep_trades_false_alarms_for_recall():
    truths = [truth(0, 10), truth(100, 110)]
    preds = [
        pred(0, 10, confidence=0.95),
        pred(100, 110, confidence=0.55),
        pred(500, 510, confidence=0.45),
    ]
    result = ev.evaluate(truths, preds, day_coverage("cam"), thresholds=(0.4, 0.5, 0.9))
    rows = {t: c for t, c in result.sweep()[PERSON]}
    assert (rows[0.4].tp, rows[0.4].fp) == (2, 1)
    assert (rows[0.5].tp, rows[0.5].fp) == (2, 0)
    assert (rows[0.9].tp, rows[0.9].fn) == (1, 1)


def test_cli_writes_reports(tmp_path):
    preds_path = tmp_path / "events.jsonl"
    lines = [
        {
            "event_id": "e1",
            "camera_id": "cam",
            "feature": "security",
            "event_type": "after_hours_person",
            "zone_id": None,
            "track_id": None,
            "started_at": "2026-10-31T03:00:05+03:00",
            "ended_at": "2026-10-31T03:00:20+03:00",
            "confidence": 0.8,
            "attributes": {},
        },
        {
            "event_id": "e2",
            "camera_id": "cam",
            "feature": "security",
            "event_type": "after_hours_person",
            "started_at": "2026-10-31T01:00:00Z",
            "ended_at": None,
            "confidence": 0.6,
        },
    ]
    preds_path.write_text("\n".join(json.dumps(x) for x in lines) + "\n\n", encoding="utf-8")
    gt = tmp_path / "gt.csv"
    with gt.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["camera_id", "event_type", "started_at", "ended_at", "note"])
        writer.writerow(
            ["cam", "after_hours_person", "2026-10-31T00:00:00Z", "2026-10-31T00:00:15Z", "door"]
        )
        writer.writerow(["cam", "drawer_open", "2026-10-31T05:00:00Z", "2026-10-31T05:00:03Z", ""])
    coverage = tmp_path / "coverage.csv"
    coverage.write_text(
        "camera_id,started_at,ended_at\ncam,2026-10-30T21:00:00Z,2026-10-31T21:00:00Z\n",
        encoding="utf-8",
    )
    out, js = tmp_path / "r" / "report.md", tmp_path / "r" / "report.json"
    args = [
        "--predictions",
        str(preds_path),
        "--ground-truth",
        str(gt),
        "--coverage",
        str(coverage),
        "--out",
        str(out),
        "--json",
        str(js),
    ]
    assert ev.main(args) == 0
    report = out.read_text(encoding="utf-8")
    assert "| security |" in report and "**fail**" in report  # precision 1/2
    assert "| cashier |" in report and "not enough data" in report
    assert "2026-10-31 04:00:00" in report  # e2 false alarm shown in Kuwait time
    data = json.loads(js.read_text(encoding="utf-8"))
    assert data["features"]["security"]["tp"] == 1
    assert data["features"]["cashier"]["fn"] == 1
    assert ev.main([*args, "--check"]) == 1


def test_bad_predictions_are_rejected(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text(
        '{"camera_id": "c", "event_type": "nope", "started_at": "2026-10-31T00:00:00Z"}\n'
    )
    with pytest.raises(InputError, match="unknown event_type"):
        ev.load_predictions(path)
    path.write_text(
        '{"camera_id": "c", "feature": "cashier", "event_type": "after_hours_person",'
        ' "started_at": "2026-10-31T00:00:00Z"}\n'
    )
    with pytest.raises(InputError, match="not part of feature"):
        ev.load_predictions(path)


def test_markdown_lists_failures_in_local_time():
    result = ev.evaluate([truth(0, 10)], [pred(3600, 3610)], day_coverage("cam"))
    report = ev.to_markdown(result, ZoneInfo("Asia/Kuwait"))
    assert "## False alarms (1)" in report
    assert "## Missed events (1)" in report
    assert "2026-10-31 04:00:00" in report
