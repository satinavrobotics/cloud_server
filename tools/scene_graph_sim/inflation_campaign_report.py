"""Summary of `inflation_campaign.py grid` output (smoothness, budget rule stability, gates):

    python tools/scene_graph_sim/inflation_campaign_report.py grid.json
"""
import itertools
import json
import sys

import numpy as np

G = json.load(open(sys.argv[1]))
K, RE, OR, KP = [4.0, 6.0, 8.0, 10.0], [0.55, 0.8, 1.0], [0.25, 0.3], [0.25, 0.5, 0.75, 1.0, 2.0]
maps = sorted({g["map"] for g in G})
seeds = ["7", "8", "9"]
idx = {(g["map"], g["k"], g["reach"], g["origin"]): g for g in G}
tot = dict(unreach=0, g_formula=0, g_dom=0, g_a4=0, g_b1=0, g_b2=0, n=0)
for g in G:
    for s in seeds:
        for kp in KP:
            r = g["seeds"][s][str(kp)]
            for t in tot:
                tot[t] += r[t]
print("gate violations over all %d cells x 3 seeds x %d kappas (n = pair routes):" % (len(G), len(KP)), tot)


def val(m, k, re, o, kp, field):
    return float(np.mean([idx[(m, k, re, o)]["seeds"][s][str(kp)][field] for s in seeds]))


for f in ("mean", "p90", "cost_red", "share"):
    print("\n%s over the grid (seed mean)" % f)
    for m in maps:
        v = [val(m, k, re, o, kp, f) for k in K for re in RE for o in OR for kp in KP]
        print("  %-9s min %.3f median %.3f max %.3f" % (m, min(v), float(np.median(v)), max(v)))
axes = {"k": K, "reach": RE, "origin": OR, "kappa": KP}
for f in ("mean", "p90", "cost_red", "share"):
    print("\nlargest jump between neighbouring grid cells, %s (seed mean)" % f)
    for ax, vals in axes.items():
        best = (0.0, None)
        for m in maps:
            for k, re, o, kp in itertools.product(K, RE, OR, KP):
                cur = {"k": k, "reach": re, "origin": o, "kappa": kp}
                i = vals.index(cur[ax])
                if i + 1 >= len(vals):
                    continue
                nxt = dict(cur)
                nxt[ax] = vals[i + 1]
                a = val(m, cur["k"], cur["reach"], cur["origin"], cur["kappa"], f)
                b = val(m, nxt["k"], nxt["reach"], nxt["origin"], nxt["kappa"], f)
                if abs(a - b) > best[0]:
                    best = (abs(a - b), (m, cur, "->", vals[i + 1], round(a, 4), round(b, 4)))
        print("  %-7s %.4f  %s" % (ax, best[0], best[1]))
print("\nbudget rule (mean <= 5% and p90 <= 10%): largest kappa of the grid per (map, k, reach, origin) and seed")
rows = []
for m in maps:
    for k, re, o in itertools.product(K, RE, OR):
        sel = []
        for s in seeds:
            r = idx[(m, k, re, o)]["seeds"][s]
            ok = [kp for kp in KP if r[str(kp)]["mean"] <= 0.05 and r[str(kp)]["p90"] <= 0.10]
            sel.append(max(ok) if ok else 0.0)
        rows.append((m, k, re, o, sel))
spread = [max(KP.index(x) if x > 0 else -1 for x in r[4]) - min(KP.index(x) if x > 0 else -1 for x in r[4]) for r in rows]
print("cells %d; same kappa in all 3 seeds %d; max spread %d grid steps; spread >= 2 steps: %d; cells where no kappa meets the budget: %d" % (
    len(rows), sum(len(set(r[4])) == 1 for r in rows), max(spread), sum(1 for x in spread if x >= 2), sum(1 for r in rows if 0.0 in r[4])))
for m in maps:
    cs = [r for r in rows if r[0] == m]
    print("  %-9s identical %d/%d; chosen kappa counts %s" % (m, sum(len(set(r[4])) == 1 for r in cs), len(cs),
                                                             {kp: sum(r[4].count(kp) for r in cs) for kp in [0.0] + KP}))
print("\nreference cell k 6, reach 0.8, origin 0.30: kappa, mean, p90, cost reduction, share < 0.35 m")
for m in maps:
    print(" ", m, [(kp, round(val(m, 6.0, 0.8, 0.3, kp, "mean"), 3), round(val(m, 6.0, 0.8, 0.3, kp, "p90"), 3),
                    round(val(m, 6.0, 0.8, 0.3, kp, "cost_red"), 3), round(val(m, 6.0, 0.8, 0.3, kp, "share"), 3)) for kp in KP])
sp = [max(idx[c]["seeds"][s][str(kp)]["mean"] for s in seeds) - min(idx[c]["seeds"][s][str(kp)]["mean"] for s in seeds)
      for c in idx for kp in KP]
print("\nseed spread of the mean length increase: median %.4f max %.4f" % (float(np.median(sp)), max(sp)))
