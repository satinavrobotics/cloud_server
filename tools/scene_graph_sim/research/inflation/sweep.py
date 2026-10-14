"""Laci's profile: phi(g) = exp(-K (g - r)) for g < REACH else 0; edge weight ceil(len/1e-4 * (1 + kappa * mean phi) - 1e-6).
Robot cost = 252 exp(-K (g - r)) for g < REACH (capped at 253 below r), 0 beyond. Lattice 0.1 m, F from the stored owner raster,
clearance from the stored occupancy (unknown = blocked), pairs >= 3 m, cost-aware pull with the 5 m cap."""
import json, base64, math, sys, numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import dijkstra, connected_components
R_IN, K, REACH, UNIT, MAXGAP, SAMPLE = 0.25, 6.0, 0.8, 1e-4, 5.0, 0.025
KAPPAS = [0.0, 0.5, 1.0, 1.5, 2.0, 3.0]

def load(name):
    d = json.load(open("maps.json"))[name]; rows, cols = d["rows"], d["cols"]
    own = np.frombuffer(base64.b64decode(d["own"]), "<u2").reshape(rows, cols)
    clr = np.frombuffer(base64.b64decode(d["clr"]), np.uint8).reshape(rows, cols) / 100.0
    return dict(rows=rows, cols=cols, F=own > 0, g=clr, ox=d["origin"][0], oy=d["origin"][1], step=d["step"])

def phi(g, r=R_IN, k=K, reach=REACH): return np.where(g < reach, np.exp(-k * (g - r)), 0.0)

def build(m):
    F = m["F"]; rows, cols = F.shape
    ys, xs = np.nonzero(F); idx = -np.ones(F.shape, int); idx[ys, xs] = np.arange(len(ys))
    R, C, L = [], [], []
    for dy, dx in ((0, 1), (1, 0), (1, 1), (1, -1)):
        y2, x2 = ys + dy, xs + dx
        ok = (y2 >= 0) & (y2 < rows) & (x2 >= 0) & (x2 < cols); ok[ok] &= F[y2[ok], x2[ok]]
        a = np.nonzero(ok)[0]; b = idx[y2[a], x2[a]]
        if dy and dx:
            k2 = F[ys[a], x2[a]] & F[y2[a], xs[a]]; a, b = a[k2], b[k2]
        R.append(a); C.append(b); L.append(np.full(len(a), math.hypot(dy, dx) * m["step"] / UNIT))
    m.update(ys=ys, xs=xs, idx=idx, R=np.concatenate(R), C=np.concatenate(C), Lu=np.concatenate(L), N=len(ys))
    m["gn"] = m["g"][ys, xs]; m["phin"] = phi(m["gn"])
    _, m["lab"] = connected_components(coo_matrix((np.ones(len(m["R"])), (m["R"], m["C"])), shape=(m["N"],) * 2), directed=False)

def graph(m, kappa):
    w = np.maximum(np.ceil(m["Lu"] * (1 + kappa * 0.5 * (m["phin"][m["R"]] + m["phin"][m["C"]])) - 1e-6), 1.0)
    return coo_matrix((np.r_[w, w], (np.r_[m["R"], m["C"]], np.r_[m["C"], m["R"]])), shape=(m["N"],) * 2).tocsr()

def lattice_path(Gm, src, dst):
    d, pr = dijkstra(Gm, directed=False, indices=src, return_predecessors=True)
    if not np.isfinite(d[dst]): return None
    p = [dst]
    while p[-1] != src: p.append(int(pr[p[-1]]))
    return p[::-1]

def cell_of(m, x, y):
    return np.floor((y - m["oy"]) / m["step"]).astype(int), np.floor((x - m["ox"]) / m["step"]).astype(int)

def seg(m, ax, ay, bx, by, kappa):
    L = math.hypot(bx - ax, by - ay); n = max(1, math.ceil(L / SAMPLE)); t = np.arange(n + 1) / n
    xs, ys = ax + t * (bx - ax), ay + t * (by - ay); r, c = cell_of(m, xs, ys)
    if (r < 0).any() or (c < 0).any() or (r >= m["rows"]).any() or (c >= m["cols"]).any(): return False, 0.0
    if not m["F"][r, c].all(): return False, 0.0
    ch = (r[1:] != r[:-1]) & (c[1:] != c[:-1])
    if ch.any():
        pr_, pc_, r2, c2 = r[:-1][ch], c[:-1][ch], r[1:][ch], c[1:][ch]
        if not (m["F"][pr_, c2] & m["F"][r2, pc_]).all(): return False, 0.0
    tm = (np.arange(n) + 0.5) / n; rm, cm = cell_of(m, ax + tm * (bx - ax), ay + tm * (by - ay))
    return True, float(((1 + kappa * phi(m["g"][rm, cm])) * (L / n)).sum())

