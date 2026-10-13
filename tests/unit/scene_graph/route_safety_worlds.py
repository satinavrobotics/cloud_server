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

from packages.scene_graph.api import Safety
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


class Reference:
    """Independent clearance, edges and priced cost of one engine state (lattice cells are addressed (y, x) in the canvas)."""

    def __init__(self, g: ChartGraph):
        s = g._solved
        self.g, self.s = g, s
        V = g._V
        free = (V != 255) & (V < 100)
        self.clr = (ndimage.distance_transform_edt(free) * RES)[::2, ::2]       # g(c), m, unknown counted blocked
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
