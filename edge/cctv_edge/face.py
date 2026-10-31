"""Faces for staff attendance (plan 5.4, option B: a camera at the staff entrance).

Privacy rules, enforced here and in `attendance.py`:

- Only faces that pass the quality checks are embedded at all.
- An embedding is compared with the enrolled staff templates and then dropped. A face that
  matches nobody (a customer) is never stored, logged or sent anywhere.
- Templates come from the cloud and are kept in memory only (`FaceGallery`).
- Enrolment photos are decoded in memory and never written to disk.

The analyzer is behind a small interface. The implementation uses OpenCV's face API with
YuNet (detector, MIT) and SFace (embedder, Apache-2.0), both from the OpenCV model zoo.
"""

import io
import logging
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID

import numpy as np
from PIL import Image, UnidentifiedImageError

from cctv_contracts.v1 import FaceTemplate
from cctv_edge.geometry import Box
from cctv_edge.imaging import crop, sharpness
from cctv_edge.models.manifest import ModelError, ModelManifest

log = logging.getLogger(__name__)

# Enrolment photos larger than this are scaled down first (phone photos are huge).
ENROL_MAX_SIDE = 1920
# Photos of one person should resemble each other at least this much (cosine).
ENROL_MIN_CONSISTENCY = 0.25
# With several faces in a photo, the largest is used only if it is this much bigger.
ENROL_DOMINANT_FACE_RATIO = 2.0


@dataclass(frozen=True)
class FaceQuality:
    min_size_px: float = 60
    min_sharpness: float = 30.0
    min_score: float = 0.85


ENROL_QUALITY = FaceQuality(min_size_px=80, min_sharpness=30.0, min_score=0.85)


@dataclass(frozen=True)
class Face:
    box: Box
    score: float
    size_px: int
    embedding: np.ndarray  # L2-normalised


class FaceAnalyzer(Protocol):
    model_version: str  # of the embedder; templates must come from the same one

    def analyze(self, frame: np.ndarray, quality: FaceQuality) -> list[Face]:
        """Faces in an RGB frame that pass `quality`, with their embeddings."""
        ...


def normalise(vector: np.ndarray) -> np.ndarray:
    vector = np.asarray(vector, np.float32).reshape(-1)
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm > 0 else vector


class OpenCvFaceAnalyzer:
    """YuNet + SFace through OpenCV (`pip install cctv-edge[face]`). One instance is
    shared by all cameras and enrolment; OpenCV's objects aren't thread-safe, so calls
    are serialised."""

    def __init__(
        self,
        detector: tuple[ModelManifest, Path],
        embedder: tuple[ModelManifest, Path],
    ) -> None:
        try:
            import cv2
        except ImportError as exc:
            raise ModelError("OpenCV is not installed (pip install cctv-edge[face])") from exc
        det_manifest, det_path = detector
        emb_manifest, emb_path = embedder
        try:
            self._detector = cv2.FaceDetectorYN.create(
                str(det_path),
                "",
                (320, 320),
                score_threshold=det_manifest.score_threshold,
                nms_threshold=det_manifest.nms_iou,
                top_k=50,
            )
            self._recognizer = cv2.FaceRecognizerSF.create(str(emb_path), "")
        except cv2.error as exc:
            raise ModelError(f"cannot load the face models: {exc}") from exc
        self.model_version = emb_manifest.model_version
        self._lock = threading.Lock()

    def analyze(self, frame: np.ndarray, quality: FaceQuality) -> list[Face]:
        h, w = frame.shape[:2]
        bgr = np.ascontiguousarray(frame[..., ::-1])
        faces = []
        with self._lock:
            self._detector.setInputSize((w, h))
            _, rows = self._detector.detect(bgr)
            for row in rows if rows is not None else []:
                x, y, fw, fh = (float(v) for v in row[:4])
                score = float(row[14])
                size = int(min(fw, fh))
                if score < quality.min_score or size < quality.min_size_px:
                    continue
                box = Box(
                    max(x / w, 0.0), max(y / h, 0.0), min((x + fw) / w, 1.0), min((y + fh) / h, 1.0)
                )
                if sharpness(crop(frame, box)) < quality.min_sharpness:
                    continue
                aligned = self._recognizer.alignCrop(bgr, row)
                embedding = normalise(self._recognizer.feature(aligned))
                faces.append(Face(box, score, size, embedding))
        return faces


