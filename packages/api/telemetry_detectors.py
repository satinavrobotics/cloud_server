"""Pure per-robot detectors for the API's Phase 0 ingest (docs/satinav-fleet-agent-phase0-v2.md
§3.3, §3.4, §5.3 "api" items 3-4).

No I/O and no clock: every timestamp is passed in. `packages/api/telemetry.py` feeds these from
the MQTT handlers in `packages/api/diagnostics.py` and puts what they return on the ingest queue.

`<robot>/diagnostics` (parsed by DiagnosticsService._parse_diagnostics into
{collector: {"level", "values"}}):

- one `diagnostics_ts` row: cpu/ram from host_stats, gpu/temperatures/power from jtop,
  nodes_down. The gnss_* columns are left NULL: GNSS is out of scope for now (owner decision),
  so there is no GNSS.RTK_* detection either.
- SYSTEM.THERMAL_HIGH / THERMAL_OK: hysteresis on the hottest jtop sensor (cpu/gpu/soc),
  85 °C / 78 °C by default, both inclusive.
- SYSTEM.NODE_DOWN / NODE_UP: the robot has no per-node liveness report, so "node" means one of
  the liveness signals the diagnostics already carry: a ros_health source (`esp32`, `gps`,
  `sati_pose`) whose `<source>_stale` flag is set, or a monitored topic in topic_availability
  that does not exist or is not publishing (named by the topic, e.g. `/scan`). The
  discriminator is that name. A block that is absent or empty (collector failed) leaves its
  set unchanged instead of reporting everything as up.

`<robot>/nav_supervisor` (NavSupervisor's SupervisorStatus as JSON):

- NAV.RECOVERY_ENTERED on DRIVE -> RECOVER (payload cause = last_drive_cause) and
  NAV.RECOVERY_EXITED on RECOVER -> anything else (cause from entry, duration_s).
- NAV.GOAL_BLOCKED on the rising edge of `blocked_pending` (cause = last_drive_cause, the
  only cause the robot reports; the hold and goal go into `detail`).

Each detector's state is written into its robot_latest column (`detectors` key) and read back
by `from_latest()` when a worker becomes the writer, so a restart or failover neither repeats
an event nor reports a state that did not change.
"""

import dataclasses
import datetime
import math
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Tuple

from packages.events.codes import EventCode
from packages.events.detectors import Direction, Hysteresis, SetDiff, StateDiff, Transition
from packages.events.emit import Event

UTC = datetime.timezone.utc

DEFAULT_THERMAL_HIGH_C = 85.0
DEFAULT_THERMAL_OK_C = 78.0

ROS_HEALTH_SOURCES = ("esp32", "gps", "sati_pose")
JTOP_TEMPERATURES = (("cpu", "cpu_temp_c"), ("gpu", "gpu_temp_c"), ("soc", "soc_temp_c"))
# jtop reports an offline sensor as -256 °C; anything this cold is not a reading.
_MIN_VALID_TEMP_C = -100.0

# Robot clocks before this (sim time, unsynced RTC) or more than a day ahead are not trusted;
# the receive time is used instead.
_MIN_ROBOT_TS = datetime.datetime(2020, 1, 1, tzinfo=UTC)
_MAX_ROBOT_SKEW = datetime.timedelta(days=1)

STATE_DRIVE = "DRIVE"
STATE_RECOVER = "RECOVER"


# --- helpers -------------------------------------------------------------------------------

def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _plausible(ts: datetime.datetime, received: datetime.datetime) -> bool:
    return _MIN_ROBOT_TS <= ts <= received + _MAX_ROBOT_SKEW


def robot_ts(epoch_s: Any, received: datetime.datetime) -> datetime.datetime:
    """The robot's timestamp (float epoch seconds) as aware UTC, or `received` if unusable."""
    seconds = _number(epoch_s)
    if seconds is not None:
        try:
            ts = datetime.datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):
            ts = None
        if ts is not None and _plausible(ts, received):
            return ts
    return received


def stamp_ts(stamp: Any, received: datetime.datetime) -> datetime.datetime:
    """A ROS `{"sec", "nanosec"}` stamp as aware UTC, or `received` if unusable."""
    if isinstance(stamp, Mapping):
        sec, nanosec = _number(stamp.get("sec")), _number(stamp.get("nanosec"))
        if sec is not None:
            return robot_ts(sec + (nanosec or 0.0) / 1e9, received)
    return received


def _iso(ts: Optional[datetime.datetime]) -> Optional[str]:
    return ts.isoformat() if ts is not None else None


def _parse_iso(value: Any) -> Optional[datetime.datetime]:
    if not isinstance(value, str):
        return None
    try:
        ts = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


def _values(diagnostics: Mapping[str, Any], collector: str) -> Optional[Dict[str, Any]]:
    """A collector's values from the parsed diagnostics, or None if absent or empty."""
    block = diagnostics.get(collector)
    if not isinstance(block, Mapping):
        return None
    values = block.get("values")
    return values if isinstance(values, Mapping) and values else None


