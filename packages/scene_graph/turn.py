"""Exact turn-cost lattice search (docs/scene_graph/close_evidence_and_turn_cost_design.md B.1, B.5), shared by `engine.ChartGraph.route`
and `lazy_plan.PlanGraph.route` on the local cell graph each of them builds.

Objective: the integer edge weights W of the cell graph (`api.edge_weight`, units of 1e-4 m) plus, at every change of heading between two
consecutive edges, `api.turn_weight(turn, steps)`; the start and the goal heading are free. The search state is (cell, incoming
heading); one exact Dijkstra on the expanded graph (non-negative integer weights, held exactly in float64).

Restriction, exact (B.5): the incumbent J_inc is the cost of a known route: the plain route P0 with its turn cost, or the expanded
optimum on a thin band of `BAND` lattice cells around P0 when that is cheaper. Every path of total cost <= J_inc passes only cells c with
d(s, c) + d(c, t) <= J_inc, d the plain distances on the same graph, because the turn term is non-negative: the expanded search runs on
those cells only. If P0 has no turn its cost is the plain optimum and a lower bound of the objective: P0 is the answer (and, being the
canonical plain route, the canonical one among the paths of that cost).

Ties: among the paths of minimal total cost the lexicographic minimum of the (y, x) cell sequence, the canonical rule of the plain search.
Cell indices are row-major ranks, so a walk from the start that takes, among the neighbours on a minimal path, the smallest index is
that minimum; the heading of a step is fixed by its two cells, so headings are not part of the order.
"""
from typing import List, Optional, Tuple

import numpy as np
from scipy.sparse import coo_matrix, csr_matrix
from scipy.sparse.csgraph import dijkstra

from packages.scene_graph.api import turn_weight

BAND = 3                         # lattice cells around P0 for the incumbent (Chebyshev distance)
_OFF = 1 << 20                   # linear keys of (y, x): (y + _OFF) * _KW + (x + _OFF), as lazy_plan's
_KW = 1 << 21
# heading index of a lattice step (dy, dx), counter-clockwise in steps of pi / 4; -1 for a non-step
_HEAD = np.full(9, -1, np.int64)
for _h, (_dy, _dx) in enumerate(((0, 1), (1, 1), (1, 0), (1, -1), (0, -1), (-1, -1), (-1, 0), (-1, 1))):
    _HEAD[(_dy + 1) * 3 + (_dx + 1)] = _h
STEPS = np.array([[min((a - b) % 8, (b - a) % 8) for b in range(8)] for a in range(8)], np.int64)   # |dtheta| / (pi / 4)


def heading(dy, dx) -> np.ndarray:
    """heading index (0..7) of the lattice steps (dy, dx)"""
    return _HEAD[(np.asarray(dy, np.int64) + 1) * 3 + (np.asarray(dx, np.int64) + 1)]


def table(turn: float) -> np.ndarray:
    """[incoming, outgoing] turn cost of two headings (`api.turn_weight`), integers in float64"""
    return turn_weight(turn, STEPS)


def path_turn(ys, xs, tab: np.ndarray) -> int:
    """turn cost of the lattice path through the cells (ys, xs)"""
    h = heading(np.diff(np.asarray(ys, np.int64)), np.diff(np.asarray(xs, np.int64)))
    return int(tab[h[:-1], h[1:]].sum()) if len(h) > 1 else 0


def near(ys, xs, py, px, band: int = BAND) -> np.ndarray:
    """bool over the cells (ys, xs): within `band` lattice steps (Chebyshev) of one of the cells (py, px)"""
    d = np.arange(-band, band + 1)
    ky = (np.asarray(py, np.int64)[:, None, None] + d[None, :, None]) + _OFF
    kx = (np.asarray(px, np.int64)[:, None, None] + d[None, None, :]) + _OFF
    keys = np.unique((ky * _KW + kx).ravel())
    return np.isin((np.asarray(ys, np.int64) + _OFF) * _KW + (np.asarray(xs, np.int64) + _OFF), keys)


