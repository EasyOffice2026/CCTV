"""Evaluation of crop classifiers: PPE attributes on person crops, the cash drawer ROI.

    python -m cctv_ml.classify_eval --predictions preds.csv --labels labels.csv \
        --out report.md --json report.json

- labels CSV: `id, attribute, label`, e.g. `crop-0012, gloves, not_worn`. `not_visible`
  means the annotator could not see the item; it is never counted as right or wrong.
- predictions CSV: `id, attribute, predicted[, score]`. The model may answer `unknown`
  (crop too small or blurred, or score between its thresholds). With `--min-score`, a
  prediction below that score counts as `unknown`.

Per attribute:
- precision / recall per class are computed on items the annotator could see; an
  `unknown` answer on a visible item counts against recall (the violation was missed)
  but not against precision;
- `decided accuracy` is the share right among visible items the model gave an answer for;
- `unknown on visible` is how often the model gave no answer although the item was
  visible: the price of being careful, which the plan prefers to guessing (§9);
- `answers on not-visible` counts items the annotator could not judge but the model did:
  worth a look, since they are guesses.
"""

import argparse
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cctv_ml.io import InputError, fmt_ratio, read_csv, safe_console

NOT_VISIBLE = "not_visible"
UNKNOWN = "unknown"
MISSING = "(no prediction)"
NO_ANSWER = frozenset({UNKNOWN, NOT_VISIBLE, MISSING})

Item = tuple[str, str]  # (id, attribute)


def load_labels(path: Path) -> dict[Item, str]:
    labels: dict[Item, str] = {}
    for line, row in read_csv(path, ("id", "attribute", "label")):
        key = (row["id"], row["attribute"])
        if not row["label"]:
            raise InputError(f"{path}:{line}: empty label")
        if labels.setdefault(key, row["label"]) != row["label"]:
            raise InputError(f"{path}:{line}: {key[0]}/{key[1]} has two different labels")
    if not labels:
        raise InputError(f"{path}: no labels")
    return labels


def load_predictions(path: Path, min_score: float | None = None) -> dict[Item, str]:
    predictions: dict[Item, str] = {}
    for line, row in read_csv(path, ("id", "attribute", "predicted")):
        key = (row["id"], row["attribute"])
        if key in predictions:
            raise InputError(f"{path}:{line}: {key[0]}/{key[1]} is predicted twice")
        predicted = row["predicted"] or UNKNOWN
        if min_score is not None and row.get("score"):
            try:
                score = float(row["score"])
            except ValueError as exc:
                raise InputError(f"{path}:{line}: score is not a number") from exc
            if score < min_score:
                predicted = UNKNOWN
        predictions[key] = predicted
    return predictions


@dataclass
class ClassMetrics:
    support: int = 0  # visible items with this label
    predicted: int = 0  # visible items the model gave this answer
    correct: int = 0

    @property
    def precision(self) -> float | None:
        return self.correct / self.predicted if self.predicted else None

    @property
    def recall(self) -> float | None:
        return self.correct / self.support if self.support else None


@dataclass
class AttributeReport:
    attribute: str
    confusion: Counter = field(default_factory=Counter)  # (label, predicted) -> n
    classes: dict[str, ClassMetrics] = field(default_factory=dict)
    visible: int = 0
    decided: int = 0
    correct: int = 0
    not_visible: int = 0
    answered_not_visible: int = 0

    @property
    def decided_accuracy(self) -> float | None:
        return self.correct / self.decided if self.decided else None

    @property
    def unknown_on_visible(self) -> float | None:
        return (self.visible - self.decided) / self.visible if self.visible else None


