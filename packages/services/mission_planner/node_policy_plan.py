"""What the planner decides for the robot's nodePolicy (docs/node_policies_2026-10-13.md).

* `corridor_per_side` / `annotate_corridors`: the clear width around the edges
  N-1 -> N -> N+1 of a waypoint, from the scene graph, as the `corridorWidth` of a skippable node.
* `insert_planned`: waypoints the planner adds between two of the operator's waypoints that are not
  in the same or neighbouring chart, so the robot always has a clear waypoint route.

Pure functions over the public scene-graph API (`snap`, `route`): no I/O, no engine import. Every
failure to know a width means "not skippable", never an error.
"""
import math
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from cloud_common.node_policy import clear_stale_measured, sanitize_width, skip_allowed, skippable_candidates  # noqa: F401
from cloud_common.objects import common
from packages import config

Pt = Tuple[float, float]

UNAVAILABLE_ROBOT_WIDTH = "robot_width_unknown"
UNAVAILABLE_WIDTH = "width_unknown"
UNAVAILABLE_BUDGET = "width_budget"
UNAVAILABLE_NO_GRAPH = "no_scene_graph"


class LegRoutes:
    """What one planning call learned about its legs, so no leg is routed twice and the trajectory costs no route
    call of its own: `nodes[(a, b)]` is the lattice path from waypoint xy `a` to `b` (the engine's route, or the
    piece of a longer one that a planned waypoint cut), `widths[(a, b)]` its `Route.min_width` (None: no route)."""

    def __init__(self) -> None:
        self.nodes: Dict[Tuple[Pt, Pt], List[Pt]] = {}
        self.widths: Dict[Tuple[Pt, Pt], Optional[float]] = {}
        # why a leg (a, b) could not be routed: "outside_graph" (a point does not snap), "unreachable" (no route), "error"
        self.failed: Dict[Tuple[Pt, Pt], str] = {}


def corridor_per_side(width: Optional[float], robot_width: Optional[float],
                      margin: float = 0.0) -> Optional[float]:
    """The `corridorWidth` (per side of the edge) for a passage of full physical width `width`:
    the room the robot's centre line has on each side, (width - robot_width - margin) / 2,
    floored to 1 cm. 0.0 when the passage was measured and leaves no room (the robot then holds
    to the distance rule only); None when either width is unknown."""
    if width is None or robot_width is None:
        return None
    if not (math.isfinite(width) and math.isfinite(robot_width) and math.isfinite(margin)):
        return None
    room = (width - robot_width - max(0.0, margin)) / 2.0
    if not math.isfinite(room) or room <= 0:
        return 0.0
    return max(0.0, math.floor(room * 100.0 + 1e-9) / 100.0)


SAME_CHART = "same_chart"


def _route_leg(graph: Any, a: Pt, b: Pt, snap_m: float, safety: Any = None, blocked: Any = None,
               skip_same_chart: bool = False, region: Any = None) -> Tuple[Any, Any, Any, Optional[str]]:
    """The one place a leg is snapped and routed: (snap a, snap b, Route or None, failure). failure is None for a route,
    "outside_graph" (a point does not snap), "unreachable" (no route), "blocked_by_keepout" (no route with the keep-out
    zones `blocked`, one without them), or SAME_CHART (only with `skip_same_chart`: both ends in one chart, not routed).
    `blocked` (a scene_graph `Blocked`) goes to the route search, so a leg is measured and planned over the same cells; so does
    `region` (a `RegionPricing`, the planner's region cost term), for a graph that takes one.
    Raises what the graph raises; the callers record that as "error"."""
    sa = graph.snap(a[0], a[1], snap_m)
    sb = graph.snap(b[0], b[1], snap_m)
    if sa.key is None or sb.key is None:
        return sa, sb, None, "outside_graph"
    if skip_same_chart and sa.key == sb.key:
        return sa, sb, None, SAME_CHART
    extra = {} if safety is None else {"safety": safety}
    if region is not None:
        extra["region"] = region
    route = graph.route(sa.key, sb.key, blocked or None, start=(sa.x, sa.y), goal=(sb.x, sb.y), **extra)
    if route is not None:
        return sa, sb, route, None
    if blocked:                               # cut off by the keep-out zones alone?
        free = graph.route(sa.key, sb.key, None, start=(sa.x, sa.y), goal=(sb.x, sb.y))
        if free is not None and free.nodes:
            return sa, sb, None, "blocked_by_keepout"
    return sa, sb, None, "unreachable"


