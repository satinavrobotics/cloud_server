"""Acceptance harness of the clearance-priced route (docs/scene_graph/route_safety.md, section "Acceptance").

    python tools/scene_graph_sim/accept_route_safety.py --data DIR [--pairs 150] [--seed 7] [--kappa 1] [--robot-half-side 0.25]
                                                         [--width W [--length L]]
                                                         [--profile exp --k 6 --reach 0.8 --origin 0.3]

`--profile exp` runs the same gates for the exponential price profile (docs/scene_graph/inflation_profile_design.md): the
tier is selected through the SCENE_GRAPH_INFLATION_* settings (config attributes set here), kappa = `--kappa` (the planner's
0.5 default is not applied), so the production selection `safety_for` is on the path. The reference prices with its own
independent formula exp(-k (g - origin)) for g < reach, and the deficit d of the notation below is then the price phi.
C2 (continuum value, a bound of the hinge's Lipschitz deficit) is not evaluated for the exponential tier.

Production path, built once: the export's leaves are folded in the engine, saved to a `SceneGraphStore(MemoryBackend)` (batches of 6, as
the worker saves), the restored engine (`store.load`) is the reference and `open_plan` the planner's tile-lazy `PlanGraph`. The same
`--pairs` random start/goal cells (>= 3 m apart, `--seed`) are planned at kappa 0 (baseline) and `--kappa` (priced) on the same graphs.
No network I/O, one process, deterministic; exit code 0 when every gate passes, 1 otherwise. DIR as in `bench_route_safety.py`.

Notation. h = 0.1 m lattice, U = 1e-4 m cost unit, r the inscribed radius, m the margin (= r_c = r * sqrt(2) unless `--width` is given),
mu = kappa / (m - r), d(c) = max(0, m - g(c)) the deficit of a lattice cell (g: clearance of the independent 0.05 m distance transform of
the engine's occupancy plane, unknown counted blocked), n the number of lattice edges of a route. Edge weight
w = max(ceil(l / U * (1 + mu * (d_a + d_b) / 2) - 1e-6), 1), J_int = sum w (`Route.cost`), L the geometric length, and the intrusion
A = sum l * (d_a + d_b) / 2. Every gate states its derivation and its numeric tolerance in the output.
"""
import argparse
import dataclasses
import math
import os
import sys
import time
from typing import Dict, List, Optional, Tuple

import numpy as np
from scipy import ndimage
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import dijkstra

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.dirname(os.path.dirname(HERE))]

import bench_route_safety as bench  # noqa: E402
from packages import config  # noqa: E402
from packages.scene_graph.api import Safety, SceneParams  # noqa: E402
from packages.scene_graph.engine import RES, ChartGraph  # noqa: E402
from packages.scene_graph.store import MemoryBackend, SceneGraphStore  # noqa: E402
from packages.services.mission_planner import scene_route as sr  # noqa: E402
from packages.services.mission_planner.lazy_plan import open_plan  # noqa: E402

H = 0.1
U = 1e-4
FOLD_BATCH = 6
MIN_PAIR_M = 3.0
METRICATION = 1.0824            # sqrt(1 + (sqrt 2 - 1)^2): the 8-neighbour length over-estimate (route_safety.md S12)
QUANT_M = 2 * H / math.sqrt(2)  # 0.1414: 2 x the lattice-cell clearance quantisation h / sqrt 2 (S12)
SAMPLE_COARSE = 0.025
SAMPLE_FINE = 0.01
PLAN_P99_ALLOWANCE = 0.1       # of SCENE_GRAPH_PLAN_BUDGET_S: an allowance, not derived (warm and cold cache alike)

RESULTS: List[Tuple[str, str, bool, str, str]] = []


def gate(gid: str, name: str, ok: bool, measured: str, rule: str) -> None:
    RESULTS.append((gid, name, bool(ok), measured, rule))


