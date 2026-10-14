"""Lattice-level campaign of the exponential price profile (docs/scene_graph/inflation_profile_validation.md, tests B and C).

    python tools/scene_graph_sim/inflation_campaign.py grid    --maps maps.json [--out grid.json] [--jobs 20]
    python tools/scene_graph_sim/inflation_campaign.py perturb --maps maps.json [--out perturb.json] [--eps 0.07]

`maps.json` is the research data set (office8, office7, office6, Hospital: per map `rows`, `cols`, `own` (u16 owner plane,
> 0 = F), `clr` (u8 clearance in cm, unknown counted blocked), `origin`, `step` = 0.1 m lattice), read from the cloud read API
by the research scripts (`extract.py`, `build_data.py`); it is not stored in the repository.

The model is the research lattice, the same weights as the planner: an 8-neighbour lattice on F (a diagonal needs both
orthogonal cells), edge weight max(ceil(len / 1e-4 * (1 + kappa * (phi_a + phi_b) / 2) - 1e-6), 1), phi(g) = exp(-k (g - origin))
for g < reach else 0, one Dijkstra per pair, the canonical walk, then the cost-aware line-of-sight pull with the 5 m gap cap
(`scene_route.priced_visible` re-implemented on the lattice arrays). It does not run the production engine (that is
`accept_route_safety.py`); it covers the parameter grid the production harness cannot afford.

grid     k {4, 6, 8, 10} x reach {0.55, 0.8, 1.0} x origin {0.25, 0.30} x kappa {0.25, 0.5, 0.75, 1, 2}, 3 seeds x 120 pairs
         (>= 3 m apart, same component) per map. Per cell: pulled-polyline length increase against kappa 0 (mean, p90), robot
         cost per metre reduction (robot cost = min(253, 252 phi), diagnostics), share of the length below 0.35 m, and the
         exactness gates at lattice level: J(priced path) recomputed from the formula == Dijkstra distance, J(priced) <=
         J(base cells, priced weights), L(priced) >= L(base) - n 1e-4 (A4), B1, B2 (phi generalised), no pair that was
         reachable becomes unreachable.
perturb  uniform clearance noise +-eps on g (one draw per cell and trial): how often the lattice route changes and the J
         change (evaluated under the clean g) against the S6 bound.
"""
import argparse
import base64
import json
import math
import sys
from multiprocessing import Pool

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components, dijkstra

UNIT, MAXGAP, SAMPLE = 1e-4, 5.0, 0.025
KS, REACHES, ORIGINS, KAPPAS = (4.0, 6.0, 8.0, 10.0), (0.55, 0.8, 1.0), (0.25, 0.30), (0.25, 0.5, 0.75, 1.0, 2.0)
SEEDS, NPAIRS, MIN_PAIR = (7, 8, 9), 120, 3.0


def phi(g, k, reach, origin):
    """exponential price; k None: the linear hinge max(0, reach - g) (`reach` is then the margin)"""
    if k is None:
        return np.maximum(0.0, reach - g)
    return np.where(g < reach, np.exp(-k * (g - origin)), 0.0)


def load(path, name):
    d = json.load(open(path))[name]
    rows, cols = d["rows"], d["cols"]
    own = np.frombuffer(base64.b64decode(d["own"]), "<u2").reshape(rows, cols)
    clr = np.frombuffer(base64.b64decode(d["clr"]), np.uint8).reshape(rows, cols) / 100.0
    m = dict(rows=rows, cols=cols, F=own > 0, g=clr, ox=d["origin"][0], oy=d["origin"][1], step=d["step"])
    F = m["F"]
    ys, xs = np.nonzero(F)
    idx = -np.ones(F.shape, int)
    idx[ys, xs] = np.arange(len(ys))
    R, C, L = [], [], []
    for dy, dx in ((0, 1), (1, 0), (1, 1), (1, -1)):
        y2, x2 = ys + dy, xs + dx
        ok = (y2 >= 0) & (y2 < rows) & (x2 >= 0) & (x2 < cols)
        ok[ok] &= F[y2[ok], x2[ok]]
        a = np.nonzero(ok)[0]
        b = idx[y2[a], x2[a]]
        if dy and dx:
            k2 = F[ys[a], x2[a]] & F[y2[a], xs[a]]
            a, b = a[k2], b[k2]
        R.append(a); C.append(b); L.append(np.full(len(a), math.hypot(dy, dx) * m["step"] / UNIT))
    m.update(ys=ys, xs=xs, idx=idx, R=np.concatenate(R), C=np.concatenate(C), Lu=np.concatenate(L), N=len(ys))
    m["gn"] = m["g"][ys, xs]
    _, m["lab"] = connected_components(coo_matrix((np.ones(len(m["R"])), (m["R"], m["C"])), shape=(m["N"],) * 2), directed=False)
    return m


