#!/usr/bin/env python3
"""First back-test sketch for link-cost estimators (docs/link_cost_estimation_2026-10-13.md).

READ ONLY. Pulls run_legs with one SELECT (inside a read-only transaction) from the live
Postgres through `docker exec ... psql`, replays the legs in time order, predicts each leg's
duration from the legs before it with several estimators, and reports error and p90 calibration.

The unit here is the LEAF LEG (waypoint to waypoint, ~1-2 m), not a chart link: transits do not
exist yet. The numbers say how the estimators behave on sparse, noisy data; they are not a
validation for chart-link planning. Stdlib only.

  python3 scripts/analysis/linkcost_backtest.py [--container docker_compose-postgres-1]
         [--min-duration 1.0] [--half-life-days 7] [--shrink-k 2] [--last-n 3] [--csv legs.csv]
"""
import argparse
import csv
import io
import math
import subprocess
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone

# straight_m is the chord between the waypoints; from 2026-10-27 planned_m is the plan trajectory length where the leg
# had one (plan_source). This backtest keeps using the chord (the history before has nothing else).
SQL = """
COPY (
  SELECT run_id, seq, robot_name,
         coalesce(from_topomap_node, regexp_replace(from_vda_node, '^.*(n[0-9]+-s[0-9]+)$', '\\1')) AS f,
         coalesce(to_topomap_node, regexp_replace(to_vda_node, '^.*(n[0-9]+-s[0-9]+)$', '\\1')) AS t,
         (from_topomap_node IS NOT NULL AND to_topomap_node IS NOT NULL) AS topo,
         extract(epoch FROM started_at) AS t0, duration_s, stopped_s, straight_m, planned_m,
         expected_s, recoveries, recovery_s, blocks
  FROM run_legs ORDER BY started_at, run_id, seq
) TO STDOUT WITH CSV HEADER
"""


def fetch(container):
    pw = subprocess.check_output(["docker", "exec", container, "printenv", "POSTGRES_PASSWORD"],
                                 text=True).strip()
    cmd = ["docker", "exec", "-i", "-e", f"PGPASSWORD={pw}", container, "psql", "-U", "postgres",
           "-d", "mission", "-q", "-v", "ON_ERROR_STOP=1"]
    script = "SET default_transaction_read_only = on;\n" + SQL
    return subprocess.run(cmd, input=script, text=True, capture_output=True, check=True).stdout


def num(x):
    return float(x) if x not in ("", None) else None


def quantile(values, q):
    v = sorted(values)
    pos = (len(v) - 1) * q
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    return v[lo] + (v[hi] - v[lo]) * (pos - lo)


