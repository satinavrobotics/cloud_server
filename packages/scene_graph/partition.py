"""Partition of the navigable cells into regions: the mcmaps.

Free-space structure comes from the clearance field c(x), the distance to the nearest blocked cell.

  decomposition  The field is flooded downhill from its local maxima: every cell joins the basin of its highest
                 neighbour (a watershed). Two basins touch where the clearance between them dips to its highest
                 crossing, the saddle; the passage there is 2 x saddle wide (a door, the mouth of a corridor).
                 Basins are merged in order of decreasing saddle height when
                   bottleneck  the saddle is at least gamma x the narrower peak: the passage is nearly as wide
                               as the places it joins,
                   chart       any two cells of the union are within tau of straight from each other by a path
                               inside it (bounded geodesic dilation, up to a free detour of a few robot widths):
                               a convex place has dilation 1, an L of equal arms sqrt 2, a ring or a U more,
                   budget      the union holds at most `budget` cells.
                 A basin whose own clearance is below min_place x the robot radius is transit space (a gap, a
                 corner), not a place, and joins its widest neighbour unconditionally.

  evolution      The decomposition is canonical but chaotic in open space, where a small change of the map
                 reorders merges. So the partition is kept and repaired, not recomputed:
                   new cells    join the nearest region (geodesic), or form a new region if no region reaches them,
                   a region that is disconnected, over budget, or no longer a chart is re-decomposed on its own,
                   neighbours   merge when the criteria hold by a margin, and stay apart until they fail by one.
                 The two margins are a hysteresis band: nothing is split and merged in the same breath.
"""
from __future__ import annotations

import heapq
from typing import Dict, List, Tuple

import numpy as np
from scipy import ndimage
from scipy.sparse.csgraph import connected_components, dijkstra

from .graph import Graph
from .params import Params


# ------------------------------------------------------------------------------------------------ primitives
def basins(g: Graph, h: np.ndarray) -> Tuple[np.ndarray, List[int]]:
    """Watershed by flooding: returns (basin label per node, peak node per basin)."""
    n = g.n
    ptr, ind = g.length.indptr.tolist(), g.length.indices.tolist()
    hl = h.tolist()
    order = np.lexsort((np.arange(n), -h)).tolist()          # descending clearance, ties by node index
    lab = [-1] * n
    peaks: List[int] = []
    for u in order:
        best, bh = -1, -1.0
        for e in range(ptr[u], ptr[u + 1]):
            v = ind[e]
            if lab[v] >= 0 and (hl[v] > bh or (hl[v] == bh and lab[v] < lab[best])):
                best, bh = v, hl[v]
        if best < 0:
            lab[u] = len(peaks)
            peaks.append(u)
        else:
            lab[u] = lab[best]
    return np.asarray(lab, np.int64), peaks


def saddles(g: Graph, label: np.ndarray, h: np.ndarray) -> Dict[Tuple[int, int], float]:
    """Height of the highest crossing between every two touching regions."""
    coo = g.length.tocoo()
    m = (coo.row < coo.col) & (label[coo.row] >= 0) & (label[coo.col] >= 0) & (label[coo.row] != label[coo.col])
    u, v = coo.row[m], coo.col[m]
    s = np.minimum(h[u], h[v])
    a, b = np.minimum(label[u], label[v]), np.maximum(label[u], label[v])
    out: Dict[Tuple[int, int], float] = {}
    for k in np.lexsort((s, b, a)):                           # ascending: the last write per pair is the highest
        out[(int(a[k]), int(b[k]))] = float(s[k])
    return out


