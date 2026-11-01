"""Writes the model.json of a person detector exported to ONNX (docs/ai-models.md).

    python -m cctv_edge.models.make_manifest models/person-dfine-s --preset dfine \\
        --name person-dfine-s --version 2026-11

The folder must hold `model.onnx`. Presets know each family's input (size, normalisation,
resizing), output format and class numbering, which differ in ways that are easy to get
wrong by hand: YOLOX counts COCO's 80 classes 0..79, while D-FINE and RF-DETR use COCO's
original category ids (1 = person, with gaps up to 90). The result is checked by loading it
the way the box does.
"""

import argparse
import json
from pathlib import Path

from cctv_edge.models.manifest import MANIFEST_NAME, ModelError, load_manifest, sha256_file

# COCO's 80 classes in the usual order (YOLOX counts them 0..79), and their original
# category ids (D-FINE and RF-DETR use these: 1 = person, ..., 90 = toothbrush).
COCO_80_NAMES = [
    "person",
    "bicycle",
    "car",
    "motorcycle",
    "airplane",
    "bus",
    "train",
    "truck",
    "boat",
    "traffic light",
    "fire hydrant",
    "stop sign",
    "parking meter",
    "bench",
    "bird",
    "cat",
    "dog",
    "horse",
    "sheep",
    "cow",
    "elephant",
    "bear",
    "zebra",
    "giraffe",
    "backpack",
    "umbrella",
    "handbag",
    "tie",
    "suitcase",
    "frisbee",
    "skis",
    "snowboard",
    "sports ball",
    "kite",
    "baseball bat",
    "baseball glove",
    "skateboard",
    "surfboard",
    "tennis racket",
    "bottle",
    "wine glass",
    "cup",
    "fork",
    "knife",
    "spoon",
    "bowl",
    "banana",
    "apple",
    "sandwich",
    "orange",
    "broccoli",
    "carrot",
    "hot dog",
    "pizza",
    "donut",
    "cake",
    "chair",
    "couch",
    "potted plant",
    "bed",
    "dining table",
    "toilet",
    "tv",
    "laptop",
    "mouse",
    "remote",
    "keyboard",
    "cell phone",
    "microwave",
    "oven",
    "toaster",
    "sink",
    "refrigerator",
    "book",
    "clock",
    "vase",
    "scissors",
    "teddy bear",
    "hair drier",
    "toothbrush",
]
COCO_CATEGORY_IDS = [
    *range(1, 12),
    *range(13, 26),
    27,
    28,
    *range(31, 45),
    *range(46, 66),
    67,
    70,
    *range(72, 83),
    *range(84, 91),
]


def coco_by_category_id() -> list[str]:
    """Index = COCO category id (0..90); ids COCO doesn't use are "N/A"."""
    names = ["N/A"] * 91
    for name, category in zip(COCO_80_NAMES, COCO_CATEGORY_IDS, strict=True):
        names[category] = name
    return names


# Edge labels the rules use; other classes are dropped.
LABELS = {"person": "person", "cell phone": "phone"}
IMAGENET = {"mean": [0.485, 0.456, 0.406], "std": [0.229, 0.224, 0.225]}