def _leg_width(graph: Any, a: Pt, b: Pt, snap_m: float, safety: Any = None,
               routes: Optional[LegRoutes] = None, blocked: Any = None, region: Any = None) -> Optional[float]:
    key = ((float(a[0]), float(a[1])), (float(b[0]), float(b[1])))
    if routes is not None and key in routes.widths:
        return routes.widths[key]
    try:
        _, _, route, failure = _route_leg(graph, a, b, snap_m, safety, blocked, region=region)
        if route is None:
            if routes is not None:
                routes.failed.setdefault(key, failure or "unreachable")
            return None
        width = sanitize_width(route.min_width)
        if routes is not None:
            routes.widths[key] = width
            if route.nodes and key not in routes.nodes:
                routes.nodes[key] = [tuple(p) for p in route.nodes]
        return width
    except Exception:  # pylint: disable=broad-except
        if routes is not None:
            routes.failed.setdefault(key, "error")
        return None


def _operator_corridor(pose: common.Pose2D) -> bool:
    """Whether the waypoint's corridor is the operator's, to be used as it is. A measured one
    describes the place the waypoint had when it was measured, so it is measured again; a
    corridor with no source (an operator's typed value, or a route stored before sources
    existed) counts as the operator's."""
    return pose.corridor_width_m is not None and \
        pose.corridor_source != common.CORRIDOR_SOURCE_MEASURED


