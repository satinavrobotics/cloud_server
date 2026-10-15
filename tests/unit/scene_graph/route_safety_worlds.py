"""Worlds and an independent reference for the route-safety tests (docs/scene_graph/route_safety.md section 4).

`random_world(seed)` is a room with random pillars and a wall with a door, driven by a lawnmower; `Reference` recomputes the clearance
field, the lattice edges and the priced integer cost from the engine's occupancy plane alone (scipy EDT, heapq Dijkstra, python ints),
sharing no code with the engine's route search or with the lazy reader."""
import heapq
import math
import random
from typing import Dict, List, Optional, Tuple

import numpy as np
from scipy import ndimage

from packages.scene_graph.api import Safety, turn_weight
from packages.scene_graph.engine import ChartGraph

from . import chart_worlds as cw

RES = 0.05
H = 0.1                      # lattice step (SceneParams default)
UNIT = 1e-4


def random_world(seed: int, W: float = 8.0, Hh: float = 6.0):
    """(world, way): a W x Hh room, random pillars, one wall with a door, a lawnmower drive over the free spots"""
    rnd = random.Random(seed)
    w = cw._blank(W, Hh)
    if rnd.random() < 0.7:                                    # a dividing wall with a door
        x = rnd.uniform(3.0, W - 3.0)
        d = rnd.uniform(0.8, 1.6)
        y0 = rnd.uniform(1.2, Hh - 1.2 - d)
        cw._rect(w, x, 0.2, x + 0.15, Hh - 0.2, 100)
        cw._rect(w, x, y0, x + 0.15, y0 + d, 0)
    for _ in range(rnd.randint(2, 5)):
        x, y = rnd.uniform(1.0, W - 2.0), rnd.uniform(0.8, Hh - 1.8)
        cw._rect(w, x, y, x + rnd.uniform(0.3, 1.2), y + rnd.uniform(0.3, 1.2), 100)
    way = []
    rows = [0.8 + 1.1 * k for k in range(int((Hh - 1.2) / 1.1) + 1)]
    for j, y in enumerate(rows):
        xs = [0.8 + 0.8 * k for k in range(int((W - 1.6) / 0.8) + 1)]
        for x in (xs if j % 2 == 0 else xs[::-1]):
            iy, ix = int(round(y / RES)), int(round(x / RES))
            if (w[iy - 7:iy + 8, ix - 7:ix + 8] == 0).all():  # the robot's half side (0.25 m) around the pose is free
                way.append((x, y))
    return w, way


def engine_of(world_way, batch: int = 9) -> ChartGraph:
    w, way = world_way
    leaves = cw.drive(w, way)
    g = ChartGraph()
    for i in range(0, len(leaves), batch):
        g.fold_many(leaves[i:i + batch])
    return g


def pairs(g: ChartGraph, n: int, seed: int) -> List[Tuple[str, str]]:
    keys = g._solved.keys if g._solved is not None else []
    allp = [(a, b) for a in keys for b in keys if a != b]
    random.Random(seed).shuffle(allp)
    return allp[:n]


def _block_min(a):
    """minimum over each 2 x 2 block of cells (a lattice node's clearance), written here from the definition"""
    out = a[::2, ::2].copy()
    for dy, dx in ((0, 1), (1, 0), (1, 1)):
        v = a[dy::2, dx::2]
        o = out[:v.shape[0], :v.shape[1]]
        np.minimum(o, v, out=o)
    return out