class Reference:
    """Independent lattice model of the restored engine: owner plane, clearance, edge list, weights, canonical walk."""

    def pricef(self, g):
        """independent price of clearance g: the hinge deficit or exp(-k (g - origin)) for g < reach (not Safety.price)"""
        g = np.asarray(g, float)
        if self.k is None:
            return np.maximum(0.0, self.margin - g)
        out = np.zeros(g.shape)
        m = g < self.margin
        out[m] = np.exp(-self.k * (g[m] - self.origin))
        return out

    def __init__(self, eng: ChartGraph, margin: float, rate: float, k: Optional[float] = None, origin: Optional[float] = None):
        s = eng._solved
        self.eng, self.margin, self.rate, self.k, self.origin = eng, margin, rate, k, origin
        self.plane = s.plane
        self.nav = self.plane >= 0
        Hn, Wn = self.nav.shape
        V = eng._V
        free = (V != 255) & (V < 100)
        self.dt = ndimage.distance_transform_edt(free) * RES            # full-resolution clearance, computed once
        self.fy0, self.fx0 = eng._cy0, eng._cx0
        clr = np.zeros((Hn, Wn))
        sub = self.dt[::2, ::2][:Hn, :Wn]
        clr[:sub.shape[0], :sub.shape[1]] = sub
        self.clr = clr
        self.deficit = self.pricef(clr)
        ys, xs = np.nonzero(self.nav)
        self.ys, self.xs = ys, xs
        self.N = len(ys)
        self.idx = -np.ones((Hn, Wn), np.int64)
        self.idx[ys, xs] = np.arange(self.N)
        R, C, L = [], [], []
        for dy, dx in ((0, 1), (1, 0), (1, 1), (1, -1)):
            y2, x2 = ys + dy, xs + dx
            ok = (y2 >= 0) & (y2 < Hn) & (x2 >= 0) & (x2 < Wn)
            ok[ok] &= self.nav[y2[ok], x2[ok]]
            if dy and dx:
                ok[ok] &= self.nav[ys[ok], x2[ok]] & self.nav[y2[ok], xs[ok]]    # both orthogonal cells navigable
            a = np.nonzero(ok)[0]
            R.append(a)
            C.append(self.idx[y2[a], x2[a]])
            L.append(np.full(len(a), math.hypot(dy, dx) * H))
        self.R, self.C, self.L = np.concatenate(R), np.concatenate(C), np.concatenate(L)
        d = self.deficit[ys, xs]
        self.w_base = np.maximum(np.ceil(self.L / U - 1e-6), 1.0)
        self.w_priced = np.maximum(np.ceil(self.L / U * (1.0 + rate * (d[self.R] + d[self.C]) / 2.0) - 1e-6), 1.0)
        self._g: Dict[str, object] = {}

    def graph(self, which: str):
        if which not in self._g:
            w = self.w_priced if which == "priced" else self.w_base
            self._g[which] = coo_matrix((np.r_[w, w], (np.r_[self.R, self.C], np.r_[self.C, self.R])),
                                        shape=(self.N, self.N)).tocsr()
        return self._g[which]

    def cell(self, p) -> Optional[int]:
        iy = int(math.floor(p[1] / H)) - self.fy0 // 2
        ix = int(math.floor(p[0] / H)) - self.fx0 // 2
        if not (0 <= iy < self.nav.shape[0] and 0 <= ix < self.nav.shape[1]):
            return None
        c = int(self.idx[iy, ix])
        return c if c >= 0 else None

    def route(self, src_pt, dst_pt, which: str):
        """(J_int, cells) of the canonical minimal-cost path: one Dijkstra from the goal, then the walk from the start that takes,
        among the neighbours on a shortest path, the smallest cell index (cells are row-major)"""
        s, t = self.cell(src_pt), self.cell(dst_pt)
        if s is None or t is None:
            return None, None
        g = self.graph(which)
        d = dijkstra(g, directed=False, indices=t)
        if not np.isfinite(d[s]):
            return None, None
        path = [s]
        while path[-1] != t:
            u = path[-1]
            lo, hi = g.indptr[u], g.indptr[u + 1]
            nb = g.indices[lo:hi]
            path.append(int(nb[(g.data[lo:hi] + d[nb]) == d[u]].min()))
        return int(d[s]), path

    def cells_of(self, nodes) -> List[int]:
        return [self.cell(p) for p in nodes]

    def j_int(self, nodes, which: str) -> int:
        """integer cost of a node list under the base or the priced weights, from the formula (not from the edge table)"""
        tot = 0
        cs = self.cells_of(nodes)
        for a, b in zip(cs[:-1], cs[1:]):
            l = math.hypot(int(self.ys[a]) - int(self.ys[b]), int(self.xs[a]) - int(self.xs[b])) * H
            if which == "base":
                tot += max(math.ceil(l / U - 1e-6), 1)
            else:
                da, db = self.deficit[self.ys[a], self.xs[a]], self.deficit[self.ys[b], self.xs[b]]
                tot += max(math.ceil(l / U * (1.0 + self.rate * (da + db) / 2.0) - 1e-6), 1)
        return tot

    def length_and_intrusion(self, nodes) -> Tuple[float, float]:
        return self.path_stats(self.cells_of(nodes))

    def path_stats(self, cs) -> Tuple[float, float]:
        L = A = 0.0
        for a, b in zip(cs[:-1], cs[1:]):
            l = math.hypot(int(self.ys[a]) - int(self.ys[b]), int(self.xs[a]) - int(self.xs[b])) * H
            L += l
            A += l * (self.deficit[self.ys[a], self.xs[a]] + self.deficit[self.ys[b], self.xs[b]]) / 2.0
        return L, A

    def in_f(self, x: float, y: float) -> bool:
        return self.cell((x, y)) is not None

    def sampled_cost(self, pts, step: float) -> Tuple[float, bool]:
        """sum over segments of length * mean(1 + rate * d(sample cell)), samples at the middle of equal pieces <= step; and whether
        every sample is in F (a cell outside F counts as clearance 0)"""
        tot, inside = 0.0, True
        for a, b in zip(pts[:-1], pts[1:]):
            length = math.dist(a, b)
            if length == 0.0:
                continue
            n = max(1, int(math.ceil(length / step - 1e-9)))
            acc = 0.0
            for k in range(n):
                t = (k + 0.5) / n
                x, y = a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])
                c = self.cell((x, y))
                if c is None:
                    inside = False
                    acc += 1.0 + self.rate * float(self.pricef(0.0))
                else:
                    acc += 1.0 + self.rate * self.deficit[self.ys[c], self.xs[c]]
            tot += length * acc / n
        return tot, inside

    def all_samples_in_f(self, pts, step: float = SAMPLE_FINE) -> bool:
        for a, b in zip(pts[:-1], pts[1:]):
            n = max(1, int(math.ceil(math.dist(a, b) / step)))
            for k in range(n + 1):
                t = k / n
                if not self.in_f(a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])):
                    return False
        return True

    def clearance_at(self, x, y):
        fx = np.floor(np.asarray(x) / RES).astype(int) - self.fx0
        fy = np.floor(np.asarray(y) / RES).astype(int) - self.fy0
        Hf, Wf = self.dt.shape
        ok = (fx >= 0) & (fx < Wf) & (fy >= 0) & (fy < Hf)
        out = np.zeros(np.shape(fx))
        out[ok] = self.dt[fy[ok], fx[ok]]
        return out

    def j_cont(self, pts) -> Tuple[float, float, float, float]:
        """continuum J = L + mu * integral (m - g)+ ds sampled every 1 cm on the full-resolution field, at the start of each piece
        so every vertex is a sample, and at the last point (weight 0): (J, L, min clearance, length below the margin); the same
        sampling as `bench_route_safety.Field.samples`"""
        xs, ys, sg = [], [], []
        for a, b in zip(pts[:-1], pts[1:]):
            n = max(1, int(math.ceil(math.dist(a, b) / SAMPLE_FINE)))
            t = np.arange(n) / n
            xs.append(a[0] + t * (b[0] - a[0]))
            ys.append(a[1] + t * (b[1] - a[1]))
            sg.append(np.full(n, math.dist(a, b) / n))
        xs.append(np.array([pts[-1][0]]))
        ys.append(np.array([pts[-1][1]]))
        sg.append(np.zeros(1))
        xs, ys, sg = np.concatenate(xs), np.concatenate(ys), np.concatenate(sg)
        c = self.clearance_at(xs, ys)
        return (float(sg.sum() + self.rate * (self.pricef(c) * sg).sum()), float(sg.sum()), float(c.min()),
                float(sg[c < self.margin].sum()))