def annotate_corridors(graph: Any, poses: List[common.Pose2D], candidates: Iterable[int],
                       robot_width: Optional[float], margin: float, snap_m: float,
                       budget_s: float = 0.0, keep_intent: bool = False, safety: Any = None,
                       routes: Optional[LegRoutes] = None, blocked: Any = None, region: Any = None) -> Dict[str, Any]:
    """Give every waypoint the robot drives through (all but the last) its `corridor_width_m`
    from the scene graph, and make the `candidates` (waypoint indexes that should be skippable)
    skippable where there is room. The corridor is the gate the robot may cross, so operator
    waypoints get one too; only `skippable` is limited to the candidates.

    The corridor of waypoint i is the narrower of the legs i-1 -> i and i -> i+1; waypoint 0 has
    only the leg 0 -> 1 (the edge before it starts at the robot, which the route does not know).
    A waypoint with the operator's `corridor_width_m` (any value, 0 included: see
    `_operator_corridor`) keeps it; one measured earlier is measured again. A leg that cannot be
    measured leaves the corridor None (unknown), except that a measured 0.0 (no room) is never
    lost to a failed re-measure; a passage with no room for the robot
    gives 0.0 (measured, never skippable). A candidate without a skippable corridor (`skip_allowed`) becomes
    `skippable=False`, except an operator's waypoint with `keep_intent`: it keeps its
    `skippable=True` (the editor shows what the operator asked; the dispatcher sends it not
    skippable and logs it). Returns {"total": candidates, "skippable": how many are,
    "unavailable": reason a width could not be known or None, "corridor": {"measured": n > 0,
    "narrow": n at 0, "unknown": n None} over all annotated waypoints}. With `routes`, a leg
    already routed is not routed again, and the legs routed here are added to it. `blocked` (keep-out zones) goes to
    every leg's route search, as in `insert_planned`, and so does `region`. `skippable` needs `skip_allowed(corridor)`: a corridor of
    0 < c <= NODE_POLICY_SKIP_MIN_CORRIDOR_M is kept as the gate but not skippable. An operator's corridor above
    NODE_POLICY_CORRIDOR_CAP_M is capped silently when stored."""
    last = len(poses) - 1
    wanted = {i for i in candidates if 0 <= i < last}
    interior = list(range(0, last))
    # Only the legs next to a waypoint that has no corridor of its own are routed.
    is_operator = {i: _operator_corridor(poses[i]) for i in interior}
    need: Set[int] = set()
    for i in interior:
        if not is_operator[i]:
            need.update(leg for leg in (i - 1, i) if 0 <= leg < last)
    widths: Dict[int, Optional[float]] = {}
    cut: Set[int] = set()                  # legs not measured because the time budget ran out
    if need and robot_width is not None and robot_width > 0:
        end = time.monotonic() + budget_s if budget_s > 0 else None
        for leg in sorted(need):
            if end is not None and time.monotonic() > end:
                widths[leg] = None
                cut.add(leg)
                continue
            widths[leg] = _leg_width(graph, (poses[leg].x, poses[leg].y),
                                     (poses[leg + 1].x, poses[leg + 1].y), snap_m, safety, routes, blocked, region)
    reason: Optional[str] = None
    done = 0
    counts = {"measured": 0, "narrow": 0, "unknown": 0}
    for i in interior:
        pose = poses[i]
        operator = is_operator[i]
        corridor: Optional[float] = pose.corridor_width_m if operator else None
        if not operator:
            if robot_width is None or not robot_width > 0:
                reason = reason or UNAVAILABLE_ROBOT_WIDTH
            else:
                legs = [widths.get(leg) for leg in (i - 1, i) if 0 <= leg < last]
                if legs and all(w is not None for w in legs):
                    corridor = corridor_per_side(min(legs), robot_width, margin)
                if corridor is None:
                    cut_here = any(leg in cut for leg in (i - 1, i))
                    reason = reason or (UNAVAILABLE_BUDGET if cut_here else UNAVAILABLE_WIDTH)
        # A failed measurement never overwrites a known "measured, no room" (0.0): wherever the
        # waypoint now is, no gate is the safe side. A positive one is not kept: it may be for
        # a place the waypoint has left.
        if corridor is None and pose.corridor_width_m == 0 and not operator:
            corridor = 0.0
        counts["unknown" if corridor is None else "measured" if corridor > 0 else "narrow"] += 1
        if i in wanted:
            usable = skip_allowed(corridor)
            if usable or not keep_intent or pose.origin == common.WAYPOINT_ORIGIN_PLANNED:
                pose.skippable = usable
            done += usable
        # stored capped, as the wire carries it: every reader of the stored width sees the same value
        pose.corridor_width_m = None if corridor is None else min(corridor, config.NODE_POLICY_CORRIDOR_CAP_M)
        pose.corridor_source = common.CORRIDOR_SOURCE_OPERATOR if operator else \
            common.CORRIDOR_SOURCE_MEASURED if corridor is not None else None
    return {"total": len(wanted), "skippable": done, "unavailable": reason, "corridor": counts}


def _bearing(a: Pt, b: Pt) -> float:
    return math.atan2(b[1] - a[1], b[0] - a[0])


