import json

import pytest

from cctv_ml import review_sweep as rs
from cctv_ml.io import InputError

HEADER = "event_id,event_type,confidence,review_status,started_at"


def manifest(path, rows):
    path.write_text("\n".join([HEADER, *(",".join(r) for r in rows)]) + "\n", encoding="utf-8")
    return path


def test_sweep_suggests_the_lowest_threshold_reaching_the_target(tmp_path):
    path = manifest(
        tmp_path / "manifest.csv",
        [
            ("e1", "missing_gloves", "0.55", "false_alarm", "2026-11-01T08:00:00+03:00"),
            ("e2", "missing_gloves", "0.60", "false_alarm", "2026-11-02T08:00:00+03:00"),
            ("e3", "missing_gloves", "0.70", "valid", "2026-11-03T08:00:00+03:00"),
            ("e4", "missing_gloves", "0.90", "valid", "2026-11-04T08:00:00+03:00"),
            ("e5", "after_hours_person", "0.95", "valid", "2026-11-04T23:00:00+03:00"),
            ("e6", "missing_gloves", "0.80", "reviewed", "2026-11-04T09:00:00+03:00"),
        ],
    )
    rows = rs.load(path)
    assert len(rows) == 5  # "reviewed" has no verdict
    assert rs.span_days(rows) == 4
    sweeps = {s.event_type: s for s in rs.sweep(rows, 4, 0.9)}
    gloves = sweeps["missing_gloves"]
    assert (gloves.valid, gloves.false_alarm) == (2, 2)
    assert gloves.steps[0].precision == 0.5 and gloves.steps[0].false_per_day == 0.5
    assert gloves.suggested.threshold == 0.7
    assert (gloves.suggested.kept_valid, gloves.suggested.kept_false) == (2, 0)
    assert gloves.suggested.kept_share == 1.0
    assert sweeps["after_hours_person"].suggested.threshold == 0.0  # already precise

    out, js = tmp_path / "sweep.md", tmp_path / "sweep.json"
    assert rs.main(["--manifest", str(path), "--out", str(out), "--json", str(js)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "| missing_gloves | 2 | 2 | 50% | 0.7 | 100% | 0 |" in text
    assert json.loads(js.read_text())["event_types"][1]["suggested"]["threshold"] == 0.7


def test_target_not_reached_and_bad_input(tmp_path):
    path = manifest(
        tmp_path / "m.csv",
        [
            ("e1", "phone_use", "0.9", "false_alarm", "2026-11-01T08:00:00+03:00"),
            ("e2", "phone_use", "0.5", "valid", "2026-11-01T09:00:00+03:00"),
        ],
    )
    [phone] = rs.sweep(rs.load(path), 1, 0.9)
    assert phone.suggested is None
    assert "not reached" in rs.to_markdown([phone], 1, 0.9)
    bad = manifest(
        tmp_path / "bad.csv", [("e1", "phone_use", "high", "valid", "2026-11-01T08:00:00+03:00")]
    )
    with pytest.raises(InputError, match="confidence"):
        rs.load(bad)
    assert rs.main(["--manifest", str(bad), "--out", str(tmp_path / "x.md")]) == 2