def pairs_of(m, seed, n=NPAIRS):
    rng = np.random.default_rng(seed)
    out = []
    while len(out) < n:
        a, b = rng.integers(0, m["N"], 2)
        d = math.hypot((m["xs"][a] - m["xs"][b]) * m["step"], (m["ys"][a] - m["ys"][b]) * m["step"])
        if d >= MIN_PAIR and m["lab"][a] == m["lab"][b]:
            out.append((int(a), int(b)))
    return out


def weights(m, phin, kappa):
    return np.maximum(np.ceil(m["Lu"] * (1.0 + kappa * 0.5 * (phin[m["R"]] + phin[m["C"]])) - 1e-6), 1.0)


def csr(m, w):
    return coo_matrix((np.r_[w, w], (np.r_[m["R"], m["C"]], np.r_[m["C"], m["R"]])), shape=(m["N"],) * 2).tocsr()


def canonical(G, d, src, dst):
    """dist from dst is `d`; walk from src taking, among neighbours on a shortest path, the smallest cell index"""
    path = [src]
    while path[-1] != dst:
        u = path[-1]
        lo, hi = G.indptr[u], G.indptr[u + 1]
        nb = G.indices[lo:hi]
        path.append(int(nb[(G.data[lo:hi] + d[nb]) == d[u]].min()))
    return path


def route(m, G, a, b):
    d = dijkstra(G, directed=False, indices=b)
    if not np.isfinite(d[a]):
        return None, None
    return canonical(G, d, a, b), d[a]


def cell_of(m, x, y):
    return np.floor((y - m["oy"]) / m["step"]).astype(int), np.floor((x - m["ox"]) / m["step"]).astype(int)


def seg(m, ax, ay, bx, by, kappa, prof):
    L = math.hypot(bx - ax, by - ay)
    n = max(1, math.ceil(L / SAMPLE))
    t = np.arange(n + 1) / n
    xs, ys = ax + t * (bx - ax), ay + t * (by - ay)
    r, c = cell_of(m, xs, ys)
    if (r < 0).any() or (c < 0).any() or (r >= m["rows"]).any() or (c >= m["cols"]).any() or not m["F"][r, c].all():
        return False, 0.0
    ch = (r[1:] != r[:-1]) & (c[1:] != c[:-1])
    if ch.any():
        if not (m["F"][r[:-1][ch], c[1:][ch]] & m["F"][r[1:][ch], c[:-1][ch]]).all():
            return False, 0.0
    tm = (np.arange(n) + 0.5) / n
    rm, cm = cell_of(m, ax + tm * (bx - ax), ay + tm * (by - ay))
    return True, float(((1.0 + kappa * phi(m["g"][rm, cm], *prof)) * (L / n)).sum())


def pull(m, path, kappa, prof):
    pts = [(m["ox"] + (m["xs"][v] + 0.5) * m["step"], m["oy"] + (m["ys"][v] + 0.5) * m["step"]) for v in path]
    pref = [0.0]
    for i in range(len(pts) - 1):
        ok, c = seg(m, *pts[i], *pts[i + 1], kappa, prof)
        pref.append(pref[-1] + (c if ok else math.dist(pts[i], pts[i + 1])))
    kept, i = [pts[0]], 0
    while i < len(pts) - 1:
        best = i + 1
        for k in range(len(pts) - 1, i + 1, -1):
            if math.dist(pts[i], pts[k]) > MAXGAP:
                continue
            ok, c = seg(m, *pts[i], *pts[k], kappa, prof)
            if ok and c <= pref[k] - pref[i] + 1e-9:
                best = k
                break
        kept.append(pts[best])
        i = best
    return kept


