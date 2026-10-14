import sys; sys.path.insert(0, ".")
import sys, time, math, numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import dijkstra, connected_components
import sweep as S

def lat_len(m, path):
    xs, ys = m["xs"][path].astype(float), m["ys"][path].astype(float)
    return float(np.hypot(np.diff(xs), np.diff(ys)).sum() * m["step"])

def one_map(name, kappas=(0.5, 1.0), npairs=120, seed=7):
    m = S.load(name); S.build(m)
    keep = m["gn"] >= S.R_IN
    rng = np.random.default_rng(seed); pairs = []
    while len(pairs) < npairs:
        a, b = rng.integers(0, m["N"], 2)
        d = math.hypot((m["xs"][a] - m["xs"][b]) * m["step"], (m["ys"][a] - m["ys"][b]) * m["step"])
        if d >= 3.0 and m["lab"][a] == m["lab"][b]: pairs.append((int(a), int(b)))
    sel = keep[m["R"]] & keep[m["C"]]
    _, lab2 = connected_components(coo_matrix((np.ones(sel.sum()), (m["R"][sel], m["C"][sel])), shape=(m["N"],) * 2), directed=False)
    print(f"\n### {name}: F {m['N']}, witness cells (g<r) {int((~keep).sum())}, pairs {npairs}")
    for kappa in kappas:
        w = np.maximum(np.ceil(m["Lu"] * (1 + kappa * 0.5 * (m["phin"][m["R"]] + m["phin"][m["C"]])) - 1e-6), 1.0)
        Gf = coo_matrix((np.r_[w, w], (np.r_[m["R"], m["C"]], np.r_[m["C"], m["R"]])), shape=(m["N"],) * 2).tocsr()
        wm, Rm, Cm = w[sel], m["R"][sel], m["C"][sel]
        Gm = coo_matrix((np.r_[wm, wm], (np.r_[Rm, Cm], np.r_[Cm, Rm])), shape=(m["N"],) * 2).tocsr()
        t_full, t_mask, t_fail = [], [], []
        need2 = differ = cross = 0; jr, lr = [], []
        for a, b in pairs:
            t = time.perf_counter(); df, pf = dijkstra(Gf, directed=False, indices=a, return_predecessors=True); t_full.append(time.perf_counter() - t)
            p_full = [b]
            while p_full[-1] != a: p_full.append(int(pf[p_full[-1]]))
            p_full = p_full[::-1]
            t = time.perf_counter(); dm, pm = dijkstra(Gm, directed=False, indices=a, return_predecessors=True); dt_ = time.perf_counter() - t
            if keep[a] and keep[b] and np.isfinite(dm[b]):
                t_mask.append(dt_)
                p_mask = [b]
                while p_mask[-1] != a: p_mask.append(int(pm[p_mask[-1]]))
                p_mask = p_mask[::-1]
                if p_mask != p_full:
                    differ += 1
                    jr.append(dm[b] / df[b]); lr.append(lat_len(m, np.array(p_mask)) / lat_len(m, np.array(p_full)))
            else:
                need2 += 1; t_fail.append(dt_)
            if (m["gn"][np.array(p_full)] < S.R_IN).any(): cross += 1
        n = len(pairs)
        f, mk, fl = np.mean(t_full) * 1e3, (np.mean(t_mask) * 1e3 if t_mask else float("nan")), (np.mean(t_fail) * 1e3 if t_fail else float("nan"))
        two_avg = (np.sum(t_mask) + np.sum(t_fail) + np.sum([t_full[i] for i in range(n) if False]) ) / n
        # two-pass cost: every pair pays the masked search; the pairs that fail also pay the full search
        two = (np.sum(t_mask) + np.sum(t_fail) + need2 * np.mean(t_full)) / n * 1e3
        print(f" kappa {kappa}: priced route crosses a g<r cell in {cross}/{n}; floor changes the route in {differ}/{n}; second pass needed in {need2}/{n} ({100*need2/n:.0f}%)")
        if jr: print(f"   where the floor route differs (n={len(jr)}): J(floor)/J(priced) median {np.median(jr):.3f} p90 {np.percentile(jr,90):.3f} max {max(jr):.3f}; length ratio median {np.median(lr):.3f} p90 {np.percentile(lr,90):.3f} max {max(lr):.3f}")
        print(f"   search ms/plan: single pass {f:.1f}; masked pass {mk:.1f}; failed masked pass {fl:.1f}; two-pass avg {two:.1f}; worst (fail + full) {(np.max(t_fail) if t_fail else 0)*1e3 + np.max(t_full)*1e3:.1f}")

for name in sys.argv[1:]: one_map(name)
