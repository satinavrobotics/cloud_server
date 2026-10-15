"""Clearance-priced route against the full-resolution optimum, on a real export (docs/scene_graph/route_safety.md section 0).

    python tools/scene_graph_sim/bench_route_safety.py --data DIR [--kappa 1] [--robot-half-side 0.25]
                                                       [--pairs 150] [--seed 7] [--methods cur,whole,corridor,anchor,portal]

DIR is an export of one map's leaves: `nodes.json` (a list of topo nodes with `_key`, `session_id`, `session_node_id`,
`capture_time`, `pose` and `costmap.occupancy` with `resolution` and `origin_map`) and one `<_key>.png` occupancy window per
node (0 free .. 99 soft, 100 occupied, 255 unknown). The engine is built from them with `robot_half_side` r; nothing is written.

What it measures. For `--pairs` random start/goal cells of the lattice at least 3 m apart (reproducible by `--seed`), each
method returns a waypoint polyline; the table gives, over the pairs:

  J/J_ref          the continuum cost J = L + mu * integral (m - g)+ ds of the polyline (sampled every 1 cm on an independent
                   full-resolution clearance field, unknown cells counted blocked) over J of the reference optimum: Dijkstra on
                   the 0.05 m grid under F with 16 neighbours and the same g and J. 1.0 is the continuum optimum.
  min clr          median over pairs of the smallest clearance (m) along the polyline.
  #<r              pairs whose polyline comes closer than r to an obstacle (the inscribed radius F guarantees).
  unsafe m         mean length (m) of the polyline with clearance below the margin m.
  len ratio        polyline length over the length of `cur` (median / p90 / max).
  ms               mean time per pair of the route and the pull (the engine search, not the pair set-up).

with m = r * sqrt(2) (the circumscribed radius, no tolerance) and mu = kappa / (m - r) (`scene_route.safety_for`, the planner's
definition with the robot width unknown). Methods:

  cur       the planner today: unpriced engine route (length corridor), line-of-sight pull.
  whole     `route(..., safety=Safety(m, mu))` (the whole lattice is searched), cost-aware pull (`priced_visible`).
  corridor  the length corridor of `cur` searched with the priced edge weights (`ChartGraph._corridor_charts`, `_edge_cost`),
            cost-aware pull: what keeping the corridor as a bound would give.
  anchor    legs of `route(..., safety=...)` through the anchors of the charts on the priced chart path, cost-aware pull.
  portal    the same through the link portals instead of the anchors.

The table of docs/scene_graph/route_safety.md (office8, 97 leaves, 150 pairs, kappa 1) is the output of

    python tools/scene_graph_sim/bench_route_safety.py --data <office8 export> --kappa 1
"""
import argparse
import datetime
import json
import math
import os
import sys
import time
from typing import Dict, List, Optional, Tuple

import numpy as np
from PIL import Image

from packages.scene_graph import weights  # noqa: E402
from scipy import ndimage
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import dijkstra

sys.path[:0] = [os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))]

from packages import config  # noqa: E402
from packages.scene_graph.api import BBox, Leaf, SceneParams  # noqa: E402
from packages.scene_graph.engine import RES, ChartGraph  # noqa: E402
from packages.services.mission_planner import scene_route as sr  # noqa: E402

Pt = Tuple[float, float]
METHODS = ("cur", "whole", "corridor", "anchor", "portal")
MIN_PAIR_DISTANCE_M = 3.0
FOLD_BATCH = 6


