import json

import pytest

from cctv_ml import classify_eval as ce
from cctv_ml.io import InputError


def _csv(path, header, rows):
    path.write_text("\n".join([header, *(",".join(r) for r in rows)]) + "\n", encoding="utf-8")
    return path


def test_metrics_exclude_not_visible_and_count_unknown(tmp_path):
    labels = _csv(
        tmp_path / "labels.csv",
        "id,attribute,label",
        [
            ("c1", "gloves", "worn"),
            ("c2", "gloves", "worn"),
            ("c3", "gloves", "not_worn"),
            ("c4", "gloves", "not_worn"),
            ("c5", "gloves", "not_visible"),
            ("c6", "gloves", "not_visible"),
            ("c1", "mask", "worn"),
        ],
    )
    preds = _csv(
        tmp_path / "preds.csv",
        "id,attribute,predicted,score",
        [
            ("c1", "gloves", "worn", "0.9"),
            ("c2", "gloves", "not_worn", "0.7"),
            ("c3", "gloves", "not_worn", "0.8"),
            ("c4", "gloves", "unknown", ""),
            ("c5", "gloves", "not_worn", "0.6"),  # a guess on a not-visible item
            ("c6", "gloves", "unknown", ""),
            ("c9", "gloves", "worn", "0.9"),  # no label
        ],
    )
    label_map, pred_map = ce.load_labels(labels), ce.load_predictions(preds)
    reports = {r.attribute: r for r in ce.evaluate(label_map, pred_map)}
    gloves = reports["gloves"]
    assert (gloves.visible, gloves.not_visible) == (4, 2)
    assert gloves.decided == 3 and gloves.correct == 2
    assert gloves.decided_accuracy == pytest.approx(2 / 3)
    assert gloves.unknown_on_visible == pytest.approx(1 / 4)
    assert gloves.answered_not_visible == 1
    not_worn = gloves.classes["not_worn"]
    assert not_worn.precision == pytest.approx(1 / 2)  # c2 wrong, c3 right
    assert not_worn.recall == pytest.approx(1 / 2)  # c4 unknown counts as a miss
    assert gloves.classes["worn"].recall == pytest.approx(1 / 2)
    # mask has no prediction at all
    assert reports["mask"].unknown_on_visible == 1.0
    assert ce.unlabelled(label_map, pred_map) == 1


def test_min_score_turns_low_scores_into_unknown(tmp_path):
    preds = _csv(
        tmp_path / "p.csv",
        "id,attribute,predicted,score",
        [("a", "drawer", "open", "0.4"), ("b", "drawer", "closed", "0.95")],
    )
    assert ce.load_predictions(preds, min_score=0.5) == {
        ("a", "drawer"): "unknown",
        ("b", "drawer"): "closed",
    }


def test_cli_reports(tmp_path):
    labels = _csv(
        tmp_path / "l.csv",
        "id,attribute,label",
        [("a", "drawer", "open"), ("b", "drawer", "closed")],
    )
    preds = _csv(
        tmp_path / "p.csv",
        "id,attribute,predicted",
        [("a", "drawer", "open"), ("b", "drawer", "open")],
    )
    out, js = tmp_path / "r.md", tmp_path / "r.json"
    args = [
        "--labels",
        str(labels),
        "--predictions",
        str(preds),
        "--out",
        str(out),
        "--json",
        str(js),
    ]
    assert ce.main(args) == 0
    assert "| open | 1 | 2 | 1 | 0.500 | 1.000 |" in out.read_text(encoding="utf-8")
    data = json.loads(js.read_text(encoding="utf-8"))
    assert data["attributes"]["drawer"]["decided_accuracy"] == 0.5


def test_conflicting_inputs_are_rejected(tmp_path):
    labels = _csv(
        tmp_path / "l.csv", "id,attribute,label", [("a", "mask", "worn"), ("a", "mask", "not_worn")]
    )
    with pytest.raises(InputError, match="two different labels"):
        ce.load_labels(labels)
    preds = _csv(
        tmp_path / "p.csv", "id,attribute,predicted", [("a", "mask", "worn"), ("a", "mask", "worn")]
    )
    with pytest.raises(InputError, match="predicted twice"):
        ce.load_predictions(preds)
