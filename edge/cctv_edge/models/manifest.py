"""Model manifests: every model on the box lives in its own folder under `models_dir`
with a `model.json` that says what it is, where it came from and how to run it.

    /var/lib/cctv-edge/models/person-yolox-s/
        model.json
        model.onnx

Weights are never in git: they are copied onto the box at install time (see
infra/edge/provision.sh). A manifest must name the model's licence, so nothing with an
unknown licence runs in production (plan §1 #12).
"""

import hashlib
import json
import logging
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

log = logging.getLogger(__name__)

MANIFEST_NAME = "model.json"


class ModelTask(str, Enum):
    DETECTOR = "detector"  # people and objects in the whole frame
    DRAWER_CLASSIFIER = "drawer_classifier"  # cash drawer crop: closed / open
    PPE_CLASSIFIER = "ppe_classifier"  # person crop: mask / gloves / hair cap worn
    FACE_DETECTOR = "face_detector"
    FACE_EMBEDDER = "face_embedder"


class Runtime(str, Enum):
    ONNXRUNTIME = "onnxruntime"
    OPENCV = "opencv"  # OpenCV's face API (FaceDetectorYN / FaceRecognizerSF)


class OutputFormat(str, Enum):
    YOLOX = "yolox"  # (N, 5 + classes): box, objectness, class scores
    RTDETR = "rtdetr"  # DETR-style: boxes + class scores (RT-DETR, D-FINE)
    SOFTMAX = "softmax"  # classifier logits, one class per crop
    SIGMOID = "sigmoid"  # classifier logits, independent attributes
    PROBABILITIES = "probabilities"  # classifier output that is already 0..1


class InputSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    width: int = Field(ge=8, le=4096)
    height: int = Field(ge=8, le=4096)
    # pixel (0..255) * scale, then (x - mean) / std per channel, in `channel_order`.
    scale: float = 1 / 255
    mean: tuple[float, float, float] = (0.0, 0.0, 0.0)
    std: tuple[float, float, float] = (1.0, 1.0, 1.0)
    channel_order: str = Field(default="rgb", pattern="^(rgb|bgr)$")
    # Keep the aspect ratio and pad (YOLOX), or stretch to the input size (RT-DETR).
    letterbox: bool = True
    pad_value: int = Field(default=114, ge=0, le=255)


class ModelManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_.\-]+$")
    version: str = Field(min_length=1, max_length=20, pattern=r"^[A-Za-z0-9_.\-]+$")
    task: ModelTask
    runtime: Runtime = Runtime.ONNXRUNTIME
    file: str = "model.onnx"
    sha256: str = Field(pattern="^[0-9a-f]{64}$")
    licence: str = Field(min_length=1, max_length=200, description="e.g. 'Apache-2.0'")
    source: str | None = Field(default=None, max_length=500, description="Where it came from")
    input: InputSpec | None = None
    output_format: OutputFormat | None = None
    classes: list[str] = Field(default_factory=list)
    # Detector class -> edge label (person, phone, ...). Classes not listed are dropped.
    label_map: dict[str, str] = Field(default_factory=dict)
    score_threshold: float = Field(default=0.4, ge=0, le=1)
    nms_iou: float = Field(default=0.5, gt=0, le=1)
    # Format-specific options, e.g. {"strides": [8, 16, 32]} for YOLOX.
    options: dict[str, object] = Field(default_factory=dict)

    @field_validator("file")
    @classmethod
    def _plain_file_name(cls, value: str) -> str:
        if "/" in value or "\\" in value or value.startswith("."):
            raise ValueError("must be a file name inside the model folder")
        return value

    @property
    def model_version(self) -> str:
        """Identifies the weights, e.g. on face templates made with them."""
        return f"{self.name}-{self.version}"


class ModelError(Exception):
    """A model can't be used: bad manifest, missing or changed file, missing runtime."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest(folder: Path) -> tuple[ModelManifest, Path]:
    """The manifest in `folder` and the path of its weights, after checking their hash."""
    try:
        manifest = ModelManifest.model_validate(
            json.loads((folder / MANIFEST_NAME).read_text(encoding="utf-8"))
        )
    except (OSError, ValueError, ValidationError) as exc:
        raise ModelError(f"{folder.name}: invalid {MANIFEST_NAME}: {exc}") from exc
    path = folder / manifest.file
    if not path.is_file():
        raise ModelError(f"{folder.name}: {manifest.file} is missing")
    if sha256_file(path) != manifest.sha256:
        raise ModelError(f"{folder.name}: {manifest.file} does not match its sha256")
    return manifest, path


def discover(models_dir: Path) -> dict[ModelTask, list[tuple[ModelManifest, Path]]]:
    """Valid models under `models_dir` by task, sorted by folder name. Broken ones are
    logged and skipped, so the box keeps running with what it has."""
    found: dict[ModelTask, list[tuple[ModelManifest, Path]]] = {}
    if not models_dir.is_dir():
        return found
    for folder in sorted(p for p in models_dir.iterdir() if (p / MANIFEST_NAME).is_file()):
        try:
            manifest, path = load_manifest(folder)
        except ModelError as exc:
            log.error("model skipped: %s", exc)
            continue
        log.info(
            "model %s (%s, licence %s)",
            manifest.model_version,
            manifest.task.value,
            manifest.licence,
        )
        found.setdefault(manifest.task, []).append((manifest, path))
    return found
