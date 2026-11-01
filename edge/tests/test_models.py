"""Model runtime: manifests, decoding, NMS and the registry (fake sessions), plus one
real ONNX Runtime run when onnxruntime is installed."""

import hashlib
import json
import math
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from cctv_edge.detection import PERSON
from cctv_edge.imaging import Placement, fit, sharpness, to_tensor
from cctv_edge.models import registry as registry_module
from cctv_edge.models.classifier import OnnxClassifier
from cctv_edge.models.detector import OnnxDetector, decode_rtdetr, decode_yolox, nms
from cctv_edge.models.manifest import (
    ModelError,
    ModelManifest,
    ModelTask,
    discover,
    load_manifest,
)
from cctv_edge.models.registry import ModelRegistry


class FakeSession:
    def __init__(self, outputs, inputs=("images",)) -> None:
        self.outputs = outputs
        self.input_names = inputs
        self.feeds: list[dict] = []

    def get_inputs(self):
        return [SimpleNamespace(name=n) for n in self.input_names]

    def run(self, _names, feeds):
        self.feeds.append(feeds)
        return self.outputs


def write_model(folder: Path, content: bytes = b"weights", **fields) -> Path:
    folder.mkdir(parents=True)
    (folder / "model.onnx").write_bytes(content)
    manifest = {
        "name": folder.name,
        "version": "1",
        "task": "detector",
        "sha256": hashlib.sha256(content).hexdigest(),
        "licence": "Apache-2.0",
        "input": {"width": 64, "height": 64},
        "output_format": "yolox",
        "classes": ["person", "cup"],
        "label_map": {"person": PERSON},
        **fields,
    }
    (folder / "model.json").write_text(json.dumps(manifest))
    return folder


def manifest(**fields) -> ModelManifest:
    base = {
        "name": "m",
        "version": "1",
        "task": "detector",
        "sha256": "0" * 64,
        "licence": "Apache-2.0",
        "input": {"width": 64, "height": 64},
        "output_format": "yolox",
        "classes": ["person", "cup"],
        "label_map": {"person": PERSON},
    }
    return ModelManifest.model_validate({**base, **fields})


# --- manifests --------------------------------------------------------------------------


def test_manifest_needs_a_licence_and_a_plain_file_name(tmp_path):
    with pytest.raises(ValueError, match="licence"):
        manifest(licence="")
    with pytest.raises(ValueError, match="file name"):
        manifest(file="../other.onnx")
    assert manifest().model_version == "m-1"


def test_load_checks_the_hash(tmp_path):
    folder = write_model(tmp_path / "person")
    m, path = load_manifest(folder)
    assert m.task is ModelTask.DETECTOR and path.name == "model.onnx"
    (folder / "model.onnx").write_bytes(b"tampered")
    with pytest.raises(ModelError, match="sha256"):
        load_manifest(folder)


def test_discover_skips_broken_models(tmp_path):
    write_model(tmp_path / "a-good")
    write_model(tmp_path / "b-no-licence", licence="")
    bad_hash = write_model(tmp_path / "c-bad-hash")
    (bad_hash / "model.onnx").write_bytes(b"other")
    (tmp_path / "not-a-model").mkdir()
    found = discover(tmp_path)
    assert [m.name for m, _ in found[ModelTask.DETECTOR]] == ["a-good"]
    assert discover(tmp_path / "missing") == {}


# --- image helpers ----------------------------------------------------------------------


def test_letterbox_keeps_aspect_ratio():
    frame = np.full((64, 128, 3), 200, np.uint8)
    image, placement = fit(frame, 64, 64, letterbox=True, pad_value=114)
    assert image.shape == (64, 64, 3)
    assert placement == Placement(0.5, 0.5)
    assert image[:32].min() == 200 and image[32:].max() == 114  # padded below
    stretched, placement = fit(frame, 64, 64, letterbox=False)
    assert placement == Placement(0.5, 1.0) and stretched.min() == 200


