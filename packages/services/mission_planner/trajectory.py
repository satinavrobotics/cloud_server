"""The dense lattice path of a plan, cut per leg between the output waypoints (docs/plan_trajectory_2026-10-14.md).

Pure functions. A leg is `{"from_index", "to_index", "points": [[x, y], ...]}`: map-frame points, rounded to
the cm, with every point removed that lies on the straight line of its neighbours (the lattice path is straight
runs between direction changes, so the polyline draws exactly the lattice path). The first and last point of a
leg are its waypoints.
"""
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

Pt = Tuple[float, float]

_COLLINEAR_EPS = 1e-9


def compress(points: Sequence[Pt]) -> List[Pt]:
    """`points` without consecutive duplicates and without points on the line of their neighbours
    (keeps the ends and every direction change), rounded to the cm (points equal after rounding
    collapse)."""
    pts: List[Pt] = []
    for p in points:
        q = (float(p[0]), float(p[1]))
        if not pts or q != pts[-1]:
            pts.append(q)
    out: List[Pt] = []
    for k, p in enumerate(pts):
        if 0 < k < len(pts) - 1:
            a, b = out[-1], pts[k + 1]
            cross = (p[0] - a[0]) * (b[1] - p[1]) - (p[1] - a[1]) * (b[0] - p[0])
            dot = (p[0] - a[0]) * (b[0] - p[0]) + (p[1] - a[1]) * (b[1] - p[1])
            if abs(cross) <= _COLLINEAR_EPS and dot > 0.0:
                continue
        out.append(p)
    rounded: List[Pt] = []
    for x, y in out:
        q = (round(x, 2) + 0.0, round(y, 2) + 0.0)
        if not rounded or q != rounded[-1]:
            rounded.append(q)
    return rounded


def leg(from_index: int, to_index: int, path: Sequence[Pt], start: Pt, end: Pt) -> Optional[Dict[str, Any]]:
    """One leg from a lattice path piece. The ends are forced to the waypoints `start` / `end` (an operator's
    waypoint is not on a cell centre). None when fewer than two distinct points remain."""
    pts = [(float(p[0]), float(p[1])) for p in path]
    start = (float(start[0]), float(start[1]))
    end = (float(end[0]), float(end[1]))
    if not pts or math.dist(pts[0], start) > 1e-6:
        pts.insert(0, start)
    if math.dist(pts[-1], end) > 1e-6:
        pts.append(end)
    pts[0], pts[-1] = start, end
    comp = compress(pts)
    if len(comp) < 2:
        return None
    return {"from_index": from_index, "to_index": to_index, "points": [[x, y] for x, y in comp]}


def split(path: Sequence[Pt], waypoints: Sequence[Pt]) -> Optional[List[Dict[str, Any]]]:
    """The legs of `path` cut at `waypoints` (each a point of the path, in order). None when a waypoint is not on
    the path, so the caller serves no trajectory rather than a wrong one."""
    pts = [(float(p[0]), float(p[1])) for p in path]
    where: List[int] = []
    pos = 0
    for w in waypoints:
        try:
            pos = pts.index((float(w[0]), float(w[1])), pos)
        except ValueError:
            return None
        where.append(pos)
    legs: List[Dict[str, Any]] = []
    for k in range(len(waypoints) - 1):
        got = leg(k, k + 1, pts[where[k]:where[k + 1] + 1], waypoints[k], waypoints[k + 1])
        if got is None:
            return None
        legs.append(got)
    return legs


def from_routes(nodes: Dict[Tuple[Pt, Pt], Sequence[Pt]], poses: Sequence[Any]) -> Optional[List[Dict[str, Any]]]:
    """The legs of a route whose lattice paths are known per (from xy, to xy) pair (`node_policy_plan.LegRoutes`):
    one leg per consecutive pair of `poses` (anything with x and y) that has a path; a pair without one has no
    entry (the client draws it straight). None when no leg is known."""
    legs: List[Dict[str, Any]] = []
    for k in range(len(poses) - 1):
        a, b = (poses[k].x, poses[k].y), (poses[k + 1].x, poses[k + 1].y)
        path = nodes.get((a, b))
        if path:
            got = leg(k, k + 1, path, a, b)
            if got is not None:
                legs.append(got)
    return legs or None


def capped(legs: Optional[List[Dict[str, Any]]], max_points: int) -> Optional[List[Dict[str, Any]]]:
    """`legs`, or None when empty, `max_points` <= 0 (switched off) or the total point count is over the cap
    (a trajectory is display data: omitted whole, never truncated)."""
    if not legs or max_points <= 0:
        return None
    if sum(len(item["points"]) for item in legs) > max_points:
        return None
    return legs
