"""What the planner decides for the robot's nodePolicy (docs/node_policies_2026-10-13.md).

* `corridor_per_side` / `leg_widths` / `annotate_corridors`: the clear width around the edges
  N-1 -> N -> N+1 of a waypoint, from the scene graph, as the `corridorWidth` of a skippable node.
* `insert_planned`: waypoints the planner adds between two of the operator's waypoints that are not
  in the same or neighbouring chart, so the robot always has a clear waypoint route.

Pure functions over the public scene-graph API (`snap`, `route`): no I/O, no engine import. Every
failure to know a width means "not skippable", never an error.
"""
import math
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from cloud_common.objects import common

Pt = Tuple[float, float]

UNAVAILABLE_ROBOT_WIDTH = "robot_width_unknown"
UNAVAILABLE_WIDTH = "width_unknown"
UNAVAILABLE_NO_GRAPH = "no_scene_graph"
UNAVAILABLE_LEGACY = "legacy_planner"
UNAVAILABLE_DISABLED = "disabled"


def corridor_per_side(width: Optional[float], robot_width: Optional[float],
                      margin: float = 0.0) -> Optional[float]:
    """The `corridorWidth` (per side of the edge) for a passage of full physical width `width`:
    the room the robot's centre line has on each side, (width - robot_width - margin) / 2,
    floored to 1 cm. None when either width is unknown or no room is left."""
    if width is None or robot_width is None:
        return None
    if not (math.isfinite(width) and math.isfinite(robot_width)):
        return None
    room = (width - robot_width - max(0.0, margin)) / 2.0
    if not math.isfinite(room) or room <= 0:
        return None
    floored = math.floor(room * 100.0 + 1e-9) / 100.0
    return floored if floored > 0 else None


def leg_widths(graph: Any, pts: Sequence[Pt], snap_m: float, budget_s: float = 0.0,
               clock: Callable[[], float] = time.monotonic) -> List[Optional[float]]:
    """The passage width of each leg pts[i] -> pts[i+1] (len(pts) - 1 entries): `Route.min_width`
    of the engine's route between the two snapped points, None when a point does not snap, there
    is no route, the graph cannot answer, or the time budget (`budget_s` > 0) ran out. Each leg
    is routed once."""
    out: List[Optional[float]] = []
    end = clock() + budget_s if budget_s > 0 else None
    for a, b in zip(pts[:-1], pts[1:]):
        if end is not None and clock() > end:
            out.append(None)
            continue
        out.append(_leg_width(graph, a, b, snap_m))
    return out


def _leg_width(graph: Any, a: Pt, b: Pt, snap_m: float) -> Optional[float]:
    try:
        sa = graph.snap(a[0], a[1], snap_m)
        sb = graph.snap(b[0], b[1], snap_m)
        if sa.key is None or sb.key is None:
            return None
        route = graph.route(sa.key, sb.key, None, start=(sa.x, sa.y), goal=(sb.x, sb.y))
        if route is None:
            return None
        width = float(route.min_width)
        return width if math.isfinite(width) and width > 0 else None
    except Exception:  # pylint: disable=broad-except
        return None