def test_tensor_layout_and_normalisation():
    image = np.zeros((2, 3, 3), np.uint8)
    image[..., 0] = 255  # red
    tensor = to_tensor(image, 1 / 255, (0.5, 0.0, 0.0), (0.5, 1.0, 1.0), bgr=False)
    assert tensor.shape == (1, 3, 2, 3) and tensor.dtype == np.float32
    assert np.allclose(tensor[0, 0], 1.0) and np.allclose(tensor[0, 1:], 0.0)
    bgr = to_tensor(image, 1.0, (0, 0, 0), (1, 1, 1), bgr=True)
    assert np.allclose(bgr[0, 2], 255.0)


def test_sharpness():
    flat = np.full((50, 50, 3), 128, np.uint8)
    checker = np.indices((50, 50)).sum(axis=0) % 2 * 255
    sharp = np.repeat(checker[..., None], 3, axis=2).astype(np.uint8)
    assert sharpness(flat) == 0.0
    assert sharpness(sharp) > 1000


# --- decoding ---------------------------------------------------------------------------


def _yolox_raw() -> np.ndarray:
    # 64x64 input, strides 8/16/32 -> 64 + 16 + 4 rows; 2 classes.
    out = np.zeros((1, 84, 7), np.float32)
    out[0, :, 2:4] = -5
    row = 1 * 8 + 2  # stride 8, cell x=2, y=1
    out[0, row] = [0.5, 0.5, math.log(2), math.log(2), 0.9, 0.8, 0.1]
    return out


def test_decode_yolox_raw_grid():
    boxes, scores, classes = decode_yolox(_yolox_raw(), 64, 64, threshold=0.5)
    assert classes.tolist() == [0]
    assert scores[0] == pytest.approx(0.72)
    assert boxes[0].tolist() == pytest.approx([12, 4, 28, 20])  # cx 20, cy 12, 16 x 16


def test_decode_yolox_checks_the_grid_size():
    with pytest.raises(ModelError, match="rows"):
        decode_yolox(np.zeros((1, 10, 7), np.float32), 64, 64, 0.5)


def test_decode_yolox_already_decoded():
    out = np.array([[[20, 12, 16, 16, 0.9, 0.1, 0.8]]], np.float32)
    boxes, _, classes = decode_yolox(out, 64, 64, 0.5, decoded=True)
    assert classes.tolist() == [1] and boxes[0].tolist() == pytest.approx([12, 4, 28, 20])


def test_decode_rtdetr_postprocessed_outputs():
    labels = np.array([[0, 1]])
    boxes = np.array([[[10, 10, 30, 40], [0, 0, 5, 5]]], np.float32)
    scores = np.array([[0.9, 0.2]], np.float32)
    out_boxes, out_scores, out_classes = decode_rtdetr([labels, boxes, scores], 64, 64, 0.4)
    assert out_classes.tolist() == [0] and out_scores.tolist() == pytest.approx([0.9])
    assert out_boxes[0].tolist() == [10, 10, 30, 40]


def test_decode_rtdetr_boxes_and_logits():
    boxes = np.array([[[0.5, 0.5, 0.2, 0.4]]], np.float32)
    logits = np.array([[[3.0, -3.0]]], np.float32)
    out_boxes, scores, classes = decode_rtdetr([boxes, logits], 64, 64, 0.4)
    assert classes.tolist() == [0] and scores[0] == pytest.approx(0.9526, abs=1e-3)
    assert out_boxes[0].tolist() == pytest.approx([25.6, 19.2, 38.4, 44.8])


def test_nms_is_per_class():
    boxes = np.array([[0, 0, 10, 10], [1, 1, 11, 11], [0, 0, 10, 10], [50, 50, 60, 60]], float)
    scores = np.array([0.6, 0.9, 0.8, 0.5])
    classes = np.array([0, 0, 1, 0])
    assert sorted(nms(boxes, scores, classes, 0.5).tolist()) == [1, 2, 3]
    assert nms(np.zeros((0, 4)), np.zeros(0), np.zeros(0, int), 0.5).tolist() == []


# --- detector and classifier ------------------------------------------------------------


def test_detector_maps_boxes_back_to_the_frame():
    session = FakeSession([_yolox_raw()])
    detector = OnnxDetector(manifest(), session)
    assert detector.labels == {PERSON}
    frame = np.zeros((64, 128, 3), np.uint8)  # letterboxed into the top half
    [d] = detector.detect(frame)
    assert d.label == PERSON and d.confidence == pytest.approx(0.72)
    assert (d.box.x1, d.box.y1, d.box.x2, d.box.y2) == pytest.approx((0.1875, 0.125, 0.4375, 0.625))
    assert session.feeds[0]["images"].shape == (1, 3, 64, 64)


