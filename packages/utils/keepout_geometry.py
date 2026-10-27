"""Snapshot of scene-graph charts as plain geometry (docs/keepout_zones.md): a chart's footprint as a polygon, and the seal between two
charts as one line segment, both in the map frame (metres). Pure numpy/scipy over an owner plane (the API's `owner_plane` shape:
`origin` = map-frame corner of cell (0, 0), `step`, 2-D `index` u16 with 0 = no chart and i = keys[i - 1]).

The zone then lives on its own: the planner resolves the polygon / line to lattice cells from the geometry alone, so a graph rebuild
that renames or merges charts cannot change or lose it.
"""
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
from scipy import ndimage

Pt = Tuple[float, float]

MAX_SNAPSHOT_POINTS = 48          # a chart outline is simplified to at most this many vertices
SIMPLIFY_START_CELLS = 0.5        # Douglas-Peucker tolerance, in cells, grown until the vertex cap holds
SEAL_MIN_HALF_CELLS = 1.0         # the line of a one-cell seal is at least this long on each side of its centre (cells)
SEAL_PAD_CELLS = 0.5              # both ends of the seal line are extended by this


def chart_mask(plane: Dict[str, Any], key: str) -> np.ndarray:
    keys = list(plane["keys"])
    if key not in keys:
        return np.zeros(np.asarray(plane["index"]).reshape(plane["shape"]).shape, bool)
    return np.asarray(plane["index"]).reshape(plane["shape"]) == keys.index(key) + 1


def touches_border(mask: np.ndarray) -> bool:
    return bool(mask[0].any() or mask[-1].any() or mask[:, 0].any() or mask[:, -1].any())


def _largest(mask: np.ndarray) -> np.ndarray:
    lab, n = ndimage.label(mask)
    if n <= 1:
        return mask
    sizes = ndimage.sum(mask, lab, range(1, n + 1))
    return lab == (1 + int(np.argmax(sizes)))


def _outer_loops(mask: np.ndarray) -> List[List[Tuple[int, int]]]:
    """The boundary loops of a bool mask along cell edges (counter-clockwise, interior on the left), as integer grid corners."""
    pad = np.pad(mask, 1)
    nxt: Dict[Tuple[int, int], List[Tuple[int, int]]] = {}
    for r, c in zip(*np.nonzero(mask)):
        r1, c1 = int(r) + 1, int(c) + 1
        r, c = int(r), int(c)
        if not pad[r1 - 1, c1]:
            nxt.setdefault((c, r), []).append((c + 1, r))
        if not pad[r1, c1 + 1]:
            nxt.setdefault((c + 1, r), []).append((c + 1, r + 1))
        if not pad[r1 + 1, c1]:
            nxt.setdefault((c + 1, r + 1), []).append((c, r + 1))
        if not pad[r1, c1 - 1]:
            nxt.setdefault((c, r + 1), []).append((c, r))
    loops = []
    while nxt:
        start = next(iter(nxt))
        loop, cur = [start], start
        while True:
            outs = nxt.get(cur)
            if not outs:
                break
            step = outs.pop()
            if not outs:
                del nxt[cur]
            cur = step
            if cur == start:
                break
            loop.append(cur)
        if len(loop) >= 3:
            loops.append(loop)
    return loops


def _area2(loop: Sequence[Tuple[float, float]]) -> float:
    return sum(loop[i][0] * loop[(i + 1) % len(loop)][1] - loop[(i + 1) % len(loop)][0] * loop[i][1] for i in range(len(loop)))


def _drop_collinear(loop: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    n = len(loop)
    out = []
    for i in range(n):
        a, b, c = loop[i - 1], loop[i], loop[(i + 1) % n]
        if (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0]) != 0:
            out.append(b)
    return out


def _seg_dist(p: Pt, a: Pt, b: Pt) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2))
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)


