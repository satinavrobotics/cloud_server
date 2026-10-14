import sys; sys.path.insert(0, ".")
import math, numpy as np
import sweep as S
KAPPA = 0.75
THRESH = {"0.30 (robot global inscribed, 0.25 + 0.05 padding)": 0.30, "0.371 (that + h/sqrt2)": 0.30 + 0.0707, "0.321 (design: 0.25 + h/sqrt2)": 0.321}
def one(name, npairs=120, seed=7):
    m = S.load(name); S.build(m)
    rng = np.random.default_rng(seed); pairs = []
    while len(pairs) < npairs:
        a, b = rng.integers(0, m["N"], 2)
        d = math.hypot((m["xs"][a] - m["xs"][b]) * m["step"], (m["ys"][a] - m["ys"][b]) * m["step"])
        if d >= 3.0 and m["lab"][a] == m["lab"][b]: pairs.append((int(a), int(b)))
    G = S.graph(m, KAPPA)
    stats = {t: dict(wp=0, bad=0, routes=0, shifts=[]) for t in THRESH}
    for a, b in pairs:
        path = S.lattice_path(G, a, b)
        pts = S.pull(m, path, KAPPA)
        pp = np.array([(m["ox"] + (m["xs"][v] + 0.5) * m["step"], m["oy"] + (m["ys"][v] + 0.5) * m["step"]) for v in path])
        gp = m["gn"][path]
        for t_name, T in THRESH.items():
            st = stats[t_name]; bad_here = 0
            for k, p in enumerate(pts):
                st["wp"] += 1
                if k in (0, len(pts) - 1): continue                      # endpoints are never moved
                r, c = S.cell_of(m, np.array([p[0]]), np.array([p[1]]))
                if not (0 <= r[0] < m["rows"] and 0 <= c[0] < m["cols"]): continue
                if m["g"][r[0], c[0]] < T:
                    st["bad"] += 1; bad_here += 1
                    ok = gp >= T
                    st["shifts"].append(float(np.min(np.hypot(pp[ok, 0] - p[0], pp[ok, 1] - p[1]))) if ok.any() else float("inf"))
            st["routes"] += bad_here > 0
    print(f"\n### {name}  (kappa {KAPPA}, {npairs} routes)")
    for t_name, st in stats.items():
        sh = np.array(st["shifts"]); fin = sh[np.isfinite(sh)]
        line = f"  floor {t_name}: interior waypoints below it {st['bad']} of {st['wp']} ({100*st['bad']/max(st['wp'],1):.1f}%), routes affected {st['routes']}/{npairs}"
        if len(fin): line += f" | shift to the nearest admissible path cell: median {np.median(fin):.2f} m, p90 {np.percentile(fin,90):.2f}, max {fin.max():.2f}; within 0.35 m: {100*np.mean(fin<=0.35):.0f}%, within 0.5 m: {100*np.mean(fin<=0.5):.0f}%"
        print(line)
for n in sys.argv[1:]: one(n)
