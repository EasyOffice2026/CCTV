"""Objects left or removed in watched areas (later phase), without a model.

Two backgrounds are kept on a small grey image (about 96 x 54), updated once a second:

- a long-term one that follows the scene over `absorb_s` (minutes): what the area
  normally looks like;
- a short-term one that follows it over `SHORT_S` (seconds): what it looks like now.

A pixel that differs from the long-term background but no longer from the short-term one
has changed and then stopped changing: something was put down or taken away. Each such
pixel gathers evidence (seconds); pixels moving (people walking past) keep theirs, pixels
back to normal lose it twice as fast. Areas of pixels with `min_static_s` of evidence are
reported while they still differ from the long-term background, and stay reported while
they keep half of it, so someone walking in front doesn't end the event. While reported,
the long-term background isn't updated there, so the event lasts until the object goes;
after `max_hold_s` the change becomes the new normal.

Left or removed: a bag or box adds edges to the picture, a missing item takes them away,
so the region's edges now are compared with its edges in the long-term background when it
is first reported. The answer is kept: once the bag is taken away its region is back to
normal and no longer reported, rather than looking like something removed.

People: boxes of tracked people (when a person model runs) are left out, so someone
sitting still isn't an object. Without a person model, draw watched areas where people
don't stay (a shelf, a safe, a corridor). A sudden change over much of the picture (lights,
a moved camera) resets the backgrounds instead of raising events.
"""

from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from uuid import UUID

import numpy as np

from cctv_edge.detection import OBJECT_LEFT, OBJECT_REMOVED
from cctv_edge.geometry import Box, Point, point_in_polygon

GRID_WIDTH = 96
INTERVAL_S = 1.0  # backgrounds and evidence are updated this often
SHORT_S = 3.0
SCENE_CHANGE_FRACTION = 0.4  # this much of the picture changed at once: a reset


@dataclass(frozen=True)
class ObjectSettings:
    min_static_s: float = 120.0
    max_hold_s: float = 7200.0
    absorb_s: float = 600.0
    difference: float = 30.0  # grey levels (0-255) between picture and background
    min_area: float = 0.003  # of the picture

    @classmethod
    def from_params(cls, params: Mapping[str, float]) -> "ObjectSettings":
        return cls(**{k: float(v) for k, v in params.items() if k in cls.__dataclass_fields__})


@dataclass(frozen=True)
class StaticChange:
    label: str  # OBJECT_LEFT or OBJECT_REMOVED
    box: Box  # normalised
    zone_id: UUID
    confidence: float


def _mask(polygon: Sequence[Point], height: int, width: int) -> np.ndarray:
    mask = np.zeros((height, width), bool)
    for y in range(height):
        for x in range(width):
            mask[y, x] = point_in_polygon(((x + 0.5) / width, (y + 0.5) / height), polygon)
    return mask


def _edges(image: np.ndarray) -> np.ndarray:
    dy, dx = np.gradient(image)
    return np.hypot(dx, dy)


