from cctv_edge.detection import MOTION, PERSON, Detection
from cctv_edge.geometry import Box
from cctv_edge.tracking import ByteTracker


def det(x: float, confidence: float = 0.9, label: str = PERSON, w: float = 0.1) -> Detection:
    return Detection(label, Box(x, 0.2, x + w, 0.8), confidence)


def test_keeps_ids_and_needs_two_hits():
    tracker = ByteTracker(min_hits=2)
    assert tracker.update([det(0.10)], 0.0) == []
    [track] = tracker.update([det(0.12)], 0.2)
    first_id = track.id
    [track] = tracker.update([det(0.14)], 0.4)
    assert track.id == first_id
    assert track.previous_box == det(0.12).box


def test_forgets_old_tracks():
    tracker = ByteTracker(min_hits=1, max_age_seconds=1)
    first = tracker.update([det(0.1)], 0.0)[0].id
    assert tracker.update([det(0.1)], 5.0)[0].id != first


def test_low_confidence_detection_keeps_a_track_alive():
    """A person half hidden behind the counter scores low but keeps their id."""
    tracker = ByteTracker(min_hits=1)
    first = tracker.update([det(0.3, 0.9)], 0.0)[0].id
    [track] = tracker.update([det(0.31, 0.3)], 0.2)
    assert track.id == first and track.confidence == 0.3


def test_low_confidence_detections_never_start_tracks():
    tracker = ByteTracker(min_hits=1)
    assert tracker.update([det(0.3, 0.3)], 0.0) == []
    assert tracker.update([det(0.3, 0.55)], 0.2) == []  # below new_track_threshold
    assert tracker.tracks == []


def test_prediction_bridges_missed_frames_of_a_moving_person():
    """Walking 0.03 per frame, then missed for two frames (behind a pillar): the last box
    no longer overlaps enough, the predicted one does."""
    tracker = ByteTracker(min_hits=1)
    ids = set()
    for i in range(5):
        ids |= {t.id for t in tracker.update([det(0.1 + 0.03 * i)], i * 0.2)}
    tracker.update([], 1.0)
    tracker.update([], 1.2)
    [track] = tracker.update([det(0.1 + 0.03 * 7)], 1.4)
    assert ids == {track.id}
    assert det(0.1 + 0.03 * 4).box.iou(track.box) < tracker.match_iou  # plain IoU would fail


def test_two_people_keep_their_own_ids():
    tracker = ByteTracker(min_hits=1)
    a, b = (t.id for t in tracker.update([det(0.1), det(0.6)], 0.0))
    for i in range(1, 6):
        tracks = tracker.update([det(0.6 - 0.01 * i), det(0.1 + 0.01 * i)], i * 0.2)
        by_x = {round(t.box.x1, 2): t.id for t in tracks}
        assert by_x[round(0.1 + 0.01 * i, 2)] == a
        assert by_x[round(0.6 - 0.01 * i, 2)] == b


def test_labels_are_never_mixed():
    tracker = ByteTracker(min_hits=1)
    person = tracker.update([det(0.3)], 0.0)[0].id
    [motion] = tracker.update([det(0.3, label=MOTION)], 0.2)
    assert motion.id != person and motion.label == MOTION
