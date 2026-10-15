"""Waypoint budget of the scene-graph route (docs/scene_graph/close_evidence_and_turn_cost_design.md C.2).

The string pull of the lattice path is replaced by the exact path with the fewest waypoints whose total priced cost exceeds the
lattice path's by at most a budget. The apex candidates of an earlier draft are removed (no measured effect; design C.2).

Pure functions: the planner supplies the line of sight and the priced chord cost.
"""
import bisect
import math
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

Pt = Tuple[float, float]


def simplify(points: Sequence[Pt], visible: Callable[[Pt, Pt], bool],
             max_gap: float) -> List[Pt]:
    """Greedy string pulling: from each kept point go to the farthest later point that is visible
    from it and at most `max_gap` away (straight line). The first and last points are always kept;
    with `max_gap` <= 0 no cap applies. Consecutive duplicates are dropped. The candidates of a
    step are the points up to the first one beyond `max_gap`; they are tried from the far end and
    the first visible one wins (the same point the ascending scan ends on, with far fewer
    `visible` calls on a free corridor). The window end starts at a bisect over the cumulative arc
    length (a chord is never longer than its arc, so every point within `max_gap` of arc is inside)
    and is extended by chord tests, which are cheap next to a line of sight."""
    pts: List[Pt] = []
    for p in points:
        if not pts or p != pts[-1]:
            pts.append(p)
    if len(pts) <= 2:
        return pts
    last = len(pts) - 1
    arc = [0.0]
    for k in range(1, len(pts)):
        arc.append(arc[-1] + math.dist(pts[k - 1], pts[k]))
    kept = [pts[0]]
    i = 0
    while i < last:
        if max_gap > 0:
            end = max(i + 1, min(last, bisect.bisect_right(arc, arc[i] + max_gap) - 1))
            while end < last and math.dist(pts[i], pts[end + 1]) <= max_gap:
                end += 1
        else:
            end = last
        best = i + 1                       # the next lattice point is always reachable
        for k in range(end, i + 1, -1):
            if visible(pts[i], pts[k]):
                best = k
                break
        kept.append(pts[best])
        i = best
    return kept


WAYPOINT_THIN_FRACTION = 0.5     # candidate thinning tolerance, as a fraction of the lattice step
THIN_SPACING_M = 0.5             # candidates are at most this far apart (a chord may leave a straight run at any of its points)


def thin_eps(graph: Any) -> float:
    """The candidate-thinning tolerance (m) of the budget solver for `graph`: `WAYPOINT_THIN_FRACTION` of its lattice step."""
    return max(0.0, float(WAYPOINT_THIN_FRACTION)) * float(getattr(getattr(graph, "params", None), "lattice", 0.0) or 0.0)


def greedy_pull_indices(pts: Sequence[Pt], visible: Callable[[Pt, Pt], bool], max_gap: float) -> List[int]:
    """Lattice indices of the vertices of `simplify(pts, visible, max_gap)` (`pts` without consecutive duplicates)."""
    where: List[int] = []
    pos = 0
    for q in simplify(pts, visible, max_gap):
        pos = pts.index(q, pos)
        where.append(pos)
    return where


def thin_indices(pts: Sequence[Pt], eps: float, keep: Sequence[int], max_gap: float) -> List[int]:
    """Sorted candidate indices of the budget solver: the first, the last, the `keep` indices, the points an iterative
    Ramer-Douglas-Peucker pass keeps at tolerance `eps` (a dropped point is within `eps` of the chord of its neighbours), and, where
    two consecutive candidates are farther apart than `THIN_SPACING_M` (and `max_gap`), the farthest later point within that
    (repeated): consecutive candidates are within the gap cap, and a straight run keeps a point every `THIN_SPACING_M`, where the
    optimum may leave it. Deterministic."""
    n = len(pts)
    mark = [False] * n
    mark[0] = mark[n - 1] = True
    for k in keep:
        mark[k] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        ax, ay = pts[a]
        dx, dy = pts[b][0] - ax, pts[b][1] - ay
        norm = math.hypot(dx, dy)
        far, fd = -1, eps
        for m in range(a + 1, b):
            if norm > 0.0:
                d = abs(dx * (pts[m][1] - ay) - dy * (pts[m][0] - ax)) / norm
            else:
                d = math.dist(pts[m], pts[a])
            if d > fd:
                far, fd = m, d
        if far >= 0:
            mark[far] = True
            stack.append((a, far))
            stack.append((far, b))
    out = [k for k in range(n) if mark[k]]
    if THIN_SPACING_M > 0:
        step = min(max_gap, THIN_SPACING_M) if max_gap > 0 else THIN_SPACING_M
        full: List[int] = []
        for a, b in zip(out[:-1], out[1:]):
            full.append(a)
            while math.dist(pts[a], pts[b]) > step and b - a > 1:
                m = b - 1
                while m > a + 1 and math.dist(pts[a], pts[m]) > step:
                    m -= 1
                full.append(m)
                a = m
        full.append(out[-1])
        out = full
    return out


