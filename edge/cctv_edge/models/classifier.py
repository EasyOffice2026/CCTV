"""Crop classifiers (cash drawer open/closed, PPE worn) with an ONNX model."""

from collections.abc import Callable
from typing import Protocol

import numpy as np

from cctv_edge.imaging import fit, to_tensor
from cctv_edge.models.manifest import ModelError, ModelManifest, OutputFormat
from cctv_edge.models.runtime import Session


class Classifier(Protocol):
    classes: tuple[str, ...]

    def classify(self, crop: np.ndarray) -> dict[str, float]: ...


ClassifierFactory = Callable[[], Classifier]


def probabilities(logits: np.ndarray, output_format: OutputFormat) -> np.ndarray:
    values = np.asarray(logits, np.float64).reshape(-1)
    if output_format is OutputFormat.SOFTMAX:
        exp = np.exp(values - values.max())
        return exp / exp.sum()
    if output_format is OutputFormat.SIGMOID:
        return 1.0 / (1.0 + np.exp(-values))
    return np.clip(values, 0.0, 1.0)


class OnnxClassifier:
    """`classify` returns {class: probability}: one class per crop for `softmax`, an
    independent probability per attribute for `sigmoid`."""

    _FORMATS = (OutputFormat.SOFTMAX, OutputFormat.SIGMOID, OutputFormat.PROBABILITIES)

    def __init__(self, manifest: ModelManifest, session: Session) -> None:
        if manifest.input is None or not manifest.classes:
            raise ModelError(f"{manifest.name}: classifiers need an input spec and classes")
        if manifest.output_format not in self._FORMATS:
            raise ModelError(f"{manifest.name}: unsupported output_format for a classifier")
        self.manifest = manifest
        self.session = session
        self.classes = tuple(manifest.classes)
        self._input_name = session.get_inputs()[0].name

    def classify(self, crop: np.ndarray) -> dict[str, float]:
        spec = self.manifest.input
        assert spec is not None and self.manifest.output_format is not None  # noqa: S101
        if crop.shape[0] == 0 or crop.shape[1] == 0:
            return {}
        image, _ = fit(crop, spec.width, spec.height, spec.letterbox, spec.pad_value)
        tensor = to_tensor(image, spec.scale, spec.mean, spec.std, spec.channel_order == "bgr")
        [logits, *_] = self.session.run(None, {self._input_name: tensor})
        values = probabilities(logits, self.manifest.output_format)
        if len(values) != len(self.classes):
            raise ModelError(
                f"{self.manifest.name}: {len(values)} outputs for {len(self.classes)} classes"
            )
        return {name: float(p) for name, p in zip(self.classes, values, strict=False)}
