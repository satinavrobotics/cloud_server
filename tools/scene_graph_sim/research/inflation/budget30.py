import sys; sys.path.insert(0, ".")
import math, numpy as np
import sweep30 as S
KS = [0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0]
def stats(name, npairs=120, seed=7):
    m = S.load(name); S.build(m)
    rng = np.random.default_rng(seed); pairs = []
    while len(pairs) < npairs:
        a, b = rng.integers(0, m["N"], 2)
        d = math.hypot((m["xs"][a] - m["xs"][b]) * m["step"], (m["ys"][a] - m["ys"][b]) * m["step"])
        if d >= 3.0 and m["lab"][a] == m["lab"][b]: pairs.append((int(a), int(b)))
    def lens(kappa):
        G = S.graph(m, kappa); out = []; rc = []
        for a, b in pairs:
            L, c, e, g = S.metrics(m, S.pull(m, S.lattice_path(G, a, b), kappa)); out.append(L); rc.append(c / L)
        return np.array(out), float(np.mean(rc))
    base, rc0 = lens(0.0); rows = {}
    for k in KS:
        L, rc = lens(k); inc = L / base - 1
        rows[k] = dict(mean=inc.mean(), med=np.median(inc), p90=np.percentile(inc, 90), p95=np.percentile(inc, 95), mx=inc.max(), over15=(inc > 0.15).mean(), cost=rc, gain=1 - rc / rc0)
    return rows
res = {n: stats(n) for n in sys.argv[1:]}
print("kappa | " + " | ".join(f"{n:^40}" for n in res))
print("      | " + " | ".join("mean  med   p90   p95   max  >15%  cost-" for _ in res))
for k in KS:
    print(f"{k:5.2f} | " + " | ".join("%4.1f%% %4.1f%% %4.1f%% %4.1f%% %4.0f%% %4.0f%% %4.0f%%" % tuple(100 * r[k][x] for x in ("mean", "med", "p90", "p95", "mx", "over15", "gain")) for r in res.values()))
rules = {"p90 <= 10%": lambda r: r["p90"] <= 0.10, "mean <= 5%": lambda r: r["mean"] <= 0.05, "median <= 5%": lambda r: r["med"] <= 0.05,
         "p95 <= 15%": lambda r: r["p95"] <= 0.15, "share>15% <= 10%": lambda r: r["over15"] <= 0.10, "mean <= 5% and p95 <= 20%": lambda r: r["mean"] <= 0.05 and r["p95"] <= 0.20}
print("\nlargest kappa (of the grid) that meets each rule")
print(f"{'rule':>28} | " + " | ".join(f"{n:>9}" for n in res))
for rn, f in rules.items():
    print(f"{rn:>28} | " + " | ".join(f"{max([k for k in KS if f(r[k])], default=0):9.2f}" for r in res.values()))