def hottest(diagnostics: Mapping[str, Any]) -> Tuple[Optional[float], Optional[str]]:
    """(temperature °C, sensor) of the hottest valid jtop sensor, or (None, None)."""
    jtop = _values(diagnostics, "jtop")
    best: Tuple[Optional[float], Optional[str]] = (None, None)
    if jtop is None:
        return best
    for sensor, key in JTOP_TEMPERATURES:
        temp = _number(jtop.get(key))
        if temp is None or temp < _MIN_VALID_TEMP_C:
            continue
        if best[0] is None or temp > best[0]:
            best = (temp, sensor)
    return best


def stale_sources(diagnostics: Mapping[str, Any]) -> Optional[FrozenSet[str]]:
    """ros_health sources flagged stale; None if the block says nothing about any source."""
    ros_health = _values(diagnostics, "ros_health")
    if ros_health is None:
        return None
    known = [s for s in ROS_HEALTH_SOURCES if f"{s}_stale" in ros_health]
    if not known:
        return None
    return frozenset(s for s in known if ros_health.get(f"{s}_stale") is True)


def down_topics(diagnostics: Mapping[str, Any]) -> Optional[FrozenSet[str]]:
    """Monitored topics that do not exist or are not publishing; None if no block."""
    topics = _values(diagnostics, "topic_availability")
    if topics is None:
        return None
    return frozenset(
        str(topic) for topic, entry in topics.items()
        if not isinstance(entry, Mapping) or not entry.get("exists") or not entry.get("publishing")
    )


def _sorted(members: Optional[FrozenSet[str]]) -> Optional[List[str]]:
    return sorted(members) if members is not None else None


def _frozen(value: Any) -> Optional[FrozenSet[str]]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return None
    return frozenset(value)


# --- diagnostics ---------------------------------------------------------------------------

@dataclasses.dataclass
class DiagnosticsResult:
    events: List[Event]
    row: Dict[str, Any]       # one diagnostics_ts row (tables.DIAGNOSTICS_COLUMNS keys)
    latest: Dict[str, Any]    # robot_latest.diagnostics


class DiagnosticsDetector:
    """Thermal and node detectors for one robot."""

    def __init__(self, robot_name: str, *, thermal_high: Optional[bool] = None,
                 sources_down: Optional[FrozenSet[str]] = None,
                 topics_down: Optional[FrozenSet[str]] = None,
                 high_c: float = DEFAULT_THERMAL_HIGH_C, ok_c: float = DEFAULT_THERMAL_OK_C):
        self.robot_name = robot_name
        self.high_c = high_c
        self.ok_c = ok_c
        self._thermal = Hysteresis(high_c, ok_c, Direction.ABOVE, active=thermal_high)
        self._sources = SetDiff(sources_down)
        self._topics = SetDiff(topics_down)
        self._sources_known = sources_down
        self._topics_known = topics_down

    @classmethod
    def from_latest(cls, robot_name: str, latest: Any, **kwargs) -> "DiagnosticsDetector":
        """Seed from a stored robot_latest.diagnostics value; anything unreadable is unseeded."""
        state = latest.get("detectors") if isinstance(latest, Mapping) else None
        if not isinstance(state, Mapping):
            return cls(robot_name, **kwargs)
        thermal = state.get("thermal_high")
        return cls(robot_name,
                   thermal_high=thermal if isinstance(thermal, bool) else None,
                   sources_down=_frozen(state.get("sources_down")),
                   topics_down=_frozen(state.get("topics_down")),
                   **kwargs)

    def _node_events(self, ts: datetime.datetime, diff: SetDiff,
                     members: Optional[FrozenSet[str]]) -> List[Event]:
        if members is None:
            return []
        change = diff.update(members)
        if change is None:
            return []
        events = [Event(EventCode.SYSTEM_NODE_DOWN, ts, robot_name=self.robot_name,
                        payload={"node": node}, discriminator=node)
                  for node in sorted(change.added)]
        events += [Event(EventCode.SYSTEM_NODE_UP, ts, robot_name=self.robot_name,
                         payload={"node": node}, discriminator=node)
                   for node in sorted(change.removed)]
        return events

    def update(self, ts: datetime.datetime, diagnostics: Mapping[str, Any]) -> DiagnosticsResult:
        events: List[Event] = []
        temp, sensor = hottest(diagnostics)
        transition = self._thermal.update(temp)
        if transition is Transition.ENTERED:
            events.append(Event(EventCode.SYSTEM_THERMAL_HIGH, ts, robot_name=self.robot_name,
                                payload={"temp_c": temp, "threshold_c": self.high_c,
                                         "sensor": sensor}))
        elif transition is Transition.EXITED:
            events.append(Event(EventCode.SYSTEM_THERMAL_OK, ts, robot_name=self.robot_name,
                                payload={"temp_c": temp, "threshold_c": self.ok_c,
                                         "sensor": sensor}))

        sources = stale_sources(diagnostics)
        topics = down_topics(diagnostics)
        events += self._node_events(ts, self._sources, sources)
        events += self._node_events(ts, self._topics, topics)
        if sources is not None:
            self._sources_known = sources
        if topics is not None:
            self._topics_known = topics

        host = _values(diagnostics, "host_stats") or {}
        jtop = _values(diagnostics, "jtop") or {}
        power_mw = _number(jtop.get("power_total_mw"))
        nodes_down = None
        if sources is not None or topics is not None:
            nodes_down = len((sources or frozenset()) | (topics or frozenset()))
        metrics = {
            "cpu": _number(host.get("cpu_percent")),
            "gpu": _number(jtop.get("gpu_percent")),
            "ram": _number(host.get("ram_percent")),
            "temp_max": temp,
            "power_w": power_mw / 1000.0 if power_mw is not None else None,
            "nodes_down": nodes_down,
        }
        row = {"ts": ts, "robot_name": self.robot_name, **metrics}
        latest = {
            "ts": _iso(ts),
            "metrics": {**metrics, "temp_sensor": sensor},
            "levels": {name: block.get("level") for name, block in diagnostics.items()
                       if isinstance(block, Mapping)},
            "diagnostics": {name: block for name, block in diagnostics.items()
                            if name != "topic_listing"},
            "detectors": {
                "thermal_high": self._thermal.active,
                "sources_down": _sorted(self._sources_known),
                "topics_down": _sorted(self._topics_known),
            },
        }
        return DiagnosticsResult(events, row, latest)


