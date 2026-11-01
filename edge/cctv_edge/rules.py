"""Rules engine: turns per-frame observations into open/closed events.

Each rule watches some labels (person, motion) anywhere in the frame or inside zones of
given types, optionally only after/within working hours. An event opens once the
condition has held for `min_duration_s`, stays open (one event, no repeats) while it
keeps holding, and closes after `max_gap_s` without a match. `cooldown_s` then suppresses
a new event for the same rule and zone (or the same person, for `cooldown_by=TRACK`), so
one intruder pacing in and out of view does not produce a stream of alerts.

An `absent` rule holds the other way round: for each zone of its types that has *no*
matching observation (an unattended counter). A rule with `min_count` holds for a zone
only while at least that many matching objects are in it (a long queue, a full room).

Feature parameters (`FeatureConfig.params`) hold per-rule overrides keyed by event type,
and feature-wide settings (thresholds for the classifiers) listed in `FEATURE_DEFAULTS`.
"""

import logging
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from uuid import UUID

from cctv_contracts.enums import EVENT_FEATURE, EventType, FeatureKey, Severity, ZoneType
from cctv_contracts.v1 import FeatureConfig, ZoneConfig
from cctv_edge.detection import (
    DRAWER_OPEN,
    MOTION,
    NO_GLOVES,
    NO_HAIR_CAP,
    NO_MASK,
    PERSON,
)
from cctv_edge.geometry import Box
from cctv_edge.schedule import BranchSchedule

log = logging.getLogger(__name__)


class When(str, Enum):
    ALWAYS = "always"
    AFTER_HOURS = "after_hours"
    OPEN_HOURS = "open_hours"


class GroupBy(str, Enum):
    TRACK = "track"  # one event per tracked object (and zone)
    ZONE = "zone"  # one event per zone, however many objects are in it


@dataclass(frozen=True)
class RuleSpec:
    event_type: EventType
    labels: frozenset[str]
    zone_types: frozenset[ZoneType] | None = None  # None: anywhere outside ignore zones
    when: When = When.ALWAYS
    group_by: GroupBy = GroupBy.TRACK
    min_duration_s: float = 2.0
    max_gap_s: float = 3.0
    cooldown_s: float = 300.0
    severity: Severity = Severity.WARNING
    absent: bool = False  # holds while a zone has NO matching observation
    cooldown_by: GroupBy = GroupBy.ZONE  # TRACK: per person, so others still alert
    min_confidence: float = 0.0  # observations below this are ignored
    min_count: float = 1  # zone rules: at least this many matching objects in the zone
    attributes: tuple[tuple[str, object], ...] = ()  # added to the event's attributes

    @property
    def feature(self) -> FeatureKey:
        return EVENT_FEATURE[self.event_type]


def _ppe_rule(event_type: EventType, label: str, item: str) -> RuleSpec:
    return RuleSpec(
        event_type,
        frozenset({label}),
        zone_types=frozenset({ZoneType.KITCHEN}),
        group_by=GroupBy.TRACK,
        cooldown_by=GroupBy.TRACK,
        min_duration_s=5,  # the item must be missing over several checks, not one frame
        max_gap_s=3,
        cooldown_s=900,  # one event per person per 15 min
        attributes=(("missing", item),),
    )