def test_detector_drops_unmapped_classes_and_feeds_the_size_input():
    out = [np.array([[1]]), np.array([[[10, 10, 30, 40]]], np.float32), np.array([[0.9]])]
    session = FakeSession(out, inputs=("images", "orig_target_sizes"))
    detector = OnnxDetector(
        manifest(output_format="rtdetr", input={"width": 64, "height": 64, "letterbox": False}),
        session,
    )
    assert detector.detect(np.zeros((64, 64, 3), np.uint8)) == []  # class 1 is "cup"
    assert session.feeds[0]["orig_target_sizes"].tolist() == [[64, 64]]


def test_classifier_softmax_and_sigmoid():
    drawer = OnnxClassifier(
        manifest(task="drawer_classifier", output_format="softmax", classes=["closed", "open"]),
        FakeSession([np.array([[0.0, 2.0]])]),
    )
    result = drawer.classify(np.zeros((20, 30, 3), np.uint8))
    assert result["open"] == pytest.approx(0.8808, abs=1e-3)
    assert sum(result.values()) == pytest.approx(1.0)
    ppe = OnnxClassifier(
        manifest(task="ppe_classifier", output_format="sigmoid", classes=["mask", "gloves"]),
        FakeSession([np.array([[4.0, -4.0]])]),
    )
    result = ppe.classify(np.zeros((40, 20, 3), np.uint8))
    assert result["mask"] > 0.98 and result["gloves"] < 0.02
    assert ppe.classify(np.zeros((0, 20, 3), np.uint8)) == {}


def test_classifier_output_must_match_classes():
    bad = OnnxClassifier(
        manifest(task="ppe_classifier", output_format="sigmoid", classes=["mask"]),
        FakeSession([np.array([[1.0, 2.0]])]),
    )
    with pytest.raises(ModelError, match="outputs"):
        bad.classify(np.zeros((10, 10, 3), np.uint8))


# --- registry ---------------------------------------------------------------------------


def test_registry_shares_one_session(tmp_path, monkeypatch):
    write_model(tmp_path / "person")
    write_model(
        tmp_path / "drawer",
        task="drawer_classifier",
        output_format="softmax",
        classes=["closed", "open"],
        label_map={},
    )
    opened = []

    def fake_open(path, providers):
        opened.append(path)
        return FakeSession([_yolox_raw()])

    monkeypatch.setattr(registry_module, "open_session", fake_open)
    registry = ModelRegistry(tmp_path)
    factories = registry.detector_factories()
    assert set(factories) == {PERSON}
    assert factories[PERSON]().session is factories[PERSON]().session
    classifiers = registry.classifier_factories()
    assert set(classifiers) == {ModelTask.DRAWER_CLASSIFIER}
    assert registry.face_analyzer() is None  # no face models installed


def test_registry_skips_models_that_fail_to_load(tmp_path, monkeypatch):
    write_model(tmp_path / "person")

    def broken(path, providers):
        raise ModelError("onnxruntime is not installed")

    monkeypatch.setattr(registry_module, "open_session", broken)
    registry = ModelRegistry(tmp_path)
    assert registry.detector_factories() == {}
    assert registry.detector_factories() == {}  # not retried


def test_face_models_need_both_parts(tmp_path, caplog):
    write_model(tmp_path / "yunet", task="face_detector", runtime="opencv", input=None)
    assert ModelRegistry(tmp_path).face_analyzer() is None
    assert "needs both" in caplog.text


# --- a real ONNX model ------------------------------------------------------------------


