"""Zone geometry. All coordinates are normalised to the frame (0..1), like the config."""

from collections.abc import Sequence
from dataclasses import dataclass

Point = tuple[float, float]


@dataclass(frozen=True)
class Box:
    x1: float
    y1: float
    x2: float
    y2: float

    @property
    def foot(self) -> Point:
        """Bottom-centre: where a person stands, which is what zones are drawn around."""
        return ((self.x1 + self.x2) / 2, self.y2)

    @property
    def area(self) -> float:
        return max(0.0, self.x2 - self.x1) * max(0.0, self.y2 - self.y1)

    def iou(self, other: "Box") -> float:
        w = min(self.x2, other.x2) - max(self.x1, other.x1)
        h = min(self.y2, other.y2) - max(self.y1, other.y1)
        if w <= 0 or h <= 0:
            return 0.0
        inter = w * h
        return inter / (self.area + other.area - inter)


def point_in_polygon(point: Point, polygon: Sequence[Point]) -> bool:
    x, y = point
    inside = False
    j = len(polygon) - 1
    for i in range(len(polygon)):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _cross(o: Point, a: Point, b: Point) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def line_crossing(previous: Point, current: Point, a: Point, b: Point) -> int:
    """Direction in which the movement previous -> current crosses the line a -> b.

    Returns +1 when crossing from the right of a -> b to its left (counted as "in"),
    -1 for the opposite direction, and 0 when the line segment is not crossed.
    Image coordinates have y pointing down, so "left" is as seen on screen.
    """
    # With y pointing down, a positive cross product means "right of a -> b" on screen.
    was_right = _cross(a, b, previous) >= 0
    is_right = _cross(a, b, current) >= 0
    if was_right == is_right:
        return 0
    # The movement crosses the infinite line; check it passes between a and b.
    side_a = _cross(previous, current, a)
    side_b = _cross(previous, current, b)
    if (side_a > 0 and side_b > 0) or (side_a < 0 and side_b < 0):
        return 0
    return 1 if was_right else -1
