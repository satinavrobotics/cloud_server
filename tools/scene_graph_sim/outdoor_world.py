"""Conceptual outdoor test: a forest + meadow world through the production path (docs/scene_graph/close_evidence_and_turn_cost_design.md E).

World (metres, 0.05 m cells): a forest block (stems of radius 0.10-0.35 m, density STEMS per m2) beside a meadow (sparse boulders,
radius 0.3-0.8 m) with a stream band (1.4 m, blocked) that has two 2 m crossings; no walls: outside the array the map is unknown.
A drive (a depth-first tour of a 1 m grid of free spots, one leaf every STEP m, window WIN cells) is folded in batches through
`ChartGraph`, saved to a `MemoryBackend` store, loaded by the lazy planner, and long pairs are planned with the live pricing, the turn
cost and the waypoint budget. It reports the structure (charts, links, levels, islands), the sizes and the timings for two chart radii.

    python tools/scene_graph_sim/outdoor_world.py [--w 100 --h 60 --stems 0.08 --step 3 --radii 2,6 --pairs 12]
                                                  [--near-range 1.5 [--near-min-clearance 0.25]] [--validated-first]

`--near-range` builds the world with the close-evidence rule (the stored `val` plane exists); `--validated-first` (needs it) plans with
`SCENE_GRAPH_VALIDATED_FIRST` on (design G): the plan time is then the validated-first search, and the unvalidated edges of the lattice
routes are reported.
"""
import argparse
import math
import os
import random
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.dirname(os.path.dirname(HERE))]

from packages import config  # noqa: E402
from packages.scene_graph.api import BBox, SceneParams  # noqa: E402
from packages.scene_graph.engine import ChartGraph  # noqa: E402
from packages.scene_graph.store import MemoryBackend, SceneGraphStore  # noqa: E402
from packages.services.mission_planner import scene_route as sr  # noqa: E402
from packages.services.mission_planner.lazy_plan import open_plan  # noqa: E402
from tests.unit.scene_graph import chart_worlds as cw  # noqa: E402

R = 0.05


def disc(w, cx, cy, r):
    y0, y1 = max(0, int((cy - r) / R)), min(w.shape[0], int((cy + r) / R) + 2)
    x0, x1 = max(0, int((cx - r) / R)), min(w.shape[1], int((cx + r) / R) + 2)
    yy, xx = np.mgrid[y0:y1, x0:x1]
    w[y0:y1, x0:x1][(((xx + .5) * R - cx) ** 2 + ((yy + .5) * R - cy) ** 2) <= r * r] = 100


def make_world(W, H, stems, seed=3):
    rnd = random.Random(seed)
    w = np.zeros((int(H / R), int(W / R)), np.uint8)
    xf = 0.6 * W
    n = int(stems * xf * H)
    for _ in range(n):                                   # forest stems
        disc(w, rnd.uniform(0, xf), rnd.uniform(0, H), rnd.uniform(0.10, 0.35))
    for _ in range(int(0.004 * (W - xf) * H)):           # meadow boulders
        disc(w, rnd.uniform(xf, W), rnd.uniform(0, H), rnd.uniform(0.3, 0.8))
    sx = 0.5 * W                                         # stream band across the whole height with two crossings
    w[:, int(sx / R):int((sx + 1.4) / R)] = 100
    for cy in (0.25 * H, 0.75 * H):
        w[int((cy - 1.0) / R):int((cy + 1.0) / R), int(sx / R):int((sx + 1.4) / R)] = 0
    return w


def tour(w, W, H, grid=1.0):
    """depth-first tour of the free 1 m grid spots (disc of 0.4 m free), walking back along the way it came"""
    nx, ny = int(W / grid), int(H / grid)
    free = {}
    for j in range(ny):
        for i in range(nx):
            x, y = (i + .5) * grid, (j + .5) * grid
            iy, ix, k = int(y / R), int(x / R), int(0.4 / R)
            if (w[iy - k:iy + k + 1, ix - k:ix + k + 1] == 0).all():
                free[(i, j)] = (x, y)

    def clear(a, b):
        n = int(math.dist(a, b) / R) + 1
        for t in np.linspace(0, 1, n):
            x, y = a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
            iy, ix, k = int(y / R), int(x / R), int(0.3 / R)
            if not (w[iy - k:iy + k + 1, ix - k:ix + k + 1] == 0).all():
                return False
        return True
    start = next(iter(free))
    seen, way, stack = {start}, [free[start]], [start]
    while stack:
        c = stack[-1]
        nxt = None
        for di, dj in ((1, 0), (0, 1), (-1, 0), (0, -1), (1, 1), (-1, 1), (1, -1), (-1, -1)):
            n = (c[0] + di, c[1] + dj)
            if n in free and n not in seen and clear(free[c], free[n]):
                nxt = n
                break
        if nxt is None:
            stack.pop()
            if stack:
                way.append(free[stack[-1]])
        else:
            seen.add(nxt); stack.append(nxt); way.append(free[nxt])
    return way, len(free), len(seen)


