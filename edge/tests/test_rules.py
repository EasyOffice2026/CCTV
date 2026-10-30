import uuid

from cctv_contracts.enums import EventType, FeatureKey, Severity, ZoneType
from cctv_contracts.v1 import FeatureConfig
from cctv_edge.detection import MOTION, PERSON
from cctv_edge.geometry import Box
from cctv_edge.rules import (
    EventClosed,
    EventOpened,
    GroupBy,
    Observation,
    RuleEngine,
    RuleSpec,
    When,
    rules_for_features,
)
from cctv_edge.schedule import BranchSchedule

from .conftest import zone

ALWAYS_OPEN = BranchSchedule([], "Asia/Kuwait")
BOX = Box(0.4, 0.4, 0.6, 0.9)


def person(track_id=1, zones=frozenset(), confidence=0.9):
    return Observation(PERSON, BOX, confidence, track_id, frozenset(zones))


def rule(**kwargs) -> RuleSpec:
    defaults = {
        "event_type": EventType.AFTER_HOURS_PERSON,
        "labels": frozenset({PERSON}),
        "group_by": GroupBy.ZONE,
        "min_duration_s": 2,
        "max_gap_s": 3,
        "cooldown_s": 60,
    }
    return RuleSpec(**{**defaults, **kwargs})


def run(engine: RuleEngine, frames):
    """frames: {ts: [observations]} for every ts in order. Returns all outputs."""
    outputs = []
    for ts in sorted(frames):
        outputs += engine.update(ts, frames[ts])
    return outputs


def present(start, end, step=0.5, **kwargs):
    t, frames = start, {}
    while t <= end:
        frames[t] = [person(**kwargs)]
        t += step
    return frames


def absent(start, end, step=0.5):
    t, frames = start, {}
    while t <= end:
        frames[t] = []
        t += step
    return frames


def test_one_event_while_present_then_closed():
    engine = RuleEngine([rule()], [], ALWAYS_OPEN)
    out = run(engine, {**present(0, 10), **absent(10.5, 20)})
    opened = [o for o in out if isinstance(o, EventOpened)]
    closed = [o for o in out if isinstance(o, EventClosed)]
    assert len(opened) == 1 and len(closed) == 1
    assert opened[0].started_at == 0  # starts when first seen, not after min_duration
    assert closed[0].event_id == opened[0].event_id
    assert closed[0].ended_at == 10  # last seen
    assert engine.open_events() == []


def test_short_presence_is_ignored():
    engine = RuleEngine([rule(min_duration_s=2)], [], ALWAYS_OPEN)
    assert run(engine, {**present(0, 1.5), **absent(2, 10)}) == []


def test_short_gaps_do_not_split_the_event():
    engine = RuleEngine([rule(max_gap_s=3)], [], ALWAYS_OPEN)
    out = run(engine, {**present(0, 5), **absent(5.5, 7), **present(7.5, 12), **absent(12.5, 20)})
    assert sum(isinstance(o, EventOpened) for o in out) == 1


def test_cooldown_suppresses_repeat_events():
    engine = RuleEngine([rule(cooldown_s=60)], [], ALWAYS_OPEN)
    out = run(engine, {**present(0, 5), **absent(5.5, 20), **present(20.5, 30)})
    assert sum(isinstance(o, EventOpened) for o in out) == 1
    out = run(engine, {**absent(30.5, 40), **present(70, 80)})  # cooldown over at 65
    assert sum(isinstance(o, EventOpened) for o in out) == 1


def test_after_hours_rule_only_fires_when_closed():
    open_now = BranchSchedule([], "Asia/Kuwait")  # always open
    engine = RuleEngine([rule(when=When.AFTER_HOURS)], [], open_now)
    assert run(engine, present(0, 10)) == []


