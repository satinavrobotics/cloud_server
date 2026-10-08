"""Which missions a robot is working on and which wait for it, derived from mission rows.

The API's mission watcher (ApiDelegationService._handle_mission_updates) feeds every
mission row it sees into this index: a row that is PENDING or RUNNING and ALIVE is held, any
other (finished, deleted, moved to another robot) is dropped. Nothing is stored beyond the
rows, so the answer cannot drift from them: the watcher also resyncs every row about once a
minute. Reading it is a dict lookup, cheap enough for the robot WebSocket (one robot_update
per robot state message).

Order is the dispatcher's: a mission that already started goes first (by start_timestamp), then
by creation (spec.created_at, rows without one last), then by name
(packages/database/postgres.py::PostgresWatcher.resync_query).
"""
import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

_FAR = datetime.datetime.max.replace(tzinfo=None)


def _naive(value: Optional[datetime.datetime]) -> Optional[datetime.datetime]:
    return value.replace(tzinfo=None) if value is not None else None


def dispatch_key(mission: Any) -> Tuple:
    """Sort key of the dispatcher's queue order for a mission object."""
    started = _naive(mission.status.start_timestamp)
    created = _naive(getattr(mission, "created_at", None))
    return (started is None, started or _FAR, created is None, created or _FAR, mission.name)


class RobotMissionIndex:
    def __init__(self) -> None:
        self._by_name: Dict[str, Tuple[str, Tuple, bool]] = {}   # name -> (robot, key, running)

    def update(self, mission: Any) -> None:
        """Take a mission row (or its deletion) into account."""
        name = mission.name
        lifecycle = getattr(mission.lifecycle, "value", mission.lifecycle)
        state = getattr(mission.status.state, "value", mission.status.state)
        if lifecycle != "ALIVE" or state not in ("PENDING", "RUNNING"):
            self._by_name.pop(name, None)
            return
        self._by_name[name] = (mission.robot, dispatch_key(mission), state == "RUNNING")

    def view(self, robot_name: str) -> Dict[str, Any]:
        """`current_mission` (the RUNNING mission's name or None) and `queued_missions`
        (the PENDING missions' names, in dispatch order)."""
        mine: List[Tuple[Tuple, str, bool]] = sorted(
            (key, name, running) for name, (robot, key, running) in self._by_name.items()
            if robot == robot_name)
        current = next((name for _, name, running in mine if running), None)
        return {"current_mission": current,
                "queued_missions": [name for _, name, running in mine if not running]}


def mission_ahead(missions: Iterable[Any], mission_name: str) -> Optional[str]:
    """Of `missions` (rows, any robot), the name of the one directly ahead of `mission_name` in
    its robot's queue: an ALIVE PENDING or RUNNING mission of the same robot that the
    dispatcher takes first. None when nothing is ahead of it (or it is not queued)."""
    queue = [m for m in missions
             if getattr(m.lifecycle, "value", m.lifecycle) == "ALIVE" and
             getattr(m.status.state, "value", m.status.state) in ("PENDING", "RUNNING")]
    me = next((m for m in queue if m.name == mission_name), None)
    if me is None:
        return None
    ahead = sorted((dispatch_key(m), m.name) for m in queue
                   if m.robot == me.robot and m.name != me.name and
                   dispatch_key(m) < dispatch_key(me))
    return ahead[-1][1] if ahead else None
