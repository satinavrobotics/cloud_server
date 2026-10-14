"""Operational pairs (docs/scene_graph/inflation_profile_validation.md, test E): hinge against exponential on the start / goal
pairs of the missions the robot actually drove.

    python tools/scene_graph_sim/inflation_operational.py --maps maps.json [--api http://localhost:8000] [--map office7]
        [--day 2026-10-13] [--limit 40]

Read-only GETs on the cloud API (`/api/v1/runs?limit=N`, `/api/v1/missions/<name>`; no credentials, nothing is written or posted):
the start and the goal of every navigation mission of `--map` started on `--day` are the first and the last waypoint of the
mission's route. Both tiers are planned on the research lattice of that map (`inflation_campaign.py`: the stored F and the
clearance of the read API of 2026-10-14, not the production engine) with the same cost-aware pull:

  hinge  today's live tier: margin = hypot(0.5, 0.5) / 2 = 0.354 m, r = 0.25 m, kappa 1 (rate kappa / (margin - r))
  exp    k 6, reach 0.8, origin 0.30 and kappa 0.5 / 0.75 / 1.0

Reported per tier against the kappa 0 route: length ratio (median / mean / p90 / max), robot cost (min(253, 252 exp(-6 (g - 0.3)
below 0.8)) per metre, diagnostics) against the hinge route and the share of the length below 0.35 m.
"""
import argparse
import json
import math
import os
import sys
import urllib.request

import numpy as np

sys.path[:0] = [os.path.dirname(os.path.abspath(__file__))]
import inflation_campaign as ic  # noqa: E402


def get(url):
    with urllib.request.urlopen(url, timeout=20) as r:
        return json.load(r)


def pairs_from_api(api, map_id, day, limit):
    out = []
    for r in get("%s/api/v1/runs?limit=%d" % (api, limit))["items"]:
        if r.get("map_id") != map_id or not (r.get("started_at") or "").startswith(day):
            continue
        try:
            mi = get("%s/api/v1/missions/%s" % (api, r["mission_name"]))
        except Exception:        # a mission that is gone
            continue
        wps = []
        for node in mi.get("mission_tree") or []:
            wps += (node.get("route") or {}).get("waypoints") or []
        if len(wps) >= 2:
            out.append((r["mission_name"], r["state"], (wps[0]["x"], wps[0]["y"]), (wps[-1]["x"], wps[-1]["y"]), len(wps)))
    return out


def nearest_f(m, x, y):
    c = int(math.floor((x - m["ox"]) / m["step"]))
    r = int(math.floor((y - m["oy"]) / m["step"]))
    best, bd = None, 1e9
    for dr in range(-6, 7):
        for dc in range(-6, 7):
            rr, cc = r + dr, c + dc
            if 0 <= rr < m["rows"] and 0 <= cc < m["cols"] and m["F"][rr, cc]:
                d = math.hypot((cc + 0.5) * m["step"] + m["ox"] - x, (rr + 0.5) * m["step"] + m["oy"] - y)
                if d < bd:
                    best, bd = int(m["idx"][rr, cc]), d
    return best, bd


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--maps", required=True)
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--map", default="office7")
    ap.add_argument("--day", default="2026-10-13")
    ap.add_argument("--limit", type=int, default=40)
    args = ap.parse_args()
    ms = pairs_from_api(args.api, args.map, args.day, args.limit)
    m = ic.load(args.maps, args.map)
    exp = (6.0, 0.8, 0.30)
    margin, r = math.hypot(0.5, 0.5) / 2.0, 0.25
    hinge = (None, margin, None)
    tiers = [("hinge k1", hinge, 1.0 / (margin - r)), ("exp .5", exp, 0.5), ("exp .75", exp, 0.75), ("exp 1.0", exp, 1.0)]
    G0 = ic.csr(m, ic.weights(m, np.zeros(m["N"]), 0.0))
    G = {n: ic.csr(m, ic.weights(m, ic.phi(m["gn"], *p), kp)) for n, p, kp in tiers}
    rows, skipped = [], 0
    for name, state, s, t, nwp in ms:
        a, da = nearest_f(m, *s)
        b, db = nearest_f(m, *t)
        if a is None or b is None or m["lab"][a] != m["lab"][b] or a == b:
            skipped += 1
            continue
        p0, _ = ic.route(m, G0, a, b)
        res = {"base": ic.metrics(m, ic.pull(m, p0, 0.0, exp), exp)}
        for n, p, kp in tiers:
            pt, _ = ic.route(m, G[n], a, b)
            res[n] = ic.metrics(m, ic.pull(m, pt, kp, p), exp)
        rows.append((name, state, nwp, da, db, res))
    print("%d missions of %s on %s, %d pairs planned (%d skipped: no F cell within 0.6 m, other component or start == goal)" % (
        len(ms), args.map, args.day, len(rows), skipped))
    print("pairs with a snap above 0.5 m: %d; distinct start/goal pairs: %d" % (
        sum(1 for r_ in rows if max(r_[3], r_[4]) > 0.5), len({(round(s[0], 2), round(s[1], 2), round(t[0], 2), round(t[1], 2))
                                                      for _, _, s, t, _ in ms})))
    L0 = np.array([r_[5]["base"][0] for r_ in rows])
    print("\n%-9s | length ratio vs kappa 0: median  mean   p90   max | robot cost/m (kappa 0 -> tier)  | vs hinge: cost/m ratio  length ratio | share < 0.35 m" % "tier")
    c0 = np.array([r_[5]["base"][1] / r_[5]["base"][0] for r_ in rows])
    e0 = np.array([r_[5]["base"][2] / r_[5]["base"][0] for r_ in rows])
    print("%-9s | %34s | %5.1f | %s | %.3f" % ("kappa 0", "1.000", float(c0.mean()), " " * 36, float(e0.mean())))
    H = np.array([r_[5]["hinge k1"][0] for r_ in rows])
    Hc = np.array([r_[5]["hinge k1"][1] / r_[5]["hinge k1"][0] for r_ in rows])
    for n, _, _ in tiers:
        L = np.array([r_[5][n][0] for r_ in rows])
        c = np.array([r_[5][n][1] / r_[5][n][0] for r_ in rows])
        e = np.array([r_[5][n][2] / r_[5][n][0] for r_ in rows])
        ratio = L / L0
        print("%-9s | %6.3f %6.3f %6.3f %6.3f  %8s | %5.1f | cost %.3f  length %.3f (median %.3f) | %.3f" % (
            n, float(np.median(ratio)), float(ratio.mean()), float(np.percentile(ratio, 90)), float(ratio.max()), "",
            float(c.mean()), float(c.mean() / Hc.mean()), float(np.mean(L / H)), float(np.median(L / H)), float(e.mean())))
    same = sum(1 for r_ in rows if abs(r_[5]["exp .5"][0] - r_[5]["hinge k1"][0]) < 1e-9)
    print("\npairs with exactly the same polyline length under hinge and exp 0.5: %d / %d" % (same, len(rows)))
    print("\nper mission (state, waypoints in the stored route, length kappa 0 / hinge / exp .5 [m])")
    for name, state, nwp, da, db, res in rows[:60]:
        print("  %-46s %-9s %2d  %6.2f %6.2f %6.2f" % (name, state, nwp, res["base"][0], res["hinge k1"][0], res["exp .5"][0]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
