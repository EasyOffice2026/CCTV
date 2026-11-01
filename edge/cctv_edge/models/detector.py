"""Object detection with an ONNX model: preprocess, run, decode, non-maximum suppression.

Two output layouts are supported, both from Apache-2.0 model families:

- `yolox`: one (1, N, 5 + classes) output of box (cx, cy, w, h), objectness and class
  scores. Raw exports need the grid decode (`options.decoded: false`, the default);
  exports made with the decode inside the graph set `decoded: true`.
- `rtdetr`: DETR-style. Either three outputs (labels, boxes xyxy in input pixels,
  scores), from exports that take `orig_target_sizes` as a second input (RT-DETR,
  D-FINE), or two outputs (boxes cx/cy/w/h normalised, class logits).

Decoding is plain numpy, so it is tested without onnxruntime.
"""

from collections.abc import Sequence

import numpy as np

from cctv_edge.detection import Detection
from cctv_edge.geometry import Box
from cctv_edge.imaging import fit, to_tensor
from cctv_edge.models.manifest import ModelError, ModelManifest, ModelTask, OutputFormat
from cctv_edge.models.runtime import Session

# Decoded detections: boxes (N, 4) xyxy in model-input pixels, scores (N,), class ids (N,).
Decoded = tuple[np.ndarray, np.ndarray, np.ndarray]

_EMPTY: Decoded = (np.zeros((0, 4), np.float32), np.zeros(0, np.float32), np.zeros(0, np.int64))


def _cxcywh_to_xyxy(boxes: np.ndarray) -> np.ndarray:
    cx, cy, w, h = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    return np.stack([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2], axis=1)


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-x))


