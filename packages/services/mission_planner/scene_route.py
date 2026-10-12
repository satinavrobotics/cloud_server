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
from typing import Callable, List, Optional, Sequence, Tuple

from cloud_common.objects import common

Pt = Tuple[float, float]

PLANNER_SCENE_GRAPH = "scene_graph"
PLANNER_LEGACY = "legacy"


def simplify(points: Sequence[Pt], visible: Callable[[Pt, Pt], bool],
             max_gap: float) -> List[Pt]:
    """Greedy string pulling: from each kept point go to the farthest later point that is visible
    from it and at most `max_gap` away. The first and last points are always kept; with
    `max_gap` <= 0 no cap applies."""
    pts = list(points)
    if len(pts) <= 2:
        return pts
    kept = [pts[0]]
    i = 0
    last = len(pts) - 1
    while i < last:
        j = i + 1
        for k in range(last, i, -1):
            if max_gap > 0 and math.dist(pts[i], pts[k]) > max_gap and k > i + 1:
                continue
            if k == i + 1 or visible(pts[i], pts[k]):
                j = k
                break
        kept.append(pts[j])
        i = j
    return kept


def build_waypoints(points: Sequence[Pt], map_id: str,
                    node_of: Callable[[float, float], Optional[str]],
                    goal_theta: Optional[float] = None
                    ) -> Tuple[List[common.Pose2D], Optional[List[str]]]:
    """(waypoints, planned_path or None) for an already simplified point list."""
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
    planned = [str(n) for n in ids] if poses and all(n is not None for n in ids) else None
    return poses, planned
