"""Kitchen compliance (plan 5.3): is each person in a kitchen zone wearing a mask, gloves
and a hair cap?

People are judged only when their crop is big and sharp enough, and only when the
classifier is clearly sure either way. Anything else is UNKNOWN, which never becomes a
violation: a blurred person far from a ceiling fisheye camera must not produce alerts.
Each person is classified at most every `classify_interval_s`; in between, their last
verdict is reused for a short while, so the rules see a steady signal.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

import numpy as np

from cctv_edge.detection import NO_GLOVES, NO_HAIR_CAP, NO_MASK
from cctv_edge.imaging import crop, sharpness
from cctv_edge.models.classifier import Classifier
from cctv_edge.tracking import Track

# Item -> label of the observation when it is missing. The PPE model's classes use the
# item names, each the probability that the item is worn.
ITEM_LABELS: dict[str, str] = {"mask": NO_MASK, "gloves": NO_GLOVES, "hair_cap": NO_HAIR_CAP}


class Verdict(str, Enum):
    WORN = "worn"
    MISSING = "missing"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PpeSettings:
    min_crop_height_px: float = 96
    min_sharpness: float = 20.0
    worn_threshold: float = 0.6
    missing_threshold: float = 0.3
    classify_interval_s: float = 1.0

    @classmethod
    def from_params(cls, params: Mapping[str, float]) -> "PpeSettings":
        return cls(**{k: v for k, v in params.items() if k in cls.__dataclass_fields__})

    @property
    def verdict_ttl_s(self) -> float:
        return 2 * self.classify_interval_s


@dataclass
class _Checked:
    at: float
    verdicts: dict[str, Verdict]
    confidence: float


class PpeChecker:
    def __init__(self, classifier: Classifier, items: Sequence[str], settings: PpeSettings):
        self.classifier = classifier
        self.items = [i for i in items if i in ITEM_LABELS]
        self.settings = settings
        self._last: dict[int, _Checked] = {}

    def check(
        self, frame: np.ndarray, tracks: Sequence[Track], ts: float
    ) -> dict[int, tuple[dict[str, Verdict], float]]:
        """{track id: ({item: verdict}, confidence)} for `tracks` (people in a kitchen
        zone). Confidence is how sure the classifier was about the missing items."""
        s = self.settings
        live = {t.id for t in tracks}
        self._last = {k: v for k, v in self._last.items() if k in live}
        results = {}
        for track in tracks:
            last = self._last.get(track.id)
            if last is None or ts - last.at >= s.classify_interval_s:
                verdicts, confidence = self._classify(crop(frame, track.box))
                if any(v is not Verdict.UNKNOWN for v in verdicts.values()):
                    last = self._last[track.id] = _Checked(ts, verdicts, confidence)
                elif last is not None and ts - last.at > s.verdict_ttl_s:
                    last = None
            if last is not None and ts - last.at <= s.verdict_ttl_s:
                results[track.id] = (last.verdicts, last.confidence)
            else:
                results[track.id] = ({i: Verdict.UNKNOWN for i in self.items}, 0.0)
        return results

    def _classify(self, image: np.ndarray) -> tuple[dict[str, Verdict], float]:
        s = self.settings
        unknown = {i: Verdict.UNKNOWN for i in self.items}
        if image.shape[0] < s.min_crop_height_px or sharpness(image) < s.min_sharpness:
            return unknown, 0.0
        probabilities = self.classifier.classify(image)
        verdicts, confidence = {}, 1.0
        for item in self.items:
            p = probabilities.get(item)
            if p is None:
                verdicts[item] = Verdict.UNKNOWN
            elif p >= s.worn_threshold:
                verdicts[item] = Verdict.WORN
            elif p <= s.missing_threshold:
                verdicts[item] = Verdict.MISSING
                confidence = min(confidence, 1.0 - p)
            else:
                verdicts[item] = Verdict.UNKNOWN
        return verdicts, confidence if Verdict.MISSING in verdicts.values() else 0.0
