"""Corridor- or ellipse-restricted turn-cost search: exactness and cost (design: docs/scene_graph/close_evidence_and_turn_cost_design.md,
part B.5). Research lattice, NOT the production engine.

Given a priced lattice (nav mask, clearance) it runs, per random pair >= 3 m apart:
  1. the plain priced search (whole graph), giving the incumbent path P0;
  2. J_inc = priced cost of P0 + the turn cost of P0;
  3. the ellipse E = {c : |s - c| + |c - t| <= J_inc * 1e-4 m}: every path of total cost <= J_inc lies in E, because every priced
     edge weight is >= its length / 1e-4 m and the turn term is >= 0 (so the restriction is exact, not a heuristic);
  3b. (--band) a tighter incumbent first: the expanded search on the cells within 3 lattice cells of P0 (cost J_band >= J*), then
      E from J_band instead of J_inc;
  4. the expanded (cell, heading) search on E only;
and, when the whole map is small enough, the expanded search on the whole graph to compare costs (they must be equal).

  python tools/scene_graph_sim/turn_cost_restricted.py --maps <maps.json> --map Hospital [--pairs 30] [--lam 0.5]
  python tools/scene_graph_sim/turn_cost_restricted.py --synthetic 50x25 [--pairs 20]
"""
import argparse
import base64
import json
import math
import os
import sys
import time

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import dijkstra

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.dirname(os.path.dirname(HERE))]

U = 1e-4
H = 0.1
R_INS, R_C = 0.25, math.hypot(0.38, 0.34)      # inscribed half side, planning half diagonal (footprint of the navstack config)
HEAD = np.arange(8) * math.pi / 4
TD = np.abs((HEAD[:, None] - HEAD[None, :] + math.pi) % (2 * math.pi) - math.pi)


class Lattice:
    def __init__(self, nav, g, margin, kappa=1.0):
        self.nav = nav
        Hn, Wn = nav.shape
        ys, xs = np.nonzero(nav)
        self.ys, self.xs, self.N = ys, xs, len(ys)
        self.idx = -np.ones(nav.shape, np.int64)
        self.idx[ys, xs] = np.arange(self.N)
        mu = kappa / (margin - R_INS)
        d = np.maximum(0.0, margin - g[ys, xs])
        R, C, L, D = [], [], [], []
        for dy, dx in ((0, 1), (1, 0), (1, 1), (1, -1)):
            y2, x2 = ys + dy, xs + dx
            ok = (y2 >= 0) & (y2 < Hn) & (x2 >= 0) & (x2 < Wn)
            ok[ok] &= nav[y2[ok], x2[ok]]
            if dy and dx:
                ok[ok] &= nav[ys[ok], x2[ok]] & nav[y2[ok], xs[ok]]
            a = np.nonzero(ok)[0]
            R.append(a); C.append(self.idx[y2[a], x2[a]]); L.append(np.full(len(a), math.hypot(dy, dx) * H))
            D.append(np.full(len(a), np.arctan2(dy, dx)))
        R, C, L, D = (np.concatenate(v) for v in (R, C, L, D))
        w = np.maximum(np.ceil(L / U * (1 + mu * (d[R] + d[C]) / 2) - 1e-6), 1).astype(np.int64)
        self.R = np.r_[R, C]; self.C = np.r_[C, R]; self.W = np.r_[w, w]
        self.dirs = np.round(np.r_[D, D + math.pi] / (math.pi / 4)).astype(int) % 8
        self.xy = np.c_[xs * H, ys * H]
        self.plain = coo_matrix((self.W, (self.R, self.C)), shape=(self.N, self.N)).tocsr()

    def expanded(self, lam, sel=None):
        """CSR over states u*8+h of the edges whose both endpoints are selected (a bool mask over cells), or of all edges"""
        keep = np.ones(len(self.R), bool) if sel is None else sel[self.R] & sel[self.C]
        R, C, W, dr = self.R[keep], self.C[keep], self.W[keep], self.dirs[keep]
        h = np.arange(8)
        cost = np.ceil(lam * TD / U - 1e-6)
        src = (R[:, None] * 8 + h[None, :]).ravel()
        dst = np.repeat(C * 8 + dr, 8)
        w = (W[:, None] + np.where(TD[h[None, :], dr[:, None]] > 1e-9, cost[h[None, :], dr[:, None]], 0)).ravel().astype(np.int64)
        n = self.N * 8
        return src, dst, w, n

    def search(self, s, t, lam, sel=None):
        src, dst, w, n = self.expanded(lam, sel)
        S, T = n, n + 1
        g = coo_matrix((np.r_[w, np.zeros(16)], (np.r_[src, np.full(8, S), t * 8 + np.arange(8)],
                                                 np.r_[dst, s * 8 + np.arange(8), np.full(8, T)])), shape=(n + 2, n + 2)).tocsr()
        d = dijkstra(g, directed=True, indices=S)
        return d[T], src.size


def world_from_maps(path, name):
    m = json.load(open(path))[name]
    rows, cols = m["rows"], m["cols"]
    own = np.frombuffer(base64.b64decode(m["own"]), "<u2").reshape(rows, cols)
    clr = np.frombuffer(base64.b64decode(m["clr"]), np.uint8).reshape(rows, cols) / 100.0
    return own > 0, clr