def dilation(g: Graph, shape, rows, cols, nodes: np.ndarray, res: float, free_m: float = 0.0,
             max_src: int = 48) -> float:
    """Largest value of (path length - free_m) / straight distance between cells of a region, measured from its
    boundary cells (the extremal pairs of a planar region lie on its boundary) to all of its cells.
    free_m is a detour that costs nothing: an obstacle a few robot widths across does not make two places."""
    mask = np.zeros(shape, bool)
    mask[rows[nodes], cols[nodes]] = True
    edge = mask & ~ndimage.binary_erosion(mask, np.ones((3, 3), bool), border_value=0)
    src = np.nonzero(edge[rows[nodes], cols[nodes]])[0]
    if len(src) == 0:
        return 1.0
    if len(src) > max_src:
        src = src[np.linspace(0, len(src) - 1, max_src).astype(int)]
    sub = g.subgraph(nodes)
    d = dijkstra(sub.length, directed=False, indices=src)
    e = np.hypot(sub.xy[src, 0][:, None] - sub.xy[:, 0][None, :], sub.xy[src, 1][:, None] - sub.xy[:, 1][None, :])
    ratio = np.maximum(d - free_m, 0.0) / (e + res)
    return float(np.where(np.isfinite(ratio), ratio, np.inf).max())


class _Ctx:
    """The graph and fields a decomposition works on, with the parameters in coarse units."""

    def __init__(self, g: Graph, rows, cols, shape, h, p: Params):
        self.g, self.rows, self.cols, self.shape, self.h, self.p = g, rows, cols, shape, h, p

    def dil(self, nodes: np.ndarray) -> float:
        return dilation(self.g, self.shape, self.rows, self.cols, nodes, self.p.res, self.p.free_detour_m)

    def transit(self, peak: float) -> bool:
        return peak < self.p.min_place * self.p.robot_radius


# ------------------------------------------------------------------------------------------------ decomposition
def decompose(ctx: _Ctx, nodes: np.ndarray) -> np.ndarray:
    """Canonical decomposition of a set of nodes: returns a label (0..k-1) per node of `nodes`."""
    if len(nodes) == 0:
        return np.zeros(0, np.int64)
    sub = ctx.g.subgraph(nodes)
    h = ctx.h[nodes]
    lab, peaks = basins(sub, h)
    nb = len(peaks)
    sad = saddles(sub, lab, h)
    gid = np.arange(nb)
    peak_h = {i: float(h[peaks[i]]) for i in range(nb)}
    size = {i: int(c) for i, c in enumerate(np.bincount(lab, minlength=nb))}
    adj: Dict[int, Dict[int, float]] = {i: {} for i in range(nb)}
    for (a, b), s in sad.items():
        adj[a][b] = adj[b][a] = s
    heap = [(-s, a, b) for (a, b), s in sad.items()]
    heapq.heapify(heap)
    dead = set()
    p = ctx.p
    # dilation needs the node coordinates of the full graph: map back through `nodes`
    while heap:
        ns, a, b = heapq.heappop(heap)
        s = -ns
        if a not in peak_h or b not in peak_h or adj[a].get(b) != s or (a, b) in dead:
            continue
        lo = min(peak_h[a], peak_h[b])
        transit = ctx.transit(lo)
        if not transit and (s < p.gamma * lo or size[a] + size[b] > p.budget):
            dead.add((a, b)); continue
        if transit and size[a] + size[b] > p.budget:
            dead.add((a, b)); continue
        if not transit:
            union = nodes[np.isin(gid[lab], (a, b))]
            if ctx.dil(union) > p.tau:
                dead.add((a, b)); continue
        if peak_h[b] > peak_h[a] or (peak_h[b] == peak_h[a] and b < a):
            a, b = b, a
        gid[gid == b] = a
        size[a] += size.pop(b)
        peak_h.pop(b)
        for c, sc in adj.pop(b).items():
            if c == a:
                continue
            adj[c].pop(b, None)
            if sc > adj[a].get(c, -1.0):
                adj[a][c] = adj[c][a] = sc
            heapq.heappush(heap, (-adj[a][c], min(a, c), max(a, c)))
        adj[a].pop(b, None)
    groups = gid[lab]
    _, out = np.unique(groups, return_inverse=True)
    return out.astype(np.int64)