def decode_yolox(
    output: np.ndarray,
    width: int,
    height: int,
    threshold: float,
    strides: Sequence[int] = (8, 16, 32),
    decoded: bool = False,
) -> Decoded:
    rows = np.asarray(output, np.float32).reshape(-1, output.shape[-1]).copy()
    if not decoded:
        grids, scales = [], []
        for stride in strides:
            xs, ys = np.meshgrid(np.arange(width // stride), np.arange(height // stride))
            grids.append(np.stack((xs, ys), axis=2).reshape(-1, 2))
            scales.append(np.full((grids[-1].shape[0], 1), stride))
        grid, scale = np.concatenate(grids), np.concatenate(scales)
        if grid.shape[0] != rows.shape[0]:
            raise ModelError(
                f"YOLOX output has {rows.shape[0]} rows; strides {list(strides)} at"
                f" {width}x{height} give {grid.shape[0]}"
            )
        rows[:, :2] = (rows[:, :2] + grid) * scale
        rows[:, 2:4] = np.exp(rows[:, 2:4]) * scale
    class_scores = rows[:, 5:] * rows[:, 4:5]
    classes = class_scores.argmax(axis=1)
    scores = class_scores[np.arange(len(rows)), classes]
    keep = scores >= threshold
    return _cxcywh_to_xyxy(rows[keep, :4]), scores[keep], classes[keep]


def decode_rtdetr(
    outputs: Sequence[np.ndarray], width: int, height: int, threshold: float
) -> Decoded:
    if len(outputs) >= 3:  # labels, boxes (xyxy, input pixels), scores
        labels, boxes, scores = (np.asarray(o) for o in outputs[:3])
        labels = labels.reshape(-1).astype(np.int64)
        boxes = boxes.reshape(-1, 4).astype(np.float32)
        scores = scores.reshape(-1).astype(np.float32)
    elif len(outputs) == 2:  # boxes (cx, cy, w, h, normalised), class logits
        raw_boxes, logits = (np.asarray(o, np.float32) for o in outputs)
        # Exports differ in the order (Hugging Face's gives the logits first): the boxes
        # are the output with 4 numbers each.
        if raw_boxes.shape[-1] != 4 and logits.shape[-1] == 4:
            raw_boxes, logits = logits, raw_boxes
        probabilities = _sigmoid(logits.reshape(-1, logits.shape[-1]))
        labels = probabilities.argmax(axis=1)
        scores = probabilities[np.arange(len(labels)), labels]
        boxes = _cxcywh_to_xyxy(raw_boxes.reshape(-1, 4)) * np.array(
            [width, height, width, height], np.float32
        )
    else:
        raise ModelError("rtdetr models need 2 or 3 outputs")
    keep = scores >= threshold
    return boxes[keep], scores[keep], labels[keep]


def nms(boxes: np.ndarray, scores: np.ndarray, classes: np.ndarray, iou: float) -> np.ndarray:
    """Indices kept by per-class non-maximum suppression, highest score first."""
    if len(boxes) == 0:
        return np.zeros(0, np.int64)
    # Shift each class far apart so boxes of different classes never overlap.
    offset = classes.astype(np.float32)[:, None] * (boxes.max() + 1)
    shifted = boxes + offset
    x1, y1, x2, y2 = shifted.T
    areas = np.maximum(x2 - x1, 0) * np.maximum(y2 - y1, 0)
    order = scores.argsort()[::-1]
    keep = []
    while order.size:
        i = order[0]
        keep.append(i)
        rest = order[1:]
        w = np.maximum(0, np.minimum(x2[i], x2[rest]) - np.maximum(x1[i], x1[rest]))
        h = np.maximum(0, np.minimum(y2[i], y2[rest]) - np.maximum(y1[i], y1[rest]))
        inter = w * h
        overlap = inter / np.maximum(areas[i] + areas[rest] - inter, 1e-9)
        order = rest[overlap <= iou]
    return np.asarray(keep, np.int64)


class OnnxDetector:
    """A detector model. The session is shared by every camera; this object is cheap."""

    def __init__(self, manifest: ModelManifest, session: Session) -> None:
        if manifest.task is not ModelTask.DETECTOR or manifest.input is None:
            raise ModelError(f"{manifest.name}: not a detector with an input spec")
        if manifest.output_format not in (OutputFormat.YOLOX, OutputFormat.RTDETR):
            raise ModelError(f"{manifest.name}: unsupported output_format for a detector")
        self.manifest = manifest
        self.session = session
        self.label_map = manifest.label_map or {c: c for c in manifest.classes}
        self.labels = frozenset(self.label_map.values())
        inputs = session.get_inputs()
        self._input_name = inputs[0].name
        # RT-DETR / D-FINE exports take the image size as a second input.
        self._size_input = inputs[1].name if len(inputs) > 1 else None

    def detect(self, frame: np.ndarray) -> list[Detection]:
        spec = self.manifest.input
        assert spec is not None  # noqa: S101
        image, placement = fit(frame, spec.width, spec.height, spec.letterbox, spec.pad_value)
        feeds = {
            self._input_name: to_tensor(
                image, spec.scale, spec.mean, spec.std, spec.channel_order == "bgr"
            )
        }
        if self._size_input:
            feeds[self._size_input] = np.array([[spec.width, spec.height]], np.int64)
        outputs = self.session.run(None, feeds)
        return self.postprocess(outputs, frame.shape[1], frame.shape[0], placement)

    def postprocess(self, outputs, frame_width, frame_height, placement) -> list[Detection]:
        spec, m = self.manifest.input, self.manifest
        assert spec is not None  # noqa: S101
        if m.output_format is OutputFormat.YOLOX:
            boxes, scores, classes = decode_yolox(
                outputs[0],
                spec.width,
                spec.height,
                m.score_threshold,
                strides=tuple(m.options.get("strides", (8, 16, 32))),  # type: ignore[arg-type]
                decoded=bool(m.options.get("decoded", False)),
            )
        else:
            boxes, scores, classes = decode_rtdetr(
                outputs, spec.width, spec.height, m.score_threshold
            )
        # Drop classes we don't use before NMS.
        names = [m.classes[c] if 0 <= c < len(m.classes) else None for c in classes]
        wanted = np.array([n in self.label_map for n in names], bool)
        boxes, scores, classes = boxes[wanted], scores[wanted], classes[wanted]
        names = [n for n, w in zip(names, wanted, strict=False) if w]
        keep = nms(boxes, scores, classes, m.nms_iou)
        normalised = placement.to_normalised(boxes[keep], frame_width, frame_height)
        detections = []
        for row, i in zip(normalised, keep, strict=False):
            box = Box(*(float(v) for v in row))
            if box.area > 0:
                detections.append(Detection(self.label_map[names[i]], box, float(scores[i])))
        return detections