def load_leaves(data: str) -> List[Leaf]:
    """the leaves of an export directory (see the module docstring)"""
    with open(os.path.join(data, "nodes.json")) as f:
        nodes = json.load(f)
    nodes = sorted([n for n in nodes if "costmap" in n and "occupancy" in n["costmap"]], key=lambda n: n["session_node_id"])
    out = []
    for n in nodes:
        c = n["costmap"]["occupancy"]
        t = int(datetime.datetime.fromisoformat(n["capture_time"]).timestamp() * 1000)
        w = np.array(Image.open(os.path.join(data, "%s.png" % n["_key"])))
        out.append(Leaf(key=n["_key"], session=n.get("session_id", "s"), stamp_ms=t, x=n["pose"]["x"], y=n["pose"]["y"],
                        yaw=n["pose"].get("yaw", 0.0), resolution=c["resolution"],
                        origin=(c["origin_map"]["x"], c["origin_map"]["y"]), window_u8=w,
                        origin_yaw=c["origin_map"].get("yaw", 0.0)))
    return out


class Field:
    """Independent continuum evaluation: the full-resolution clearance (unknown counted blocked) and the 0.05 m 16-neighbour
    reference optimum on the cells under F, both from the engine's occupancy plane only."""

    def __init__(self, g: ChartGraph, margin: float, rate: float):
        s = g._solved
        sp = s.sp
        self.g, self.margin, self.rate = g, margin, rate
        V = g._V
        free = (V != 255) & (V < 100)
        self.Hf, self.Wf = V.shape
        self.dt = ndimage.distance_transform_edt(free) * RES
        plane = np.zeros((sp.H, sp.W), bool)
        plane[sp.ys, sp.xs] = True
        allowed = np.zeros((self.Hf, self.Wf), bool)
        big = np.kron(plane, np.ones((2, 2), bool))
        allowed[:big.shape[0], :big.shape[1]] = big[:self.Hf, :self.Wf]
        fy, fx = np.nonzero(allowed)
        self.nid = -np.ones((self.Hf, self.Wf), int)
        self.nid[fy, fx] = np.arange(len(fy))
        self.fy, self.fx = fy, fx
        gfull = self.dt[fy, fx]
        RR, CC, LL = [], [], []
        for dy, dx, need in ((0, 1, ()), (1, 0, ()), (1, 1, ((0, 1), (1, 0))), (1, -1, ((0, -1), (1, 0))),
                             (1, 2, ((0, 1), (1, 1))), (2, 1, ((1, 0), (1, 1))), (1, -2, ((0, -1), (1, -1))),
                             (2, -1, ((1, 0), (1, -1)))):
            y2, x2 = fy + dy, fx + dx
            ok = (y2 >= 0) & (y2 < self.Hf) & (x2 >= 0) & (x2 < self.Wf)
            ok[ok] &= allowed[y2[ok], x2[ok]]
            for py, px in need:
                yy, xx = fy + py, fx + px
                ok &= (yy >= 0) & (yy < self.Hf) & (xx >= 0) & (xx < self.Wf)
                ok[ok] &= allowed[yy[ok], xx[ok]]
            a = np.nonzero(ok)[0]
            RR.append(a)
            CC.append(self.nid[y2[a], x2[a]])
            LL.append(np.full(len(a), math.hypot(dy, dx) * RES))
        RR, CC, LL = np.concatenate(RR), np.concatenate(CC), np.concatenate(LL)
        w = LL * (1.0 + rate * np.maximum(0.0, margin - 0.5 * (gfull[RR] + gfull[CC])))
        self.graph = coo_matrix((np.r_[w, w], (np.r_[RR, CC], np.r_[CC, RR])), shape=(len(fy), len(fy))).tocsr()

    def clearance(self, x, y):
        fx = np.floor(np.asarray(x) / RES).astype(int) - self.g._cx0
        fy = np.floor(np.asarray(y) / RES).astype(int) - self.g._cy0
        ok = (fx >= 0) & (fx < self.Wf) & (fy >= 0) & (fy < self.Hf)
        out = np.zeros(np.shape(fx))
        out[ok] = self.dt[fy[ok], fx[ok]]
        return out

    def samples(self, pts: List[Pt], step: float = 0.01):
        """(clearance, piece length) of the samples of a polyline: every `step` along it, every vertex, and the last point
        (weight 0), the same as `accept_route_safety.Reference.j_cont`"""
        xs, ys, sg = [], [], []
        for a, b in zip(pts[:-1], pts[1:]):
            n = max(1, int(math.ceil(math.dist(a, b) / step)))
            t = np.arange(n) / n
            xs.append(a[0] + t * (b[0] - a[0]))
            ys.append(a[1] + t * (b[1] - a[1]))
            sg.append(np.full(n, math.dist(a, b) / n))
        xs.append(np.array([pts[-1][0]]))
        ys.append(np.array([pts[-1][1]]))
        sg.append(np.zeros(1))
        xs, ys, sg = np.concatenate(xs), np.concatenate(ys), np.concatenate(sg)
        return self.clearance(xs, ys), sg

    def cost(self, pts: List[Pt]) -> float:
        c, sg = self.samples(pts)
        return float(sg.sum() + self.rate * (np.maximum(0.0, self.margin - c) * sg).sum())

    def optimum(self, st: Pt, go: Pt) -> Optional[List[Pt]]:
        g = self.g
        a = self.nid[int(math.floor(st[1] / RES)) - g._cy0, int(math.floor(st[0] / RES)) - g._cx0]
        b = self.nid[int(math.floor(go[1] / RES)) - g._cy0, int(math.floor(go[0] / RES)) - g._cx0]
        if a < 0 or b < 0:
            return None
        d, pr = dijkstra(self.graph, directed=False, indices=int(a), return_predecessors=True)
        if not np.isfinite(d[b]):
            return None
        p = [int(b)]
        while p[-1] != a:
            p.append(int(pr[p[-1]]))
        p = p[::-1]
        return [st] + [((self.fx[k] + g._cx0 + 0.5) * RES, (self.fy[k] + g._cy0 + 0.5) * RES) for k in p] + [go]