# --- nav_supervisor ------------------------------------------------------------------------

@dataclasses.dataclass
class NavSupervisorResult:
    events: List[Event]
    latest: Dict[str, Any]    # robot_latest.nav_supervisor


class NavSupervisorDetector:
    """Recovery and blocked-goal detectors for one robot."""

    def __init__(self, robot_name: str, *, state: Optional[str] = None,
                 recover_since: Optional[datetime.datetime] = None,
                 recover_cause: Optional[str] = None, blocked: Optional[bool] = None):
        self.robot_name = robot_name
        self._state = StateDiff() if state is None else StateDiff(state)
        self._blocked = StateDiff() if blocked is None else StateDiff(blocked)
        self._recover_since = recover_since
        self._recover_cause = recover_cause

    @classmethod
    def from_latest(cls, robot_name: str, latest: Any) -> "NavSupervisorDetector":
        state = latest.get("detectors") if isinstance(latest, Mapping) else None
        if not isinstance(state, Mapping):
            return cls(robot_name)
        nav_state = state.get("state")
        blocked = state.get("blocked")
        cause = state.get("recover_cause")
        return cls(robot_name,
                   state=nav_state if isinstance(nav_state, str) else None,
                   recover_since=_parse_iso(state.get("recover_since")),
                   recover_cause=cause if isinstance(cause, str) else None,
                   blocked=blocked if isinstance(blocked, bool) else None)

    def update(self, ts: datetime.datetime, supervisor: Any) -> NavSupervisorResult:
        events: List[Event] = []
        payload = supervisor if isinstance(supervisor, Mapping) else {}
        cause = payload.get("last_drive_cause")
        cause = str(cause) if cause is not None else None

        nav_state = payload.get("state")
        if isinstance(nav_state, str):
            change = self._state.update(nav_state)
            if change is not None and change.new == STATE_RECOVER:
                self._recover_since, self._recover_cause = ts, cause
                events.append(Event(EventCode.NAV_RECOVERY_ENTERED, ts,
                                    robot_name=self.robot_name, payload={"cause": cause}))
            elif change is not None and change.old == STATE_RECOVER:
                duration = None
                if self._recover_since is not None and ts >= self._recover_since:
                    duration = (ts - self._recover_since).total_seconds()
                events.append(Event(EventCode.NAV_RECOVERY_EXITED, ts,
                                    robot_name=self.robot_name,
                                    payload={"cause": self._recover_cause or cause,
                                             "duration_s": duration}))
                self._recover_since, self._recover_cause = None, None

        blocked = payload.get("blocked_pending")
        if isinstance(blocked, bool):
            change = self._blocked.update(blocked)
            if change is not None and change.new is True:
                events.append(Event(EventCode.NAV_GOAL_BLOCKED, ts, robot_name=self.robot_name,
                                    payload={"cause": cause or "UNATTRIBUTED",
                                             "detail": _blocked_detail(payload)}))

        latest = {
            "ts": _iso(ts),
            "supervisor": dict(payload),
            "detectors": {
                "state": self._state.value,
                "recover_since": _iso(self._recover_since),
                "recover_cause": self._recover_cause,
                "blocked": self._blocked.value,
            },
        }
        return NavSupervisorResult(events, latest)


def _blocked_detail(payload: Mapping[str, Any]) -> str:
    hold = _number(payload.get("blocked_hold_s"))
    x, y = _number(payload.get("goal_x")), _number(payload.get("goal_y"))
    parts = ["blocked-goal hold started"]
    if hold is not None:
        parts.append(f"hold {hold:g}s")
    if x is not None and y is not None:
        parts.append(f"goal ({x:g}, {y:g}) in {payload.get('goal_frame') or '?'}")
    return "; ".join(parts)