def build(data: str, r: float):
    leaves = bench.load_leaves(data)
    g = ChartGraph(SceneParams(robot_half_side=r))
    be = MemoryBackend()
    st = SceneGraphStore(be, be)
    version = None
    for i in range(0, len(leaves), FOLD_BATCH):
        version = st.save("m", g, g.fold_many(leaves[i:i + FOLD_BATCH]), version, {"n": 1})
    eng = st.load("m")
    return leaves, st, eng


def pct(a, q):
    return float(np.percentile(np.asarray(a, float), q)) if len(a) else float("nan")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data", required=True)
    ap.add_argument("--pairs", type=int, default=150)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--kappa", type=float, default=1.0)
    ap.add_argument("--robot-half-side", type=float, default=0.25)
    ap.add_argument("--width", type=float, default=None, help="planning robot width (m); default: margin = r * sqrt(2)")
    ap.add_argument("--length", type=float, default=None)
    ap.add_argument("--profile", choices=("hinge", "exp"), default="hinge")
    ap.add_argument("--k", type=float, default=6.0)
    ap.add_argument("--reach", type=float, default=0.8)
    ap.add_argument("--origin", type=float, default=None, help="default: the footprint's inscribed radius, else r")
    args = ap.parse_args()
    t_start = time.perf_counter()
    leaves, st, eng = build(args.data, args.robot_half_side)
    lazy = open_plan(st, "m", max_tiles=config.SCENE_GRAPH_LAZY_MAX_TILES, tile_cache=config.SCENE_GRAPH_LAZY_TILE_CACHE)
    if args.profile == "exp":
        config.SCENE_GRAPH_INFLATION_RADIUS_M = args.reach
        config.SCENE_GRAPH_INFLATION_SCALING = args.k
        config.SCENE_GRAPH_GLOBAL_INSCRIBED_M = args.origin
        config.SCENE_GRAPH_SAFETY_KAPPA_EXPLICIT = True
    safety = sr.safety_for(eng, args.width, args.length, args.kappa)
    if safety is None or (args.profile == "exp") != (safety.k is not None):
        print("no pricing / wrong tier (kappa <= 0, margin <= r, or an invalid exp profile)")
        return 1
    m, mu, r = safety.margin, safety.rate, args.robot_half_side
    ref = Reference(eng, m, mu, safety.k, safety.origin)
    sp = eng._solved.sp
    xy = np.array([eng._cell_xy(int(sp.ys[i]), int(sp.xs[i])) for i in range(sp.N)])
    leaf_xy = np.array([(l.x, l.y) for l in leaves])
    leaf_key = [l.key for l in leaves]

    def node_of(x: float, y: float) -> str:        # the nearest leaf (production: within SCENE_GRAPH_LEAF_SNAP_M; here no cap)
        return leaf_key[int(np.argmin((leaf_xy[:, 0] - x) ** 2 + (leaf_xy[:, 1] - y) ** 2))]

    rng = np.random.default_rng(args.seed)
    pairs = []
    while len(pairs) < args.pairs:
        a, b = rng.integers(0, sp.N, 2)
        if math.dist(xy[a], xy[b]) < MIN_PAIR_M:
            continue
        sa, sb = eng.snap(*xy[a], 0.5), eng.snap(*xy[b], 0.5)
        if sa.key is not None and sb.key is not None:
            pairs.append((sa, sb))
    gap = config.SCENE_GRAPH_MAX_WAYPOINT_GAP_M

    def plan(graph, p, q, kappa, times=None):
        t = time.perf_counter()
        try:
            out = sr.plan_on_graph(graph, "m", p, q, ([], []), node_of, config.SCENE_GRAPH_START_SNAP_M,
                                   config.SCENE_GRAPH_GOAL_SNAP_M, gap, args.width, 0.05, args.length, None, kappa, False)
        except sr.ChartFallback as err:
            out = err.reason
        if times is not None:
            times.append(time.perf_counter() - t)
        return out

    def route(graph, sa, sb, sf):
        return graph.route(sa.key, sb.key, None, (sa.x, sa.y), (sb.x, sb.y), **({} if sf is None else {"safety": sf}))

    # ---- routes ---------------------------------------------------------------------------------------------------------
    base_l, pri_l, base_e, pri_e, zero_l, zero_e = [], [], [], [], [], []
    for sa, sb in pairs:
        base_l.append(route(lazy, sa, sb, None))
        pri_l.append(route(lazy, sa, sb, safety))
        zero_l.append(route(lazy, sa, sb, dataclasses.replace(safety, rate=0.0)))
        base_e.append(route(eng, sa, sb, None))
        pri_e.append(route(eng, sa, sb, safety))
        zero_e.append(route(eng, sa, sb, dataclasses.replace(safety, rate=0.0)))
    # ---- plans (waypoints) at kappa 0, legacy defaults and kappa --------------------------------------------------------
    plans0, plans1, plans_default = [], [], []
    for sa, sb in pairs:                                  # first pass fills the tile and clearance caches
        plan(lazy, (sa.x, sa.y), (sb.x, sb.y), 0.0)
        plan(lazy, (sa.x, sa.y), (sb.x, sb.y), args.kappa)
    t0, t1 = [], []
    for sa, sb in pairs:
        p, q = (sa.x, sa.y), (sb.x, sb.y)
        plans0.append(plan(lazy, p, q, 0.0, t0))
        plans1.append(plan(lazy, p, q, args.kappa, t1))
        try:
            plans_default.append(sr.plan_on_graph(lazy, "m", p, q, ([], []), node_of, config.SCENE_GRAPH_START_SNAP_M,
                                                  config.SCENE_GRAPH_GOAL_SNAP_M, gap))
        except sr.ChartFallback as err:
            plans_default.append(err.reason)
    cold = open_plan(st, "m", max_tiles=config.SCENE_GRAPH_LAZY_MAX_TILES, tile_cache=config.SCENE_GRAPH_LAZY_TILE_CACHE)
    cold_pri = [route(cold, sa, sb, safety) for sa, sb in pairs]

    def make_cold(graph):
        """empty the graph's clearance cache, its tile cache and the store's decoded-tile cache: the next plan reads, decodes and
        computes everything it needs (the backend is in memory: no network)"""
        from packages.services.mission_planner.lazy_plan import _TileCache
        with graph._clr_lock:
            graph._clr.clear()
        graph.view._cache = _TileCache(config.SCENE_GRAPH_LAZY_TILE_CACHE)
        with st._lock:
            st._tiles.clear()
            st._tile_bytes = 0
    c0, c1 = [], []
    for sa, sb in pairs:
        p, q = (sa.x, sa.y), (sb.x, sb.y)
        make_cold(lazy)
        plan(lazy, p, q, 0.0, c0)
        make_cold(lazy)
        plan(lazy, p, q, args.kappa, c1)

    n_ok = [i for i, r_ in enumerate(base_l) if r_ is not None]

    def same(a, b):
        return (a is None and b is None) or (a is not None and b is not None and a.cost == b.cost
                                             and [tuple(p) for p in a.nodes] == [tuple(p) for p in b.nodes])

    def wp(plan_):
        return [(w.x, w.y) for w in plan_.waypoints]

    # ---- A ----------------------------------------------------------------------------------------------------------------
    bad = [i for i in range(len(pairs)) if not same(zero_l[i], base_l[i]) or not same(zero_e[i], base_e[i])]
    bad_plan = [i for i in range(len(pairs)) if (isinstance(plans0[i], str) != isinstance(plans_default[i], str))
                or (not isinstance(plans0[i], str) and (wp(plans0[i]) != wp(plans_default[i])))]
    gate("A1", "reduction", not bad and not bad_plan,
         "%d / %d routes differ, %d plans differ" % (len(bad), len(pairs), len(bad_plan)),
         "S8: rate 0 (same k, origin) and kappa 0 are the baseline, bit for bit (nodes, cost); tolerance 0")
    rb = {i for i, r_ in enumerate(base_l) if r_ is not None}
    rp = {i for i, r_ in enumerate(pri_l) if r_ is not None}
    sb_ = {i for i, p_ in enumerate(plans0) if not isinstance(p_, str)}
    sp_ = {i for i, p_ in enumerate(plans1) if not isinstance(p_, str)}
    gate("A2", "completeness", rb == rp and sb_ >= sp_ and sp_ >= sb_,
         "routable %d / %d (priced %d), planned %d (priced %d)" % (len(rb), len(pairs), len(rp), len(sb_), len(sp_)),
         "S8: same edges, finite weights, so the routable set is equal; tolerance 0")
    mism, nopt, dom = [], 0, []
    for i in n_ok:
        sa, sb = pairs[i]
        cost, path = ref.route((sa.x, sa.y), (sb.x, sb.y), "priced")
        got = pri_l[i]
        if cost is None or got is None or got.cost != cost or ref.cells_of(got.nodes) != path:
            mism.append(i)
        j_base_priced = ref.j_int(base_l[i].nodes, "priced")
        if got is not None and got.cost > j_base_priced:
            dom.append(i)
        nopt += 1
    gate("A3", "lattice optimality", not mism and not dom,
         "%d / %d differ from the reference, %d with J(priced) > J(baseline cells, priced weights)" % (len(mism), nopt, len(dom)),
         "S7: Dijkstra is exact for non-negative additive integer weights; J_int(priced) == reference, <= J_int(baseline cells); "
         "tolerance 0")
    # the length optimum (independent reference, base weights): the production baseline route is the corridor route, which is not
    # proved shortest (formulation.md statement 27), so A4 and B compare with this one
    opt: Dict[int, Tuple[float, float, int]] = {}
    for i in n_ok:
        sa, sb = pairs[i]
        _, path = ref.route((sa.x, sa.y), (sb.x, sb.y), "base")
        L_o, A_o = ref.path_stats(path)
        opt[i] = (L_o, A_o, len(path) - 1)
    short, worst = [], 0.0
    for i in n_ok:
        n_p = len(pri_l[i].nodes) - 1
        deficit = opt[i][0] - pri_l[i].length
        worst = max(worst, deficit)
        if deficit > n_p * U:
            short.append(i)
    subopt = sum(1 for i in n_ok if base_l[i].length > opt[i][0] + len(base_l[i].nodes) * U)
    gate("A4", "length", not short,
         "%d / %d priced routes shorter than the length optimum by more than n * 1e-4 (largest shortfall %.6f m); the production "
         "baseline is longer than that optimum on %d" % (len(short), len(n_ok), worst, subopt),
         "o = length optimum (reference, base weights): cost(o) <= cost(P) and L/U <= cost <= L/U + n give L(priced) >= L(o) - "
         "n_priced * 1e-4 m (n = edges)")
    diff = [i for i in range(len(pairs)) if not same(base_l[i], base_e[i]) or not same(pri_l[i], pri_e[i])]
    gate("A5", "lazy == engine", not diff, "%d / %d pairs differ (baseline and priced)" % (len(diff), len(pairs)),
         "both minimise J_int and share the canonical tie rule: nodes and cost equal; tolerance 0")
    det = [i for i in range(len(pairs)) if not same(pri_l[i], cold_pri[i])]
    gate("A6", "determinism", not det, "%d / %d routes differ between a cold and a warm cache" % (len(det), len(pairs)),
         "the route is a function of the stored cells (S7, canonical rule), not of cache state; tolerance 0")
    out_f, over = [], []
    for i in range(len(pairs)):
        p_ = plans1[i]
        if isinstance(p_, str) or pri_l[i] is None:
            continue
        sa, sb = pairs[i]
        pts = wp(p_)
        lattice = [(sa.x, sa.y)] + [tuple(n) for n in pri_l[i].nodes] + [(sb.x, sb.y)]
        if not ref.all_samples_in_f(pts):
            out_f.append(i)
        j_fin, _ = ref.sampled_cost(pts, SAMPLE_COARSE)
        j_lat, _ = ref.sampled_cost(lattice, SAMPLE_COARSE)
        if j_fin > j_lat + (len(lattice) - 1) * U:
            over.append(i)
    gate("A7", "waypoints", not out_f and not over,
         "%d plans leave F (1 cm samples), %d with J(final) > J(lattice) + n * 1e-4" % (len(out_f), len(over)),
         "S11: a shortcut is accepted only if visible and J(segment) <= J(sub-path), so J(final) <= J(lattice); tolerance n * 1e-4 m")

    # ---- B ----------------------------------------------------------------------------------------------------------------
    b1, b2 = [], []
    ratios = []
    for i in n_ok:
        Lb, Ab, n_b = opt[i]
        Lp, Ap = ref.length_and_intrusion(pri_l[i].nodes)
        n = max(n_b, len(pri_l[i].nodes) - 1)
        tol = n * U
        if Ap > Ab + tol / mu + 1e-9:
            b1.append(i)
        dbar = Ab / Lb if Lb > 0 else 0.0
        if Lp > Lb * (1.0 + mu * dbar) + tol + 1e-9:
            b2.append(i)
        Lc = base_l[i].length
        ratios.append(Lp / Lc if Lc > 0 else 1.0)
    gate("B1", "intrusion", not b1, "%d / %d violate A(priced) <= A(o) + n * 1e-4 / mu (o = length optimum)" % (len(b1), len(n_ok)),
         "mu A_new + L_new <= mu A_o + L_o (priced optimality) and L_new >= L_o - n*1e-4 (A4): A_new <= A_o + n * 1e-4 / mu "
         "(n = max edges of the two)")
    gate("B2", "detour", not b2, "%d / %d violate L(priced) <= L(o) (1 + mu dbar_o) + n * 1e-4" % (len(b2), len(n_ok)),
         "L_new + mu A_new <= L_o + mu A_o and A_new >= 0: L_new <= L_o (1 + mu A_o / L_o) (+ rounding n * 1e-4)")

    # ---- C ----------------------------------------------------------------------------------------------------------------
    r0 = {p_ for p_ in plans0 if isinstance(p_, str)}
    r1 = {p_ for p_ in plans1 if isinstance(p_, str)}
    new_fb = [i for i in range(len(pairs)) if not isinstance(plans0[i], str) and isinstance(plans1[i], str)]
    cap = sum(1 for p_ in plans1 if p_ == "corridor_too_large")
    gate("C1", "fallbacks", not new_fb and r1 <= r0,
         "%d newly falling back, reasons kappa0 %s, kappa %s, corridor_too_large %d" % (len(new_fb), sorted(r0), sorted(r1), cap),
         "S8/S10 (statistical): the same cells and edges; the ellipse can need more tiles than the corridor: reported")
    viol, med = [], []
    for i in range(len(pairs)):
        if isinstance(plans0[i], str) or isinstance(plans1[i], str):
            continue
        jb, lb, _, _ = ref.j_cont(wp(plans0[i]))
        jp, lp, _, _ = ref.j_cont(wp(plans1[i]))
        med.append(jp / jb)
        if jp > jb * METRICATION + mu * QUANT_M * lp:
            viol.append(i)
    if safety.k is None:
      gate("C2", "continuum value", not viol,
         "%d / %d violate J_cont(priced) <= %.4f J_cont(base) + mu * %.4f * L(priced); median ratio %.3f" % (
             len(viol), len(med), METRICATION, QUANT_M, float(np.median(med)) if med else float("nan")),
         "J_lat(P) <= J_lat(B) (S7) and J_lat vs J_cont differ by the 8-neighbour metrication 1.0824 (S12) and, in the deficit term, "
         "twice the clearance quantisation h / sqrt 2 = 0.1414 m per metre times mu")
    budget = config.SCENE_GRAPH_PLAN_BUDGET_S
    p99 = pct(t1, 99)
    gate("C3", "plan time (ALLOWANCE)", p99 <= PLAN_P99_ALLOWANCE * budget,
         "kappa 0 p50/p95/p99 %.1f/%.1f/%.1f ms, kappa p50/p95/p99 %.1f/%.1f/%.1f ms, p50 ratio %.2f, p99 ratio %.2f" % (
             pct(t0, 50) * 1e3, pct(t0, 95) * 1e3, pct(t0, 99) * 1e3, pct(t1, 50) * 1e3, pct(t1, 95) * 1e3, p99 * 1e3,
             pct(t1, 50) / pct(t0, 50), p99 / pct(t0, 99)),
         "p99 <= %.1f x SCENE_GRAPH_PLAN_BUDGET_S (%.1f s) = %.2f s: an ALLOWANCE, not derived; warm tile cache, in-memory store "
         "(live tile I/O latency is measured separately)" % (PLAN_P99_ALLOWANCE, budget, PLAN_P99_ALLOWANCE * budget))

    gate("C3c", "plan time cold (ALLOWANCE)", pct(c1, 99) <= PLAN_P99_ALLOWANCE * budget,
         "COLD caches per plan: kappa 0 p50/p95/p99 %.1f/%.1f/%.1f ms, kappa p50/p95/p99 %.1f/%.1f/%.1f ms, p50 ratio %.2f, p99 ratio %.2f" % (
             pct(c0, 50) * 1e3, pct(c0, 95) * 1e3, pct(c0, 99) * 1e3, pct(c1, 50) * 1e3, pct(c1, 95) * 1e3, pct(c1, 99) * 1e3,
             pct(c1, 50) / pct(c0, 50), pct(c1, 99) / pct(c0, 99)),
         "the same allowance, p99 <= %.2f s, with the clearance, tile and decoded-tile caches emptied before every plan "
         "(an ALLOWANCE, not derived; no network I/O: the live tile read latency comes on top)" % (PLAN_P99_ALLOWANCE * budget))

    # ---- report -----------------------------------------------------------------------------------------------------------
    print("### %s: %d leaves, N=%d F cells, %d charts; r=%.3f m=%.3f mu=%.2f/m (kappa %.2f); %d pairs seed %d; routable %d" % (
        os.path.basename(os.path.normpath(args.data)), len(leaves), sp.N, len(eng._solved.A), r, m, mu, args.kappa,
        len(pairs), args.seed, len(n_ok)))
    rows = []
    for name, plans in (("kappa 0", plans0), ("priced", plans1)):
        vals = [ref.j_cont(wp(p_)) for p_ in plans if not isinstance(p_, str)]
        rows.append((name, np.median([v[0] for v in vals]), np.median([v[2] for v in vals]),
                     sum(1 for v in vals if v[2] < r), float(np.mean([v[3] for v in vals])), np.mean([v[1] for v in vals])))
    print("%-8s %12s %10s %6s %10s %9s" % ("", "median J_cont", "min clr", "#<r", "unsafe m", "mean L"))
    for row in rows:
        print("%-8s %12.2f %10.3f %6d %10.2f %9.2f" % row)
    print("length ratio priced / production baseline route (median / p90 / max): %.3f / %.3f / %.3f" % (
        float(np.median(ratios)), pct(ratios, 90), max(ratios)))
    print("B1/B2 for a price phi >= 0 (phi = the hinge deficit or exp(-k (g - origin)) below reach; kappa = rate), o the length optimum, "
          "P the priced route, A = sum l phi_mean, n edges:")
    print("  J = L + kappa A (up to the n * 1e-4 rounding), J(P) <= J(o) and L(P) >= L(o) - n 1e-4 give "
          "A(P) <= A(o) + n 1e-4 / kappa (B1); with A(P) >= 0, L(P) <= L(o) + kappa A(o) = L(o) (1 + kappa phibar_o) + n 1e-4 (B2)")
    print()
    for gid, name, ok, measured, rule in RESULTS:
        print("%-3s %-22s %s  %s" % (gid, name, "PASS" if ok else "FAIL", measured))
        print("      rule: %s" % rule)
    allowance = [x for x in RESULTS if x[0] == "C3"][0]
    failed = [x[0] for x in RESULTS if not x[2]]
    print("\nelapsed %.1f s; %s" % (time.perf_counter() - t_start,
                                     "ALL GATES PASS" if not failed else "FAILED: " + ", ".join(failed)))
    if not allowance[2]:
        print("(C3 is an allowance, not a derived bound)")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
