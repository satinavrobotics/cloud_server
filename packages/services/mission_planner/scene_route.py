"""Turn a scene-graph lattice route into the planner's plain waypoint poses.

Pure functions, no I/O and no engine import: the planner supplies the line-of-sight test and the
nearest-leaf lookup. The output contract is the one the dispatcher, leg tracker and edge statistics
already rely on (docs/scene_graph/integration.md, "Planner contract"):

* waypoints are `Pose2D` in the map frame, `theta` = bearing to the next waypoint, the last one the
  goal heading;
* `node_id` = the nearest leaf within a threshold, else None;
* `planned_path` is returned only when every waypoint has a leaf id, because the leg tracker pairs
  `waypoints[i]` with `planned_path[i]` and drops topomap ids on any length mismatch.
"""
import math
from typing import Any, Callable, List, Optional, Sequence, Tuple

from cloud_common.objects import common

Pt = Tuple[float, float]

PLANNER_SCENE_GRAPH = "scene_graph"
PLANNER_LEGACY = "legacy"


def simplify(points: Sequence[Pt], visible: Callable[[Pt, Pt], bool],
             max_gap: float) -> List[Pt]:
    """Greedy string pulling: from each kept point go to the farthest later point that is visible
    from it and at most `max_gap` away (straight line). The first and last points are always kept;
    with `max_gap` <= 0 no cap applies. Consecutive duplicates are dropped. The candidates of a
    step are the points up to the first one beyond `max_gap`, so the cost is O(points x window),
    not O(points^2): a 1 km route on a 0.1 m lattice has 10 000 points."""
    pts: List[Pt] = []
    for p in points:
        if not pts or p != pts[-1]:
            pts.append(p)
    if len(pts) <= 2:
        return pts
    kept = [pts[0]]
    i, last = 0, len(pts) - 1
    while i < last:
        best = i + 1                       # the next lattice point is always reachable
        for k in range(i + 2, last + 1):
            if max_gap > 0 and math.dist(pts[i], pts[k]) > max_gap:
                break
            if visible(pts[i], pts[k]):
                best = k
        kept.append(pts[best])
        i = best
    return kept


def build_waypoints(points: Sequence[Pt], map_id: str,
                    node_of: Callable[[float, float], Optional[str]],
                    goal_theta: Optional[float] = None
                    ) -> Tuple[List[common.Pose2D], Optional[List[str]]]:
    """(waypoints, planned_path or None) for an already simplified point list.
    `planned_path` is None unless every waypoint has a distinct leaf id."""
    pts = list(points)
    poses: List[common.Pose2D] = []
    for i, (x, y) in enumerate(pts):
        if i + 1 < len(pts):
            nx, ny = pts[i + 1]
            theta = math.atan2(ny - y, nx - x)
        elif goal_theta is not None:
            theta = goal_theta
        elif i > 0:
            theta = poses[-1].theta
        else:
            theta = 0.0
        poses.append(common.Pose2D(x=x, y=y, theta=theta, map_id=map_id, node_id=node_of(x, y)))
    ids = [p.node_id for p in poses]
    # one id per waypoint, no repeats: two waypoints snapped to the same leaf would be a zero-length
    # leg for the leg tracker, so such a route carries no planned_path (its legs are VDA-id only).
    planned = ([str(n) for n in ids]
               if poses and all(n is not None for n in ids) and len(set(ids)) == len(ids) else None)
    return poses, planned


# Why a start or goal cannot be used (failed_at "find_robot_node" / "find_target_node" text).
SNAP_NO_SPACE = "no_navigable_space"
SNAP_TOO_FAR = "too_far"
SNAP_CROSSES_BLOCKED = "crosses_blocked"


def snap_problem(snap: Any, max_dist: float) -> Optional[str]:
    """None when `snap` (the library's Snap: key, distance, crosses_blocked) may be used, else the
    reason. The planner never moves a point across a known blocked cell or further than `max_dist`
    (a start is the robot's own position: small; a goal may be tapped in a wall: a little larger)."""
    if getattr(snap, "key", None) is None or not math.isfinite(getattr(snap, "distance", math.inf)):
        return SNAP_NO_SPACE if not math.isfinite(getattr(snap, "distance", math.inf)) else SNAP_TOO_FAR
    if snap.distance > max_dist:
        return SNAP_TOO_FAR
    if getattr(snap, "crosses_blocked", False):
        return SNAP_CROSSES_BLOCKED
    return None
