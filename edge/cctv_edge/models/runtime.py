"""ONNX Runtime sessions. onnxruntime is an optional dependency (`pip install
cctv-edge[models]`; `onnxruntime-gpu` from NVIDIA's Jetson builds on the box), so it is
imported only when a model is loaded."""

import logging
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from cctv_edge.models.manifest import ModelError

log = logging.getLogger(__name__)


class Session(Protocol):
    """The part of `onnxruntime.InferenceSession` we use (tests pass fakes)."""

    def get_inputs(self) -> Sequence[Any]: ...

    def run(self, output_names: Any, feeds: dict[str, np.ndarray]) -> list[np.ndarray]: ...


_cache: dict[tuple[Path, tuple[str, ...]], Session] = {}
_cache_lock = threading.Lock()


def open_session(path: Path, providers: Sequence[str]) -> Session:
    """One session per model file, shared by every camera (`run` is thread-safe), so
    applying a new config never reloads the weights."""
    key = (path.resolve(), tuple(providers))
    with _cache_lock:
        session = _cache.get(key)
        if session is None:
            session = _cache[key] = _create(path, providers)
        return session


def _create(path: Path, providers: Sequence[str]) -> Session:
    try:
        import onnxruntime
    except ImportError as exc:
        raise ModelError("onnxruntime is not installed (pip install cctv-edge[models])") from exc
    available = set(onnxruntime.get_available_providers())
    chosen = [p for p in providers if p in available] or ["CPUExecutionProvider"]
    try:
        session = onnxruntime.InferenceSession(str(path), providers=chosen)
    except Exception as exc:  # onnxruntime raises its own exception types
        raise ModelError(f"{path.parent.name}: cannot load {path.name}: {exc}") from exc
    log.info("loaded %s on %s", path.parent.name, session.get_providers())
    return session
