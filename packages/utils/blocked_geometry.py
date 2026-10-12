"""Where a blocked report puts the obstacle, as geometry (scene-graph planner and reroute check).

A `blocked_graph_nodes` row holds `x, y`, the position of the waypoint the robot could not reach.
With the sparse waypoints of a lattice route that waypoint can be many metres past the obstacle, and
the reroute check tests waypoints only, so a long leg through the obstacle passes. The row's optional
`robot_x, robot_y` (robot pose when it reported the block) turn it into a segment: the obstacle lies
on the way from the robot to that waypoint.

The segment starts `lead_m` ahead of the robot, never at it, so a detour that leaves from where the
robot stands is not blocked by its own start. Rows without `robot_x, robot_y` (written before the
columns existed) keep the old rule: a disc of `MATCH_RADIUS_M` around `x, y`.

Pure functions: nothing here imports the engine or the database. Applied in the reroute
check and the planner's path search; see docs/scene_graph/integration.md, "Blocked overlay".
"""
import math
import os
from typing import Any, Iterable, List, Mapping, Optional, Sequence, Tuple

from packages.utils.blocked_nodes import position_of

Pt = Tuple[float, float]

# Must exceed HALF_WIDTH_M: otherwise a leg leaving the robot sideways is within the segment's width.
LEAD_M = float(os.getenv("BLOCKED_SEGMENT_LEAD_M", "1.0"))
HALF_WIDTH_M = float(os.getenv("BLOCKED_SEGMENT_HALF_WIDTH_M", "0.5"))


def point_segment_distance(p: Pt, a: Pt, b: Pt) -> float:
    ax, ay, bx, by = a[0], a[1], b[0], b[1]
    dx, dy = bx - ax, by - ay
    n = dx * dx + dy * dy
    t = 0.0 if n == 0.0 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / n))
    return math.hypot(p[0] - (ax + t * dx), p[1] - (ay + t * dy))


def _cross(o: Pt, a: Pt, b: Pt) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def segments_intersect(a: Pt, b: Pt, c: Pt, d: Pt) -> bool:
    d1, d2 = _cross(c, d, a), _cross(c, d, b)
    d3, d4 = _cross(a, b, c), _cross(a, b, d)
    if ((d1 > 0 > d2) or (d1 < 0 < d2)) and ((d3 > 0 > d4) or (d3 < 0 < d4)):
        return True
    return False


def segment_segment_distance(a: Pt, b: Pt, c: Pt, d: Pt) -> float:
    if segments_intersect(a, b, c, d):
        return 0.0
    return min(point_segment_distance(a, c, d), point_segment_distance(b, c, d),
               point_segment_distance(c, a, b), point_segment_distance(d, a, b))


def _waypoint(row: Mapping[str, Any]) -> Optional[Pt]:
    """The blocked waypoint: the row's x, y, else the position a synthetic "@x,y" id names."""
    try:
        return float(row["x"]), float(row["y"])
    except (KeyError, TypeError, ValueError):
        return position_of(str(row.get("graph_node_id", "")))


def row_segment(row: Mapping[str, Any], lead_m: float = LEAD_M) -> Optional[Tuple[Pt, Pt]]:
    """(a, b): from `lead_m` ahead of the robot's pose to the blocked waypoint; None when the row
    has no robot pose or no waypoint position. When the waypoint is within `lead_m` of the
    robot, a is the robot's pose itself (conservative), so a is never beyond b."""
    wp = _waypoint(row)
    try:
        rx, ry = float(row["robot_x"]), float(row["robot_y"])
    except (KeyError, TypeError, ValueError):
        return None
    if wp is None:
        return None
    bx, by = wp
    length = math.hypot(bx - rx, by - ry)
    if length == 0.0:
        return (bx, by), (bx, by)
    if length <= lead_m:
        # A stop closer than the lead: the whole way from the pose to the waypoint, never a
        # point at the waypoint or a segment pointing back at the robot.
        return (rx, ry), (bx, by)
    t = max(lead_m, 0.0) / length
    return (rx + t * (bx - rx), ry + t * (by - ry)), (bx, by)


def segment_hits(seg: Optional[Tuple[Pt, Pt]], polyline: Sequence[Pt],
                 half_width_m: float = HALF_WIDTH_M) -> bool:
    """Whether the straight legs of `polyline` pass within `half_width_m` of `seg` (a row_segment
    result, None never hit). Split out of route_hits_row so a caller testing many legs against the
    same rows computes each row's segment once."""
    pts = list(polyline)
    if seg is None or not pts:
        return False
    a, b = seg
    if len(pts) == 1:
        return point_segment_distance(pts[0], a, b) <= half_width_m
    return any(segment_segment_distance(p, q, a, b) <= half_width_m for p, q in zip(pts, pts[1:]))


def route_hits_row(row: Mapping[str, Any], polyline: Sequence[Pt], half_width_m: float = HALF_WIDTH_M,
                   lead_m: float = LEAD_M) -> bool:
    """Whether the straight legs of `polyline` pass within `half_width_m` of the row's obstacle
    segment. A row without a robot pose is not judged here (the disc rule decides)."""
    return segment_hits(row_segment(row, lead_m), polyline, half_width_m)


def near_obstacle(rows: Iterable[Mapping[str, Any]], xy: Pt, half_width_m: float = HALF_WIDTH_M,
                  lead_m: float = LEAD_M) -> bool:
    """Whether the point `xy` is within `half_width_m` of the obstacle segment of any of `rows`
    (rows without a robot pose have no segment and never match)."""
    for row in rows:
        seg = row_segment(row, lead_m)
        if seg is not None and point_segment_distance(xy, seg[0], seg[1]) <= half_width_m:
            return True
    return False


def overlay(rows: Iterable[Mapping[str, Any]], disc_radius_m: float, half_width_m: float = HALF_WIDTH_M,
            lead_m: float = LEAD_M) -> Tuple[List[Tuple[float, float, float]], List[Tuple[Pt, Pt, float]]]:
    """(discs, segments) for the planner's transient `Blocked`: a row with a robot pose is a
    segment, any other row with a position a disc. A row without any position (a plain graph node id)
    contributes nothing here: the graph-node exclusion handles it."""
    discs: List[Tuple[float, float, float]] = []
    segments: List[Tuple[Pt, Pt, float]] = []
    for row in rows:
        seg = row_segment(row, lead_m)
        if seg is not None:
            segments.append((seg[0], seg[1], half_width_m))
            continue
        wp = _waypoint(row)
        if wp is not None:
            discs.append((wp[0], wp[1], disc_radius_m))
    return discs, segments