def fewest_waypoints(points: Sequence[Pt], visible: Callable[[Pt, Pt], bool], max_gap: float,
                     cost: Callable[[Pt, Pt], float], budget_m: float, end_extra: float = 0.0,
                     thin_eps: float = 0.0) -> List[Pt]:
    """Fewest waypoints under a cost budget (docs/scene_graph/close_evidence_and_turn_cost_design.md C.2), exact.

    `points` are the lattice path p_0..p_{n-1} (consecutive duplicates dropped), `visible` the free-space line of sight,
    `cost` the priced cost of a chord (`chord_cost`), P(i, j) the cost of the lattice sub-path (prefix sums over consecutive
    points). A chord (i, j) is allowed when it is visible and at most `max_gap` long (<= 0: no cap), or when j = i + 1; its slack is
    max(0, cost(i, j) - P(i, j)), rounded up to whole centimetres. The result is a path 0 -> n-1 with the fewest chords whose slacks
    sum to at most `budget_m`: a dynamic program over (point, slack used in cm). Ties: fewest waypoints, then least slack, then the
    lexicographically smallest index sequence.

    Cost bound: J(final) <= J(lattice) + budget_m, because the chords partition the lattice path and each chord's cost is at most
    its lattice sub-path plus its slack. A larger budget never gives more waypoints.

    `end_extra`: the chord that ends at the last point may be this much longer than `max_gap` (the end-stub merge).
    `thin_eps` > 0: the DP runs over the candidate indices of `thin_indices` only; the slacks are still measured against the
    original lattice sub-path, so the bound is unchanged. If the thinned candidates leave no path, the full DP runs.
    """
    pts: List[Pt] = []
    for p in points:
        if not pts or p != pts[-1]:
            pts.append(p)
    if len(pts) <= 2:
        return pts
    n = len(pts)
    pull_idx = greedy_pull_indices(pts, visible, max_gap) if thin_eps > 0.0 else None
    budget = max(0, int(math.ceil(max(0.0, budget_m) * 100.0 - 1e-9)))
    arc = [0.0]
    prefix = [0.0]
    for k in range(1, n):
        arc.append(arc[-1] + math.dist(pts[k - 1], pts[k]))
        prefix.append(prefix[-1] + cost(pts[k - 1], pts[k]))
    if thin_eps > 0.0:
        out = _budget_dp(pts, thin_indices(pts, thin_eps, pull_idx, max_gap), arc, prefix, visible, max_gap, cost, budget, end_extra)
        if out is not None:
            return out
    return _budget_dp(pts, list(range(n)), arc, prefix, visible, max_gap, cost, budget, end_extra)


def _budget_dp(pts: Sequence[Pt], cand: Sequence[int], arc: Sequence[float], prefix: Sequence[float],
               visible: Callable[[Pt, Pt], bool], max_gap: float, cost: Callable[[Pt, Pt], float], budget: int,
               end_extra: float) -> Optional[List[Pt]]:
    """The dynamic program of `fewest_waypoints` over the candidate indices `cand` (sorted, first and last included); None when no
    path reaches the last point."""
    n = len(pts)
    # best[k][s] = (chords, indices) of the best way to reach point k having used s cm of slack
    best: List[Dict[int, Tuple[int, Tuple[int, ...]]]] = [dict() for _ in range(n)]
    best[0][0] = (0, (0,))
    for i in cand[:-1]:
        if not best[i]:
            continue
        if max_gap > 0:
            end = max(i + 1, min(n - 1, bisect.bisect_right(arc, arc[i] + max_gap) - 1))
            while end < n - 1 and math.dist(pts[i], pts[end + 1]) <= max_gap:
                end += 1
        else:
            end = n - 1
        lo = bisect.bisect_right(cand, i)
        candidates = cand[lo:bisect.bisect_right(cand, end if max_gap > 0 else n - 1)]
        last = max_gap > 0 and end < n - 1 and math.dist(pts[i], pts[n - 1]) <= max_gap + end_extra
        if last and (not candidates or candidates[-1] != n - 1):
            candidates = candidates + [n - 1]
        elif not candidates and lo < len(cand):
            candidates = [cand[lo]]                  # the next candidate is tried even beyond the window
        nxt = cand[lo] if lo < len(cand) else -1
        for j in candidates:
            if j == i + 1:
                slack = 0
            else:
                if j > end and j != nxt and not (j == n - 1 and last):
                    continue                         # a chord stays inside the window of the plain solver
                if not visible(pts[i], pts[j]):
                    continue
                ref = prefix[j] - prefix[i]
                over = cost(pts[i], pts[j]) - ref
                slack = 0 if over <= ref * 1e-9 + 1e-9 else int(math.ceil(over * 100.0 - 1e-9))
            if slack > budget:
                continue
            target = best[j]
            for s, (count, path) in best[i].items():
                ns = s + slack
                if ns > budget:
                    continue
                new = (count + 1, path + (j,))
                if ns not in target or new < target[ns]:
                    target[ns] = new
    if not best[n - 1]:
        return None
    _, _, path = min((count, s, path) for s, (count, path) in best[n - 1].items())
    return [pts[k] for k in path]
