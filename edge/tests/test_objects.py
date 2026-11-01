"""Objects left or removed (later phase): static changes in watched areas, on synthetic
pictures - a smooth wall and floor, and a patterned bag."""

import uuid

import numpy as np

from cctv_contracts.enums import EventType, FeatureKey, ZoneType
from cctv_contracts.v1 import FeatureConfig
from cctv_edge.detection import OBJECT_LEFT, OBJECT_REMOVED, PERSON
from cctv_edge.geometry import Box
from cctv_edge.objects import ObjectSettings, StaticChangeDetector
from cctv_edge.pipeline import CameraPipeline
from cctv_edge.rules import EventOpened

from .conftest import camera, zone
from .test_pipeline import ScriptedDetector
from .test_wave2 import ALWAYS_OPEN

WATCH = [(0.0, 0.4), (0.6, 0.4), (0.6, 1.0), (0.0, 1.0)]  # bottom-left part of the picture
ZONE = uuid.uuid4()
FAST = ObjectSettings(min_static_s=20, max_hold_s=300, absorb_s=600)


def scene(bag: bool = False, at: tuple[int, int] = (200, 250), brightness: float = 0) -> np.ndarray:
    """A smooth background; with `bag`, a 64 x 48 checked object at `at` (x, y in pixels)."""
    row = np.linspace(60, 120, 640, dtype=np.float32)
    frame = np.repeat(np.tile(row, (360, 1))[:, :, None], 3, axis=2) + brightness
    if bag:
        x, y = at
        checks = (np.indices((48, 64)).sum(axis=0) // 8) % 2
        frame[y : y + 48, x : x + 64] = (30 + 190 * checks)[:, :, None]
    return np.clip(frame, 0, 255).astype(np.uint8)


def run(detector, frames, start: float = 0.0, people=()):
    """One frame per second; returns what was found at each second."""
    return [detector.update(frame, start + i, people) for i, frame in enumerate(frames)]


def test_a_bag_left_in_the_watched_area():
    detector = StaticChangeDetector({ZONE: WATCH}, FAST)
    found = run(detector, [scene()] * 10 + [scene(bag=True)] * 60)
    first = next(i for i, f in enumerate(found) if f)
    assert 25 <= first <= 50  # settled (short background) then 20 s still
    [change] = found[-1]
    assert (change.label, change.zone_id) == (OBJECT_LEFT, ZONE)
    box = change.box
    assert abs(box.x1 - 200 / 640) < 0.03 and abs(box.y1 - 250 / 360) < 0.04
    assert abs(box.x2 - 264 / 640) < 0.03 and abs(box.y2 - 298 / 360) < 0.04


def test_something_taken_away():
    detector = StaticChangeDetector({ZONE: WATCH}, FAST)
    found = run(detector, [scene(bag=True)] * 10 + [scene()] * 60)
    assert [c.label for c in found[-1]] == [OBJECT_REMOVED]


def test_people_passing_light_changes_and_other_areas_raise_nothing():
    detector = StaticChangeDetector({ZONE: WATCH}, FAST)
    # Someone (the bag) crosses the area, a new spot every second.
    walking = [scene(bag=True, at=(40 + 25 * i, 250)) for i in range(15)]
    # The lights get a little brighter, then the bag stays outside the watched area.
    brighter = [scene(brightness=25)] * 30
    outside = [scene(bag=True, at=(500, 40))] * 60
    assert not any(run(detector, [scene()] * 5 + walking + brighter + outside))


def test_a_person_standing_still_is_not_an_object():
    detector = StaticChangeDetector({ZONE: WATCH}, FAST)
    person = Box(190 / 640, 240 / 360, 270 / 640, 305 / 360)
    assert not any(run(detector, [scene()] * 5 + [scene(bag=True)] * 60, people=[person]))


def test_someone_walking_in_front_does_not_end_it_and_long_holds_become_normal():
    detector = StaticChangeDetector({ZONE: WATCH}, FAST)
    run(detector, [scene()] * 5 + [scene(bag=True)] * 50)
    assert detector.update(scene(bag=True), 55)
    # Covered by someone moving across it for 4 s: still there.
    crossing = [scene(bag=True, at=(180 + 10 * i, 240)) for i in range(4)]
    assert all(run(detector, crossing, start=56))
    # After max_hold_s it is part of the scene.
    found = run(detector, [scene(bag=True)] * 320, start=60)
    assert found[100] and not found[-1]


def test_switching_on_the_lights_resets_instead_of_alerting():
    detector = StaticChangeDetector({ZONE: WATCH}, FAST)
    dark = scene(brightness=-55)
    lights = np.clip(scene().astype(np.int16) * 2, 0, 255).astype(np.uint8)
    lights[:, :320] = 255  # half the picture changes a lot, not just its level
    assert not any(run(detector, [dark] * 5 + [lights] * 60))


def test_pipeline_turns_changes_into_events():
    watch = zone(ZoneType.WATCH_AREA, WATCH)
    feature = FeatureConfig(
        key=FeatureKey.OBJECT_LEFT_REMOVED, params={"min_static_s": 20, "max_hold_s": 300}
    )
    pipeline = CameraPipeline(camera([feature], [watch]), ALWAYS_OPEN, {PERSON: ScriptedDetector})
    assert pipeline.objects is not None and not pipeline.missing_labels
    assert pipeline.objects.settings.min_static_s == 20
    opened = []
    for i, frame in enumerate([scene()] * 5 + [scene(bag=True)] * 60):
        opened += [
            o for o in pipeline.process(frame, 1000.0 + i).outputs if isinstance(o, EventOpened)
        ]
    [event] = opened
    assert event.rule.event_type is EventType.OBJECT_LEFT and event.zone_id == watch.id

    # Taken away again: the event ends, and the empty spot isn't "something removed".
    closed = []
    for i, frame in enumerate([scene()] * 40):
        result = pipeline.process(frame, 1065.0 + i).outputs
        opened += [o for o in result if isinstance(o, EventOpened)]
        closed += [o for o in result if not isinstance(o, EventOpened)]
    assert [c.event_id for c in closed] == [event.event_id] and len(opened) == 1

    # Without a watched area there is nothing to do, and nothing is missing.
    bare = CameraPipeline(camera([feature]), ALWAYS_OPEN, {PERSON: ScriptedDetector})
    assert bare.objects is None and not bare.missing_labels