def bisect(ctx: _Ctx, nodes: np.ndarray, limit: float, min_nodes: int = 6) -> List[np.ndarray]:
    """Cut a region that is still not a chart: seed at the two ends of its longest path and give every cell to the
    nearer seed (geodesically, so both parts stay connected). Repeats on the parts that still fail."""
    if len(nodes) < 2 * min_nodes or ctx.dil(nodes) <= limit:
        return [nodes]
    sub = ctx.g.subgraph(nodes)
    d0 = dijkstra(sub.length, directed=False, indices=0)
    u = int(np.argmax(np.where(np.isfinite(d0), d0, -1)))
    du = dijkstra(sub.length, directed=False, indices=u)
    v = int(np.argmax(np.where(np.isfinite(du), du, -1)))
    dv = dijkstra(sub.length, directed=False, indices=v)
    near_u = du <= dv
    parts = [nodes[near_u], nodes[~near_u]]
    if min(len(q) for q in parts) < min_nodes:
        return [nodes]
    return [q for part in parts for q in bisect(ctx, part, limit, min_nodes)]


# ------------------------------------------------------------------------------------------------ evolution
def settle(g: Graph, rows, cols, shape, h: np.ndarray, p: Params, prior: np.ndarray, first_new: int
           ) -> np.ndarray:
    """Repair the previous partition. prior: region id of each node before (-1 none); returns the region id of
    every node; regions that did not exist before get ids first_new, first_new + 1, ..."""
    n = g.n
    ctx = _Ctx(g, rows, cols, shape, h, p)
    label = prior.astype(np.int64).copy()
    nxt = [first_new]

    def fresh() -> int:
        nxt[0] += 1
        return nxt[0] - 1

    # 1 new cells join the nearest region; components nobody reaches are decomposed on their own
    if n and (label < 0).any():
        src = np.nonzero(label >= 0)[0]
        if len(src):
            _, _, origin = dijkstra(g.length, directed=False, indices=src, min_only=True, return_predecessors=True)
            near = np.where(origin >= 0, label[np.maximum(origin, 0)], -1)
            label = np.where(label >= 0, label, near)
        rest = np.nonzero(label < 0)[0]
        if len(rest):
            _, comp = connected_components(g.length[rest][:, rest], directed=False)
            for c in np.unique(comp):
                part = rest[comp == c]
                sub = decompose(ctx, part)
                for k in np.unique(sub):
                    for q in bisect(ctx, part[sub == k], p.tau * (1 + p.hysteresis)):
                        label[q] = fresh()
    # 2 regions that are disconnected, over budget or no longer a chart are decomposed on their own
    for rid in list(np.unique(label)):
        nodes = np.nonzero(label == rid)[0]
        parts = [nodes]
        ncomp, comp = connected_components(g.length[nodes][:, nodes], directed=False)
        if ncomp > 1:
            parts = [nodes[comp == c] for c in range(ncomp)]
        out = []
        for part in parts:
            if len(part) > p.budget or ctx.dil(part) > p.tau * (1 + p.hysteresis):
                sub = decompose(ctx, part)
                for k in np.unique(sub):
                    out += bisect(ctx, part[sub == k], p.tau * (1 + p.hysteresis))
            else:
                out.append(part)
        if len(out) > 1:
            out.sort(key=len, reverse=True)
            for part in out[1:]:
                label[part] = fresh()
    # 3 neighbours merge when the criteria hold by a margin; transit space joins its widest neighbour
    peak = lambda r: float(h[label == r].max())
    changed = True
    while changed:
        changed = False
        sad = saddles(g, label, h)
        for (a, b), s in sorted(sad.items(), key=lambda kv: -kv[1]):
            pa, pb = peak(a), peak(b)
            lo = min(pa, pb)
            na, nb_ = int((label == a).sum()), int((label == b).sum())
            if na + nb_ > p.budget:
                continue
            transit = ctx.transit(lo)
            if not transit:
                if s < p.gamma * (1 + p.hysteresis) * lo:
                    continue
                if ctx.dil(np.nonzero((label == a) | (label == b))[0]) > p.tau * (1 - p.hysteresis):
                    continue
            keep, drop = (a, b) if (pa, -a) >= (pb, -b) else (b, a)
            label[label == drop] = keep
            changed = True
            break
    return label
