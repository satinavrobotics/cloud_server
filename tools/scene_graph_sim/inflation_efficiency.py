"""Efficiency of the pricing tiers on synthetic worlds (docs/scene_graph/inflation_profile_validation.md, test D).

    python tools/scene_graph_sim/inflation_efficiency.py [--pairs 40] [--sizes 5x4,20x10,50x25] [--k 6 --reach 0.8 --origin 0.3 --kappa 0.5]
                                                         [--min-pair-m 3] [--turn-lambda L [--turn-only]]

Worlds: `route_safety_worlds.random_world` (a room with random pillars, usually a wall with a door, a lawnmower drive) of W x H
metres, built through the production path (fold in batches of 6, `SceneGraphStore(MemoryBackend)`, restored engine + the tile-lazy
`PlanGraph`). Per world, per tier (unpriced / hinge / exponential) the route call of the whole-graph engine (`ChartGraph.route`)
and of the lazy reader (`PlanGraph.route`), p50 / p95 / p99 in ms over random pairs >= 3 m apart: warm (second pass, caches
filled) and, for the lazy reader, cold (clearance, tile and decoded-tile caches emptied before every call). Also the memory of
the lazy clearance cache after the warm pass. The office8 production-path plan times are the C3 / C3c lines of
`accept_route_safety.py`.

`--turn-lambda L` (docs/scene_graph/close_evidence_and_turn_cost_design.md B.5) skips the tier table and times the turn-cost route instead:
the hinge tier with turn L (`--turn-only`: rate 0, the length plus the turn cost), the engine's and the lazy reader's route call and the
lazy `plan_on_graph` (the planner's whole plan: snap, route, pull), warm and cold, p50 / p95 / p99 / max in ms, and how many exceed
0.5 s (the search allowance of B.5). `--min-pair-m 30` probes the worst case on 50 x 25 m.
"""
import argparse
import math
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.dirname(os.path.dirname(HERE))]

from packages import config  # noqa: E402
from packages.scene_graph.api import Safety, SceneParams  # noqa: E402
from packages.scene_graph.engine import ChartGraph  # noqa: E402
from packages.scene_graph.store import MemoryBackend, SceneGraphStore  # noqa: E402
from packages.services.mission_planner import lazy_plan  # noqa: E402
from packages.services.mission_planner.lazy_plan import open_plan  # noqa: E402
from tests.unit.scene_graph import chart_worlds as cw  # noqa: E402
from tests.unit.scene_graph.route_safety_worlds import random_world  # noqa: E402


def pct(a, q):
    return float(np.percentile(np.asarray(a, float), q) * 1e3) if len(a) else float("nan")