# Rules each feature runs on the edge. Attendance and customer counting have no rules:
# they send sightings and counts instead (see attendance.py, counts.py).
DEFAULT_RULES: dict[FeatureKey, tuple[RuleSpec, ...]] = {
    FeatureKey.SECURITY: (
        RuleSpec(
            EventType.AFTER_HOURS_PERSON,
            frozenset({PERSON}),
            when=When.AFTER_HOURS,
            group_by=GroupBy.ZONE,
            min_duration_s=3,
            cooldown_s=300,
            severity=Severity.CRITICAL,
        ),
        RuleSpec(
            EventType.AFTER_HOURS_MOTION,
            frozenset({MOTION}),
            when=When.AFTER_HOURS,
            group_by=GroupBy.ZONE,
            min_duration_s=2,
            cooldown_s=600,
        ),
        RuleSpec(
            EventType.RESTRICTED_AREA_ENTRY,
            frozenset({PERSON}),
            zone_types=frozenset({ZoneType.RESTRICTED}),
            min_duration_s=2,
            cooldown_s=120,
            severity=Severity.CRITICAL,
        ),
    ),
    FeatureKey.CASHIER: (
        RuleSpec(
            EventType.COUNTER_UNATTENDED,
            frozenset({PERSON}),
            zone_types=frozenset({ZoneType.COUNTER}),
            when=When.OPEN_HOURS,
            group_by=GroupBy.ZONE,
            absent=True,
            min_duration_s=60,
            max_gap_s=2,  # someone back at the counter for 2 s ends it
            cooldown_s=300,
        ),
        RuleSpec(
            EventType.DRAWER_OPEN,
            frozenset({DRAWER_OPEN}),
            zone_types=frozenset({ZoneType.CASH_DRAWER}),
            group_by=GroupBy.ZONE,
            min_duration_s=1,
            max_gap_s=2,
            cooldown_s=5,  # every opening is logged
            severity=Severity.INFO,
        ),
    ),
    FeatureKey.KITCHEN_COMPLIANCE: (
        _ppe_rule(EventType.MISSING_MASK, NO_MASK, "mask"),
        _ppe_rule(EventType.MISSING_GLOVES, NO_GLOVES, "gloves"),
        _ppe_rule(EventType.MISSING_HAIR_CAP, NO_HAIR_CAP, "hair_cap"),
    ),
    # Wave 2 (plan 10.2). The thresholds depend on the branch: set them per camera.
    FeatureKey.QUEUE: (
        RuleSpec(
            EventType.QUEUE_LONG,
            frozenset({PERSON}),
            zone_types=frozenset({ZoneType.QUEUE}),
            group_by=GroupBy.ZONE,
            min_count=5,
            min_duration_s=60,
            max_gap_s=10,  # a queue that is briefly shorter is still the same queue
            cooldown_s=600,
        ),
    ),
    FeatureKey.CUSTOMER_COUNT: (
        RuleSpec(
            EventType.OCCUPANCY_HIGH,
            frozenset({PERSON}),
            zone_types=frozenset({ZoneType.DINING}),
            group_by=GroupBy.ZONE,
            min_count=30,
            min_duration_s=120,
            max_gap_s=30,
            cooldown_s=1800,
            severity=Severity.INFO,
        ),
    ),
}

# Features that run without rules (other stages of the pipeline).
NO_RULES = frozenset({FeatureKey.ATTENDANCE})

# Feature-wide settings and their defaults; override in the feature's params, e.g.
# {"min_crop_height_px": 120}. See edge/README.md for what each one does.
FEATURE_DEFAULTS: dict[FeatureKey, dict[str, float]] = {
    FeatureKey.CASHIER: {"drawer_open_threshold": 0.6},
    FeatureKey.KITCHEN_COMPLIANCE: {
        "min_crop_height_px": 96,
        "min_sharpness": 20.0,
        "worn_threshold": 0.6,
        "missing_threshold": 0.3,
        "classify_interval_s": 1.0,
    },
    FeatureKey.ATTENDANCE: {
        "match_threshold": 0.40,
        "min_face_px": 60,
        "min_sharpness": 30.0,
        "min_detector_score": 0.85,
        "sighting_interval_s": 60,
    },
}

_TUNABLE = ("min_duration_s", "max_gap_s", "cooldown_s", "min_confidence", "min_count")


def _event_keys(feature: FeatureKey) -> set[str]:
    return {e.value for e, f in EVENT_FEATURE.items() if f is feature}


def feature_settings(feature: FeatureConfig) -> dict[str, float]:
    """`FEATURE_DEFAULTS` for the feature with valid overrides from its params."""
    settings = dict(FEATURE_DEFAULTS.get(feature.key, {}))
    for name in settings:
        if name not in feature.params:
            continue
        value = feature.params[name]
        if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
            log.warning("ignoring %s.%s: must be a non-negative number", feature.key.value, name)
            continue
        settings[name] = float(value)
    return settings