def metrics(m, pts, prof):
    """(length, robot cost integral, length below 0.35 m); the robot cost is always the exponential `prof`"""
    L = rc = e35 = 0.0
    for a, b in zip(pts[:-1], pts[1:]):
        l = math.hypot(b[0] - a[0], b[1] - a[1])
        n = max(1, math.ceil(l / SAMPLE))
        tm = (np.arange(n) + 0.5) / n
        r, c = cell_of(m, a[0] + tm * (b[0] - a[0]), a[1] + tm * (b[1] - a[1]))
        g = m["g"][r, c]
        rc += float((np.minimum(253.0, 252.0 * phi(g, *prof)) * (l / n)).sum())
        e35 += float(((g < 0.35) * (l / n)).sum())
        L += l
    return L, rc, e35


def path_LA(m, path, phin):
    """geometric length (m) and intrusion A = sum l (phi_a + phi_b) / 2 (m) of a lattice path, and its edge count"""
    L = A = 0.0
    for a, b in zip(path[:-1], path[1:]):
        l = math.hypot(int(m["ys"][a]) - int(m["ys"][b]), int(m["xs"][a]) - int(m["xs"][b])) * m["step"]
        L += l
        A += l * (phin[a] + phin[b]) / 2.0
    return L, A, len(path) - 1


def j_int(m, path, phin, kappa):
    tot = 0
    for a, b in zip(path[:-1], path[1:]):
        l = math.hypot(int(m["ys"][a]) - int(m["ys"][b]), int(m["xs"][a]) - int(m["xs"][b])) * m["step"] / UNIT
        tot += max(math.ceil(l * (1.0 + kappa * (phin[a] + phin[b]) / 2.0) - 1e-6), 1)
    return tot


# ------------------------------------------------------------------------------------------------------------------
_CACHE = {}


def _map(path, name):
    if (path, name) not in _CACHE:
        _CACHE[(path, name)] = load(path, name)
    return _CACHE[(path, name)]


def cell_task(arg):
    path, name, k, reach, origin = arg
    m = _map(path, name)
    prof = (k, reach, origin)
    phin = phi(m["gn"], *prof)
    out = {"map": name, "k": k, "reach": reach, "origin": origin, "seeds": {}}
    for seed in SEEDS:
        prs = pairs_of(m, seed)
        base_len, base_cost, base_e35, base = [], [], [], []
        G0 = csr(m, weights(m, np.zeros(m["N"]), 0.0))
        for a, b in prs:
            p, _ = route(m, G0, a, b)
            L, rc, e = metrics(m, pull(m, p, 0.0, prof), prof)
            base_len.append(L); base_cost.append(rc / L); base_e35.append(e / L); base.append(p)
        base_len = np.array(base_len)
        res = {}
        for kappa in KAPPAS:
            G = csr(m, weights(m, phin, kappa))
            inc, cost, e35, kept = [], [], [], []
            gate_unreach = gate_formula = gate_dom = gate_a4 = gate_b1 = gate_b2 = 0
            for i, ((a, b), pb) in enumerate(zip(prs, base)):
                p, d = route(m, G, a, b)
                if p is None:
                    gate_unreach += 1
                    continue
                if j_int(m, p, phin, kappa) != int(d):
                    gate_formula += 1
                if j_int(m, p, phin, kappa) > j_int(m, pb, phin, kappa):
                    gate_dom += 1
                Lp, Ap, n = path_LA(m, p, phin)
                Lo, Ao, no = path_LA(m, pb, phin)
                nn = max(n, no)
                if Lp < Lo - n * UNIT - 1e-9:
                    gate_a4 += 1
                if Ap > Ao + nn * UNIT / kappa + 1e-9:
                    gate_b1 += 1
                if Lp > Lo * (1.0 + kappa * (Ao / Lo if Lo > 0 else 0.0)) + nn * UNIT + 1e-9:
                    gate_b2 += 1
                L, rc, e = metrics(m, pull(m, p, kappa, prof), prof)
                kept.append(i)
                inc.append(L)
                cost.append(rc / L)
                e35.append(e / L)
            inc = np.array(inc) / base_len[kept] - 1.0
            res[kappa] = dict(mean=float(inc.mean()), p90=float(np.percentile(inc, 90)), p99=float(np.percentile(inc, 99)),
                              cost=float(np.mean(cost)), cost0=float(np.mean(base_cost)),
                              cost_red=float(1.0 - np.mean(cost) / np.mean(base_cost)), share=float(np.mean(e35)),
                              share0=float(np.mean(base_e35)), unreach=gate_unreach, g_formula=gate_formula, g_dom=gate_dom,
                              g_a4=gate_a4, g_b1=gate_b1, g_b2=gate_b2, n=len(prs))
        out["seeds"][seed] = res
    return out