def evaluate(labels: dict[Item, str], predictions: dict[Item, str]) -> list[AttributeReport]:
    reports: dict[str, AttributeReport] = {}
    for (item_id, attribute), label in sorted(labels.items()):
        report = reports.setdefault(attribute, AttributeReport(attribute))
        predicted = predictions.get((item_id, attribute), MISSING)
        report.confusion[(label, predicted)] += 1
        answered = predicted not in NO_ANSWER
        if label == NOT_VISIBLE:
            report.not_visible += 1
            report.answered_not_visible += answered
            continue
        report.visible += 1
        report.classes.setdefault(label, ClassMetrics()).support += 1
        if answered:
            report.decided += 1
            report.classes.setdefault(predicted, ClassMetrics()).predicted += 1
            if predicted == label:
                report.correct += 1
                report.classes[label].correct += 1
    return [reports[a] for a in sorted(reports)]


def unlabelled(labels: dict[Item, str], predictions: dict[Item, str]) -> int:
    return sum(1 for key in predictions if key not in labels)


def to_json(reports: list[AttributeReport], ignored: int) -> dict[str, Any]:
    return {
        "predictions_without_label": ignored,
        "attributes": {
            r.attribute: {
                "visible": r.visible,
                "not_visible": r.not_visible,
                "decided_accuracy": r.decided_accuracy,
                "unknown_on_visible": r.unknown_on_visible,
                "answers_on_not_visible": r.answered_not_visible,
                "classes": {
                    name: {
                        "support": m.support,
                        "predicted": m.predicted,
                        "correct": m.correct,
                        "precision": m.precision,
                        "recall": m.recall,
                    }
                    for name, m in sorted(r.classes.items())
                },
                "confusion": [
                    {"label": label, "predicted": predicted, "count": n}
                    for (label, predicted), n in sorted(r.confusion.items())
                ],
            }
            for r in reports
        },
    }


def to_markdown(reports: list[AttributeReport], ignored: int) -> str:
    out = ["# Classifier evaluation report", ""]
    if ignored:
        out += [f"{ignored} prediction(s) had no label and were ignored.", ""]
    for r in reports:
        out += [
            f"## {r.attribute}",
            "",
            f"- Visible items: {r.visible}; not visible: {r.not_visible}",
            f"- Decided accuracy: {fmt_ratio(r.decided_accuracy)}",
            f"- Unknown on visible items: {fmt_ratio(r.unknown_on_visible)}",
            f"- Answers on not-visible items: {r.answered_not_visible}",
            "",
            "| Class | Support | Predicted | Correct | Precision | Recall |",
            "|---|---|---|---|---|---|",
        ]
        out += [
            f"| {name} | {m.support} | {m.predicted} | {m.correct} | {fmt_ratio(m.precision)}"
            f" | {fmt_ratio(m.recall)} |"
            for name, m in sorted(r.classes.items())
        ]
        labels = sorted({label for label, _ in r.confusion})
        predicted = sorted({p for _, p in r.confusion})
        out += [
            "",
            "Confusion (rows: label, columns: prediction):",
            "",
            "| label \\ predicted | " + " | ".join(predicted) + " |",
            "|---" * (len(predicted) + 1) + "|",
        ]
        by_label: dict[str, dict[str, int]] = defaultdict(dict)
        for (label, p), n in r.confusion.items():
            by_label[label][p] = n
        out += [
            f"| {label} | " + " | ".join(str(by_label[label].get(p, 0)) for p in predicted) + " |"
            for label in labels
        ]
        out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m cctv_ml.classify_eval", description=__doc__.split("\n\n")[0]
    )
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--min-score", type=float, help="below this, a prediction is unknown")
    parser.add_argument("--out", type=Path, help="markdown report (default: print)")
    parser.add_argument("--json", type=Path, help="also write the results as JSON")
    args = parser.parse_args(argv)
    safe_console()
    try:
        labels = load_labels(args.labels)
        predictions = load_predictions(args.predictions, args.min_score)
    except InputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    reports = evaluate(labels, predictions)
    ignored = unlabelled(labels, predictions)
    report = to_markdown(reports, ignored)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report, encoding="utf-8", newline="\n")
        print(f"wrote {args.out}")
    else:
        print(report)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(to_json(reports, ignored), indent=2), encoding="utf-8", newline="\n"
        )
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