PRESETS: dict[str, dict] = {
    # github.com/Megvii-BaseDetection/YOLOX: BGR 0..255, letterboxed, raw head output.
    "yolox": {
        "licence": "Apache-2.0",
        "source": "https://github.com/Megvii-BaseDetection/YOLOX (COCO)",
        "size": 640,
        "input": {"scale": 1.0, "channel_order": "bgr", "letterbox": True},
        "output_format": "yolox",
        "classes": "coco80",
        "options": {"strides": [8, 16, 32], "decoded": False},
    },
    # github.com/Peterande/D-FINE, tools/deployment/export_onnx.py: RGB 0..1, stretched;
    # labels/boxes/scores with COCO category ids. COCO-only checkpoints (the Objects365
    # ones aren't cleared for commercial use).
    "dfine": {
        "licence": "Apache-2.0",
        "source": "https://github.com/Peterande/D-FINE (COCO-only checkpoint)",
        "size": 640,
        "input": {"scale": 1 / 255, "channel_order": "rgb", "letterbox": False},
        "output_format": "rtdetr",
        "classes": "coco_ids",
        "options": {},
    },
    # The same model as exported on Hugging Face (huggingface.co/onnx-community/dfine_s_coco-ONNX,
    # from ustc-community/dfine-small-coco): RGB 0..1, stretched; logits + boxes over COCO's
    # 80 classes counted 0..79.
    "dfine-hf": {
        "licence": "Apache-2.0",
        "source": "https://huggingface.co/onnx-community/dfine_s_coco-ONNX"
        " (ustc-community/dfine-small-coco, COCO)",
        "size": 640,
        "input": {"scale": 1 / 255, "channel_order": "rgb", "letterbox": False},
        "output_format": "rtdetr",
        "classes": "coco80",
        "options": {},
    },
    # github.com/roboflow/rf-detr (Nano to Large are Apache-2.0; the Plus models aren't):
    # RGB, ImageNet normalisation, stretched; boxes + logits over COCO category ids.
    "rfdetr": {
        "licence": "Apache-2.0",
        "source": "https://github.com/roboflow/rf-detr (Nano/Small/Medium/Large)",
        "size": 512,
        "input": {"scale": 1 / 255, "channel_order": "rgb", "letterbox": False, **IMAGENET},
        "output_format": "rtdetr",
        "classes": "coco_ids",
        "options": {},
    },
}

# Checkpoints known not to be cleared for commercial use (docs/ai-models.md).
NOT_COMMERCIAL = (
    "obj365",  # D-FINE checkpoints pretrained on Objects365
    "obj2coco",
    "rf-detr-xl",  # RF-DETR Plus (PML 1.0)
    "rfdetr_plus",
    "yolo-nas",  # Deci licence: non-commercial weights
    "ultralytics",  # AGPL-3.0 unless an Enterprise licence is bought
    "yolov5",
    "yolov8",
    "yolo11",
    "yolov10",
    "yolov12",
    "yolov7",  # GPL-3.0
    "yolov9",
)


def build(
    folder: Path,
    preset: str,
    name: str,
    version: str,
    size: int | None = None,
    source: str | None = None,
    score_threshold: float = 0.45,
) -> dict:
    spec = PRESETS[preset]
    weights = folder / "model.onnx"
    if not weights.is_file():
        raise ModelError(f"{weights} is missing (export the model to ONNX first)")
    lowered = f"{folder.name} {name} {source or ''}".lower()
    flagged = [word for word in NOT_COMMERCIAL if word in lowered]
    if flagged:
        raise ModelError(f"{flagged[0]!r}: not cleared for commercial use (docs/ai-models.md)")
    side = size or spec["size"]
    classes = COCO_80_NAMES if spec["classes"] == "coco80" else coco_by_category_id()
    return {
        "name": name,
        "version": version,
        "task": "detector",
        "file": "model.onnx",
        "sha256": sha256_file(weights),
        "licence": spec["licence"],
        "source": source or spec["source"],
        "input": {"width": side, "height": side, **spec["input"]},
        "output_format": spec["output_format"],
        "classes": classes,
        "label_map": LABELS,
        "score_threshold": score_threshold,
        "nms_iou": 0.5,
        "options": spec["options"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("folder", type=Path, help="the model's folder, holding model.onnx")
    parser.add_argument("--preset", required=True, choices=sorted(PRESETS))
    parser.add_argument("--name", required=True, help="e.g. person-dfine-s")
    parser.add_argument("--version", required=True, help="e.g. 2026-11")
    parser.add_argument("--size", type=int, help="input size the model was exported with")
    parser.add_argument("--source", help="where the weights came from (default: the project)")
    parser.add_argument("--score-threshold", type=float, default=0.45)
    args = parser.parse_args(argv)
    try:
        manifest = build(
            args.folder,
            args.preset,
            args.name,
            args.version,
            args.size,
            args.source,
            args.score_threshold,
        )
        (args.folder / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        loaded, _ = load_manifest(args.folder)  # exactly as the box will
    except ModelError as exc:
        print(f"error: {exc}")
        return 1
    person = manifest["classes"].index("person")
    print(
        f"wrote {args.folder / MANIFEST_NAME}: {loaded.model_version}, {loaded.licence}, "
        f"{loaded.input.width}x{loaded.input.height}, person = class {person}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