class Reference:
    """Independent clearance, edges and priced cost of one engine state (lattice cells are addressed (y, x) in the canvas)."""

    def __init__(self, g: ChartGraph):
        s = g._solved
        self.g, self.s = g, s
        V = g._V
        free = (V != 255) & (V < 100)
        self.clr = _block_min(ndimage.distance_transform_edt(free) * RES)       # block minimum; g(c), m, unknown counted blocked
        self.nav = s.sp.idx >= 0

    def deficit(self, y: int, x: int, margin: float) -> float:
        return max(0.0, margin - float(self.clr[y, x]))

    def weight(self, a: Tuple[int, int], b: Tuple[int, int], safety: Optional[Safety]) -> int:
        l = math.hypot(a[0] - b[0], a[1] - b[1]) * H
        if safety is None:
            return max(math.ceil(l / UNIT - 1e-6), 1)
        da, db = self.deficit(*a, safety.margin), self.deficit(*b, safety.margin)
        return max(math.ceil(l / UNIT * (1.0 + safety.rate * (da + db) / 2.0) - 1e-6), 1)

    def neighbours(self, c: Tuple[int, int], alive: Optional[np.ndarray] = None):
        nav = self.nav
        end = nav if alive is None else alive                  # a route's cells must be alive ...
        Hn, Wn = nav.shape
        y, x = c
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if not (dy or dx):
                    continue
                yy, xx = y + dy, x + dx
                if not (0 <= yy < Hn and 0 <= xx < Wn) or not end[yy, xx]:
                    continue
                if dy and dx and not (nav[y, xx] and nav[yy, x]):         # ... a diagonal needs both orthogonal cells navigable
                    continue
                yield (yy, xx)

    def path_cost(self, cells: List[Tuple[int, int]], safety: Optional[Safety]) -> int:
        return sum(self.weight(p, q, safety) for p, q in zip(cells[:-1], cells[1:]))

    def cells_of(self, route) -> List[Tuple[int, int]]:
        """canvas lattice cells of a Route's nodes (centres in the map frame)"""
        out = []
        for x, y in route.nodes:
            out.append(((int(math.floor(y / RES)) - self.g._cy0) // 2, (int(math.floor(x / RES)) - self.g._cx0) // 2))
        return out

    def dijkstra(self, src: Tuple[int, int], dst: Tuple[int, int], safety: Optional[Safety],
                 alive: Optional[np.ndarray] = None) -> Tuple[Optional[int], Optional[List[Tuple[int, int]]]]:
        """(minimal priced cost, the canonical path: among minimal-cost paths the lexicographic minimum of the (y, x) sequence)"""
        dist: Dict[Tuple[int, int], int] = {dst: 0}
        pq = [(0, dst)]
        while pq:
            d, u = heapq.heappop(pq)
            if d > dist[u]:
                continue
            for v in self.neighbours(u, alive):
                nd = d + self.weight(u, v, safety)
                if nd < dist.get(v, math.inf):
                    dist[v] = nd
                    heapq.heappush(pq, (nd, v))
        if src not in dist:
            return None, None
        path = [src]
        while path[-1] != dst:
            u = path[-1]
            path.append(min(v for v in self.neighbours(u, alive) if dist.get(v, math.inf) + self.weight(u, v, safety) == dist[u]))
        return dist[src], path

    @staticmethod
    def turn_units(p: Tuple[int, int], q: Tuple[int, int], r: Tuple[int, int], turn: float) -> int:
        """turn cost of the corner p -> q -> r: the wrapped heading change from atan2, in multiples of pi / 4, priced by
        `api.turn_weight` (the one formula; the heading arithmetic here is the reference's own)"""
        a1 = math.atan2(q[0] - p[0], q[1] - p[1])
        a2 = math.atan2(r[0] - q[0], r[1] - q[1])
        d = abs((a2 - a1 + math.pi) % (2 * math.pi) - math.pi)
        return int(turn_weight(turn, int(round(d / (math.pi / 4)))))

    def turn_cost(self, cells: List[Tuple[int, int]], safety: Safety) -> int:
        """J of a cell path under `safety` with its turn term (rate 0: the unpriced weights)"""
        sf = safety if safety.rate > 0 else None
        return self.path_cost(cells, sf) + sum(self.turn_units(p, q, r, safety.turn)
                                               for p, q, r in zip(cells[:-2], cells[1:-1], cells[2:]))

    def dijkstra_turn(self, src: Tuple[int, int], dst: Tuple[int, int], safety: Safety,
                      alive: Optional[np.ndarray] = None) -> Tuple[Optional[int], Optional[List[Tuple[int, int]]]]:
        """(minimal J with the turn term, the canonical path): heapq over (cell, incoming step) states, backwards from the goal (any
        heading), then the walk from the start (free heading) that takes the smallest (y, x) next cell on a minimal path"""
        sf = safety if safety.rate > 0 else None
        steps = [(dy, dx) for dy in (-1, 0, 1) for dx in (-1, 0, 1) if dy or dx]
        if src == dst:
            return 0, [src]

        tus = {(hu, hv): self.turn_units((0, 0), hu, (hu[0] + hv[0], hu[1] + hv[1]), safety.turn) for hu in steps for hv in steps}

        def tu(hu, hv):
            return tus[(hu, hv)]
        dist: Dict[Tuple[Tuple[int, int], Tuple[int, int]], int] = {}
        pq = []
        for h in steps:
            dist[(dst, h)] = 0
            pq.append((0, dst, h))
        heapq.heapify(pq)
        while pq:
            d, v, hv = heapq.heappop(pq)
            if d > dist[(v, hv)]:
                continue
            u = (v[0] - hv[0], v[1] - hv[1])
            if not (0 <= u[0] < self.nav.shape[0] and 0 <= u[1] < self.nav.shape[1]) or not self.nav[u] \
                    or (alive is not None and not alive[u]) or v not in self.neighbours(u, alive):
                continue
            w = self.weight(u, v, sf)
            for hu in steps:
                nd = d + w + tu(hu, hv)
                if nd < dist.get((u, hu), math.inf):
                    dist[(u, hu)] = nd
                    heapq.heappush(pq, (nd, u, hu))
        inf = math.inf

        def D(c, h):
            return dist.get((c, h), inf)
        cand = [(self.weight(src, v, sf) + D(v, (v[0] - src[0], v[1] - src[1])), v) for v in self.neighbours(src, alive)]
        if not cand or min(cand)[0] == inf:
            return None, None
        cost = min(c for c, _ in cand)
        path = [src, min(v for c, v in cand if c == cost)]
        h = (path[1][0] - src[0], path[1][1] - src[1])
        while path[-1] != dst:
            u = path[-1]
            nxt = min(v for v in self.neighbours(u, alive)
                      if self.weight(u, v, sf) + tu(h, (v[0] - u[0], v[1] - u[1])) + D(v, (v[0] - u[0], v[1] - u[1])) == D(u, h))
            h = (nxt[0] - u[0], nxt[1] - u[1])
            path.append(nxt)
        return int(cost), path

    def endpoints(self, a: str, b: str) -> Tuple[Tuple[int, int], Tuple[int, int]]:
        s = self.s
        out = []
        for k in (a, b):
            c = int(s.A[s.keys.index(k)])
            out.append((int(s.sp.ys[c]), int(s.sp.xs[c])))
        return out[0], out[1]

    def intrusion(self, cells: List[Tuple[int, int]], margin: float) -> float:
        """A = sum over edges of l * (d(a) + d(b)) / 2 (m^2): the intrusion area of a lattice path"""
        return sum(math.hypot(p[0] - q[0], p[1] - q[1]) * H * (self.deficit(*p, margin) + self.deficit(*q, margin)) / 2.0
                   for p, q in zip(cells[:-1], cells[1:]))