def pull(g: ChartGraph, pts: List[Pt], safety) -> List[Pt]:
    """the planner's pull (`simplify` and the end-stub merge of `_plan_on_graph`) with the line of sight of `cur` (safety None)
    or the cost-aware one (`priced_visible`)"""
    vis = sr.blocked_free_los(g, ([], []))
    if safety is not None:
        vis = sr.priced_visible(vis, g, pts, safety)
    out = sr.simplify(pts, vis, config.SCENE_GRAPH_MAX_WAYPOINT_GAP_M)
    while len(out) >= 3 and math.dist(out[-2], out[-1]) < sr.MIN_END_STUB_M and vis(out[-3], out[-1]):
        del out[-2]
    return out


def chart_chain(g: ChartGraph, ka: str, kb: str, safety) -> Optional[List[str]]:
    """chart path ka -> kb on the link graph priced by the engine's own route between the anchors of two linked charts"""
    view = g.query(BBox(-1e9, -1e9, 1e9, 1e9), raster=False, parents=False)
    keys = [c.key for c in view.charts]
    where = {k: i for i, k in enumerate(keys)}
    anchor = {c.key: c.anchor for c in view.charts}
    M = np.full((len(keys), len(keys)), np.inf)
    for l in view.links:
        r = g.route(l.a, l.b, None, anchor[l.a], anchor[l.b], safety=safety)
        if r is not None:
            i, j = where[l.a], where[l.b]
            M[i, j] = M[j, i] = min(M[i, j], r.cost)
    d, pr = dijkstra(np.where(np.isfinite(M), M, 0.0), directed=False, indices=where[ka], return_predecessors=True)
    if not np.isfinite(d[where[kb]]):
        return None
    path = [where[kb]]
    while path[-1] != where[ka]:
        path.append(int(pr[path[-1]]))
    return [keys[i] for i in path[::-1]]