class Graph:
    """A local lattice graph: n cells in row-major order (`ys`, `xs` their lattice coordinates), the undirected edges (R, C) with the
    integer weights W, the edge from R to C has heading H. `plain` (optional) is its symmetric CSR of W, when the caller has it."""

    def __init__(self, n: int, R, C, W, H, ys, xs, plain: Optional[csr_matrix] = None):
        eu = np.concatenate([R, C]).astype(np.int64)
        o = np.argsort(eu, kind="stable")
        self.n, self.ys, self.xs = int(n), np.asarray(ys, np.int64), np.asarray(xs, np.int64)
        self.eu = eu[o]
        self.ev = np.concatenate([C, R]).astype(np.int64)[o]
        self.ew = np.concatenate([W, W]).astype(float)[o]
        self.eh = np.concatenate([H, (np.asarray(H) + 4) % 8]).astype(np.int64)[o]
        self._plain = plain

    @property
    def plain(self) -> csr_matrix:
        if self._plain is None:
            self._plain = coo_matrix((self.ew, (self.eu, self.ev)), shape=(self.n, self.n)).tocsr()
        return self._plain

    def near(self, path: List[int], band: int = BAND) -> np.ndarray:
        """cells within `band` lattice steps (Chebyshev) of a cell of `path`"""
        return near(self.ys, self.xs, self.ys[path], self.xs[path], band)

    def expanded(self, sel: np.ndarray, src: int, dst: int, tab: np.ndarray,
                 limit: Optional[float] = None) -> Optional[Tuple[int, List[int]]]:
        """(minimal total cost, canonical cell path) from src to dst over the cells `sel` (bool over the n cells; both ends in it),
        or None. `limit`: a proved upper bound of the answer (Dijkstra stops there)."""
        if src == dst:
            return 0, [src]
        cells = np.nonzero(sel)[0]
        m = len(cells)
        loc = -np.ones(self.n, np.int64)
        loc[cells] = np.arange(m)
        k = sel[self.eu] & sel[self.ev]
        u, v, w, h = loc[self.eu[k]], loc[self.ev[k]], self.ew[k], self.eh[k]   # sorted by u (loc keeps the order)
        hin = np.arange(8)
        # transposed expanded graph: (v, h) <- (u, hin), weight w + turn(hin, h); one Dijkstra from the goal states (any heading)
        frm = (u[:, None] * 8 + hin[None, :]).ravel()
        to = np.repeat(v * 8 + h, 8)
        wt = (w[:, None] + tab[hin[None, :], h[:, None]]).ravel()
        gt = csr_matrix((wt, (to, frm)), shape=(8 * m, 8 * m))
        D = dijkstra(gt, directed=True, indices=loc[dst] * 8 + hin, min_only=True,
                     **({} if limit is None else {"limit": limit}))
        ptr = np.searchsorted(u, np.arange(m + 1))
        s, t = int(loc[src]), int(loc[dst])
        lo, hi = ptr[s], ptr[s + 1]
        val = w[lo:hi] + D[v[lo:hi] * 8 + h[lo:hi]]                  # the start heading is free
        if not len(val) or not np.isfinite(val.min()):
            return None
        cost = float(val.min())
        j = lo + int(np.argmin(np.where(val == cost, v[lo:hi], np.iinfo(np.int64).max)))
        path, cur, hd = [s, int(v[j])], int(v[j]), int(h[j])
        while cur != t:
            lo, hi = ptr[cur], ptr[cur + 1]
            val = w[lo:hi] + tab[hd, h[lo:hi]] + D[v[lo:hi] * 8 + h[lo:hi]]
            j = lo + int(np.argmin(np.where(val == D[cur * 8 + hd], v[lo:hi], np.iinfo(np.int64).max)))
            cur, hd = int(v[j]), int(h[j])
            path.append(cur)
        return int(cost), cells[path].tolist()

    def incumbent(self, src: int, dst: int, p0: List[int], cost0: int, tab: np.ndarray,
                  band: Optional[np.ndarray] = None) -> Tuple[int, List[int]]:
        """(J_inc, its path): P0 with its turn cost, or the expanded optimum on the band around P0 (`band`, default `near(p0)`) when
        that is cheaper; both are routes of this graph, so J_inc >= the optimum"""
        j0 = int(cost0) + path_turn(self.ys[p0], self.xs[p0], tab)
        if j0 == int(cost0):
            return j0, list(p0)
        got = self.expanded(self.near(p0) if band is None else band, src, dst, tab, limit=j0 + 0.5)
        return got if got is not None and got[0] < j0 else (j0, list(p0))

    def restricted(self, src: int, dst: int, j_inc: int, tab: np.ndarray,
                   d_t: Optional[np.ndarray] = None) -> Optional[Tuple[int, List[int]]]:
        """the exact expanded optimum, searched on E = {c : d(src, c) + d(c, dst) <= j_inc} (a route of cost j_inc exists)"""
        lim = j_inc + 0.5
        d_s = dijkstra(self.plain, directed=True, indices=src, limit=lim)
        if d_t is None:
            d_t = dijkstra(self.plain, directed=True, indices=dst, limit=lim)
        return self.expanded(d_s + d_t <= j_inc, src, dst, tab, limit=lim)


def route(g: Graph, src: int, dst: int, p0: List[int], cost0: int, tab: np.ndarray,
          d_t: Optional[np.ndarray] = None) -> Tuple[int, List[int]]:
    """The exact turn-cost optimum on `g` (canonical), given the canonical plain optimum P0 of `g` and its cost: `incumbent`, then
    `restricted` unless P0 has no turn (it is then the answer)."""
    j_inc, p_inc = g.incumbent(src, dst, p0, cost0, tab)
    if j_inc == int(cost0) and p_inc == list(p0):
        return j_inc, p_inc
    got = g.restricted(src, dst, j_inc, tab, d_t)
    return got if got is not None else (j_inc, p_inc)