def rules_for_features(features: Sequence[FeatureConfig]) -> list[RuleSpec]:
    """Rules for the enabled features, with per-rule overrides from `params`:
    `{"after_hours_motion": {"enabled": false}, "after_hours_person": {"min_duration_s": 5}}`.
    """
    rules: list[RuleSpec] = []
    for feature in features:
        known = _event_keys(feature.key) | set(FEATURE_DEFAULTS.get(feature.key, {}))
        unknown = sorted(set(feature.params) - known)
        if unknown:
            log.warning("feature %s: unknown params %s ignored", feature.key.value, unknown)
        defaults = DEFAULT_RULES.get(feature.key)
        if defaults is None:
            if feature.key not in NO_RULES:
                log.warning("feature %s has no edge rules yet; skipping", feature.key.value)
            continue
        start = len(rules)
        for rule in defaults:
            overrides = feature.params.get(rule.event_type.value)
            if overrides is None:
                rules.append(rule)
                continue
            try:
                updated = _apply_overrides(rule, overrides)
            except (TypeError, ValueError) as exc:
                log.warning("ignoring params for %s: %s", rule.event_type.value, exc)
                updated = rule
            if updated is not None:
                rules.append(updated)
        if feature.key is FeatureKey.KITCHEN_COMPLIANCE:
            # The required items are the enabled rules; events say what was checked.
            items = tuple(sorted(str(dict(r.attributes)["missing"]) for r in rules[start:]))
            rules[start:] = [
                replace(r, attributes=(*r.attributes, ("checked_items", items)))
                for r in rules[start:]
            ]
    return rules


def _apply_overrides(rule: RuleSpec, overrides: object) -> RuleSpec | None:
    if not isinstance(overrides, Mapping):
        raise TypeError("expected an object")
    if overrides.get("enabled", True) is False:
        return None
    changes: dict[str, object] = {}
    for name in _TUNABLE:
        if name in overrides:
            value = overrides[name]
            if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
                raise ValueError(f"{name} must be a non-negative number")
            changes[name] = float(value)
    if "severity" in overrides:
        changes["severity"] = Severity(overrides["severity"])
    return replace(rule, **changes)


@dataclass(frozen=True)
class Observation:
    label: str
    box: Box
    confidence: float
    track_id: int | None
    zone_ids: frozenset[UUID]  # zones containing the object's foot point


@dataclass(frozen=True)
class EventOpened:
    event_id: UUID
    rule: RuleSpec
    started_at: float
    confidence: float
    zone_id: UUID | None
    track_id: int | None
    boxes: tuple[Box, ...]
    continues: UUID | None = None  # previous event when a long event is split


@dataclass(frozen=True)
class EventClosed:
    event_id: UUID
    rule: RuleSpec
    started_at: float
    ended_at: float
    confidence: float


RuleOutput = EventOpened | EventClosed


@dataclass
class _State:
    first_seen: float
    last_seen: float
    confidence: float = 0.0
    boxes: tuple[Box, ...] = ()
    event_id: UUID | None = None
    started_at: float = 0.0


_Key = tuple[int | None, UUID | None]


def _cooldown_key(rule: RuleSpec, key: _Key) -> _Key:
    return key if rule.cooldown_by is GroupBy.TRACK else (None, key[1])


@dataclass
class _RuleState:
    rule: RuleSpec
    states: dict[_Key, _State] = field(default_factory=dict)
    cooldown_until: dict[_Key, float] = field(default_factory=dict)