def pct(a, q):
    return float(np.percentile(np.asarray(a, float), q)) if len(a) else float("nan")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--w", type=float, default=100.0)
    ap.add_argument("--h", type=float, default=60.0)
    ap.add_argument("--stems", type=float, default=0.08, help="forest stems per m2")
    ap.add_argument("--step", type=float, default=3.0)
    ap.add_argument("--win", type=int, default=200, help="leaf window cells (0.05 m): 200 = 10 m")
    ap.add_argument("--radii", default="2,6")
    ap.add_argument("--pairs", type=int, default=12)
    ap.add_argument("--min-pair-m", type=float, default=30.0)
    ap.add_argument("--near-range", type=float, default=None, help="close-evidence rule (SceneParams.near_range_m), m")
    ap.add_argument("--near-min-clearance", type=float, default=None)
    ap.add_argument("--validated-first", action="store_true", help="plan with SCENE_GRAPH_VALIDATED_FIRST on (needs --near-range)")
    a = ap.parse_args()
    if a.validated_first and a.near_range is None:
        ap.error("--validated-first needs --near-range")
    config.SCENE_GRAPH_VALIDATED_FIRST = bool(a.validated_first)
    t0 = time.perf_counter()
    w = make_world(a.w, a.h, a.stems)
    way, nfree, nvis = tour(w, a.w, a.h)
    leaves = cw.drive(w, way, step=a.step, win=a.win)
    print("world %.0f x %.0f m, %.1f M cells | free grid spots %d, reached %d | drive %.0f m, %d leaves | built in %.0f s" % (
        a.w, a.h, w.size / 1e6, nfree, nvis, sum(math.dist(p, q) for p, q in zip(way[:-1], way[1:])), len(leaves), time.perf_counter() - t0))
    for rad in (float(x) for x in a.radii.split(",")):
        g = ChartGraph(SceneParams(robot_half_side=0.25, chart_radius=rad, near_range_m=a.near_range,
                                   near_min_clearance_m=a.near_min_clearance))
        be = MemoryBackend()
        st = SceneGraphStore(be, be)
        t1 = time.perf_counter()
        ver = None
        for i in range(0, len(leaves), 48):
            ver = st.save("m", g, g.fold_many(leaves[i:i + 48]), ver, {"n": 1})
        t_fold = time.perf_counter() - t1
        v = g.query(BBox(-1e9, -1e9, 1e9, 1e9), level=0, raster=False)
        nbytes = sum(len(b) for b in getattr(be, "_blobs", {}).values()) if hasattr(be, "_blobs") else float("nan")
        t2 = time.perf_counter()
        eng = st.load("m")
        t_load = time.perf_counter() - t2
        lazy = open_plan(st, "m", max_tiles=config.SCENE_GRAPH_LAZY_MAX_TILES, tile_cache=config.SCENE_GRAPH_LAZY_TILE_CACHE)
        sp = eng._solved.sp
        xy = np.array([eng._cell_xy(int(sp.ys[i]), int(sp.xs[i])) for i in range(sp.N)])
        lk = [l.key for l in leaves]
        lxy = np.array([(l.x, l.y) for l in leaves])
        node_of = lambda x, y: lk[int(np.argmin((lxy[:, 0] - x) ** 2 + (lxy[:, 1] - y) ** 2))]
        rng = np.random.default_rng(5)
        pairs = []
        for _ in range(4000):
            i, j = rng.integers(0, sp.N, 2)
            if math.dist(xy[i], xy[j]) >= a.min_pair_m:
                pairs.append((xy[i], xy[j]))
            if len(pairs) >= a.pairs:
                break
        times, ok, fb, lens, wps = [], 0, {}, [], []
        unval, inexact = [], 0
        for p, q in pairs:
            t3 = time.perf_counter()
            try:
                plan = sr.plan_on_graph(lazy, "m", (p[0], p[1]), (q[0], q[1]), ([], []), node_of, config.SCENE_GRAPH_START_SNAP_M,
                                        config.SCENE_GRAPH_GOAL_SNAP_M, config.SCENE_GRAPH_MAX_WAYPOINT_GAP_M, None, 0.05, None, None,
                                        1.0, False, waypoint_budget_m=0.35)
                ok += 1
                if plan.validated is not None:
                    unval.append(plan.validated["unvalidated_edges"])
                    inexact += not plan.validated["exact"]
                pts = [(x.x, x.y) for x in plan.waypoints]
                lens.append(sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1)))
                wps.append(len(pts))
            except sr.ChartFallback as e:
                fb[e.reason] = fb.get(e.reason, 0) + 1
            times.append(time.perf_counter() - t3)
        print("chart_radius %.0f m | charts %d links %d levels %d | F cells %d | fold+save %.1f s, load %.1f s, store %.1f MB | plans %d: "
              "served %d, fallbacks %s | time p50/p95/max %.0f/%.0f/%.0f ms | route %.0f m median, %.1f waypoints median" % (
                  rad, len(v.charts), len(v.links), v.levels, sp.N, t_fold, t_load, nbytes / 1e6, len(pairs), ok, fb or "none",
                  1e3 * pct(times, 50), 1e3 * pct(times, 95), 1e3 * max(times) if times else 0, pct(lens, 50), pct(wps, 50)))
        if a.near_range is not None:
            print("    validated nodes %.1f%% of F | validated-first %s%s" % (
                100 * float(eng._val[sp.ys, sp.xs].mean()), "on" if a.validated_first else "off",
                "; unvalidated edges per route median %.0f, max %d, inexact (tile cap) %d" % (pct(unval, 50), max(unval), inexact) if unval else ""))


if __name__ == "__main__":
    main()