class Backtest:
    def __init__(self, half_life_s, k, last_n):
        self.hl, self.k, self.last_n = half_life_s, k, last_n
        self.all = []                       # all prior durations
        self.pace = []                      # prior s/m (straight_m known)
        self.link = defaultdict(list)       # key -> [(t0, duration)]
        self.ratios = defaultdict(list)     # estimator -> prior actual/point ratios

    def prior(self):
        return statistics.median(self.all) if self.all else None

    def predict(self, key, t0, straight):
        g = self.prior()
        if g is None:
            return {}
        out = {"global_median": g}
        if self.pace and straight:
            out["pace_x_length"] = statistics.median(self.pace) * straight
        base = out.get("pace_x_length", g)
        obs = self.link.get(key, [])
        n = len(obs)
        if n:
            out["link_mean_shrunk"] = (sum(d for _, d in obs) + self.k * base) / (n + self.k)
            last = [d for _, d in obs[-self.last_n:]]
            out["link_lastN_shrunk"] = (sum(last) + self.k * base) / (len(last) + self.k)
            w = [0.5 ** ((t0 - t) / self.hl) for t, _ in obs]
            out["link_ewma_shrunk"] = (sum(wi * d for wi, (_, d) in zip(w, obs))
                                       + self.k * base) / (sum(w) + self.k)
        else:
            for name in ("link_mean_shrunk", "link_lastN_shrunk", "link_ewma_shrunk"):
                out[name] = base          # unseen link: the prior only
        return out

    def update(self, key, t0, duration, straight):
        self.all.append(duration)
        if straight and straight > 0.3:
            self.pace.append(duration / straight)
        self.link[key].append((t0, duration))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--container", default="docker_compose-postgres-1")
    ap.add_argument("--min-duration", type=float, default=1.0,
                    help="drop legs shorter than this (padding / zero-length hops)")
    ap.add_argument("--half-life-days", type=float, default=7.0)
    ap.add_argument("--shrink-k", type=float, default=2.0, help="prior weight in observations")
    ap.add_argument("--last-n", type=int, default=3)
    ap.add_argument("--topo-only", action="store_true",
                    help="only legs whose both ends are topomap nodes (the link identity is then\n"
                         "geometric; the VDA-tail identity of the others is mission-structural)")
    ap.add_argument("--csv", help="read legs from this CSV (same columns) instead of the DB")
    args = ap.parse_args()

    raw = open(args.csv).read() if args.csv else fetch(args.container)
    rows = list(csv.DictReader(io.StringIO(raw)))
    legs = [r for r in rows if num(r["duration_s"]) >= args.min_duration
            and (r["topo"] == "t" or not args.topo_only)]
    print(f"legs read: {len(rows)}, used (duration >= {args.min_duration}s): {len(legs)}, "
          f"robots: {len({r['robot_name'] for r in legs})}")
    if len(legs) < 30:
        print("too few legs for any comparison"); return 1
    first = datetime.fromtimestamp(num(legs[0]["t0"]), timezone.utc)
    last = datetime.fromtimestamp(num(legs[-1]["t0"]), timezone.utc)
    print(f"span: {first:%Y-%m-%d} .. {last:%Y-%m-%d}")

    bt = Backtest(args.half_life_days * 86400, args.shrink_k, args.last_n)
    errs = defaultdict(list)                # estimator -> [(abs err, actual, pred, seen_link)]
    cover = defaultdict(list)               # estimator -> [actual <= p90 pred]
    for r in legs:
        key = (r["f"], r["t"])
        t0, dur, straight = num(r["t0"]), num(r["duration_s"]), num(r["straight_m"])
        clean = int(r["recoveries"]) == 0 and int(r["blocks"]) == 0
        preds = bt.predict(key, t0, straight)
        seen = bool(bt.link.get(key))
        for name, p in preds.items():
            errs[name].append((abs(dur - p), dur, p, seen, clean))
            rs = bt.ratios[name]
            if len(rs) >= 20:               # p90 = point x running p90 of past actual/point
                cover[name].append(dur <= p * quantile(rs, 0.9))
            if p > 0:
                rs.append(dur / p)
        bt.update(key, t0, dur, straight)

    def report(title, pick):
        print(f"\n{title}")
        print(f"{'estimator':22} {'n':>5} {'MAE s':>7} {'MedAE s':>8} {'bias s':>7}")
        for name, e in errs.items():
            sel = [x for x in e if pick(x)]
            if not sel:
                continue
            print(f"{name:22} {len(sel):5d} {statistics.mean(x[0] for x in sel):7.2f} "
                  f"{statistics.median(x[0] for x in sel):8.2f} "
                  f"{statistics.mean(x[2] - x[1] for x in sel):7.2f}")

    report("All legs", lambda x: True)
    report("Legs on a link seen before (the only place link estimators can differ)", lambda x: x[3])
    report("Clean legs (no recovery, no block) on a seen link", lambda x: x[3] and x[4])
    seen_n = sum(1 for x in errs["global_median"] if x[3])
    print(f"\nlegs whose link had a prior observation: {seen_n} of {len(errs['global_median'])}")
    print("\np90 calibration (target 0.90; running ratio quantile, needs 20 prior legs)")
    for name, c in cover.items():
        print(f"{name:22} n={len(c):4d} coverage={sum(c) / len(c):.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
