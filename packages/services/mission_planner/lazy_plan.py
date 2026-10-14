"""Read-only, tile-lazy chart planner over a stored version (Phase 0 of docs/scene_graph/planner_upgrade.md).

`PlanGraph` answers the calls the planner makes on a loaded graph (`params`, `snap`, `route`, `free_cell`) from a
`StoredView` only: the manifest (charts, links, tile table) and the owner tiles a request touches. No writer engine is
restored, no canvas is allocated; the work and the memory of a request scale with its corridor, not with the map.

`route` is the engine's search made sparse. Stage 1 is `ChartGraph._corridor_charts` (shortest chart path on the link
graph, plus its neighbours). Stage 2 is the engine's lattice search restricted to those charts: the same 8-neighbour
rule (a diagonal needs both orthogonal cells navigable), the same blocked-overlay mask (cell centres) and the same
integer edge cost (ceil(hypot * lattice / ROUTE_UNIT_M) units of 1e-4 m, `engine.ROUTE_UNIT_M`), so the minimal cost is
exact and `Route.cost` is the engine's. Ties are broken by the engine's canonical rule, not by a solver's predecessor
order: one Dijkstra from the goal, then a walk from the start that takes, among the neighbours on a shortest path, the
smallest cell index. Nodes are in row-major (y, x) order, so the route is the lexicographically smallest (y, x)
sequence among the minimal-cost paths, and the cells equal `ChartGraph.route`'s with or without an overlay
(`tests/unit/test_lazy_plan_equality.py`). Stage 1 (the chart corridor) only selects charts; its path choice does not
affect the cells of the result.

Differences from `ChartGraph.route`, all measured by `tests/unit/test_lazy_plan.py`:
* under an overlay the engine searches the whole graph; a lazy reader searches the charts that can hold a route no longer
  than the corridor's (an ellipse around the endpoints): the same answer, unless that needs more than `max_tiles`
  (then the corridor's route is returned and `last["exact"]` is False);
* a diagonal-only contact between two charts has no link; its width is taken from the charts' anchor clearance (the
  engine reads the cells' clearance, which needs a clearance plane: Phase 3), so `min_width` can differ there;
* the overlay search is bounded by the corridor route's own length (scipy `limit=`, and the cell ellipse that length
  implies): the optimum is never longer, so the answer is unchanged and the graph built is smaller;
* tile reads are single flight per tile (no global lock); a tile that cannot be read raises `TileUnavailable` and flags
  the graph (`unavailable`) for `GraphCache` to drop;
* with `safety` (clearance pricing, `docs/scene_graph/route_safety.md`) the corridor is a seed as under an overlay: the answer is
  the search of the charts that can hold a path of cost <= J_corridor; the clearance comes from the `v` planes of the tile and
  its 8 neighbours (a halo of cap / RES + 2 cells; exact for min(g, 1 m), so for margins up to 1 m);
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
from scipy import ndimage
from scipy.sparse import coo_matrix, csr_matrix
from scipy.sparse.csgraph import dijkstra

from packages.scene_graph.api import Blocked, Route, Safety, Snap, edge_weight
from packages.scene_graph.store_codec import read_planes

ROUTE_UNIT_M = 1e-4              # route cost unit (packages/scene_graph/engine.py)
RES = 0.05                      # costmap cell; the lattice is 2 x RES (store.py)
TILE_L = 64                     # lattice cells per tile side (store.py)
TILE_F = 2 * TILE_L             # costmap cells per tile side
_CLR_CAP_M = 1.0                # clearance is computed exactly up to this (S9 of docs/scene_graph/route_safety.md), capped here;
                                # pricing is off for a margin above it (scene_route.safety_for)
_CLR_HALO = int(math.ceil(_CLR_CAP_M / RES)) + 2   # costmap cells read around a tile to know min(g, cap) inside it
_OFF = 1 << 20                  # linear cell keys: (y + _OFF) * _KW + (x + _OFF), neighbours never wrap
_KW = 1 << 21


class CorridorTooLarge(Exception):
    """The corridor of a request needs more tiles than the cap: the caller falls back."""

    def __init__(self, tiles: int, cap: int):
        super().__init__("corridor needs %d tiles (cap %d)" % (tiles, cap))
        self.tiles, self.cap = tiles, cap


class TileUnavailable(Exception):
    """A stored tile of the version could not be read (a garbage-collected version, a backend error). The graph is spent:
    `PlanGraph.unavailable` is set and the cache drops it, so the next request opens the current version."""

    def __init__(self, tile: Tuple[int, int], cause: BaseException):
        super().__init__("tile %d,%d unavailable: %s: %s" % (tile[0], tile[1], type(cause).__name__, cause))
        self.tile, self.cause = tile, cause


class _Flight:
    """One tile fetch in progress: the leader fills value / error, the waiters wait on `done`."""
    __slots__ = ("done", "value", "error")

    def __init__(self):
        self.done = threading.Event()
        self.value = None
        self.error: Optional[BaseException] = None


class _TileCache(dict):
    """The view's tile cache, bounded: keeps `own` and `v` of the newest `max_tiles` tiles (about 35 KB each)."""

    def __init__(self, max_tiles: int):
        super().__init__()
        self.max_tiles = max(8, int(max_tiles))
        self._order: "OrderedDict[Tuple[int, int], None]" = OrderedDict()
        self._lock = threading.Lock()

    def __setitem__(self, k, v):
        if v is not None:
            v = read_planes(v)
        with self._lock:
            super().__setitem__(k, v)
            self._order[k] = None
            self._order.move_to_end(k)
            while len(self._order) > self.max_tiles:
                old, _ = self._order.popitem(last=False)
                super().pop(old, None)

    def values(self):
        with self._lock:                 # a snapshot: `nbytes` runs while other threads insert and evict
            return list(super().values())

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
        self.unavailable = False                # set when a tile could not be read (see TileUnavailable)
        self._mu = threading.Lock()             # guards `_inflight` only; no I/O runs under it
        self._inflight: Dict[Tuple[int, int], _Flight] = {}
        view._own = self._read                  # the view's own readers (snap, windows) share the single flight
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
        self._clr: "OrderedDict[Tuple[int, int], np.ndarray]" = OrderedDict()   # tile -> clearance (m, capped) of its lattice cells
        self._clr_max = max(8, int(tile_cache))
        self._clr_lock = threading.Lock()       # guards the dict only: the computation is pure and may run twice

    # ---- plumbing ----------------------------------------------------------------------------
    @property
    def nbytes(self) -> int:
        with self._clr_lock:
            clr = sum(a.nbytes for a in self._clr.values())
        return int(self.view.nbytes + self._adj.data.nbytes + self._g.data.nbytes + clr)

    def snap(self, x: float, y: float, max_dist: Optional[float] = None) -> Snap:
        return self.view.snap(x, y, max_dist)

    def _read(self, tx: int, ty: int) -> Optional[Dict[str, np.ndarray]]:
        """The owner tile (tx, ty), single flight: concurrent requests read different tiles in parallel and the same tile
        once. A failed read raises TileUnavailable to the reader and to every waiter, and is not cached."""
        key = (tx, ty)
        c = self.view._cache
        try:
            return c[key]
        except KeyError:
            pass
        with self._mu:
            try:
                return c[key]                    # filled between the miss above and the lock
            except KeyError:
                pass
            flight = self._inflight.get(key)
            leader = flight is None
            if leader:
                flight = self._inflight[key] = _Flight()
        if not leader:
            flight.done.wait()
            if flight.error is not None:
                raise flight.error
            return flight.value
        try:
            v = self.view.store._tile(self.view.map_id, self.view.man, tx, ty)
            c[key] = v
            flight.value = v if v is None else read_planes(v)
            return flight.value
        except BaseException as err:             # pylint: disable=broad-except
            if isinstance(err, Exception):
                self.unavailable = True
                err = err if isinstance(err, TileUnavailable) else TileUnavailable(key, err)
            flight.error = err
            raise err
        finally:
            with self._mu:
                self._inflight.pop(key, None)
            flight.done.set()

    _tile = _read

    def free_cell(self, ix: int, iy: int) -> bool:
        """Whether lattice cell (ix, iy) (global, centre ((ix + .5) h, (iy + .5) h)) is navigable; an absent tile is not."""
        t = self._tile(ix // TILE_L, iy // TILE_L)
        if t is None or "own" not in t:
            return False
        return bool(t["own"][iy % TILE_L, ix % TILE_L] > 0)

    # ---- clearance (route pricing) ---------------------------------------------------------------
    def _clearance_tile(self, tx: int, ty: int) -> np.ndarray:
        """[TILE_L, TILE_L] physical clearance (m, capped at `_CLR_CAP_M`) of the lattice cells of tile (tx, ty): the engine's
        `sp.clr` (distance to the nearest blocked or unknown costmap cell, sampled at the even costmap cell) from the stored `v`
        planes of the tile and a halo of `_CLR_HALO` cells around it (the neighbouring tiles read); an absent tile is unknown,
        hence blocked. Exact for min(g, cap): a blocked cell within cap of a cell of the tile is inside the window."""
        with self._clr_lock:
            hit = self._clr.get((tx, ty))
            if hit is not None:
                self._clr.move_to_end((tx, ty))
                return hit
        halo = _CLR_HALO
        self.view._need([(tx + i, ty + j) for j in (-1, 0, 1) for i in (-1, 0, 1)])     # the halo reads, checked as one batch
        size = TILE_F + 2 * halo
        free = np.zeros((size, size), bool)
        for j in (-1, 0, 1):
            for i in (-1, 0, 1):
                t = self._tile(tx + i, ty + j)
                if t is None or "v" not in t:
                    continue
                # the part of tile (tx + i, ty + j) inside the window, in window coordinates (the tile (tx, ty) starts at `halo`)
                y0, y1 = max(halo + j * TILE_F, 0), min(halo + (j + 1) * TILE_F, size)
                x0, x1 = max(halo + i * TILE_F, 0), min(halo + (i + 1) * TILE_F, size)
                v = t["v"][y0 - (halo + j * TILE_F):y1 - (halo + j * TILE_F), x0 - (halo + i * TILE_F):x1 - (halo + i * TILE_F)]
                free[y0:y1, x0:x1] = (v != 255) & (v < 100)
        dt = ndimage.distance_transform_edt(free) * RES
        out = np.minimum(dt[halo:halo + TILE_F:2, halo:halo + TILE_F:2], _CLR_CAP_M)
        out.flags.writeable = False
        with self._clr_lock:
            self._clr[(tx, ty)] = out
            while len(self._clr) > self._clr_max:
                self._clr.popitem(last=False)
        return out

    def _clearance(self, ys: np.ndarray, xs: np.ndarray) -> np.ndarray:
        """clearance (m, capped) of the global lattice cells (ys, xs)"""
        out = np.empty(len(ys))
        ty, tx = ys // TILE_L, xs // TILE_L
        code = (ty + _OFF) * _KW + (tx + _OFF)
        for c in np.unique(code):
            m = code == c
            i = int(np.nonzero(m)[0][0])
            out[m] = self._clearance_tile(int(tx[i]), int(ty[i]))[ys[m] % TILE_L, xs[m] % TILE_L]
        return out

    def price_cell(self, ix: int, iy: int, safety: Safety) -> Optional[float]:
        """`safety.price(g)` of the navigable global lattice cell (ix, iy), None when it is not navigable (as the engine's;
        exact for a margin <= `_CLR_CAP_M`)."""
        g = self._clearance_cell(ix, iy)
        return None if g is None else float(safety.price(g))

    def deficit_cell(self, ix: int, iy: int, margin: float) -> Optional[float]:
        """max(0, margin - g) of the navigable global lattice cell (ix, iy), None when it is not navigable (as the engine's;
        exact for margin <= `_CLR_CAP_M`)."""
        g = self._clearance_cell(ix, iy)
        return None if g is None else max(0.0, float(margin) - g)

    def _clearance_cell(self, ix: int, iy: int) -> Optional[float]:
        t = self._tile(ix // TILE_L, iy // TILE_L)
        if t is None or "own" not in t or not t["own"][iy % TILE_L, ix % TILE_L] > 0:
            return None
        return float(self._clearance_tile(ix // TILE_L, iy // TILE_L)[iy % TILE_L, ix % TILE_L])

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

    def _reach(self) -> float:
        """how far a chart's cell lies from its anchor: the anchor is the chart's centre and every cell is within chart_radius
        of the net point, so within 2 x chart_radius (the chart diameter) of the anchor"""
        return 2.0 * float(self.params.chart_radius)

    def _ellipse(self, src, dst, length: float) -> Set[int]:
        h = float(self.params.lattice)
        r = self._reach() + h
        a = np.array([(2 * src[1] + 1) * RES, (2 * src[0] + 1) * RES])
        b = np.array([(2 * dst[1] + 1) * RES, (2 * dst[0] + 1) * RES])
        d = np.hypot(*(self._anchor - a).T) + np.hypot(*(self._anchor - b).T)
        return set(np.nonzero(d <= length + 2 * r)[0].tolist())

    # ---- stage 2 -----------------------------------------------------------------------------
    def _tiles_for(self, keep: Iterable[int]) -> List[Tuple[int, int]]:
        """lattice tiles that hold every cell of the charts (a chart reaches its diameter from its anchor) and the cells
        4-adjacent to them (the orthogonals of a diagonal step)"""
        r = int(math.ceil(self._reach() / float(self.params.lattice))) + 2
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
        self.view._need(tiles)
        ch, ys, xs = [], [], []
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

    @staticmethod
    def _centre(cell) -> np.ndarray:
        return np.array([(2 * cell[1] + 1) * RES, (2 * cell[0] + 1) * RES])

    def _within(self, tiles: List[Tuple[int, int]], a: np.ndarray, b: np.ndarray, limit: float) -> List[Tuple[int, int]]:
        """the tiles that can hold a cell c, or a neighbour of one, with |c - a| + |c - b| <= limit: a lattice step is at
        least the Euclidean distance of its cell centres, so no route of length <= limit leaves that ellipse"""
        h = float(self.params.lattice)
        out = []
        for (tx, ty) in tiles:
            lo = np.array([tx * TILE_L * h, ty * TILE_L * h]); hi = lo + TILE_L * h
            da = np.hypot(*(np.maximum(0.0, np.maximum(lo - a, a - hi))))
            db = np.hypot(*(np.maximum(0.0, np.maximum(lo - b, b - hi))))
            if da + db <= limit + 2 * h:
                out.append((tx, ty))
        return out

    def _search(self, keep: Set[int], src_cell, dst_cell, blocked: Optional[Blocked], limit: Optional[float] = None,
                safety: Optional[Safety] = None, jlimit: Optional[float] = None):
        """(length, path as (chart, y, x) rows, tiles, cost) of the lattice route inside `keep`, or None. `limit`, when given,
        is a proved upper bound of the answer (a route of that length exists in `keep`): cells and tiles that no route
        of that length can use are left out and Dijkstra stops there; the answer, and its tie-breaks, are unchanged.
        With `safety` the edge cost is the priced one (as `ChartGraph._edge_cost`); `limit` is then the geometric bound
        (metres) and `jlimit` the bound of the cost J (integer units) that Dijkstra stops at."""
        tiles = self._tiles_for(keep)
        if limit is not None:
            a, b = self._centre(src_cell), self._centre(dst_cell)
            tiles = self._within(tiles, a, b, limit)
        if len(tiles) > self.max_tiles:
            raise CorridorTooLarge(len(tiles), self.max_tiles)
        key, ch, ys, xs = self._cells(tiles)
        if limit is not None and len(key):
            # the plane keeps the cells of the ellipse and one lattice step around it (the orthogonals of a diagonal)
            cx, cy = (2 * xs + 1) * RES, (2 * ys + 1) * RES
            near = np.hypot(cx - a[0], cy - a[1]) + np.hypot(cx - b[0], cy - b[1]) <= limit + 2 * float(self.params.lattice) + 1e-6
            key, ch, ys, xs = key[near], ch[near], ys[near], xs[near]
        n = len(key)
        if n == 0:
            return None
        inkeep = np.zeros(len(self.keys) + 1, bool)
        inkeep[list(keep)] = True
        member = inkeep[ch] & (ch >= 0)
        member &= self._alive(blocked, ys, xs)
        if limit is not None:
            cx, cy = (2 * xs + 1) * RES, (2 * ys + 1) * RES
            member &= np.hypot(cx - a[0], cy - a[1]) + np.hypot(cx - b[0], cy - b[1]) <= limit + 1e-6
        node = np.cumsum(member) - 1                       # compact index among members, row-major as the engine's
        h = float(self.params.lattice)

        def at(k):                                         # position of linear keys in `key` (-1 absent)
            p = np.minimum(np.searchsorted(key, k), n - 1)
            return np.where(key[p] == k, p, -1)
        R, C, Ln, Lm = [], [], [], []
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
            R.append(node[mi[a]]); C.append(node[q[a]])
            if safety is None:
                # the engine's integer edge cost (engine.ROUTE_UNIT_M): length in units of 1e-4 m, rounded up
                Ln.append(np.full(len(a), max(math.ceil(math.hypot(dy, dx) * h / ROUTE_UNIT_M - 1e-6), 1.0)))
            else:
                Lm.append(np.full(len(a), math.hypot(dy, dx) * h))
        R, C = np.concatenate(R), np.concatenate(C)
        nm = len(mi)
        if safety is None:
            Ln = np.concatenate(Ln)
        else:
            # priced edge cost: the engine's `edge_weight` on the clearance of the member cells
            dn = safety.price(self._clearance(ys[mi], xs[mi]))
            Ln = edge_weight(np.concatenate(Lm) / ROUTE_UNIT_M, dn[R] + dn[C], safety.rate)

        def find(cell):
            p = int(at(np.array([(cell[0] + _OFF) * _KW + (cell[1] + _OFF)], np.int64))[0])
            return None if p < 0 or not member[p] else int(node[p])
        s, d = find(src_cell), find(dst_cell)
        if s is None or d is None:
            return None
        g = coo_matrix((np.concatenate([Ln, Ln]), (np.concatenate([R, C]), np.concatenate([C, R]))), shape=(nm, nm)).tocsr()
        # one Dijkstra from the goal, then the canonical walk from the start (engine.ChartGraph.route): the neighbour on a
        # shortest path with the smallest cell index; nodes are in row-major (y, x) order, so this is the lexicographic
        # minimum of the (y, x) sequence among the minimal-cost paths.
        stop = jlimit if jlimit is not None else (None if limit is None else limit / ROUTE_UNIT_M + 1.0)
        dist = dijkstra(g, directed=False, indices=d, **({} if stop is None else {"limit": stop}))
        if not np.isfinite(dist[s]):
            return None
        path = [s]
        while path[-1] != d:
            u = path[-1]
            lo, hi = g.indptr[u], g.indptr[u + 1]
            nb = g.indices[lo:hi]
            path.append(int(nb[(g.data[lo:hi] + dist[nb]) == dist[u]].min()))
        pa = np.array(path)
        rows = mi[pa]
        length = float(np.hypot(np.diff(ys[rows]), np.diff(xs[rows])).sum() * h)
        return length, np.stack([ch[rows], ys[rows], xs[rows]], 1), len(tiles), int(dist[s])

    # ---- route -------------------------------------------------------------------------------
    def route(self, a: str, b: str, blocked: Optional[Blocked] = None, start: Optional[Tuple[float, float]] = None,
              goal: Optional[Tuple[float, float]] = None, safety: Optional[Safety] = None) -> Optional[Route]:
        """`ChartGraph.route` from the stored tiles it needs (see the module docstring for the differences). With `safety`
        the objective is the priced cost J (`Route.cost`): the corridor route is the seed, the answer the search of the
        charts that can hold a path of cost <= J_corridor (length <= J_corridor * 1e-4 m, S10), like an overlay."""
        if safety is not None and safety.rate == 0.0:
            safety = None                                        # no price: today's route, the corridor included (S8)
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
                rings, found = hops, self._search(self._rings(path, hops), src, dst, blocked, safety=safety)
                if found is not None or not overlay:
                    break
            if (overlay or safety is not None) and found is not None:
                # The engine searches every chart. Any route of length <= L lies in the charts whose anchors are within
                # |a - p| + |p - b| <= L + 2 (chart radius + lattice) (a lattice step is at least the Euclidean one):
                # search those, the answer is the engine's. Under `safety` L is J_corridor * 1e-4 (J >= length / 1e-4, S7).
                if safety is None:
                    slack = 1 + ROUTE_UNIT_M / float(self.params.lattice)   # a min-cost path may be this much longer than found
                    bound, jlimit = found[0] * slack, None
                else:
                    bound, jlimit = found[3] * ROUTE_UNIT_M, found[3] * (1 + 1e-9) + 1e-9
                keep = self._ellipse(src, dst, bound) | self._rings(path, rings)
                try:
                    # the corridor's own route is in `keep`: it bounds the optimum from above
                    found = self._search(keep, src, dst, blocked, limit=bound * (1 + 1e-9) + 1e-9, safety=safety,
                                         jlimit=jlimit) or found
                except CorridorTooLarge:
                    exact = False                              # the corridor's route stands: valid, maybe longer
        if found is None:
            whole = True                                       # nothing in the corridor: the engine searches every chart
            found = self._search(set(range(len(self.keys))), src, dst, blocked, safety=safety)
        if found is not None:
            tiles = found[2]
        self.last = dict(rings=rings, whole=whole, exact=exact, tiles=tiles)
        if found is None:
            return None
        length, rows, _, cost = found
        charts: List[str] = []
        for j in rows[:, 0].tolist():
            kk = self.keys[j]
            if not charts or charts[-1] != kk:
                charts.append(kk)
        nodes = [((2 * int(x) + 1) * RES, (2 * int(y) + 1) * RES) for y, x in rows[:, 1:].tolist()]
        return Route(charts=charts, length=length, min_width=self._route_width(rows), nodes=nodes, cost=cost, exact=exact)

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
