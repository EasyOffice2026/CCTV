"""The models installed on this box, loaded once and shared.

Applying a new config rebuilds every camera's pipeline, so the factories handed out here
only wrap already-loaded sessions. A model that fails to load is logged once and left
out; the box keeps running the features that don't need it.
"""

import logging
import threading
from collections.abc import Sequence
from pathlib import Path

from cctv_edge.detection import DetectorFactory
from cctv_edge.face import FaceAnalyzer, OpenCvFaceAnalyzer
from cctv_edge.models.classifier import ClassifierFactory, OnnxClassifier
from cctv_edge.models.detector import OnnxDetector
from cctv_edge.models.manifest import ModelError, ModelManifest, ModelTask, discover
from cctv_edge.models.runtime import open_session

log = logging.getLogger(__name__)


class ModelRegistry:
    def __init__(self, models_dir: Path, providers: Sequence[str] = ()) -> None:
        self.models_dir = models_dir
        self.providers = list(providers)
        self.found = discover(models_dir)
        self._lock = threading.Lock()
        self._failed: set[str] = set()
        self._face: FaceAnalyzer | None = None
        self._face_loaded = False

    def _first(self, task: ModelTask) -> tuple[ModelManifest, Path] | None:
        models = self.found.get(task, [])
        if len(models) > 1:
            log.warning("several %s models; using %s", task.value, models[0][0].model_version)
        return models[0] if models else None

    def _session(self, manifest: ModelManifest, path: Path):
        if manifest.model_version in self._failed:
            return None
        try:
            return open_session(path, self.providers)
        except ModelError as exc:
            log.error("model %s not used: %s", manifest.model_version, exc)
            self._failed.add(manifest.model_version)
            return None

    def detector_factories(self) -> dict[str, DetectorFactory]:
        """Label -> factory. One model reporting several labels runs once per frame."""
        factories: dict[str, DetectorFactory] = {}
        for manifest, path in self.found.get(ModelTask.DETECTOR, []):
            session = self._session(manifest, path)
            if session is None:
                continue
            try:
                labels = OnnxDetector(manifest, session).labels
            except ModelError as exc:
                log.error("model %s not used: %s", manifest.model_version, exc)
                continue

            def factory(m: ModelManifest = manifest, s=session) -> OnnxDetector:
                return OnnxDetector(m, s)

            for label in sorted(labels):
                if label in factories:
                    log.warning("%s: label %r already has a detector", manifest.name, label)
                    continue
                factories[label] = factory
        return factories

    def classifier_factories(self) -> dict[ModelTask, ClassifierFactory]:
        factories: dict[ModelTask, ClassifierFactory] = {}
        for task in (ModelTask.DRAWER_CLASSIFIER, ModelTask.PPE_CLASSIFIER):
            chosen = self._first(task)
            if chosen is None:
                continue
            manifest, path = chosen
            session = self._session(manifest, path)
            if session is None:
                continue
            try:
                OnnxClassifier(manifest, session)
            except ModelError as exc:
                log.error("model %s not used: %s", manifest.model_version, exc)
                continue
            factories[task] = lambda m=manifest, s=session: OnnxClassifier(m, s)
        return factories

    def face_analyzer(self) -> FaceAnalyzer | None:
        """The shared face analyzer, or None without both face models."""
        with self._lock:
            if not self._face_loaded:
                self._face_loaded = True
                detector = self._first(ModelTask.FACE_DETECTOR)
                embedder = self._first(ModelTask.FACE_EMBEDDER)
                if detector and embedder:
                    try:
                        self._face = OpenCvFaceAnalyzer(detector, embedder)
                    except ModelError as exc:
                        log.error("face models not used: %s", exc)
                elif detector or embedder:
                    log.error("face recognition needs both a face_detector and a face_embedder")
            return self._face