def turn_rows(args, eng, lazy, sp, prs, tiers, timed, W, Hh, build_s, n_leaves) -> None:
    """the turn-cost route on one world: engine, lazy and the lazy plan, warm and cold (see the module docstring)"""
    from packages.services.mission_planner import scene_route as sr
    hinge = tiers["hinge"]
    sf = Safety(hinge.margin, 0.0 if args.turn_only else hinge.rate, turn=args.turn_lambda)
    kappa = 0.0 if args.turn_only else 1.0
    rec = {"lambda_m_per_rad": args.turn_lambda, "source": "config", "speed_max": None, "angular_speed_max": None, "reason": "tool"}
    ids = iter(range(10 ** 9))

    def plan(graph, sa, sb, _sf):
        try:
            sr.plan_on_graph(graph, "m", (sa.x, sa.y), (sb.x, sb.y), ([], []), lambda x, y: "n%d" % next(ids), 0.5, 1.5,
                             config.SCENE_GRAPH_MAX_WAYPOINT_GAP_M, None, 0.05, None, None, kappa, False, turn=rec)
        except sr.ChartFallback:
            pass
    d = [math.dist((a.x, a.y), (b.x, b.y)) for a, b in prs]
    print("%gx%g: %d F cells, %d leaves (built in %.1f s); %d pairs >= %.0f m (median %.1f, max %.1f m); %s, turn %.2f m/rad" % (
        W, Hh, sp.N, n_leaves, build_s, len(prs), args.min_pair_m, float(np.median(d)), max(d),
        "rate 0 (turn only)" if args.turn_only else "hinge kappa 1", args.turn_lambda), flush=True)
    for name, graph, cold, fn in (("engine route", eng, False, None), ("lazy route", lazy, False, None),
                                  ("lazy route cold", lazy, True, None), ("lazy plan", lazy, False, plan),
                                  ("lazy plan cold", lazy, True, plan)):
        if not cold:
            timed(graph, sf, fn=fn)                            # warm-up pass
        ts = timed(graph, sf, cold, fn)
        print("  %-16s p50 / p95 / p99 / max %7.1f / %7.1f / %7.1f / %7.1f ms; above 0.5 s: %d of %d" % (
            name, pct(ts, 50), pct(ts, 95), pct(ts, 99), max(ts) * 1e3, sum(1 for t in ts if t > 0.5), len(ts)), flush=True)
    ok = sum(1 for sa, sb in prs if (lambda e, l: e is not None and l is not None and e.cost == l.cost and e.nodes == l.nodes)(
        eng.route(sa.key, sb.key, None, (sa.x, sa.y), (sb.x, sb.y), safety=sf),
        lazy.route(sa.key, sb.key, None, (sa.x, sa.y), (sb.x, sb.y), safety=sf)))
    print("  lazy == engine (nodes, cost): %d of %d" % (ok, len(prs)), flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", type=int, default=40)
    ap.add_argument("--sizes", default="5x4,20x10,50x25")
    ap.add_argument("--k", type=float, default=6.0)
    ap.add_argument("--reach", type=float, default=0.8)
    ap.add_argument("--origin", type=float, default=0.3)
    ap.add_argument("--kappa", type=float, default=0.5)
    ap.add_argument("--seed", type=int, default=5)
    ap.add_argument("--min-pair-m", type=float, default=3.0)
    ap.add_argument("--turn-lambda", type=float, default=0.0)
    ap.add_argument("--turn-only", action="store_true")
    args = ap.parse_args()
    r = 0.25
    margin = r * math.sqrt(2)
    tiers = {"unpriced": None, "hinge": Safety(margin, 1.0 / (margin - r)),
             "exp": Safety(args.reach, args.kappa, k=args.k, origin=args.origin)}
    if not args.turn_lambda > 0.0:
        print("%-8s %-7s %-9s | %-26s | %-26s | %-26s" % ("world", "F cells", "reader", "unpriced p50/p95/p99 ms", "hinge p50/p95/p99 ms",
                                                          "exp p50/p95/p99 ms"))
    for size in args.sizes.split(","):
        W, Hh = (float(x) for x in size.split("x"))
        t0 = time.perf_counter()
        w, way = random_world(args.seed, W, Hh)
        leaves = cw.drive(w, way)
        g = ChartGraph(SceneParams(robot_half_side=r))
        be = MemoryBackend()
        st = SceneGraphStore(be, be)
        v = None
        for i in range(0, len(leaves), 6):
            v = st.save("m", g, g.fold_many(leaves[i:i + 6]), v, {"n": 1})
        eng = st.load("m")
        lazy = open_plan(st, "m", max_tiles=10 ** 6, tile_cache=config.SCENE_GRAPH_LAZY_TILE_CACHE)
        sp = eng._solved.sp
        build_s = time.perf_counter() - t0
        xy = np.array([eng._cell_xy(int(sp.ys[i]), int(sp.xs[i])) for i in range(sp.N)])
        rng = np.random.default_rng(7)
        prs = []
        while len(prs) < args.pairs:
            a, b = rng.integers(0, sp.N, 2)
            if math.dist(xy[a], xy[b]) < args.min_pair_m:
                continue
            sa, sb = eng.snap(*xy[a], 0.5), eng.snap(*xy[b], 0.5)
            if sa.key is not None and sb.key is not None:
                prs.append((sa, sb))

        def call(graph, sa, sb, sf):
            return graph.route(sa.key, sb.key, None, (sa.x, sa.y), (sb.x, sb.y), **({} if sf is None else {"safety": sf}))

        def timed(graph, sf, cold=False, fn=None):
            out = []
            for sa, sb in prs:
                if cold:
                    with graph._clr_lock:
                        graph._clr.clear()
                    graph.view._cache = lazy_plan._TileCache(config.SCENE_GRAPH_LAZY_TILE_CACHE)
                    with st._lock:
                        st._tiles.clear()
                        st._tile_bytes = 0
                t = time.perf_counter()
                (fn or call)(graph, sa, sb, sf)
                out.append(time.perf_counter() - t)
            return out
        if args.turn_lambda > 0.0:
            turn_rows(args, eng, lazy, sp, prs, tiers, timed, W, Hh, build_s, len(leaves))
            continue
        for name, graph, cold in (("engine", eng, False), ("lazy", lazy, False), ("lazy cold", lazy, True)):
            row = []
            for tier in ("unpriced", "hinge", "exp"):
                sf = tiers[tier]
                if not cold:
                    timed(graph, sf)                       # warm-up pass
                ts = timed(graph, sf, cold)
                row.append("%7.1f /%7.1f /%7.1f" % (pct(ts, 50), pct(ts, 95), pct(ts, 99)))
            print("%-8s %-7d %-9s | %s | %s | %s" % ("%gx%g" % (W, Hh), sp.N, name, row[0], row[1], row[2]), flush=True)
        # memory of the lazy clearance cache after a warm pass over all pairs (exp tier)
        timed(lazy, tiers["exp"])
        with lazy._clr_lock:
            n_t = len(lazy._clr)
            b = sum(a.nbytes for a in lazy._clr.values())
        print("         clearance cache (lazy): %d tiles, %.2f MB (%.0f B per F cell); engine sp.clr %.2f MB; built in %.1f s; %d leaves" % (
            n_t, b / 1e6, b / sp.N, sp.clr.nbytes / 1e6, build_s, len(leaves)), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