def legs(g: ChartGraph, via: List[Tuple[str, Pt]], safety) -> Optional[List[Pt]]:
    """the priced route through the landmarks (chart key, point), one engine route per leg"""
    out: List[Pt] = []
    for (ka, pa), (kb, pb) in zip(via[:-1], via[1:]):
        if pa == pb:
            continue
        r = g.route(ka, kb, None, pa, pb, safety=safety)
        if r is None:
            return None
        out += [tuple(p) for p in r.nodes] if not out else [tuple(p) for p in r.nodes[1:]]
    return out


def run(args) -> None:
    leaves = load_leaves(args.data)
    g = ChartGraph(SceneParams(robot_half_side=args.robot_half_side))
    for i in range(0, len(leaves), FOLD_BATCH):
        g.fold_many(leaves[i:i + FOLD_BATCH])
    s = g._solved
    sp = s.sp
    safety = sr.safety_for(g, None, None, args.kappa)
    if safety is None:
        sys.exit("no pricing: kappa <= 0 or margin <= r")
    r = args.robot_half_side
    m, mu = safety.margin, safety.rate
    fld = Field(g, m, mu)
    xy = np.array([g._cell_xy(int(sp.ys[i]), int(sp.xs[i])) for i in range(sp.N)])
    rng = np.random.default_rng(args.seed)
    pairs = []
    while len(pairs) < args.pairs:
        a, b = rng.integers(0, sp.N, 2)
        if math.dist(xy[a], xy[b]) < MIN_PAIR_DISTANCE_M:
            continue
        sa, sb = g.snap(*xy[a], 0.5), g.snap(*xy[b], 0.5)
        if sa.key is None or sb.key is None:
            continue
        st, go = (sa.x, sa.y), (sb.x, sb.y)
        rt = g.route(sa.key, sb.key, None, st, go)
        if rt is None or not rt.nodes:
            continue
        pairs.append((sa.key, sb.key, st, go, rt))
    anchors = {c.key: c.anchor for c in g.query(BBox(-1e9, -1e9, 1e9, 1e9), raster=False, parents=False).charts}
    links = {}
    for l in g.query(BBox(-1e9, -1e9, 1e9, 1e9), raster=False, parents=False).links:
        links[(l.a, l.b)] = l
        links[(l.b, l.a)] = l

    def corridor_route(ka, kb, st, go):
        """priced weights, length corridor: the engine's private helpers, nothing re-implemented"""
        ja, jb = s.keys.index(ka), s.keys.index(kb)
        keep = g._corridor_charts(ja, jb)
        d = safety.price(s.sp.clr_price)
        W = weights.edge_costs(s.edges[2], d[s.edges[0]] + d[s.edges[1]], safety.rate)
        R, C, _ = s.edges
        ok = np.isin(s.full[R], list(keep)) & np.isin(s.full[C], list(keep))
        G = coo_matrix((np.r_[W[ok], W[ok]], (np.r_[R[ok], C[ok]], np.r_[C[ok], R[ok]])), shape=(sp.N, sp.N)).tocsr()
        src, dst = g._cell_at(st), g._cell_at(go)
        d = dijkstra(G, directed=False, indices=dst)
        if not np.isfinite(d[src]):
            return None
        p = [src]
        while p[-1] != dst:
            u = p[-1]
            lo, hi = G.indptr[u], G.indptr[u + 1]
            nb = G.indices[lo:hi]
            p.append(int(nb[(G.data[lo:hi] + d[nb]) == d[u]].min()))
        return [tuple(xy[c]) for c in p], float(np.isin(s.full, list(keep)).mean())

    res: Dict[str, List[List[Pt]]] = {k: [] for k in args.methods}
    secs = {k: 0.0 for k in args.methods}
    cover: List[float] = []
    ref_cost: List[float] = []
    for ka, kb, st, go, rt in pairs:
        ref_cost.append(fld.cost(fld.optimum(st, go)))
        for mth in args.methods:
            t = time.perf_counter()
            if mth == "cur":
                pts = pull(g, [st] + [tuple(p) for p in rt.nodes] + [go], None)
            elif mth == "whole":
                rp = g.route(ka, kb, None, st, go, safety=safety)
                pts = pull(g, [st] + [tuple(p) for p in rp.nodes] + [go], safety)
            elif mth == "corridor":
                got = corridor_route(ka, kb, st, go)
                if got is None:
                    got = ([tuple(p) for p in g.route(ka, kb, None, st, go, safety=safety).nodes], 1.0)
                cover.append(got[1])
                pts = pull(g, [st] + got[0] + [go], safety)
            else:
                chain = chart_chain(g, ka, kb, safety) or [ka, kb]
                via = [(ka, st)]
                for q, k in enumerate(chain):
                    if mth == "anchor":
                        via.append((k, anchors[k]))
                    elif q + 1 < len(chain):
                        l = links[(k, chain[q + 1])]
                        via.append((k, l.portal[0] if l.a == k else l.portal[1]))
                via.append((kb, go))
                path = legs(g, via, safety)
                if path is None:
                    path = [tuple(p) for p in g.route(ka, kb, None, st, go, safety=safety).nodes]
                pts = pull(g, [st] + path + [go], safety)
            secs[mth] += time.perf_counter() - t
            res[mth].append(pts)

    def length(p):
        return sum(math.dist(a, b) for a, b in zip(p[:-1], p[1:]))
    Jref = np.array(ref_cost)
    cur = np.array([length(p) for p in res["cur"]]) if "cur" in res else None
    print("### %s: %d leaves, N=%d F cells, %d charts, %d links, r=%.3f m=%.3f mu=%.1f/m (kappa %.1f), %d pairs, seed %d"
          % (os.path.basename(os.path.normpath(args.data)), len(leaves), sp.N, len(s.A), len(s.links), r, m, mu, args.kappa,
             len(pairs), args.seed))
    print("%-9s %9s %6s %6s | %8s %4s %8s | %21s | %6s" % ("method", "J/J_ref", "p90", "max", "min clr", "#<r", "unsafe m",
                                                           "len ratio med/p90/max", "ms"))
    for mth in args.methods:
        gap = np.array([fld.cost(p) for p in res[mth]]) / Jref
        samples = [fld.samples(p) for p in res[mth]]
        minc = np.array([c.min() for c, _ in samples])
        unsafe = np.array([sg[c < m].sum() for c, sg in samples])
        ratio = np.array([length(p) for p in res[mth]]) / cur if cur is not None else None
        lr = "%.3f / %.3f / %.3f" % (np.median(ratio), np.percentile(ratio, 90), ratio.max()) if ratio is not None else ""
        print("%-9s %9.3f %6.3f %6.3f | %8.3f %4d %8.2f | %21s | %6.1f"
              % (mth, np.median(gap), np.percentile(gap, 90), gap.max(), np.median(minc), int((minc < r).sum()), unsafe.mean(),
                 lr, secs[mth] / len(pairs) * 1e3))
    if cover:
        print("corridor cell coverage of the lattice (mean, min): %.3f, %.3f" % (np.mean(cover), np.min(cover)))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data", required=True, help="export directory: nodes.json and <key>.png per node")
    ap.add_argument("--kappa", type=float, default=1.0, help="cost multiplier at the inscribed floor (default 1)")
    ap.add_argument("--robot-half-side", type=float, default=0.25, help="r, the map's inscribed radius (m, default 0.25)")
    ap.add_argument("--pairs", type=int, default=150)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--methods", default=",".join(METHODS), help="comma-separated subset of %s" % ",".join(METHODS))
    args = ap.parse_args()
    args.methods = [x for x in args.methods.split(",") if x]
    bad = [x for x in args.methods if x not in METHODS]
    if bad:
        ap.error("unknown method(s): %s" % ", ".join(bad))
    if "cur" in args.methods:
        args.methods.insert(0, args.methods.pop(args.methods.index("cur")))
    run(args)


if __name__ == "__main__":
    main()
