"""Export the conceptual outdoor world (outdoor_world.py) as one JSON for a viewer: occupancy and clearance on the 0.1 m lattice,
the graph F, charts and chart links for two radii, the drive, and a set of long plans through the production planner path
(waypoints and the lattice path, with the plan time).

    python tools/scene_graph_sim/outdoor_world_export.py --out outdoor.json [--w 60 --h 40 --radii 2,6 --pairs 10]
"""
import argparse
import base64
import json
import math
import os
import sys
import time

import numpy as np
from scipy import ndimage

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.dirname(os.path.dirname(HERE))]

import outdoor_world as O  # noqa: E402
from packages import config  # noqa: E402
from packages.scene_graph import morse  # noqa: E402
from packages.scene_graph.api import SceneParams  # noqa: E402
from packages.scene_graph.engine import ChartGraph  # noqa: E402
from packages.scene_graph.store import MemoryBackend, SceneGraphStore  # noqa: E402
from packages.services.mission_planner import scene_route as sr  # noqa: E402
from packages.services.mission_planner.lazy_plan import open_plan  # noqa: E402
from tests.unit.scene_graph import chart_worlds as cw  # noqa: E402

b64 = lambda a: base64.b64encode(np.ascontiguousarray(a).tobytes()).decode()


def charts(F, h, tau, radius):
    p = morse.partition(F, h, tau, radius=radius)
    sp = p.sp
    lab = np.zeros(F.shape, np.uint16)
    lab[sp.ys, sp.xs] = p.full + 1
    anchors = [[round((int(sp.xs[a]) + .5) * h, 2), round((int(sp.ys[a]) + .5) * h, 2)] for a in p.anchor]
    acc = {}
    for dy, dx in ((0, 1), (1, 0)):
        a = lab[:F.shape[0] - dy, :F.shape[1] - dx].astype(int) - 1
        b = lab[dy:, dx:].astype(int) - 1
        ys, xs = np.nonzero((a >= 0) & (b >= 0) & (a != b))
        for y, x in zip(ys, xs):
            k = (int(min(a[y, x], b[y, x])), int(max(a[y, x], b[y, x])))
            e = acc.setdefault(k, [0, 0.0, 0.0])
            e[0] += 1; e[1] += (x + .5 + dx / 2) * h; e[2] += (y + .5 + dy / 2) * h
    links = [[a, b, round(n * h, 2), round(sx / n, 2), round(sy / n, 2)] for (a, b), (n, sx, sy) in sorted(acc.items())]
    return dict(own=b64(lab), anchors=anchors, cells=[int(c) for c in np.bincount(p.full, minlength=p.K)], links=links)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--w", type=float, default=60.0)
    ap.add_argument("--h", type=float, default=40.0)
    ap.add_argument("--stems", type=float, default=0.08)
    ap.add_argument("--step", type=float, default=3.0)
    ap.add_argument("--radii", default="2,6")
    ap.add_argument("--pairs", type=int, default=10)
    ap.add_argument("--min-pair-m", type=float, default=25.0)
    a = ap.parse_args()
    w = O.make_world(a.w, a.h, a.stems)
    way, _, _ = O.tour(w, a.w, a.h)
    leaves = cw.drive(w, way, step=a.step, win=200)
    g = ChartGraph(SceneParams(robot_half_side=0.25, chart_radius=2.0))
    be = MemoryBackend()
    st = SceneGraphStore(be, be)
    ver = None
    t0 = time.perf_counter()
    for i in range(0, len(leaves), 48):
        ver = st.save("m", g, g.fold_many(leaves[i:i + 48]), ver, {"n": 1})
    t_fold = time.perf_counter() - t0
    eng = st.load("m")
    rows, cols = w.shape[0] // 2, w.shape[1] // 2
    free5 = (w == 0)
    blocked5 = (w >= 100)
    occ = np.zeros((rows, cols), np.uint8)
    f2 = free5[:rows * 2, :cols * 2].reshape(rows, 2, cols, 2).any(axis=(1, 3))
    b2 = blocked5[:rows * 2, :cols * 2].reshape(rows, 2, cols, 2).any(axis=(1, 3))
    occ[f2] = 1; occ[b2] = 2
    dt = ndimage.distance_transform_edt(free5) * 0.05
    d4 = dt[:rows * 2, :cols * 2].reshape(rows, 2, cols, 2).min(axis=(1, 3))       # a node's clearance: the minimum over its 2 x 2 cells
    clr = np.minimum(255, np.round(d4 * 100)).astype(np.uint8)
    sp = eng._solved.sp
    F = np.zeros((rows, cols), bool)
    gy, gx = sp.ys + eng._cy0 // 2, sp.xs + eng._cx0 // 2
    ok = (gy >= 0) & (gy < rows) & (gx >= 0) & (gx < cols)
    F[gy[ok], gx[ok]] = True
    tau = eng._tau
    ch = {str(float(r)): charts(F, 0.1, tau, float(r)) for r in a.radii.split(",")}
    lazy = open_plan(st, "m", max_tiles=config.SCENE_GRAPH_LAZY_MAX_TILES, tile_cache=config.SCENE_GRAPH_LAZY_TILE_CACHE)
    xy = np.array([eng._cell_xy(int(sp.ys[i]), int(sp.xs[i])) for i in range(sp.N)])
    lk = [l.key for l in leaves]
    lxy = np.array([(l.x, l.y) for l in leaves])
    node_of = lambda x, y: lk[int(np.argmin((lxy[:, 0] - x) ** 2 + (lxy[:, 1] - y) ** 2))]
    rng = np.random.default_rng(5)
    plans = []
    tries = 0
    while len(plans) < a.pairs and tries < 4000:
        tries += 1
        i, j = rng.integers(0, sp.N, 2)
        p, q = xy[i], xy[j]
        if math.dist(p, q) < a.min_pair_m:
            continue
        t1 = time.perf_counter()
        try:
            plan = sr.plan_on_graph(lazy, "m", (p[0], p[1]), (q[0], q[1]), ([], []), node_of, config.SCENE_GRAPH_START_SNAP_M,
                                    config.SCENE_GRAPH_GOAL_SNAP_M, config.SCENE_GRAPH_MAX_WAYPOINT_GAP_M, None, 0.05, None, None,
                                    1.0, False, waypoint_budget_m=0.35)
        except sr.ChartFallback:
            continue
        ms = 1e3 * (time.perf_counter() - t1)
        wp = [[round(x.x, 2), round(x.y, 2)] for x in plan.waypoints]
        sa, sb = eng.snap(p[0], p[1], 0.5), eng.snap(q[0], q[1], 1.5)
        r = lazy.route(sa.key, sb.key, None, (sa.x, sa.y), (sb.x, sb.y))
        lat = [[round(x, 2), round(y, 2)] for x, y in r.nodes] if r is not None else []
        plans.append(dict(a=[round(float(p[0]), 2), round(float(p[1]), 2)], b=[round(float(q[0]), 2), round(float(q[1]), 2)], wp=wp, lat=lat,
                          ms=round(ms), length=round(sum(math.dist(wp[k], wp[k + 1]) for k in range(len(wp) - 1)), 1)))
    out = dict(rows=int(rows), cols=int(cols), step=0.1, ox=0.0, oy=0.0, w=a.w, h=a.h, stems=a.stems, occ=b64(occ), clr=b64(clr), F=b64(F.astype(np.uint8)),
               charts=ch, leaves=[[round(l.x, 1), round(l.y, 1)] for l in leaves[::3]], plans=plans,
               stats=dict(F=int(F.sum()), leaves=len(leaves), fold_s=round(t_fold, 1)))
    json.dump(out, open(a.out, "w"), separators=(",", ":"))
    print("wrote %s: %d x %d lattice, F %d, %d plans, fold %.0f s, %.1f MB" % (a.out, rows, cols, F.sum(), len(plans), t_fold, os.path.getsize(a.out) / 1e6))


if __name__ == "__main__":
    main()
