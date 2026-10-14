"""Turn-cost prototype on the research lattice (design: docs/scene_graph/close_evidence_and_turn_cost_design.md, part B).

State = (cell, incoming heading of 8); an edge adds ceil(cost(dtheta) / 1e-4 m) integer units to the priced edge weight; the start
heading is free. NOT the production engine: scipy Dijkstra on the harness `Reference` lattice, non-canonical ties. The routes are
then pulled by the production string pull (`scene_route.simplify` with `priced_visible`), without a turn term.

  python tools/scene_graph_sim/turn_cost_prototype.py --data <office8 export> --pairs 150 [--lambdas 0.25,0.5,1.0] [--forms]
"""
import argparse
import math
import os
import sys

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import dijkstra

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
import accept_route_safety as A                                   # noqa: E402
from packages import config                                       # noqa: E402
from packages.services.mission_planner import scene_route as sr   # noqa: E402
from packages.services.mission_planner.lazy_plan import open_plan  # noqa: E402

U = A.U
HEAD = np.arange(8) * math.pi / 4
TD = np.abs((HEAD[:, None] - HEAD[None, :] + math.pi) % (2 * math.pi) - math.pi)       # [incoming, outgoing]


def turns(pts):
    p = np.asarray(pts, float)
    d = np.diff(p, axis=0)
    d = d[np.hypot(d[:, 0], d[:, 1]) > 1e-9]
    if len(d) < 2:
        return np.array([])
    t = np.diff(np.arctan2(d[:, 1], d[:, 0]))
    t = np.abs((t + np.pi) % (2 * np.pi) - np.pi)
    return t[t > 1e-6]


def plen(p):
    return sum(math.dist(p[i], p[i + 1]) for i in range(len(p) - 1))


