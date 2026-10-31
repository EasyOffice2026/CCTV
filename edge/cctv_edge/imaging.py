"""Small image helpers shared by the models: crops, resizing, model input tensors and a
sharpness measure. Frames are H x W x 3 RGB uint8, boxes normalised to the frame."""

from dataclasses import dataclass

import numpy as np
from PIL import Image

from cctv_edge.geometry import Box


def pixel_box(box: Box, width: int, height: int) -> tuple[int, int, int, int]:
    """The box in whole pixels, clipped to the frame: (x1, y1, x2, y2), x2/y2 exclusive."""
    x1 = min(max(int(box.x1 * width), 0), width)
    y1 = min(max(int(box.y1 * height), 0), height)
    x2 = min(max(round(box.x2 * width), x1), width)
    y2 = min(max(round(box.y2 * height), y1), height)
    return x1, y1, x2, y2


def crop(frame: np.ndarray, box: Box) -> np.ndarray:
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = pixel_box(box, w, h)
    return frame[y1:y2, x1:x2]


def resize(image: np.ndarray, width: int, height: int) -> np.ndarray:
    if image.shape[1] == width and image.shape[0] == height:
        return image
    return np.asarray(Image.fromarray(image).resize((width, height), Image.BILINEAR))


def sharpness(image: np.ndarray) -> float:
    """Variance of the Laplacian of the grey image: low for blurred or flat crops."""
    if image.shape[0] < 3 or image.shape[1] < 3:
        return 0.0
    grey = image.mean(axis=2, dtype=np.float32) if image.ndim == 3 else image.astype(np.float32)
    laplacian = (
        grey[:-2, 1:-1] + grey[2:, 1:-1] + grey[1:-1, :-2] + grey[1:-1, 2:] - 4 * grey[1:-1, 1:-1]
    )
    return float(laplacian.var())


@dataclass(frozen=True)
class Placement:
    """Where the original image sits in the model input, to map boxes back."""

    scale_x: float
    scale_y: float

    def to_normalised(self, xyxy: np.ndarray, width: int, height: int) -> np.ndarray:
        """Model-input pixel boxes (N, 4) -> boxes normalised to the original image."""
        out = xyxy.astype(np.float32).copy()
        out[:, [0, 2]] /= self.scale_x * width
        out[:, [1, 3]] /= self.scale_y * height
        return np.clip(out, 0.0, 1.0)


def fit(
    image: np.ndarray, width: int, height: int, letterbox: bool, pad_value: int = 114
) -> tuple[np.ndarray, Placement]:
    """Resize to the model input. With `letterbox` the aspect ratio is kept and the image
    sits in the top-left corner on a `pad_value` background (as YOLOX expects)."""
    h, w = image.shape[:2]
    if not letterbox:
        return resize(image, width, height), Placement(width / w, height / h)
    ratio = min(width / w, height / h)
    new_w, new_h = max(1, round(w * ratio)), max(1, round(h * ratio))
    canvas = np.full((height, width, 3), pad_value, np.uint8)
    canvas[:new_h, :new_w] = resize(image, new_w, new_h)
    return canvas, Placement(ratio, ratio)


def to_tensor(
    image: np.ndarray,
    scale: float,
    mean: tuple[float, float, float],
    std: tuple[float, float, float],
    bgr: bool,
) -> np.ndarray:
    """H x W x 3 uint8 RGB -> 1 x 3 x H x W float32."""
    data = image[..., ::-1] if bgr else image
    tensor = data.astype(np.float32) * scale
    tensor = (tensor - np.asarray(mean, np.float32)) / np.asarray(std, np.float32)
    return np.ascontiguousarray(tensor.transpose(2, 0, 1)[None])