def _regions(mask: np.ndarray) -> list[np.ndarray]:
    """4-connected regions of `mask`, as boolean masks."""
    seen = np.zeros_like(mask)
    regions = []
    height, width = mask.shape
    for start in zip(*np.nonzero(mask), strict=True):
        if seen[start]:
            continue
        region = np.zeros_like(mask)
        queue = deque([start])
        seen[start] = True
        while queue:
            y, x = queue.popleft()
            region[y, x] = True
            for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                if 0 <= ny < height and 0 <= nx < width and mask[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True
                    queue.append((ny, nx))
        regions.append(region)
    return regions


class StaticChangeDetector:
    def __init__(
        self, zones: Mapping[UUID, Sequence[Point]], settings: ObjectSettings | None = None
    ) -> None:
        self.zones = dict(zones)
        self.settings = settings or ObjectSettings()
        self._masks: dict[UUID, np.ndarray] = {}
        self._long: np.ndarray | None = None
        self._short: np.ndarray | None = None
        self._evidence: np.ndarray | None = None
        self._reported: np.ndarray | None = None
        self._visible: np.ndarray | None = None
        self._kind: np.ndarray | None = None  # per pixel: 1 left, -1 removed, 0 not yet
        self._last_update: float | None = None
        self._found: list[StaticChange] = []

    def _grey(self, frame: np.ndarray) -> np.ndarray:
        step = max(1, frame.shape[1] // GRID_WIDTH)
        return frame[::step, ::step].mean(axis=2, dtype=np.float32)

    def _reset(self, grey: np.ndarray) -> None:
        self._long, self._short = grey.copy(), grey.copy()
        self._evidence = np.zeros(grey.shape, np.float32)
        self._reported = np.zeros(grey.shape, bool)
        self._visible = np.zeros(grey.shape, bool)
        self._kind = np.zeros(grey.shape, np.int8)
        self._found = []
        height, width = grey.shape
        self._masks = {zid: _mask(polygon, height, width) for zid, polygon in self.zones.items()}

    def update(
        self, frame: np.ndarray, ts: float, people: Sequence[Box] = ()
    ) -> list[StaticChange]:
        """Changes standing still in the watched areas now (the last result between
        updates, which happen every INTERVAL_S)."""
        if self._last_update is not None and ts - self._last_update < INTERVAL_S:
            return self._found
        grey = self._grey(frame)
        if self._long is None or self._long.shape != grey.shape:
            self._reset(grey)
            self._last_update = ts
            return []
        assert self._short is not None and self._evidence is not None  # noqa: S101
        assert self._reported is not None  # noqa: S101
        dt = min(ts - (self._last_update or ts), 5 * INTERVAL_S)
        self._last_update = ts
        s = self.settings
        changed = np.abs(grey - self._long) > s.difference
        if changed.mean() > SCENE_CHANGE_FRACTION:  # lights, a moved camera: start again
            self._reset(grey)
            return []
        moving = np.abs(grey - self._short) > s.difference / 2
        height, width = grey.shape
        person = np.zeros(grey.shape, bool)
        for box in people:
            person[
                int(box.y1 * height) : int(np.ceil(box.y2 * height)),
                int(box.x1 * width) : int(np.ceil(box.x2 * width)),
            ] = True

        evidence = self._evidence
        still = changed & ~moving & ~person
        evidence[still] += dt
        back = ~changed & ~moving
        evidence[back] = np.maximum(0.0, evidence[back] - 2 * dt)
        self._reported = (evidence >= s.min_static_s) | (
            self._reported & (evidence >= s.min_static_s / 2)
        )
        # Held long enough: the change is the new normal.
        expired = evidence >= s.max_hold_s
        if expired.any():
            self._long[expired] = grey[expired]
            evidence[expired] = 0
            self._reported &= ~expired

        self._visible = self._reported & changed
        assert self._kind is not None  # noqa: S101
        self._kind[~self._reported] = 0

        rate_long = min(1.0, dt / s.absorb_s)
        rate_short = min(1.0, dt / SHORT_S)
        learn = ~self._reported & ~person
        self._long[learn] += rate_long * (grey[learn] - self._long[learn])
        self._short[~person] += rate_short * (grey[~person] - self._short[~person])

        self._found = self._changes(grey)
        return self._found

    def _changes(self, grey: np.ndarray) -> list[StaticChange]:
        assert self._reported is not None and self._long is not None  # noqa: S101
        assert self._evidence is not None and self._visible is not None  # noqa: S101
        assert self._kind is not None  # noqa: S101
        height, width = grey.shape
        min_cells = max(1, round(self.settings.min_area * height * width))
        found = []
        edges_now, edges_before = _edges(grey), _edges(self._long)
        for zone_id, zone in self._masks.items():
            for region in _regions(self._visible & zone):
                if region.sum() < min_cells:
                    continue
                rows, cols = np.nonzero(region)
                box = Box(
                    cols.min() / width,
                    rows.min() / height,
                    (cols.max() + 1) / width,
                    (rows.max() + 1) / height,
                )
                kinds = self._kind[region]
                if kinds.any():  # decided when first reported
                    added = (kinds == 1).sum() >= (kinds == -1).sum()
                else:
                    added = edges_now[region].sum() >= edges_before[region].sum()
                    self._kind[region] = 1 if added else -1
                held = float(self._evidence[region].mean())
                found.append(
                    StaticChange(
                        OBJECT_LEFT if added else OBJECT_REMOVED,
                        box,
                        zone_id,
                        min(1.0, 0.5 + held / (4 * self.settings.min_static_s)),
                    )
                )
        return found
