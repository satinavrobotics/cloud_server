"""Read-only, tile-lazy chart planner over a stored version (Phase 0 of docs/scene_graph/planner_upgrade.md).

`PlanGraph` answers the calls the planner makes on a loaded graph (`params`, `snap`, `route`, `free_cell`) from a
`StoredView` only: the manifest (charts, links, tile table) and the owner tiles a request touches. No writer engine is
restored, no canvas is allocated; the work and the memory of a request scale with its corridor, not with the map.

`route` is the engine's search made sparse. Stage 1 is `ChartGraph._corridor_charts` (shortest chart path on the link
graph, plus its neighbours). Stage 2 is the engine's lattice search restricted to those charts: the same 8-neighbour
rule (a diagonal needs both orthogonal cells navigable, cost hypot * lattice), the same blocked-overlay mask (cell
centres), scipy Dijkstra over nodes in the same row-major order, so equal length and the same cells.

Differences from `ChartGraph.route`, all measured by `tests/unit/test_lazy_plan.py`:
* under an overlay the engine searches the whole graph; a lazy reader searches the charts that can hold a route no longer
  than the corridor's (an ellipse around the endpoints): the same answer, unless that needs more than `max_tiles`
  (then the corridor's route is returned and `last["exact"]` is False);
* a diagonal-only contact between two charts has no link; its width is taken from the charts' anchor clearance (the
  engine reads the cells' clearance, which needs a clearance plane: Phase 3), so `min_width` can differ there;
* `max_tiles` caps a request: a bigger corridor raises `CorridorTooLarge` (the planner falls back to the legacy one).

It reads `StoredView` internals (`_own`, `_cache`, `_idx`); that is the cost of a LACI-only phase. Phase 1 gives these
a public home next to the engine (`store.open_plan`).
"""
from __future__ import annotations

import math
import threading
from collections import OrderedDict
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import numpy as np
from scipy.sparse import coo_matrix, csr_matrix
from scipy.sparse.csgraph import dijkstra

from packages.scene_graph.api import Blocked, Route, Snap

RES = 0.05                      # costmap cell; the lattice is 2 x RES (store.py)
TILE_L = 64                     # lattice cells per tile side (store.py)
_OFF = 1 << 20                  # linear cell keys: (y + _OFF) * _KW + (x + _OFF), neighbours never wrap
_KW = 1 << 21


class CorridorTooLarge(Exception):
    """The corridor of a request needs more tiles than the cap: the caller falls back."""

    def __init__(self, tiles: int, cap: int):
        super().__init__("corridor needs %d tiles (cap %d)" % (tiles, cap))
        self.tiles, self.cap = tiles, cap


class _TileCache(dict):
    """The view's tile cache, bounded: keeps `own` and `v` of the newest `max_tiles` tiles (about 35 KB each)."""

    def __init__(self, max_tiles: int):
        super().__init__()
        self.max_tiles = max(8, int(max_tiles))
        self._order: "OrderedDict[Tuple[int, int], None]" = OrderedDict()
        self._lock = threading.Lock()

    def __setitem__(self, k, v):
        if v is not None:
            v = {n: v[n] for n in ("own", "v") if n in v}
        with self._lock:
            super().__setitem__(k, v)
            self._order[k] = None
            self._order.move_to_end(k)
            while len(self._order) > self.max_tiles:
                old, _ = self._order.popitem(last=False)
                super().pop(old, None)

    def __getitem__(self, k):
        v = super().__getitem__(k)
        with self._lock:
            if k in self._order:
                self._order.move_to_end(k)
        return v