class Prototype:
    def __init__(self, data, pairs, kappa=1.0, half_side=0.25, seed=7):
        leaves, st, self.eng = A.build(data, half_side)
        self.lazy = open_plan(st, "m", max_tiles=config.SCENE_GRAPH_LAZY_MAX_TILES, tile_cache=config.SCENE_GRAPH_LAZY_TILE_CACHE)
        self.safety = sr.safety_for(self.eng, None, None, kappa)
        self.ref = A.Reference(self.eng, self.safety.margin, self.safety.rate, self.safety.k, self.safety.origin)
        ref, sp = self.ref, self.eng._solved.sp
        self.N = ref.N
        xy = np.array([self.eng._cell_xy(int(sp.ys[i]), int(sp.xs[i])) for i in range(sp.N)])
        rng = np.random.default_rng(seed)
        self.pairs = []
        while len(self.pairs) < pairs:
            a, b = rng.integers(0, sp.N, 2)
            if math.dist(xy[a], xy[b]) < A.MIN_PAIR_M:
                continue
            sa, sb = self.eng.snap(*xy[a], 0.5), self.eng.snap(*xy[b], 0.5)
            if sa.key is not None and sb.key is not None:
                self.pairs.append((sa, sb))
        self.R = np.r_[ref.R, ref.C]
        self.C = np.r_[ref.C, ref.R]
        self.W = np.r_[ref.w_priced, ref.w_priced]
        dy, dx = ref.ys[self.C] - ref.ys[self.R], ref.xs[self.C] - ref.xs[self.R]
        self.dirs = np.round(np.arctan2(dy, dx) / (math.pi / 4)).astype(int) % 8
        self.vis = sr.blocked_free_los(self.lazy, ([], []))

    def table(self, cost):
        """cost: callable on an array of angles (rad) -> metres"""
        c = np.where(TD > 1e-9, cost(TD), 0.0)
        h = np.arange(8)
        src = (self.R[:, None] * 8 + h[None, :]).ravel()
        dst = np.repeat(self.C * 8 + self.dirs, 8)
        w = (self.W[:, None] + np.ceil(c[h[None, :], self.dirs[:, None]] / U - 1e-6)).ravel()
        return src, dst, w

    def route(self, s, t, tab):
        src, dst, w = tab
        n, S, T = self.N * 8 + 2, self.N * 8, self.N * 8 + 1
        g = coo_matrix((np.r_[w, np.zeros(16)], (np.r_[src, np.full(8, S), t * 8 + np.arange(8)],
                                                 np.r_[dst, s * 8 + np.arange(8), np.full(8, T)])), shape=(n, n)).tocsr()
        d, pred = dijkstra(g, directed=True, indices=S, return_predecessors=True)
        if not np.isfinite(d[T]):
            return None
        path, u = [], T
        while u != S:
            u = pred[u]
            if u < self.N * 8:
                path.append(u // 8)
        path = path[::-1]
        out = [path[0]]
        for c in path[1:]:
            if c != out[-1]:
                out.append(c)
        return out

    def points(self, cells):
        return [tuple(map(float, self.eng._cell_xy(int(self.ref.ys[c]), int(self.ref.xs[c])))) for c in cells]

    def pull(self, pts):
        return sr.simplify(pts, sr.priced_visible(self.vis, self.lazy, pts, self.safety), config.SCENE_GRAPH_MAX_WAYPOINT_GAP_M)

    def run(self, cost):
        tab = self.table(cost)
        lat, wp, fail = [], [], 0
        for sa, sb in self.pairs:
            s, t = self.ref.cell((sa.x, sa.y)), self.ref.cell((sb.x, sb.y))
            path = self.route(s, t, tab)
            if path is None:
                fail += 1
                continue
            pts = self.points(path)
            lat.append(pts)
            wp.append(self.pull(pts))
        return lat, wp, fail


def stats(seqs):
    L = sum(plen(x) for x in seqs)
    t = [turns(x) for x in seqs]
    n = sum(len(a) for a in t)
    return dict(L=L, chg10=10 * n / L, rad_m=sum(a.sum() for a in t) / L, s90=100 * sum((a > 1.55).sum() for a in t) / L,
                s45=100 * sum((a > 0.8).sum() for a in t) / L, segs=float(np.mean([len(x) - 1 for x in seqs])),
                short=sum(1 for x in seqs if any(math.dist(x[i], x[i + 1]) < 0.5 for i in range(1, len(x) - 2))))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data", required=True)
    ap.add_argument("--pairs", type=int, default=150)
    ap.add_argument("--lambdas", default="0,0.25,0.5,1.0")
    ap.add_argument("--forms", action="store_true", help="also the fixed-cost, quadratic and hard-limit variants")
    a = ap.parse_args()
    P = Prototype(a.data, a.pairs)
    big = 50.0
    forms = {f"linear {float(l):g} m/rad": (lambda x, l=float(l): l * x) for l in a.lambdas.split(",")}
    if a.forms:
        forms.update({"linear 0.5 + 0.2 m per turn": lambda x: 0.2 + 0.5 * x, "linear 0.5 + 0.4 m per turn": lambda x: 0.4 + 0.5 * x,
                      "quadratic 0.63 d^2": lambda x: 0.63 * x ** 2, "0.2 + quadratic": lambda x: 0.2 + 0.63 * x ** 2,
                      "linear + hard 45 deg": lambda x: 0.5 * x + np.where(x > 0.8, big, 0.0),
                      "linear + hard 90 deg": lambda x: 0.5 * x + np.where(x > 1.6, big, 0.0)})
    print("%-30s | lattice: chg/10m rad/m >=90/100m | waypoints: chg/10m rad/m >=90/100m >45/100m segs short-routes | fail" % "form")
    for name, f in forms.items():
        lat, wp, fail = P.run(f)
        s, w = stats(lat), stats(wp)
        print("%-30s | %12.1f %5.2f %9.1f | %12.1f %5.2f %9.1f %9.1f %5.1f %5d/%d | %d" % (
            name, s["chg10"], s["rad_m"], s["s90"], w["chg10"], w["rad_m"], w["s90"], w["s45"], w["segs"], w["short"], len(wp), fail))


if __name__ == "__main__":
    main()