class EnrolmentError(Exception):
    """A message for the person enrolling (safe to show in the dashboard)."""


def _decode(data: bytes, number: int) -> np.ndarray:
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in ("JPEG", "PNG"):
                raise EnrolmentError(f"photo {number}: use a JPEG or PNG image")
            image.thumbnail((ENROL_MAX_SIDE, ENROL_MAX_SIDE))
            return np.asarray(image.convert("RGB"))
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise EnrolmentError(f"photo {number}: not a readable image") from exc


def enrolment_embedding(analyzer: FaceAnalyzer, photos: Sequence[bytes]) -> np.ndarray:
    """One template from 1-5 photos of the same person: the mean of their normalised
    embeddings, normalised again.

    Each photo must show one clear face. If it shows several, the largest is used only
    when it is clearly the largest (`ENROL_DOMINANT_FACE_RATIO`); otherwise the photo is
    refused rather than risk enrolling the wrong person.
    """
    embeddings = []
    for number, data in enumerate(photos, start=1):
        faces = analyzer.analyze(_decode(data, number), ENROL_QUALITY)
        if not faces:
            raise EnrolmentError(
                f"photo {number}: no clear face found (too small, blurred or not facing the camera)"
            )
        faces.sort(key=lambda f: f.size_px, reverse=True)
        if (
            len(faces) > 1
            and faces[0].size_px ** 2 < ENROL_DOMINANT_FACE_RATIO * faces[1].size_px ** 2
        ):
            raise EnrolmentError(f"photo {number}: more than one face; use a photo of one person")
        embeddings.append(normalise(faces[0].embedding))
    template = normalise(np.mean(embeddings, axis=0))
    if len(embeddings) > 1:
        for number, embedding in enumerate(embeddings, start=1):
            others = normalise(np.mean([e for e in embeddings if e is not embedding], axis=0))
            if float(embedding @ others) < ENROL_MIN_CONSISTENCY:
                raise EnrolmentError(
                    f"photo {number} does not look like the same person as the other photos"
                )
    return template


class FaceGallery:
    """Enrolled staff templates for this box's branch, in memory only (never on disk).
    Shared by all cameras; `replace` swaps the whole set atomically."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._staff: list[UUID] = []
        self._matrix = np.zeros((0, 0), np.float32)
        self.model_version: str | None = None

    def __len__(self) -> int:
        return len(self._staff)

    def replace(self, templates: Sequence[FaceTemplate], model_version: str) -> int:
        """Keep the templates made with `model_version`; returns how many."""
        usable = [t for t in templates if t.model_version == model_version]
        skipped = len(templates) - len(usable)
        if skipped:
            log.warning("%d face templates are from another model version; ignored", skipped)
        dims = {len(t.embedding) for t in usable}
        if len(dims) > 1:
            log.error("face templates have different lengths %s; ignored", sorted(dims))
            usable = []
        matrix = (
            np.stack([normalise(np.asarray(t.embedding)) for t in usable])
            if usable
            else np.zeros((0, 0), np.float32)
        )
        with self._lock:
            self._staff = [t.staff_id for t in usable]
            self._matrix = matrix
            self.model_version = model_version
        return len(usable)

    def clear(self) -> None:
        with self._lock:
            self._staff, self._matrix = [], np.zeros((0, 0), np.float32)

    def match(self, embedding: np.ndarray, threshold: float) -> tuple[UUID, float] | None:
        """The best-matching staff member and cosine similarity, or None."""
        with self._lock:
            staff, matrix = self._staff, self._matrix
        if not staff or matrix.shape[1] != embedding.shape[0]:
            return None
        similarities = matrix @ normalise(embedding)
        best = int(similarities.argmax())
        score = float(similarities[best])
        return (staff[best], score) if score >= threshold else None