def insert_planned(graph: Any, poses: List[common.Pose2D], visible: Callable[[Pt, Pt], bool],
                   max_gap: float, snap_m: float, simplify: Callable[..., List[Pt]],
                   safety: Any = None, routes: Optional[LegRoutes] = None, blocked: Any = None,
                   region: Any = None) -> Tuple[List[common.Pose2D], int]:
    """`poses` with planned waypoints between every two consecutive ones that are not in the same
    or a neighbouring chart. The planned points come from the engine's route between the two, cut
    (line of sight, gap cap) so that each consecutive pair touches at most two charts; they are
    `origin="planned"`, `skippable=True` (the caller adds the corridors) and take no field of
    the operator's waypoints. Pairs that do not snap or route are left as they are. Returns
    (new list, number inserted); the input is not modified. With `safety` the route and the cut use the
    planner's clearance pricing (the cut accepts a shortcut only if it costs no more than the lattice path).
    With `routes` the lattice path of every pair routed here is recorded, cut at the planned points. `blocked` (a scene_graph
    `Blocked`, the keep-out zones) is passed to every route search; `visible` must honour it as well. `region` (the planner's region cost
    term) prices the route and the cut like `safety` does."""
    out: List[common.Pose2D] = []
    inserted = 0
    for k, pose in enumerate(poses):
        out.append(pose)
        if k + 1 >= len(poses):
            break
        extra = _between(graph, pose, poses[k + 1], visible, max_gap, snap_m, simplify, safety, routes, blocked, region)
        for j, (x, y) in enumerate(extra):
            nxt = extra[j + 1] if j + 1 < len(extra) else (poses[k + 1].x, poses[k + 1].y)
            out.append(common.Pose2D(
                x=x, y=y, theta=_bearing((x, y), nxt), map_id=pose.map_id,
                origin=common.WAYPOINT_ORIGIN_PLANNED, skippable=True))
        inserted += len(extra)
    return out, inserted


def _between(graph: Any, a: common.Pose2D, b: common.Pose2D,
             visible: Callable[[Pt, Pt], bool], max_gap: float, snap_m: float,
             simplify: Callable[..., List[Pt]], safety: Any = None,
             routes: Optional[LegRoutes] = None, blocked: Any = None, region: Any = None) -> List[Pt]:
    whole = ((a.x, a.y), (b.x, b.y))
    try:
        # a pair in one chart gets no planned waypoint, but with `routes` it is routed once for its trajectory
        # (the width of its leg needs the same route)
        sa, sb, route, failure = _route_leg(graph, (a.x, a.y), (b.x, b.y), snap_m, safety, blocked,
                                            skip_same_chart=routes is None, region=region)
        if route is None:
            if routes is not None and failure != SAME_CHART:
                routes.failed[whole] = failure
            return []
        nodes = [tuple(p) for p in route.nodes]
        if routes is not None and nodes:
            routes.nodes[whole] = nodes
            routes.widths[whole] = sanitize_width(route.min_width)
        if sa.key == sb.key or len(set(route.charts)) <= 2:
            return []
        owners = [graph.snap(x, y).key for (x, y) in nodes]
    except Exception:  # pylint: disable=broad-except
        if routes is not None:
            routes.failed.setdefault(whole, "error")
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

    # a turn cost alone leaves the cut as it is; a clearance price or a region term makes the line of sight cost-aware
    from packages.services.mission_planner import scene_route    # not at module level: scene_route imports this module
    if not getattr(simplify, "unpriced_visible", False) and (
            (safety is not None and safety.rate > 0.0) or scene_route.region_arg(graph, region) is not None):
        visible = scene_route.priced_visible(visible, graph, nodes, safety, region)
    kept = simplify(nodes, lambda p, q: window_ok(p, q) and visible(p, q), max_gap)
    if routes is not None and len(kept) > 2:
        # the whole-route entry is replaced by its pieces; the widths of the pieces are not known
        routes.nodes.pop(whole, None)
        routes.widths.pop(whole, None)
        stops = [(a.x, a.y)] + [tuple(p) for p in kept[1:-1]] + [(b.x, b.y)]
        cuts = [0] + [index[tuple(p)] for p in kept[1:-1]] + [len(nodes) - 1]
        for k in range(len(stops) - 1):
            routes.nodes[(stops[k], stops[k + 1])] = nodes[cuts[k]:cuts[k + 1] + 1]
    return [tuple(p) for p in kept[1:-1]]