def test_closing_time_ends_open_hours_events():
    engine = RuleEngine([rule(when=When.OPEN_HOURS)], [], ALWAYS_OPEN)
    out = run(engine, present(0, 5))
    assert len(out) == 1
    engine.schedule = BranchSchedule.__new__(BranchSchedule)
    engine.schedule.is_open = lambda _when: False  # the branch closes
    out = engine.update(5.5, [person()])
    assert len(out) == 1 and isinstance(out[0], EventClosed)


def test_zone_rule_per_track():
    restricted = zone(ZoneType.RESTRICTED, [(0, 0), (1, 0), (1, 1), (0, 1)])
    kitchen = zone(ZoneType.KITCHEN, [(0, 0), (1, 0), (1, 1), (0, 1)])
    spec = rule(
        event_type=EventType.RESTRICTED_AREA_ENTRY,
        zone_types=frozenset({ZoneType.RESTRICTED}),
        group_by=GroupBy.TRACK,
    )
    engine = RuleEngine([spec], [restricted, kitchen], ALWAYS_OPEN)
    frames = {
        t / 2: [
            person(track_id=1, zones={restricted.id}),
            person(track_id=2, zones={restricted.id}),
            person(track_id=3, zones={kitchen.id}),  # not in a restricted zone
        ]
        for t in range(0, 11)
    }
    opened = [o for o in run(engine, frames) if isinstance(o, EventOpened)]
    assert sorted(o.track_id for o in opened) == [1, 2]
    assert {o.zone_id for o in opened} == {restricted.id}


def test_long_events_are_split():
    engine = RuleEngine([rule()], [], ALWAYS_OPEN, max_event_seconds=30)
    out = run(engine, present(0, 70))
    opened = [o for o in out if isinstance(o, EventOpened)]
    assert len(opened) == 3
    assert opened[1].continues == opened[0].event_id
    assert opened[1].started_at == 30


def test_close_all():
    engine = RuleEngine([rule()], [], ALWAYS_OPEN)
    run(engine, present(0, 5))
    out = engine.close_all()
    assert len(out) == 1 and isinstance(out[0], EventClosed)
    assert out[0].ended_at == 5
    assert engine.open_events() == []


def test_rules_for_features_and_params():
    rules = rules_for_features([FeatureConfig(key=FeatureKey.SECURITY)])
    assert {r.event_type for r in rules} == {
        EventType.AFTER_HOURS_PERSON,
        EventType.AFTER_HOURS_MOTION,
        EventType.RESTRICTED_AREA_ENTRY,
    }
    rules = rules_for_features(
        [
            FeatureConfig(
                key=FeatureKey.SECURITY,
                params={
                    "after_hours_motion": {"enabled": False},
                    "after_hours_person": {"min_duration_s": 5, "severity": "warning"},
                    "restricted_area_entry": {"cooldown_s": "lots"},  # invalid: ignored
                },
            ),
            FeatureConfig(key=FeatureKey.ATTENDANCE),  # not on the edge yet
        ]
    )
    by_type = {r.event_type: r for r in rules}
    assert EventType.AFTER_HOURS_MOTION not in by_type
    assert by_type[EventType.AFTER_HOURS_PERSON].min_duration_s == 5
    assert by_type[EventType.AFTER_HOURS_PERSON].severity is Severity.WARNING
    assert by_type[EventType.RESTRICTED_AREA_ENTRY].cooldown_s == 120


def test_engine_reports_needed_labels():
    rules = rules_for_features([FeatureConfig(key=FeatureKey.SECURITY)])
    assert RuleEngine(rules, [], ALWAYS_OPEN).labels == {PERSON, MOTION}
    assert RuleEngine([], [], ALWAYS_OPEN).labels == frozenset()


def test_event_ids_are_unique():
    engine = RuleEngine([rule(cooldown_s=0)], [], ALWAYS_OPEN)
    out = run(engine, {**present(0, 3), **absent(3.5, 8), **present(8.5, 12)})
    ids = [o.event_id for o in out if isinstance(o, EventOpened)]
    assert len(ids) == 2 and len(set(ids)) == 2 and all(isinstance(i, uuid.UUID) for i in ids)