def annotate_corridors(graph: Any, poses: List[common.Pose2D], candidates: Iterable[int],
                       robot_width: Optional[float], margin: float, snap_m: float,
                       budget_s: float = 0.0, keep_intent: bool = False) -> Dict[str, Any]:
    """Make the `candidates` (waypoint indexes that should be skippable) skippable where the scene
    graph says how much room there is: sets `skippable` and `corridor_width_m` on those poses. A
    waypoint that already carries its own `corridor_width_m` keeps it. The last waypoint, and a
    waypoint without both adjacent legs (the first one: the edge before it starts at the robot,
    which the route does not know), cannot get a computed corridor. A candidate without a corridor
    becomes `skippable=False`, except an operator's waypoint with `keep_intent`: it keeps its
    `skippable=True` (the editor shows what the operator asked; the dispatcher sends it not
    skippable and logs it). Returns
    {"total": candidates, "skippable": how many are, "unavailable": reason or None}."""
    last = len(poses) - 1
    wanted = sorted({i for i in candidates if 0 <= i < last})
    # Only the legs next to a candidate that has no corridor of its own are routed.
    need: Set[int] = set()
    for i in wanted:
        if not (poses[i].corridor_width_m or 0) > 0 and i > 0:
            need.update((i - 1, i))
    widths: Dict[int, Optional[float]] = {}
    if need and robot_width is not None and robot_width > 0:
        end = time.monotonic() + budget_s if budget_s > 0 else None
        for leg in sorted(need):
            if end is not None and time.monotonic() > end:
                widths[leg] = None
                continue
            widths[leg] = _leg_width(graph, (poses[leg].x, poses[leg].y),
                                     (poses[leg + 1].x, poses[leg + 1].y), snap_m)
    reason: Optional[str] = None
    done = 0
    for i in wanted:
        pose = poses[i]
        corridor = pose.corridor_width_m if (pose.corridor_width_m or 0) > 0 else None
        if corridor is None:
            if robot_width is None or not robot_width > 0:
                reason = UNAVAILABLE_ROBOT_WIDTH
            else:
                before, after = widths.get(i - 1), widths.get(i)
                if i > 0 and before is not None and after is not None:
                    corridor = corridor_per_side(min(before, after), robot_width, margin)
                if corridor is None:
                    reason = reason or UNAVAILABLE_WIDTH
        if corridor is not None or not keep_intent or pose.origin == common.WAYPOINT_ORIGIN_PLANNED:
            pose.skippable = corridor is not None
        pose.corridor_width_m = corridor
        done += corridor is not None
    return {"total": len(wanted), "skippable": done, "unavailable": reason}


def _bearing(a: Pt, b: Pt) -> float:
    return math.atan2(b[1] - a[1], b[0] - a[0])


def insert_planned(graph: Any, poses: List[common.Pose2D], visible: Callable[[Pt, Pt], bool],
                   max_gap: float, snap_m: float, simplify: Callable[..., List[Pt]]
                   ) -> Tuple[List[common.Pose2D], int]:
    """`poses` with planned waypoints between every two consecutive ones that are not in the same
    or a neighbouring chart. The planned points come from the engine's route between the two, cut
    (line of sight, gap cap) so that each consecutive pair touches at most two charts; they are
    `origin="planned"`, `skippable=True` (the caller adds the corridors) and take no field of
    the operator's waypoints. Pairs that do not snap or route are left as they are. Returns
    (new list, number inserted); the input is not modified."""
    out: List[common.Pose2D] = []
    inserted = 0
    for k, pose in enumerate(poses):
        out.append(pose)
        if k + 1 >= len(poses):
            break
        extra = _between(graph, pose, poses[k + 1], visible, max_gap, snap_m, simplify)
        for j, (x, y) in enumerate(extra):
            nxt = extra[j + 1] if j + 1 < len(extra) else (poses[k + 1].x, poses[k + 1].y)
            out.append(common.Pose2D(
                x=x, y=y, theta=_bearing((x, y), nxt), map_id=pose.map_id,
                origin=common.WAYPOINT_ORIGIN_PLANNED, skippable=True))
        inserted += len(extra)
    return out, inserted


def _between(graph: Any, a: common.Pose2D, b: common.Pose2D,
             visible: Callable[[Pt, Pt], bool], max_gap: float, snap_m: float,
             simplify: Callable[..., List[Pt]]) -> List[Pt]:
    try:
        sa = graph.snap(a.x, a.y, snap_m)
        sb = graph.snap(b.x, b.y, snap_m)
        if sa.key is None or sb.key is None or sa.key == sb.key:
            return []
        route = graph.route(sa.key, sb.key, None, start=(sa.x, sa.y), goal=(sb.x, sb.y))
        if route is None or len(set(route.charts)) <= 2:
            return []
        nodes = [tuple(p) for p in route.nodes]
        owners = [graph.snap(x, y).key for (x, y) in nodes]
    except Exception:  # pylint: disable=broad-except
        return []
    if len(nodes) < 3 or len(set(owners)) <= 2:
        return []
    index = {p: i for i, p in enumerate(nodes)}

    def window_ok(p: Pt, q: Pt) -> bool:
        i, k = index.get(p), index.get(q)
        if i is None or k is None:
            return False
        lo, hi = (i, k) if i <= k else (k, i)
        return len(set(owners[lo:hi + 1])) <= 2

    kept = simplify(nodes, lambda p, q: window_ok(p, q) and visible(p, q), max_gap)
    return [tuple(p) for p in kept[1:-1]]
