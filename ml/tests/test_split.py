import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from cctv_ml import split
from cctv_ml.io import InputError

KUWAIT = ZoneInfo("Asia/Kuwait")
COLUMNS = ["path", "camera_id", "started_at", "ended_at", "site", "notes"]


def _rows(cameras, days, start_day=1, site_of=lambda cam: "ardiya", per_day=2):
    rows = []
    for cam in cameras:
        for day in range(start_day, start_day + days):
            for part in range(per_day):
                start = datetime(2026, 10, day, 8 + part * 6, tzinfo=timezone(timedelta(hours=3)))
                rows.append(
                    {
                        "path": f"/data/{cam}/{day:02d}-{part}.mp4",
                        "camera_id": cam,
                        "started_at": start.isoformat(),
                        "ended_at": (start + timedelta(hours=2)).isoformat(),
                        "site": site_of(cam),
                        "notes": "x",
                    }
                )
    return rows


def _write(path: Path, rows):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def _read(path: Path):
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _groups(rows):
    seen = {}
    for row in rows:
        key = (row["camera_id"], row["day"])
        assert seen.setdefault(key, row["split"]) == row["split"], f"{key} in two splits"
    return seen


def test_camera_day_never_in_two_splits_and_columns_kept(tmp_path, capsys):
    inventory, out = tmp_path / "footage.csv", tmp_path / "manifest.csv"
    _write(inventory, _rows(["cam-a", "cam-b", "cam-c"], days=20))
    assert split.main(["--inventory", str(inventory), "--out", str(out)]) == 0
    rows = _read(out)
    assert len(rows) == 3 * 20 * 2
    assert set(rows[0]) == {*COLUMNS, "day", "split"}
    assert rows[0]["notes"] == "x"
    groups = _groups(rows)
    assert {g for g in groups.values()} == {"train", "val", "test"}
    assert "total" in capsys.readouterr().out


def test_split_is_deterministic(tmp_path):
    inventory = tmp_path / "footage.csv"
    _write(inventory, _rows(["cam-a", "cam-b"], days=15))
    out1, out2 = tmp_path / "m1.csv", tmp_path / "m2.csv"
    split.main(["--inventory", str(inventory), "--out", str(out1)])
    split.main(["--inventory", str(inventory), "--out", str(out2)])
    assert _read(out1) == _read(out2)


def test_adding_footage_does_not_move_existing_days(tmp_path):
    inventory, out = tmp_path / "footage.csv", tmp_path / "manifest.csv"
    _write(inventory, _rows(["cam-a", "cam-b"], days=10))
    split.main(["--inventory", str(inventory), "--out", str(out)])
    before = _groups(_read(out))

    _write(inventory, _rows(["cam-a", "cam-b"], days=20) + _rows(["cam-z"], days=5))
    split.main(["--inventory", str(inventory), "--out", str(out)])
    after = _groups(_read(out))
    assert all(after[key] == value for key, value in before.items())
    assert len(after) == 2 * 20 + 5


def test_hash_split_is_stable_even_without_the_old_manifest(tmp_path):
    ratios = split.parse_ratios("70,15,15")
    small = split.load_inventory(_inventory(tmp_path, _rows(["cam-a"], days=10)))[1]
    large = split.load_inventory(_inventory(tmp_path, _rows(["cam-a", "cam-b"], days=25)))[1]
    first = split.assign(small, ratios, "seed", KUWAIT)
    second = split.assign(large, ratios, "seed", KUWAIT)
    assert all(second[key] == value for key, value in first.items())


def _inventory(tmp_path, rows):
    path = tmp_path / "inv.csv"
    _write(path, rows)
    return path


def test_kept_assignment_wins_over_hash(tmp_path):
    inventory, out = tmp_path / "footage.csv", tmp_path / "manifest.csv"
    _write(inventory, _rows(["cam-a"], days=3))
    split.main(["--inventory", str(inventory), "--out", str(out)])
    rows = _read(out)
    for row in rows:
        row["split"] = "test"
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    split.main(["--inventory", str(inventory), "--out", str(out)])
    assert {r["split"] for r in _read(out)} == {"test"}
    split.main(["--inventory", str(inventory), "--out", str(out), "--fresh"])
    fresh = {r["split"] for r in _read(out)}
    assert fresh  # recomputed from the hash


def test_day_is_local_calendar_day(tmp_path):
    # 22:30 UTC on 2026-10-01 is 01:30 on 2026-10-02 in Kuwait.
    rows = [
        {
            "path": "a.mp4",
            "camera_id": "cam",
            "started_at": "2026-10-01T22:30:00Z",
            "ended_at": "2026-10-01T23:30:00Z",
            "site": "",
            "notes": "",
        }
    ]
    footage = split.load_inventory(_inventory(tmp_path, rows))[1]
    assert split.group_key(footage[0], KUWAIT)[1].isoformat() == "2026-10-02"


def test_ratios_roughly_respected_on_many_days(tmp_path):
    rows = _rows([f"cam-{i}" for i in range(5)], days=28)
    footage = split.load_inventory(_inventory(tmp_path, rows))[1]
    assigned = split.assign(footage, split.parse_ratios("70,15,15"), "cctv-v1", KUWAIT)
    share = {s: sum(1 for v in assigned.values() if v == s) / len(assigned) for s in split.SPLITS}
    assert 0.55 < share["train"] < 0.85
    assert share["test"] > 0.05


def test_stratify_by_site_balances_hours_per_site(tmp_path):
    rows = _rows(["k1", "k2"], days=10, site_of=lambda _c: "kitchen") + _rows(
        ["b1"], days=10, site_of=lambda _c: "branch"
    )
    footage = split.load_inventory(_inventory(tmp_path, rows))[1]
    assigned = split.assign(
        footage, split.parse_ratios("70,15,15"), "s", KUWAIT, stratify_site=True
    )
    for cams in (("k1", "k2"), ("b1",)):
        splits = [v for (cam, _), v in assigned.items() if cam in cams]
        assert set(splits) == {"train", "val", "test"}
        assert splits.count("train") / len(splits) == pytest.approx(0.7, abs=0.11)


def test_bad_inventory_is_reported(tmp_path, capsys):
    rows = _rows(["cam"], days=1)
    rows[0]["ended_at"] = rows[0]["started_at"]
    path = _inventory(tmp_path, rows)
    assert split.main(["--inventory", str(path), "--out", str(tmp_path / "m.csv")]) == 2
    assert "ended_at is not after started_at" in capsys.readouterr().err

    rows = _rows(["cam"], days=1)
    rows[0]["started_at"] = "2026-10-01T08:00:00"
    with pytest.raises(InputError, match="no UTC offset"):
        split.load_inventory(_inventory(tmp_path, rows))
    with pytest.raises(InputError):
        split.parse_ratios("70,30")