def test_real_onnx_detector(tmp_path):
    try:
        import onnx
        import onnxruntime  # noqa: F401
        from onnx import TensorProto, helper
    except ImportError as exc:  # also a broken install (missing DLL on Windows)
        pytest.skip(f"onnx/onnxruntime not usable: {exc}")

    # A "model" that ignores the image and returns one decoded YOLOX row.
    row = np.array([[[20, 12, 16, 16, 0.9, 0.8, 0.1]]], np.float32)
    graph = helper.make_graph(
        [
            helper.make_node("Shape", ["images"], ["shape"]),
            helper.make_node(
                "Constant",
                [],
                ["output"],
                value=helper.make_tensor("row", TensorProto.FLOAT, row.shape, row.ravel()),
            ),
        ],
        "fake-yolox",
        [helper.make_tensor_value_info("images", TensorProto.FLOAT, [1, 3, 64, 64])],
        [helper.make_tensor_value_info("output", TensorProto.FLOAT, list(row.shape))],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    model.ir_version = 8
    content = model.SerializeToString()
    onnx.checker.check_model(model)
    write_model(tmp_path / "person", content=content, options={"decoded": True})

    registry = ModelRegistry(tmp_path, ["CPUExecutionProvider"])
    detector = registry.detector_factories()[PERSON]()
    [d] = detector.detect(np.zeros((64, 64, 3), np.uint8))
    assert d.label == PERSON and d.confidence == pytest.approx(0.72)


# --- make_manifest (docs/ai-models.md) ---------------------------------------------------


def test_manifests_for_the_recommended_detectors(tmp_path):
    from cctv_edge.models.make_manifest import main
    from cctv_edge.models.manifest import load_manifest

    expected = {"yolox": (0, "bgr", True), "dfine": (1, "rgb", False), "rfdetr": (1, "rgb", False)}
    for preset, (person, channels, letterbox) in expected.items():
        folder = tmp_path / f"person-{preset}"
        folder.mkdir()
        (folder / "model.onnx").write_bytes(b"onnx bytes " + preset.encode())
        assert (
            main([str(folder), "--preset", preset, "--name", folder.name, "--version", "2026-11"])
            == 0
        )
        manifest, _ = load_manifest(folder)
        assert manifest.licence == "Apache-2.0"
        assert manifest.classes[person] == "person" and manifest.label_map["person"] == "person"
        assert (manifest.input.channel_order, manifest.input.letterbox) == (channels, letterbox)
    # D-FINE and RF-DETR number classes by COCO category id: 77 is the phone, 12 unused.
    dfine, _ = load_manifest(tmp_path / "person-dfine")
    assert (
        len(dfine.classes) == 91
        and dfine.classes[77] == "cell phone"
        and dfine.classes[12] == "N/A"
    )
    rfdetr, _ = load_manifest(tmp_path / "person-rfdetr")
    assert rfdetr.input.mean == (0.485, 0.456, 0.406) and rfdetr.input.width == 512


def test_checkpoints_not_cleared_for_commercial_use_are_refused(tmp_path, capsys):
    from cctv_edge.models.make_manifest import main

    folder = tmp_path / "dfine-s-obj2coco"
    folder.mkdir()
    (folder / "model.onnx").write_bytes(b"x")
    assert main([str(folder), "--preset", "dfine", "--name", "person-dfine", "--version", "1"]) == 1
    assert "not cleared for commercial use" in capsys.readouterr().out
    assert not (folder / "model.json").exists()


def test_dfine_manifest_maps_coco_category_ids_to_labels(tmp_path):
    """The helper's D-FINE manifest, with D-FINE's outputs (labels are COCO category ids)."""
    from cctv_edge.models.make_manifest import main
    from cctv_edge.models.manifest import load_manifest

    folder = tmp_path / "person-dfine-s"
    folder.mkdir()
    (folder / "model.onnx").write_bytes(b"weights")
    main(
        [str(folder), "--preset", "dfine", "--name", folder.name, "--version", "1", "--size", "64"]
    )
    loaded, _ = load_manifest(folder)
    labels = np.array([[1, 77, 62]])  # person, cell phone, chair
    boxes = np.array([[[10, 10, 30, 40], [40, 40, 50, 50], [0, 0, 20, 20]]], np.float32)
    scores = np.array([[0.9, 0.8, 0.95]], np.float32)
    detector = OnnxDetector(
        loaded, FakeSession([labels, boxes, scores], ("images", "orig_target_sizes"))
    )
    found = sorted(d.label for d in detector.detect(np.zeros((64, 64, 3), np.uint8)))
    assert found == ["person", "phone"]  # the chair isn't used by any rule
