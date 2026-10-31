import csv
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from cctv_contracts.enums import EventType
from cctv_ml import labelstudio as ls
from cctv_ml.io import InputError

CONFIGS = Path(__file__).resolve().parents[1] / "labelling" / "configs"


def _videos(tmp_path):
    path = tmp_path / "videos.csv"
    path.write_text(
        "video,camera_id,started_at,fps\nkitchen-0101.mp4,cam-k,2026-10-01T12:00:00+03:00,25\n",
        encoding="utf-8",
    )
    return path


def _task(task_id=7, video="/data/upload/3/1a2b3c4d-kitchen-0101.mp4", annotations=None):
    return {"id": task_id, "data": {"video": video}, "annotations": annotations or []}


def _result(label, start, end):
    return {
        "type": "timelinelabels",
        "from_name": "events",
        "to_name": "video",
        "value": {"ranges": [{"start": start, "end": end}], "timelinelabels": [label]},
    }


def test_frames_become_utc_times(tmp_path):
    videos = ls.load_videos(_videos(tmp_path))
    tasks = [
        _task(annotations=[{"id": 1, "result": [_result("missing_gloves", 26, 50)]}]),
    ]
    rows, warnings = ls.convert(tasks, videos)
    assert warnings == []
    assert rows == [
        {
            "camera_id": "cam-k",
            "event_type": "missing_gloves",
            "started_at": "2026-10-01T09:00:01+00:00",
            "ended_at": "2026-10-01T09:00:02+00:00",
            "note": "ls task 7",
        }
    ]


def test_latest_non_cancelled_annotation_is_used(tmp_path):
    videos = ls.load_videos(_videos(tmp_path))
    annotations = [
        {"id": 1, "updated_at": "2026-10-02T10:00:00Z", "result": [_result("drawer_open", 1, 25)]},
        {"id": 2, "updated_at": "2026-10-03T10:00:00Z", "result": [_result("drawer_open", 51, 75)]},
        {"id": 3, "updated_at": "2026-10-04T10:00:00Z", "was_cancelled": True, "result": []},
    ]
    rows, _ = ls.convert([_task(annotations=annotations)], videos)
    assert [r["started_at"] for r in rows] == ["2026-10-01T09:00:02+00:00"]


def test_unknown_videos_and_unannotated_tasks_are_warned(tmp_path):
    videos = ls.load_videos(_videos(tmp_path))
    rows, warnings = ls.convert(
        [_task(1, video="other.mp4"), _task(2), {"id": 3, "data": {}}], videos
    )
    assert rows == []
    assert len(warnings) == 3


def test_unknown_label_and_seconds_units(tmp_path):
    videos = ls.load_videos(_videos(tmp_path))
    with pytest.raises(InputError, match="unknown event type"):
        ls.convert([_task(annotations=[{"result": [_result("cat", 1, 2)]}])], videos)
    rows, _ = ls.convert(
        [_task(annotations=[{"result": [_result("drawer_open", 10, 12.5)]}])],
        videos,
        units="seconds",
    )
    assert rows[0]["ended_at"] == "2026-10-01T09:00:12.500000+00:00"


def test_cli_writes_ground_truth_csv(tmp_path):
    export = tmp_path / "export.json"
    export.write_text(
        json.dumps([_task(annotations=[{"result": [_result("after_hours_person", 1, 250)]}])]),
        encoding="utf-8",
    )
    out = tmp_path / "gt.csv"
    assert (
        ls.main(["--export", str(export), "--videos", str(_videos(tmp_path)), "--out", str(out)])
        == 0
    )
    with out.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["ended_at"] == "2026-10-01T09:00:10+00:00"


def test_label_configs_are_valid_xml_with_contract_event_types():
    for path in CONFIGS.glob("*.xml"):
        ET.parse(path)  # noqa: S314 - our own files
    root = ET.parse(CONFIGS / "event_intervals.xml").getroot()  # noqa: S314
    labels = [label.get("value") for label in root.iter("Label")]
    assert labels and all(EventType(value) for value in labels)
    ppe = ET.parse(CONFIGS / "ppe_attributes.xml").getroot()  # noqa: S314
    names = {c.get("name") for c in ppe.iter("Choices")}
    assert {"mask", "gloves", "hair_cap"} <= names
    for choices in ppe.iter("Choices"):
        if choices.get("name") in {"mask", "gloves", "hair_cap"}:
            values = {c.get("value") for c in choices.iter("Choice")}
            assert values == {"worn", "not_worn", "not_visible"}