def world_synthetic(size, seed=5):
    from packages.scene_graph.api import SceneParams
    from packages.scene_graph.engine import ChartGraph
    from packages.scene_graph.store import MemoryBackend, SceneGraphStore
    from tests.unit.scene_graph import chart_worlds as cw
    from tests.unit.scene_graph.route_safety_worlds import random_world
    import accept_route_safety as A
    W, Hh = (float(x) for x in size.split("x"))
    w, way = random_world(seed, W, Hh)
    leaves = cw.drive(w, way)
    g = ChartGraph(SceneParams(robot_half_side=R_INS))
    be = MemoryBackend()
    st = SceneGraphStore(be, be)
    ver = None
    for i in range(0, len(leaves), 6):
        ver = st.save("m", g, g.fold_many(leaves[i:i + 6]), ver, {"n": 1})
    eng = st.load("m")
    ref = A.Reference(eng, R_C, 1.0 / (R_C - R_INS))
    return ref.nav, ref.clr


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--maps")
    ap.add_argument("--map")
    ap.add_argument("--synthetic")
    ap.add_argument("--pairs", type=int, default=20)
    ap.add_argument("--lam", type=float, default=0.5)
    ap.add_argument("--band", action="store_true", help="tighten the ellipse with a thin-band expanded search around the plain route")
    ap.add_argument("--whole-max", type=int, default=15000, help="compare with the whole-graph expanded search up to this many cells")
    a = ap.parse_args()
    nav, g = world_from_maps(a.maps, a.map) if a.map else world_synthetic(a.synthetic)
    Lt = Lattice(nav, g, R_C)
    rng = np.random.default_rng(3)
    print("cells %d, directed edges %d, expanded states %d" % (Lt.N, len(Lt.R), 8 * Lt.N))
    rows = []
    mism = 0
    while len(rows) < a.pairs:
        s, t = (int(v) for v in rng.integers(0, Lt.N, 2))
        e = math.dist(Lt.xy[s], Lt.xy[t])
        if e < 3.0:
            continue
        d, pred = dijkstra(Lt.plain, directed=True, indices=s, return_predecessors=True)
        if not np.isfinite(d[t]):
            continue
        t0 = time.perf_counter()
        path, u = [t], t
        while u != s:
            u = pred[u]
            path.append(u)
        path = path[::-1]
        # incumbent cost with the turn term
        hd = []
        for p, q in zip(path[:-1], path[1:]):
            dy, dx = int(Lt.ys[q] - Lt.ys[p]), int(Lt.xs[q] - Lt.xs[p])
            hd.append(int(round(math.atan2(dy, dx) / (math.pi / 4))) % 8)
        turn = sum(math.ceil(a.lam * TD[x, y] / U - 1e-6) for x, y in zip(hd[:-1], hd[1:]) if TD[x, y] > 1e-9)
        jinc = int(d[t]) + turn
        if a.band:
            from scipy import ndimage
            m = np.zeros(Lt.nav.shape, bool)
            m[Lt.ys[path], Lt.xs[path]] = True
            m = ndimage.binary_dilation(m, structure=np.ones((3, 3), bool), iterations=3) & Lt.nav
            bandsel = m[Lt.ys, Lt.xs]
            tb0 = time.perf_counter()
            jb, _ = Lt.search(s, t, a.lam, bandsel)
            t_band = time.perf_counter() - tb0 + (tb0 - t0)
            jinc = min(jinc, int(jb))
        sel = (np.hypot(Lt.xy[:, 0] - Lt.xy[s, 0], Lt.xy[:, 1] - Lt.xy[s, 1]) + np.hypot(Lt.xy[:, 0] - Lt.xy[t, 0], Lt.xy[:, 1] - Lt.xy[t, 1])) <= jinc * U + 1e-9
        t_sel = time.perf_counter() - t0
        t1 = time.perf_counter()
        j_e, nnz = Lt.search(s, t, a.lam, sel)
        t_e = time.perf_counter() - t1
        j_w = None
        if Lt.N <= a.whole_max:
            j_w, _ = Lt.search(s, t, a.lam, None)
            mism += int(j_w != j_e)
        t2 = time.perf_counter()
        dijkstra(Lt.plain, directed=True, indices=s)
        t_plain = time.perf_counter() - t2
        rows.append((e, int(sel.sum()), nnz, 1e3 * (t_sel + t_e), 1e3 * t_plain, int(j_e), jinc, j_w, 1e3 * t_band if a.band else 0.0, int(jb) if a.band else 0))
    r = np.array([(x[0], x[1], x[2], x[3], x[4]) for x in rows], float)
    print("pairs %d | euclid m: median %.1f max %.1f | ellipse cells median %d p95 %d (%.0f%% of map) | expanded edges median %.0f k"
          % (len(rows), np.median(r[:, 0]), r[:, 0].max(), np.median(r[:, 1]), np.percentile(r[:, 1], 95), 100 * np.median(r[:, 1]) / Lt.N,
             np.median(r[:, 2]) / 1e3))
    print("restricted turn-cost stage (select + build + search): p50 %.1f p95 %.1f p99 %.1f max %.1f ms | plain whole-graph search for reference p50 %.1f ms"
          % (np.percentile(r[:, 3], 50), np.percentile(r[:, 3], 95), np.percentile(r[:, 3], 99), r[:, 3].max(), np.median(r[:, 4])))
    if a.band:
        tb = [x[8] for x in rows]
        gap = [x[9] - x[5] for x in rows]
        print("band only (inexact, J_band >= J*): p50 %.1f p95 %.1f max %.1f ms | cost above the optimum: median %d, p95 %d, max %d units (1e-4 m); equal in %d of %d"
              % (np.percentile(tb, 50), np.percentile(tb, 95), max(tb), np.median(gap), np.percentile(gap, 95), max(gap), sum(g == 0 for g in gap), len(gap)))
    imp = [x[6] - x[5] for x in rows]
    print("incumbent minus optimum (units 1e-4 m): median %d max %d | whole-graph comparison: %s"
          % (np.median(imp), max(imp), "%d mismatches in %d" % (mism, len(rows)) if Lt.N <= a.whole_max else "not run (map too large)"))


if __name__ == "__main__":
    main()