def pull(m, path, kappa):
    pts = [(m["ox"] + (m["xs"][v] + 0.5) * m["step"], m["oy"] + (m["ys"][v] + 0.5) * m["step"]) for v in path]
    pref = [0.0]
    for i in range(len(pts) - 1):
        ok, c = seg(m, *pts[i], *pts[i + 1], kappa); pref.append(pref[-1] + (c if ok else math.dist(pts[i], pts[i + 1])))
    kept, i = [pts[0]], 0
    while i < len(pts) - 1:
        best = i + 1
        for k in range(len(pts) - 1, i + 1, -1):
            if math.dist(pts[i], pts[k]) > MAXGAP: continue
            ok, c = seg(m, *pts[i], *pts[k], kappa)
            if ok and c <= pref[k] - pref[i] + 1e-9: best = k; break
        kept.append(pts[best]); i = best
    return kept

def metrics(m, pts):
    L = rc = e35 = 0.0; mg = 1e9
    for a, b in zip(pts[:-1], pts[1:]):
        l = math.hypot(b[0] - a[0], b[1] - a[1]); n = max(1, math.ceil(l / SAMPLE)); tm = (np.arange(n) + 0.5) / n
        r, c = cell_of(m, a[0] + tm * (b[0] - a[0]), a[1] + tm * (b[1] - a[1])); g = m["g"][r, c]
        rc += float((np.minimum(253.0, 252.0 * phi(g)) * (l / n)).sum()); e35 += float(((g < 0.35) * (l / n)).sum()); mg = min(mg, float(g.min())); L += l
    return L, rc, e35, mg

def run(name, npairs=120, seed=7):
    m = load(name); build(m)
    rng = np.random.default_rng(seed); pairs = []
    while len(pairs) < npairs:
        a, b = rng.integers(0, m["N"], 2)
        d = math.hypot((m["xs"][a] - m["xs"][b]) * m["step"], (m["ys"][a] - m["ys"][b]) * m["step"])
        if d >= 3.0 and m["lab"][a] == m["lab"][b]: pairs.append((int(a), int(b)))
    res = {k: [] for k in KAPPAS}
    for kappa in KAPPAS:
        G = graph(m, kappa)
        for a, b in pairs:
            p = lattice_path(G, a, b); pts = pull(m, p, kappa); res[kappa].append(metrics(m, pts))
    base = np.array([x[0] for x in res[0.0]])
    nlt = int((m["gn"] < R_IN).sum())
    print(f"\n### {name}: F cells {m['N']}, cells with g < r (witness): {nlt} ({100*nlt/m['N']:.1f}%), {len(pairs)} pairs")
    print(f"{'kappa':>6} | {'robot cost/m':>12} {'robot cost':>10} | {'len inc med':>11} {'p90':>7} {'max':>7} | {'share<0.35':>10} {'min g':>6} {'routes g<r':>10}")
    out = {}
    for kappa in KAPPAS:
        arr = np.array(res[kappa]); L = arr[:, 0]; inc = L / base - 1
        out[kappa] = dict(cost_m=float((arr[:, 1] / L).mean()), cost=float(arr[:, 1].mean()), med=float(np.median(inc)), p90=float(np.percentile(inc, 90)),
                          mx=float(inc.max()), share=float((arr[:, 2] / L).mean()), ming=float(np.median(arr[:, 3])), lt=int((arr[:, 3] < R_IN).sum()))
        o = out[kappa]
        print(f"{kappa:6.1f} | {o['cost_m']:12.1f} {o['cost']:10.0f} | {o['med']:11.1%} {o['p90']:7.1%} {o['mx']:7.1%} | {o['share']:10.1%} {o['ming']:6.2f} {o['lt']:10d}", flush=True)
    ok = [k for k in KAPPAS if k > 0 and out[k]["p90"] <= 0.10]
    print("  largest kappa with p90 length increase <= 10%:", max(ok) if ok else "none")
    # would excluding g < r cells disconnect pairs?
    keep = m["gn"] >= R_IN
    sel = keep[m["R"]] & keep[m["C"]]
    _, lab2 = connected_components(coo_matrix((np.ones(sel.sum()), (m["R"][sel], m["C"][sel])), shape=(m["N"],) * 2), directed=False)
    dis = sum(1 for a, b in pairs if not (keep[a] and keep[b] and lab2[a] == lab2[b]))
    print(f"  pairs disconnected if cells with g < r were excluded: {dis} of {len(pairs)}")
    return out

if __name__ == "__main__":
    for name in sys.argv[1:]: run(name)