class PlanGraph:
    """The planner's view of one stored version. Thread-safe for concurrent `snap` / `route` / `free_cell`."""

    def __init__(self, view, max_tiles: int = 400, tile_cache: int = 512):
        self.view = view
        view._cache = _TileCache(tile_cache)
        self.version = view.version
        self.params = SimpleNamespace(**view.params)
        self.max_tiles = int(max_tiles)
        self._fetch = threading.Lock()
        man = view.man
        by_id = {c["id"]: c for c in man["charts"]}
        order = man.get("order")        # chart ids in ENGINE order: ties in its searches break by it
        charts = [by_id[i] for i in order] if order and len(order) == len(by_id) and set(order) == set(by_id) \
            else list(man["charts"])
        self.keys: List[str] = [c["key"] for c in charts]
        k = len(self.keys)
        self._index = {key: j for j, key in enumerate(self.keys)}
        self._cell = np.array([c["cell"] for c in charts], np.int64).reshape(-1, 2)      # global lattice (y, x)
        self._anchor = np.array([c["anchor"] for c in charts], float).reshape(-1, 2)
        self._clear = np.array([c["clearance"] for c in charts], float)
        self._lut = np.full(max(by_id, default=0) + 1, -1, np.int32)                      # stored id -> chart index
        for j, c in enumerate(charts):
            self._lut[c["id"]] = j
        best: Dict[Tuple[int, int], float] = {}
        width: Dict[Tuple[int, int], float] = {}
        for l in man["links"]:
            a, b = self._index[l["a"]], self._index[l["b"]]
            p = (min(a, b), max(a, b))
            best[p] = min(best.get(p, math.inf), float(l["length"]))
            width[p] = max(width.get(p, 0.0), float(l["width"]))
        self._width = width
        ab = np.array(list(best), np.int64).reshape(-1, 2)
        ln = np.array(list(best.values()), float)
        self._adj = csr_matrix((np.ones(2 * len(ab)), (np.concatenate([ab[:, 0], ab[:, 1]]),
                                                       np.concatenate([ab[:, 1], ab[:, 0]]))), shape=(k, k))
        pos = ln > 0.0               # the engine reads a zero length as "no edge" in its search, as a neighbour all the same
        self._g = csr_matrix((np.concatenate([ln[pos], ln[pos]]), (np.concatenate([ab[pos, 0], ab[pos, 1]]),
                                                                    np.concatenate([ab[pos, 1], ab[pos, 0]]))), shape=(k, k))
        self._g.sort_indices()       # the engine's dense matrix reaches Dijkstra with sorted columns: same tie-breaks
        self.last: Dict[str, Any] = {}

    # ---- plumbing ----------------------------------------------------------------------------
    @property
    def nbytes(self) -> int:
        return int(self.view.nbytes + self._adj.data.nbytes + self._g.data.nbytes)

    def snap(self, x: float, y: float, max_dist: Optional[float] = None) -> Snap:
        with self._fetch:
            return self.view.snap(x, y, max_dist)

    def locate(self, x: float, y: float, max_dist: Optional[float] = None) -> Optional[str]:
        return self.snap(x, y, max_dist).key

    def _tile(self, tx: int, ty: int) -> Optional[Dict[str, np.ndarray]]:
        c = self.view._cache
        if (tx, ty) in c:
            return c[(tx, ty)]
        with self._fetch:
            return self.view._own(tx, ty)

    def free_cell(self, ix: int, iy: int) -> bool:
        """Whether lattice cell (ix, iy) (global, centre ((ix + .5) h, (iy + .5) h)) is navigable; an absent tile is not."""
        t = self._tile(ix // TILE_L, iy // TILE_L)
        if t is None or "own" not in t:
            return False
        return bool(t["own"][iy % TILE_L, ix % TILE_L] > 0)

    # ---- stage 1 -----------------------------------------------------------------------------
    def _path(self, ja: int, jb: int) -> Optional[List[int]]:
        d, pr = dijkstra(self._g, directed=False, indices=ja, return_predecessors=True)
        if not np.isfinite(d[jb]):
            return None
        path = [jb]
        while path[-1] != ja:
            path.append(int(pr[path[-1]]))
        return path

    def _rings(self, path: List[int], hops: int) -> Set[int]:
        keep = set(path)
        ring = set(path)
        for _ in range(hops):
            nxt: Set[int] = set()
            for c in ring:
                nxt.update(self._adj.indices[self._adj.indptr[c]:self._adj.indptr[c + 1]].tolist())
            ring = nxt - keep
            keep |= nxt
        return keep

    def _ellipse(self, src, dst, length: float) -> Set[int]:
        h = float(self.params.lattice)
        r = 0.4 * float(self.params.sight_range) + h
        a = np.array([(2 * src[1] + 1) * RES, (2 * src[0] + 1) * RES])
        b = np.array([(2 * dst[1] + 1) * RES, (2 * dst[0] + 1) * RES])
        d = np.hypot(*(self._anchor - a).T) + np.hypot(*(self._anchor - b).T)
        return set(np.nonzero(d <= length + 2 * r)[0].tolist())

    # ---- stage 2 -----------------------------------------------------------------------------
    def _tiles_for(self, keep: Iterable[int]) -> List[Tuple[int, int]]:
        """lattice tiles that hold every cell of the charts (a chart reaches 0.4 sight range from its anchor) and the cells
        4-adjacent to them (the orthogonals of a diagonal step)"""
        r = int(math.ceil(0.4 * float(self.params.sight_range) / float(self.params.lattice))) + 2
        c = self._cell[sorted(keep)]
        out: Set[Tuple[int, int]] = set()
        for (cy, cx) in c.tolist():
            for ty in range((cy - r) // TILE_L, (cy + r) // TILE_L + 1):
                for tx in range((cx - r) // TILE_L, (cx + r) // TILE_L + 1):
                    out.add((tx, ty))
        have = self.view.man["tiles"]
        return sorted(t for t in out if "%d,%d" % t in have)

    def _cells(self, tiles: List[Tuple[int, int]]):
        """linear key, chart index, global (y, x) of every owned cell of the tiles, ascending by key (row-major)"""
        with self._fetch:
            self.view._need(tiles)
        ks, ch, ys, xs = [], [], [], []
        lut = self._lut
        for (tx, ty) in tiles:
            t = self._tile(tx, ty)
            if t is None or "own" not in t:
                continue
            o = t["own"]
            iy, ix = np.nonzero(o > 0)
            if not len(iy):
                continue
            gy, gx = iy + ty * TILE_L, ix + tx * TILE_L
            ys.append(gy); xs.append(gx)
            ch.append(lut[np.minimum(o[iy, ix], len(lut) - 1)])
        if not ys:
            e = np.zeros(0, np.int64)
            return e, e.astype(np.int32), e, e
        ys, xs, ch = np.concatenate(ys), np.concatenate(xs), np.concatenate(ch)
        key = (ys + _OFF) * _KW + (xs + _OFF)
        o = np.argsort(key, kind="stable")
        return key[o], ch[o], ys[o], xs[o]

    @staticmethod
    def _alive(blocked: Optional[Blocked], ys: np.ndarray, xs: np.ndarray) -> np.ndarray:
        m = np.zeros(len(ys), bool)
        if blocked is None or not (blocked.discs or blocked.segments):
            return ~m
        cx = (2 * xs + 1) * RES
        cy = (2 * ys + 1) * RES
        for x, y, r in blocked.discs:
            m |= (cx - x) ** 2 + (cy - y) ** 2 <= r * r
        for (x0, y0), (x1, y1), hw in blocked.segments:
            dx, dy = x1 - x0, y1 - y0
            L2 = dx * dx + dy * dy
            t = np.clip(((cx - x0) * dx + (cy - y0) * dy) / L2, 0, 1) if L2 > 0 else np.zeros_like(cx)
            m |= (cx - (x0 + t * dx)) ** 2 + (cy - (y0 + t * dy)) ** 2 <= hw * hw
        return ~m

    def _search(self, keep: Set[int], src_cell, dst_cell, blocked: Optional[Blocked]):
        """(length, path as (chart, y, x) rows, tiles) of the lattice route inside `keep`, or None"""
        tiles = self._tiles_for(keep)
        if len(tiles) > self.max_tiles:
            raise CorridorTooLarge(len(tiles), self.max_tiles)
        key, ch, ys, xs = self._cells(tiles)
        n = len(key)
        if n == 0:
            return None
        inkeep = np.zeros(len(self.keys) + 1, bool)
        inkeep[list(keep)] = True
        member = inkeep[ch] & (ch >= 0)
        member &= self._alive(blocked, ys, xs)
        node = np.cumsum(member) - 1                       # compact index among members, row-major as the engine's
        h = float(self.params.lattice)

        def at(k):                                         # position of linear keys in `key` (-1 absent)
            p = np.minimum(np.searchsorted(key, k), n - 1)
            return np.where(key[p] == k, p, -1)
        R, C, Ln = [], [], []
        mi = np.nonzero(member)[0]
        base = key[mi]
        for dy, dx in ((0, 1), (1, 0), (1, 1), (1, -1)):
            q = at(base + dy * _KW + dx)
            ok = q >= 0
            ok[ok] &= member[q[ok]]
            if dy and dx:
                o1 = at(base + dx); o2 = at(base + dy * _KW)       # (y, x+dx) and (y+dy, x): navigable in the plane
                ok &= (o1 >= 0) & (o2 >= 0)
            a = np.nonzero(ok)[0]
            R.append(node[mi[a]]); C.append(node[q[a]]); Ln.append(np.full(len(a), math.hypot(dy, dx) * h))
        R, C, Ln = np.concatenate(R), np.concatenate(C), np.concatenate(Ln)
        nm = len(mi)

        def find(cell):
            p = int(at(np.array([(cell[0] + _OFF) * _KW + (cell[1] + _OFF)], np.int64))[0])
            return None if p < 0 or not member[p] else int(node[p])
        s, d = find(src_cell), find(dst_cell)
        if s is None or d is None:
            return None
        g = coo_matrix((np.concatenate([Ln, Ln]), (np.concatenate([R, C]), np.concatenate([C, R]))), shape=(nm, nm)).tocsr()
        dist, pr = dijkstra(g, directed=False, indices=s, return_predecessors=True)
        if not np.isfinite(dist[d]):
            return None
        path = [d]
        while path[-1] != s:
            path.append(int(pr[path[-1]]))
        path = path[::-1]
        rows = mi[np.array(path)]
        return float(dist[d]), np.stack([ch[rows], ys[rows], xs[rows]], 1), len(tiles)

    # ---- route -------------------------------------------------------------------------------
    def route(self, a: str, b: str, blocked: Optional[Blocked] = None, start: Optional[Tuple[float, float]] = None,
              goal: Optional[Tuple[float, float]] = None) -> Optional[Route]:
        """`ChartGraph.route` from the stored tiles it needs (see the module docstring for the differences)."""
        ja, jb = self._index.get(a), self._index.get(b)
        if ja is None or jb is None:
            return None
        src, dst = tuple(int(v) for v in self._cell[ja]), tuple(int(v) for v in self._cell[jb])
        c0, c1 = self._cell_at(start, ja), self._cell_at(goal, jb)
        src, dst = c0 or src, c1 or dst
        overlay = blocked is not None and bool(blocked.discs or blocked.segments)
        path = self._path(ja, jb)
        found, rings, whole, exact, tiles = None, 0, path is None, True, 0
        if path is not None:
            for hops in ((1, 2, 3) if overlay else (1,)):     # the engine's corridor; under an overlay widen until one exists
                rings, found = hops, self._search(self._rings(path, hops), src, dst, blocked)
                if found is not None or not overlay:
                    break
            if overlay and found is not None:
                # The engine searches every chart. Any route of length <= L lies in the charts whose anchors are within
                # |a - p| + |p - b| <= L + 2 (chart radius + lattice) (a lattice step is at least the Euclidean one):
                # search those, the answer is the engine's.
                keep = self._ellipse(src, dst, found[0]) | self._rings(path, rings)
                try:
                    found = self._search(keep, src, dst, blocked) or found
                except CorridorTooLarge:
                    exact = False                              # the corridor's route stands: valid, maybe longer
        if found is None:
            whole = True                                       # nothing in the corridor: the engine searches every chart
            found = self._search(set(range(len(self.keys))), src, dst, blocked)
        if found is not None:
            tiles = found[2]
        self.last = dict(rings=rings, whole=whole, exact=exact, tiles=tiles)
        if found is None:
            return None
        length, rows, _ = found
        charts: List[str] = []
        for j in rows[:, 0].tolist():
            kk = self.keys[j]
            if not charts or charts[-1] != kk:
                charts.append(kk)
        nodes = [((2 * int(x) + 1) * RES, (2 * int(y) + 1) * RES) for y, x in rows[:, 1:].tolist()]
        return Route(charts=charts, length=length, min_width=self._route_width(rows), nodes=nodes)

    def _cell_at(self, p: Optional[Tuple[float, float]], j: int) -> Optional[Tuple[int, int]]:
        """global lattice cell (y, x) containing the point when it is owned by chart j, else None (the anchor is used)"""
        if p is None:
            return None
        iy, ix = int(math.floor(p[1] / RES)) // 2, int(math.floor(p[0] / RES)) // 2
        t = self._tile(ix // TILE_L, iy // TILE_L)
        if t is None or "own" not in t:
            return None
        o = int(t["own"][iy % TILE_L, ix % TILE_L])
        if o <= 0 or o >= len(self._lut) or int(self._lut[o]) != j:
            return None
        return (iy, ix)

    def _route_width(self, rows: np.ndarray) -> float:
        """`ChartGraph._route_width` on the stored links; a diagonal-only contact (no link) reads the charts' anchor
        clearance, the engine the cells' own"""
        w = math.inf
        ch = rows[:, 0]
        for j0, j1 in zip(ch[:-1].tolist(), ch[1:].tolist()):
            if j0 == j1:
                continue
            p = (min(j0, j1), max(j0, j1))
            cand = self._width.get(p)
            w = min(w, cand if cand is not None else 2.0 * float(min(self._clear[j0], self._clear[j1])))
        return float(w) if math.isfinite(w) else float(2.0 * self._clear[int(ch[0])])


def open_plan(store, map_id: str, version: Optional[int] = None, **kw) -> PlanGraph:
    """The lazy planner graph of a stored version: the manifest only, no cells."""
    return PlanGraph(store.view(map_id, version), **kw)