def perturb_task(arg):
    path, name, k, reach, origin, kappa, eps = arg
    m = _map(path, name)
    prof = (k, reach, origin)
    phin = phi(m["gn"], *prof)
    Gc = csr(m, weights(m, phin, kappa))
    rng = np.random.default_rng(1234)
    n = changed = 0
    dj, dj_rel, bound_ex, bound_lip, ok_ex, ok_lip, per_m = [], [], [], [], 0, 0, []
    lip = k * math.exp(k * origin)                      # the steepest slope of phi (at g = 0), the step at reach excluded
    for seed in SEEDS:
        for a, b in pairs_of(m, seed, 40):
            gn = np.maximum(0.0, m["gn"] + rng.uniform(-eps, eps, m["N"]))
            phn = phi(gn, *prof)
            Gn = csr(m, weights(m, phn, kappa))
            pc, dc = route(m, Gc, a, b)
            pn, _ = route(m, Gn, a, b)
            n += 1
            if pc != pn:
                changed += 1
            Jc_pn = j_int(m, pn, phin, kappa)
            Jc_pc = int(dc)
            d_j = (Jc_pn - Jc_pc) * UNIT
            Lc = path_LA(m, pc, phin)[0]
            dj.append(d_j)
            per_m.append(d_j / Lc)
            # ex-post S6: J_c(Pn) - J_c(Pc) <= kappa (sum_Pn l |phi - phi'| + sum_Pc l |phi - phi'|) (+ n 1e-4 rounding)
            dphi = np.abs(phin - phn)
            e1 = path_LA(m, pn, dphi)[1]
            e2 = path_LA(m, pc, dphi)[1]
            ex = kappa * (e1 + e2) + (len(pn) + len(pc)) * UNIT
            bound_ex.append(ex)
            ok_ex += d_j <= ex + 1e-9
            # S6 with the Lipschitz slope: kappa * lip * eps * (L(Pc) + L(Pn)) (invalid across the step at reach)
            lb = kappa * lip * eps * (Lc + path_LA(m, pn, phin)[0]) + (len(pn) + len(pc)) * UNIT
            bound_lip.append(lb)
            ok_lip += d_j <= lb + 1e-9
    return dict(map=name, k=k, reach=reach, origin=origin, kappa=kappa, n=n, changed=changed / n,
                dJ_mean=float(np.mean(dj)), dJ_p90=float(np.percentile(dj, 90)), dJ_max=float(np.max(dj)),
                dJ_per_m_mean=float(np.mean(per_m)), dJ_per_m_max=float(np.max(per_m)),
                bound_expost_mean=float(np.mean(bound_ex)), ok_expost=ok_ex / n,
                bound_lip_mean=float(np.mean(bound_lip)), ok_lip=ok_lip / n,
                literal_kappa_eps_per_m=kappa * eps)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("mode", choices=("grid", "perturb"))
    ap.add_argument("--maps", required=True)
    ap.add_argument("--out", default=None)
    ap.add_argument("--jobs", type=int, default=20)
    ap.add_argument("--eps", type=float, default=0.07)
    args = ap.parse_args()
    names = list(json.load(open(args.maps)))
    if args.mode == "grid":
        tasks = [(args.maps, n, k, r, o) for n in names for k in KS for r in REACHES for o in ORIGINS]
        fn = cell_task
    else:
        tasks = [(args.maps, n, 6.0, 0.8, o, kp, args.eps) for n in names for o in ORIGINS for kp in (0.5, 1.0)]
        fn = perturb_task
    with Pool(args.jobs) as pool:
        out = pool.map(fn, tasks, chunksize=1)
    if args.out:
        json.dump(out, open(args.out, "w"))
    print("done %d tasks" % len(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