class RuleEngine:
    def __init__(
        self,
        rules: Iterable[RuleSpec],
        zones: Sequence[ZoneConfig],
        schedule: BranchSchedule,
        max_event_seconds: float = 120,
    ) -> None:
        self._rules = [_RuleState(r) for r in rules]
        self._zone_types = {z.id: z.type for z in zones}
        self._zone_boxes = {
            z.id: Box(
                min(p[0] for p in z.points),
                min(p[1] for p in z.points),
                max(p[0] for p in z.points),
                max(p[1] for p in z.points),
            )
            for z in zones
        }
        self.schedule = schedule
        self.max_event_seconds = max_event_seconds

    @property
    def rules(self) -> list[RuleSpec]:
        return [rs.rule for rs in self._rules]

    @property
    def labels(self) -> frozenset[str]:
        """Labels the rules need, so only those detectors run."""
        return frozenset().union(*(rs.rule.labels for rs in self._rules))

    def open_events(self) -> list[UUID]:
        return [
            s.event_id for rs in self._rules for s in rs.states.values() if s.event_id is not None
        ]

    def update(self, ts: float, observations: Sequence[Observation]) -> list[RuleOutput]:
        outputs: list[RuleOutput] = []
        is_open = self.schedule.is_open(datetime.fromtimestamp(ts, timezone.utc))
        for rs in self._rules:
            rule = rs.rule
            active = (
                rule.when is When.ALWAYS
                or (rule.when is When.AFTER_HOURS and not is_open)
                or (rule.when is When.OPEN_HOURS and is_open)
            )
            if not active:
                outputs.extend(self._close_all(rs))
                continue
            seen = self._group(rule, observations)
            for key, (confidence, boxes) in seen.items():
                state = rs.states.get(key)
                if state is None:
                    state = rs.states[key] = _State(first_seen=ts, last_seen=ts)
                state.last_seen = ts
                state.confidence = max(state.confidence, confidence)
                state.boxes = boxes
                outputs.extend(self._advance(rs, key, state, ts))
            for key, state in list(rs.states.items()):
                if key not in seen and ts - state.last_seen > rule.max_gap_s:
                    del rs.states[key]
                    if state.event_id is not None:
                        outputs.append(self._closed(rs, key, state, state.last_seen))
        return outputs

    def close_all(self) -> list[RuleOutput]:
        """Close every open event (ended when last seen), e.g. on config change or shutdown."""
        outputs: list[RuleOutput] = []
        for rs in self._rules:
            outputs.extend(self._close_all(rs))
        return outputs

    def _group(
        self, rule: RuleSpec, observations: Sequence[Observation]
    ) -> dict[_Key, tuple[float, tuple[Box, ...]]]:
        seen: dict[_Key, tuple[float, tuple[Box, ...]]] = {}
        matching = [
            o
            for o in observations
            if o.label in rule.labels and o.confidence >= rule.min_confidence
        ]
        if rule.absent:
            occupied = {z for o in matching for z in o.zone_ids}
            for zone_id, zone_type in self._zone_types.items():
                if rule.zone_types and zone_type in rule.zone_types and zone_id not in occupied:
                    seen[(None, zone_id)] = (1.0, (self._zone_boxes[zone_id],))
            return seen
        for obs in matching:
            if rule.zone_types is None:
                zone_ids: Iterable[UUID | None] = [None]
            else:
                zone_ids = [z for z in obs.zone_ids if self._zone_types.get(z) in rule.zone_types]
            track = obs.track_id if rule.group_by is GroupBy.TRACK else None
            for zone_id in zone_ids:
                confidence, boxes = seen.get((track, zone_id), (0.0, ()))
                seen[(track, zone_id)] = (max(confidence, obs.confidence), (*boxes, obs.box))
        if rule.min_count > 1:
            seen = {k: v for k, v in seen.items() if len(v[1]) >= rule.min_count}
        return seen

    def _advance(self, rs: _RuleState, key: _Key, state: _State, ts: float) -> list[RuleOutput]:
        rule = rs.rule
        if state.event_id is None:
            held = ts - state.first_seen >= rule.min_duration_s
            if held and ts >= rs.cooldown_until.get(_cooldown_key(rule, key), float("-inf")):
                return [self._open(rule, key, state, state.first_seen)]
            return []
        if ts - state.started_at < self.max_event_seconds:
            return []
        # Split long events so each clip stays short; the next one continues immediately.
        closed = EventClosed(state.event_id, rule, state.started_at, ts, state.confidence)
        return [closed, self._open(rule, key, state, ts, continues=state.event_id)]

    def _open(
        self,
        rule: RuleSpec,
        key: _Key,
        state: _State,
        started_at: float,
        continues: UUID | None = None,
    ) -> EventOpened:
        state.event_id = uuid.uuid4()
        state.started_at = started_at
        return EventOpened(
            state.event_id,
            rule,
            started_at,
            state.confidence,
            zone_id=key[1],
            track_id=key[0],
            boxes=state.boxes,
            continues=continues,
        )

    def _closed(self, rs: _RuleState, key: _Key, state: _State, ended_at: float) -> EventClosed:
        assert state.event_id is not None  # noqa: S101
        rs.cooldown_until[_cooldown_key(rs.rule, key)] = ended_at + rs.rule.cooldown_s
        return EventClosed(state.event_id, rs.rule, state.started_at, ended_at, state.confidence)

    def _close_all(self, rs: _RuleState) -> list[RuleOutput]:
        outputs: list[RuleOutput] = [
            self._closed(rs, key, state, state.last_seen)
            for key, state in rs.states.items()
            if state.event_id is not None
        ]
        rs.states.clear()
        return outputs
