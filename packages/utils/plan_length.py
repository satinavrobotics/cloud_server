"""The planned length of one leg, from the route's stored plan trajectory. Pure; shared by mission-dispatch
(which records it when the leg ends: packages/controllers/mission/leg_tracker.py) and the API (which works it out
at read time for legs recorded before that: packages/api/path_efficiency.py). Plain math, no numpy.

A route node's `trajectory` (MissionRouteNodeV1, docs/plan_trajectory_2026-10-14.md) holds the planner's dense path
between consecutive waypoints, one entry `{from_index, to_index, points}` per leg it knows. The leg between waypoints
k-1 and k of ONE route node has the entry (k-1, k). Every other leg has none: the first leg of an order (it starts at
the robot, not at a waypoint), a leg across two route nodes, a leg after a reroute (it starts at a waypoint of the old
route), a route without a trajectory (switched off, over its size cap, legacy planner, operator-drawn route).

Rules (`resolve`): a leg with a trajectory entry whose ends are where the leg's ends are is "trajectory" and its
planned length is the polyline; one whose ends are not (the route was replanned after the entry was made) is "stale"
and its planned length is the straight line; a leg without an entry is "straight". The straight line is a lower bound
of the plan, so only "trajectory" legs say anything about detours. None as the source: nothing is known (no straight
line either).
"""
import dataclasses
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

SOURCE_TRAJECTORY = "trajectory"
SOURCE_STRAIGHT = "straight"
SOURCE_STALE = "stale"
SOURCES = (SOURCE_TRAJECTORY, SOURCE_STRAIGHT, SOURCE_STALE)


def polyline_m(points: Sequence[Sequence[float]]) -> float:
    """Length of the polyline through the [x, y] points (0 for fewer than two)."""
    return float(sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(points, points[1:])))


@dataclasses.dataclass
class Trajectory:
    """The stored plan of one route node: waypoints (x, y) and the trajectory legs by (from, to) index."""
    waypoints: List[Tuple[float, float]]
    legs: Dict[Tuple[int, int], List[List[float]]]


def parse_trajectory(waypoints: Any, trajectory: Any) -> Optional[Trajectory]:
    """A Trajectory from [x, y] waypoints and [{from_index, to_index, points}] entries (dicts); None when there
    is no usable entry or the data is malformed."""
    try:
        wps = [(float(w[0]), float(w[1])) for w in waypoints or []]
        legs = {(int(t["from_index"]), int(t["to_index"])): t["points"] for t in trajectory or []
                if len(t["points"]) >= 2}
    except (TypeError, ValueError, KeyError, IndexError):
        return None
    return Trajectory(wps, legs) if legs else None


def ends_match(points: Sequence[Sequence[float]], start: Tuple[float, float], end: Tuple[float, float],
               tol_m: float) -> bool:
    """Whether the path starts within `tol_m` of `start` and ends within `tol_m` of `end` (metres, Euclidean)."""
    return (math.hypot(points[0][0] - start[0], points[0][1] - start[1]) <= tol_m
            and math.hypot(points[-1][0] - end[0], points[-1][1] - end[1]) <= tol_m)


def resolve(points: Optional[Sequence[Sequence[float]]], straight_m: Optional[float], matches: bool
            ) -> Tuple[Optional[float], Optional[str]]:
    """(planned metres, source) of a leg. `points` is the leg's trajectory entry (None: it has none), `matches`
    whether its ends are the leg's ends. Without a straight line the leg has no plan at all: (None, None)."""
    if straight_m is None:
        return None, None
    if points is None:
        return float(straight_m), SOURCE_STRAIGHT
    if matches:
        return polyline_m(points), SOURCE_TRAJECTORY
    return float(straight_m), SOURCE_STALE


def plan_of(points: Optional[Sequence[Sequence[float]]], start: Optional[Tuple[float, float]],
            end: Optional[Tuple[float, float]], straight_m: Optional[float], tol_m: float
            ) -> Tuple[Optional[float], Optional[str]]:
    """`resolve` with the stale rule applied: THE endpoint rule (the entry's first point within `tol_m` of the
    leg's start AND its last of the leg's end, `ends_match`). `start` / `end` are the (x, y) the leg runs
    between, None when the leg has no waypoint pose there (then an entry cannot match)."""
    matches = (points is not None and start is not None and end is not None
               and ends_match(points, start, end, tol_m))
    return resolve(points, straight_m, matches)


def plan_of_snapshot(points: Optional[Sequence[Sequence[float]]], start: Optional[Tuple[float, float]],
                     end: Optional[Tuple[float, float]], straight_m: Optional[float], tol_m: float
                     ) -> Tuple[Optional[float], Optional[str]]:
    """`plan_of` for a leg recorded before `plan_source` existed, worked out at read time. The leg's own poses were
    not stored, only `straight_m`, so `start` / `end` are the run SNAPSHOT's waypoints, and the endpoint rule can
    only be checked against those. The leg's ends are within `tol_m` of the poses it ran between, so a snapshot
    whose waypoint distance differs from `straight_m` by more than 2 * `tol_m` cannot hold the leg's waypoints:
    stale. Within that the leg is called as the writer would have, except for a reroute that moved both waypoints
    and kept their spacing, which only the writer (it has the poses) can see."""
    if (points is not None and start is not None and end is not None and straight_m is not None
            and abs(math.hypot(end[0] - start[0], end[1] - start[1]) - straight_m) > 2.0 * tol_m):
        return resolve(points, straight_m, False)
    return plan_of(points, start, end, straight_m, tol_m)
