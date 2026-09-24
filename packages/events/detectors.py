"""Pure change detectors. They hold one signal's state for one robot and never
read the clock: every timestamp is passed in by the caller.

A detector constructed without a known previous state treats its first sample
as a silent baseline, so a restart does not emit spurious events. Hosts seed
the previous state from `robot_latest` (rehydration) to keep continuity.
"""

import dataclasses
import datetime
import enum
from typing import Any, FrozenSet, Hashable, Iterable, Optional


@dataclasses.dataclass(frozen=True)
class Change:
    old: Any
    new: Any


class StateDiff:
    """Emits a Change whenever the observed value differs from the previous one."""

    _UNSET = object()

    def __init__(self, initial: Any = _UNSET):
        self._value = initial

    @property
    def value(self) -> Any:
        return None if self._value is self._UNSET else self._value

    def update(self, value: Any) -> Optional[Change]:
        old = self._value
        self._value = value
        if old is self._UNSET or old == value:
            return None
        return Change(old, value)


@dataclasses.dataclass(frozen=True)
class SetChange:
    added: FrozenSet[Hashable]
    removed: FrozenSet[Hashable]


class SetDiff:
    """Emits the members added to and removed from a set between samples."""

    def __init__(self, initial: Optional[Iterable[Hashable]] = None):
        self._members = None if initial is None else frozenset(initial)

    @property
    def members(self) -> FrozenSet[Hashable]:
        return self._members or frozenset()

    def update(self, members: Iterable[Hashable]) -> Optional[SetChange]:
        new = frozenset(members)
        old = self._members
        self._members = new
        if old is None or old == new:
            return None
        return SetChange(added=new - old, removed=old - new)


class Direction(str, enum.Enum):
    ABOVE = "above"
    BELOW = "below"


class Transition(str, enum.Enum):
    ENTERED = "entered"
    EXITED = "exited"


class Hysteresis:
    """Two-threshold alarm. Both thresholds are inclusive.

    ABOVE (e.g. thermal 85/78): active once value >= enter, inactive once value <= exit.
    BELOW (e.g. battery 20/25): active once value <= enter, inactive once value >= exit.
    A first sample with unknown state sets the state silently; a first sample
    inside the dead band counts as inactive. None samples are ignored.
    """

    def __init__(self, enter: float, exit: float, direction: Direction,
                 active: Optional[bool] = None):
        direction = Direction(direction)
        if direction is Direction.ABOVE and not exit < enter:
            raise ValueError("ABOVE hysteresis needs exit < enter")
        if direction is Direction.BELOW and not exit > enter:
            raise ValueError("BELOW hysteresis needs exit > enter")
        self.enter = enter
        self.exit = exit
        self.direction = direction
        self._active = active

    @property
    def active(self) -> Optional[bool]:
        return self._active

    def _entering(self, value: float) -> bool:
        return value >= self.enter if self.direction is Direction.ABOVE else value <= self.enter

    def _exiting(self, value: float) -> bool:
        return value <= self.exit if self.direction is Direction.ABOVE else value >= self.exit

    def update(self, value: Optional[float]) -> Optional[Transition]:
        if value is None:
            return None
        if self._active is None:
            self._active = self._entering(value)
            return None
        if not self._active and self._entering(value):
            self._active = True
            return Transition.ENTERED
        if self._active and self._exiting(value):
            self._active = False
            return Transition.EXITED
        return None


@dataclasses.dataclass(frozen=True)
class Lost:
    last_seen: datetime.datetime


@dataclasses.dataclass(frozen=True)
class Restored:
    last_seen: datetime.datetime
    gap_s: float


class Timeout:
    """Heartbeat watchdog.

    `seen(ts)` records a message; `check(now)` is called by a periodic sweep.
    Lost fires once when now - last_seen > timeout_s (strictly greater); the next
    `seen` after that fires Restored. Out-of-order `seen` timestamps are ignored.
    """

    def __init__(self, timeout_s: float, last_seen: Optional[datetime.datetime] = None,
                 lost: bool = False):
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        self.timeout = datetime.timedelta(seconds=timeout_s)
        self._last_seen = last_seen
        self._lost = lost

    @property
    def last_seen(self) -> Optional[datetime.datetime]:
        return self._last_seen

    @property
    def lost(self) -> bool:
        return self._lost

    def seen(self, ts: datetime.datetime) -> Optional[Restored]:
        previous = self._last_seen
        if previous is not None and ts <= previous:
            return None
        self._last_seen = ts
        if not self._lost:
            return None
        self._lost = False
        gap = (ts - previous).total_seconds() if previous is not None else 0.0
        return Restored(last_seen=previous if previous is not None else ts, gap_s=gap)

    def check(self, now: datetime.datetime) -> Optional[Lost]:
        if self._lost or self._last_seen is None:
            return None
        if now - self._last_seen > self.timeout:
            self._lost = True
            return Lost(last_seen=self._last_seen)
        return None
