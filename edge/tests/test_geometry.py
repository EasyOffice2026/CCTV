import pytest

from cctv_edge.credentials import Credential, redact, with_credentials
from cctv_edge.geometry import Box, line_crossing, point_in_polygon

SQUARE = [(0.2, 0.2), (0.8, 0.2), (0.8, 0.8), (0.2, 0.8)]


@pytest.mark.parametrize(
    ("point", "inside"),
    [((0.5, 0.5), True), ((0.1, 0.5), False), ((0.5, 0.9), False), ((0.79, 0.21), True)],
)
def test_point_in_polygon(point, inside):
    assert point_in_polygon(point, SQUARE) is inside


def test_foot_point_is_bottom_centre():
    assert Box(0.2, 0.1, 0.4, 0.9).foot == pytest.approx((0.3, 0.9))


def test_iou():
    a = Box(0, 0, 0.5, 0.5)
    assert a.iou(a) == pytest.approx(1)
    assert a.iou(Box(0.25, 0, 0.75, 0.5)) == pytest.approx(1 / 3)
    assert a.iou(Box(0.6, 0.6, 1, 1)) == 0


def test_line_crossing_direction():
    # Horizontal door line drawn left to right; on screen, below it is its right side.
    a, b = (0.2, 0.5), (0.8, 0.5)
    assert line_crossing((0.5, 0.7), (0.5, 0.3), a, b) == 1  # walks up: in
    assert line_crossing((0.5, 0.3), (0.5, 0.7), a, b) == -1  # walks down: out
    assert line_crossing((0.5, 0.7), (0.5, 0.6), a, b) == 0  # does not reach the line
    assert line_crossing((0.9, 0.7), (0.9, 0.3), a, b) == 0  # passes beside the line


def test_credentials_are_added_and_redacted():
    url = with_credentials(
        "rtsp://10.0.0.5:554/Streaming/Channels/101", Credential("admin", "p@ss:1")
    )
    assert url == "rtsp://admin:p%40ss%3A1@10.0.0.5:554/Streaming/Channels/101"
    assert redact(f"failed to open {url}") == (
        "failed to open rtsp://***@10.0.0.5:554/Streaming/Channels/101"
    )
    assert "p@ss" not in repr(Credential("admin", "p@ss:1"))