def _dp(pts: Sequence[Pt], tol: float) -> List[Pt]:
    """Douglas-Peucker on an open polyline (both ends kept)."""
    if len(pts) < 3:
        return list(pts)
    a, b = pts[0], pts[-1]
    dmax, imax = -1.0, 0
    for i in range(1, len(pts) - 1):
        d = _seg_dist(pts[i], a, b)
        if d > dmax:
            dmax, imax = d, i
    if dmax <= tol:
        return [a, b]
    return _dp(pts[:imax + 1], tol)[:-1] + _dp(pts[imax:], tol)


def simplify_ring(ring: Sequence[Pt], tol: float) -> List[Pt]:
    """Douglas-Peucker on a closed ring: split at the vertex farthest from the first, simplify both halves."""
    if len(ring) <= 3:
        return list(ring)
    j = max(range(len(ring)), key=lambda i: math.dist(ring[i], ring[0]))
    left, right = list(ring[:j + 1]), list(ring[j:]) + [ring[0]]
    out = _dp(left, tol)[:-1] + _dp(right, tol)[:-1]
    return out if len(out) >= 3 else list(ring)


def mask_polygon(mask: np.ndarray, origin: Pt, step: float, max_points: int = MAX_SNAPSHOT_POINTS) -> Optional[List[List[float]]]:
    """The outline of the largest blob of `mask` (holes filled) in map-frame metres, simplified to at most `max_points` vertices;
    None for an empty mask."""
    if not mask.any():
        return None
    blob = ndimage.binary_fill_holes(_largest(mask))
    loops = _outer_loops(blob)
    if not loops:
        return None
    loop = max(loops, key=lambda l: abs(_area2(l)))
    ring: List[Pt] = _drop_collinear([(float(x), float(y)) for x, y in loop])
    tol = SIMPLIFY_START_CELLS
    pts = simplify_ring(ring, tol)
    while len(pts) > max_points:
        tol *= 1.5
        pts = simplify_ring(ring, tol)
    return [[round(origin[0] + x * step, 3), round(origin[1] + y * step, 3)] for x, y in pts]


def chart_polygon(plane: Dict[str, Any], key: str) -> Optional[List[List[float]]]:
    return mask_polygon(chart_mask(plane, key), plane["origin"], float(plane["step"]))


def edge_line(plane: Dict[str, Any], a: str, b: str, anchors: Optional[Tuple[Pt, Pt]] = None
              ) -> Optional[List[List[float]]]:
    """The seal between charts a and b as one segment: the cells of a touching b and of b touching a (the planner's edge seal), a
    line through them along their principal axis, a little past the ends. None when the charts do not touch in this plane."""
    ma, mb = chart_mask(plane, a), chart_mask(plane, b)
    ring = np.ones((3, 3), bool)
    seal = (ndimage.binary_dilation(ma, ring) & mb) | (ndimage.binary_dilation(mb, ring) & ma)
    if not seal.any():
        return None
    step = float(plane["step"])
    ys, xs = np.nonzero(seal)
    pts = np.stack([plane["origin"][0] + (xs + 0.5) * step, plane["origin"][1] + (ys + 0.5) * step], axis=1)
    c = pts.mean(axis=0)
    d = None
    if len(pts) >= 2:
        _, sv, vt = np.linalg.svd(pts - c, full_matrices=False)
        if sv[0] > 1e-9:
            d = vt[0]
    if d is None:                       # a single cell: across the line between the two anchors
        if anchors is not None and math.dist(*anchors) > 0:
            ax = (anchors[1][0] - anchors[0][0], anchors[1][1] - anchors[0][1])
            d = np.array([-ax[1], ax[0]]) / math.hypot(*ax)
        else:
            d = np.array([1.0, 0.0])
    t = (pts - c) @ d
    lo = min(float(t.min()), -SEAL_MIN_HALF_CELLS * step) - SEAL_PAD_CELLS * step
    hi = max(float(t.max()), SEAL_MIN_HALF_CELLS * step) + SEAL_PAD_CELLS * step
    p0, p1 = c + lo * d, c + hi * d
    return [[round(float(p0[0]), 3), round(float(p0[1]), 3)], [round(float(p1[0]), 3), round(float(p1[1]), 3)]]
