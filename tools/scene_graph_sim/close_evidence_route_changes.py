"""What the close-evidence rule (and its away-from-walls filter) does to routes, on a leaf export (design:
docs/scene_graph/close_evidence_and_turn_cost_design.md A.6b, A.6c).

Three graphs are built through the production path (batches of 6 folded in the engine, saved to a SceneGraphStore(MemoryBackend),
the planner's tile-lazy `PlanGraph` on that store): rule off, rule on (`--rdet`), rule on with the filter (`--min-clearance`).
`--pairs` random pairs of navigable lattice cells >= 3 m apart (`--seed`) are drawn from the RULE-OFF graph; the same cells are
routed (unpriced, the lazy graph's `route` between the snapped cells) on every graph. Reports per rule graph: navigable lattice cells
before / after, held components and cells, and the pairs with the same route, a changed route (mean length ratio new / old),
start or goal no longer navigable, and unroutable although both ends are navigable.

  python tools/scene_graph_sim/close_evidence_route_changes.py --data <export> [--rdet 1.5] [--min-clearance 0.25 [0.1 ...]]
                                                                [--pairs 150] [--seed 7] [--half-side 0.25]

Read-only. Only the maps exported to a leaf directory can be measured (export: tools/scene_graph_sim/README.md).
"""
import argparse
import math
import os
import sys

import numpy as np
from scipy import ndimage

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
import accept_route_safety as A  # noqa: E402
from packages import config  # noqa: E402
from packages.services.mission_planner.lazy_plan import open_plan  # noqa: E402

MIN_PAIR_M = 3.0


def _graph(data, half_side, **params):
    leaves, st, eng = A.build(data, half_side, **params)
    lazy = open_plan(st, "m", max_tiles=config.SCENE_GRAPH_LAZY_MAX_TILES, tile_cache=config.SCENE_GRAPH_LAZY_TILE_CACHE)
    return eng, lazy


def _nav_cell(eng, ix, iy):
    """is the global lattice cell (ix, iy) navigable (owned by a chart) in `eng`"""
    plane = eng._solved.plane if eng._solved is not None else None
    if plane is None: return False
    y, x = iy - eng._cy0 // 2, ix - eng._cx0 // 2
    return 0 <= y < plane.shape[0] and 0 <= x < plane.shape[1] and plane[y, x] >= 0


def _held(eng):
    """(cells, components) the rule holds in `eng`, after the filter when it has one"""
    if eng._Fn is None: return 0, 0
    h = eng._hold if eng._hold is not None else (eng._V < 100) & (eng._Bn >= 0) & (eng._Bn > eng._Fn)
    return int(h.sum()), int(ndimage.label(h, structure=np.ones((3, 3)))[1])


def _route(lazy, p, q):
    sa, sb = lazy.snap(p[0], p[1], 0.5), lazy.snap(q[0], q[1], 0.5)
    if sa.key is None or sb.key is None: return None
    return lazy.route(sa.key, sb.key, None, (sa.x, sa.y), (sb.x, sb.y))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data", required=True)
    ap.add_argument("--rdet", type=float, default=1.5)
    ap.add_argument("--min-clearance", type=float, nargs="*", default=[0.25],
                    help="filter values to report after the rule without the filter (default 0.25)")
    ap.add_argument("--pairs", type=int, default=150)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--half-side", type=float, default=0.25)
    a = ap.parse_args()

    off, off_lazy = _graph(a.data, a.half_side)
    n_off = int(off._build_F((0, 0))[0].sum())
    sp = off._solved.sp
    ix0, iy0 = off._cx0 // 2, off._cy0 // 2
    cells = [(int(sp.xs[i]) + ix0, int(sp.ys[i]) + iy0) for i in range(sp.N)]
    xy = [off._cell_xy(int(sp.ys[i]), int(sp.xs[i])) for i in range(sp.N)]
    rng = np.random.default_rng(a.seed)
    pairs = []
    while len(pairs) < a.pairs:
        i, j = (int(v) for v in rng.integers(0, sp.N, 2))
        if math.dist(xy[i], xy[j]) < MIN_PAIR_M: continue
        sa, sb = off.snap(*xy[i], 0.5), off.snap(*xy[j], 0.5)
        if sa.key is not None and sb.key is not None: pairs.append((i, j))
    base = [_route(off_lazy, xy[i], xy[j]) for i, j in pairs]
    print("%s: %d leaves, rule off: %d navigable lattice cells (0.1 m), %d pairs (seed %d, >= %.0f m apart), %d routable" % (
        os.path.basename(os.path.normpath(a.data)), len(A.bench.load_leaves(a.data)), n_off, len(pairs), a.seed, MIN_PAIR_M,
        sum(r is not None for r in base)))
    hdr = "%-26s %8s %8s %6s %6s | %5s %7s %9s %8s %6s"
    print(hdr % ("setting", "nav cells", "after", "comps", "cells", "same", "changed", "length x", "end lost", "unrout"))
    runs = [("rule %.1f m" % a.rdet, dict(near_range_m=a.rdet))]
    runs += [("rule %.1f m + away %.2f m" % (a.rdet, m), dict(near_range_m=a.rdet, near_min_clearance_m=m)) for m in a.min_clearance]
    for name, params in runs:
        eng, lazy = _graph(a.data, a.half_side, **params)
        n_on = int(eng._build_F((0, 0))[0].sum())
        hc, hn = _held(eng)
        same = changed = lost = unr = 0
        ratios = []
        for (i, j), b in zip(pairs, base):
            if not (_nav_cell(eng, *cells[i]) and _nav_cell(eng, *cells[j])):
                lost += 1; continue
            r = _route(lazy, xy[i], xy[j])
            if r is None:
                if b is None: same += 1                       # unroutable before and after
                else: unr += 1
            elif b is not None and r.cost == b.cost and [tuple(p) for p in r.nodes] == [tuple(p) for p in b.nodes]:
                same += 1
            else:
                changed += 1
                if b is not None and b.length > 0: ratios.append(r.length / b.length)
        print(hdr % (name, n_off, "%d (%+.1f%%)" % (n_on, 100 * (n_on / n_off - 1)), hn, hc, same, changed,
                     "%.3f" % (sum(ratios) / len(ratios)) if ratios else "-", lost, unr))


if __name__ == "__main__":
    main()
