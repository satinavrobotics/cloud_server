#!/usr/bin/env python3
"""Offline, read-only replay of the planner's region cost model on the historical runs (S1 of docs/planner_region_stats_2026-10-27.md).

For every run, in time order, build the region statistics from EARLIER runs only (finished before this one started, inside
the REGION_STATS window), turn them into a pricing snapshot (packages/utils/region_cost.py) and predict the time the run
needed along the cells it actually drove. No planner and no counterfactual route: this is the calibration of the model,
not of route choice. Compared with the actual time of the run (its own cell summary) and with today's predictor, the
leg `expected_s` of run_legs.

It needs none of the new tables: the cell summaries are made in memory from the raw rows with the production code
(`run_cells.summarise_rows`, dwell and speed basis included) and grouped into charts with the same owner-raster lookup and
`region_stats.aggregate` the API worker uses.

Three steps, because the analysis imports packages.api (pydantic 1 pins; use /tmp/lkvenv or the pinned image):

  1. host, stdlib only:   python3 scripts/analysis/region_cost_replay.py dump --out rows.json --map office9 --map office7
     SELECTs in a read-only transaction through `docker exec ... psql`; the password stays inside the container.
  2. the owner raster of each map's CURRENT graph version (charts of the stored version, one read of the stored tiles), run in
     the planner container, which has the credentials of the stores (nothing is written):
       docker exec -i docker_compose-mission-planner-service-1 python - owner office9 -6 -6 12 10 \\
           < scripts/analysis/region_cost_replay.py > owner_office9.json
     (the box is the area to cover, x0 y0 x1 y1 in metres; the history is assigned to today's charts, as the builder does)
  3. analysis:  /tmp/lkvenv/bin/python scripts/analysis/region_cost_replay.py replay --rows rows.json \\
         --owner office9=owner_office9.json --owner office7=owner_office7.json [--json out.json]

What is compared, per map, over the runs with at least --min-history earlier runs, a real factsheet speed_max (a placeholder
robot has the region term off) and a track (cells), on the cells inside a chart:
  * free        length / speed_max                       vs the moving time (time - stopped - dwell)
  * moving      the model with the `moving` term         vs the moving time
  * pace x len  earlier runs' time per metre x length    vs the total time
  * moving+stop the model with `moving` + `stop` terms   vs the moving + stopped time (dwell is waiting at the nodes)
  * total       moving+stop model + the median dwell of earlier runs  vs the total time, and vs the legs' duration_s
  * expected_s  today's leg predictor                    vs the legs' duration_s (its own actual: sum over the run's legs)
and, per (run, chart) crossing, the moving model by the number of earlier samples the chart had.
"""
import argparse
import base64
import dataclasses
import datetime
import io
import json
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

MAP_RE = r"^[A-Za-z0-9_.-]+$"


# --- step 1: dump (stdlib only) -----------------------------------------------------------------

def psql_json(container: str, sql: str) -> Any:
    """The rows of `sql` as parsed JSON (a list of objects), in a read-only transaction; the password never leaves the
    container."""
    script = ("PGPASSWORD=\"$POSTGRES_PASSWORD\" PGOPTIONS='-c default_transaction_read_only=on' psql -U \"$POSTGRES_USER\" "
              "-d \"$POSTGRES_DB\" -X -A -t -q -v ON_ERROR_STOP=1 -c \"$SQL\"")
    wrapped = f"SELECT coalesce(json_agg(t), '[]'::json) FROM ({sql}) t"
    out = subprocess.run(["docker", "exec", "-i", "-e", f"SQL={wrapped}", container, "sh", "-c", script],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def dump_map(container: str, map_id: str) -> Dict[str, Any]:
    import re
    if not re.match(MAP_RE, map_id):
        raise SystemExit(f"bad map name {map_id!r}")
    runs = psql_json(container, (
        "SELECT run_id, robot_name, map_id, started_at, ended_at, mission_tree FROM mission_runs "
        f"WHERE map_id = '{map_id}' AND ended_at IS NOT NULL ORDER BY started_at"))
    track = psql_json(container, (
        "SELECT t.run_id, extract(epoch FROM t.ts) AS ts, t.x_map AS x, t.y_map AS y, t.map_session_id AS session, "
        "t.run_epoch AS epoch, COALESCE(s.map_name, r.map_id) AS map "
        "FROM robot_track_ts t JOIN mission_runs r ON r.run_id = t.run_id "
        "LEFT JOIN map_sessions s ON s.session_id = t.map_session_id "
        f"WHERE r.map_id = '{map_id}' AND r.ended_at IS NOT NULL AND t.x_map IS NOT NULL AND t.y_map IS NOT NULL "
        "ORDER BY t.run_id, t.ts"))
    legs = psql_json(container, (
        "SELECT run_id, seq, expected_s, duration_s FROM run_legs "
        f"WHERE map_id = '{map_id}' AND duration_s > 0"))
    return {"runs": runs, "track": track, "legs": legs}


def dump(args: argparse.Namespace) -> int:
    out = {"dumped_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
           "factsheets": {r["name"]: r["factsheet"] for r in psql_json(
               args.container, "SELECT name, status->'factsheet' AS factsheet FROM robotobjectv1 "
                               "WHERE lifecycle != 'DELETED'")},
           "maps": {m: dump_map(args.container, m) for m in args.map}}
    with open(args.out, "w") as fh:
        json.dump(out, fh)
    print(f"{args.out}: " + ", ".join(f"{m}: {len(v['runs'])} runs, {len(v['track'])} track rows"
                                      for m, v in out["maps"].items()))
    return 0


# --- step 2: owner raster (runs in the planner container) -----------------------------------------

def owner_dump(map_id: str, box: Sequence[float]) -> Dict[str, Any]:
    import numpy as np
    from packages.scene_graph.api import BBox
    from packages.scene_graph.store import SceneGraphStore
    from packages.topomap_dbs.client import TopomapDatabaseClient
    view = SceneGraphStore.from_client(TopomapDatabaseClient()).view(map_id)
    res = view.query(BBox(*box), 0, raster=True, parents=False)
    r = res.raster
    buf = io.BytesIO()
    np.savez_compressed(buf, index=np.asarray(r.index, np.uint16))
    return {"map": map_id, "version": view.version, "origin": [float(r.origin[0]), float(r.origin[1])],
            "step": float(r.step), "shape": list(r.index.shape), "keys": list(r.keys),
            "charts": [{"key": c.key, "cells": int(c.cells)} for c in res.charts],
            "index_npz_b64": base64.b64encode(buf.getvalue()).decode()}


# --- step 3: the analysis ---------------------------------------------------------------------------

@dataclasses.dataclass
class Owner:
    """The owner plane of the current graph version, as `drive_stats.plane_lookup` takes it."""
    version: int
    plane: Dict[str, Any]
    names_by_key: List[str]
    cells: Dict[str, int]

    @classmethod
    def load(cls, path: str) -> "Owner":
        import numpy as np
        d = json.load(open(path))
        index = np.load(io.BytesIO(base64.b64decode(d["index_npz_b64"])))["index"]
        plane = {"version": d["version"], "origin": tuple(d["origin"]), "step": d["step"], "shape": tuple(d["shape"]),
                 "keys": list(d["keys"]), "index": index}
        return cls(int(d["version"]), plane, list(d["keys"]), {c["key"]: c["cells"] for c in d["charts"]})

    def chart_ids(self, key):
        """(chart id per cell key: 0 none, i = names[i-1]) for sorted/any absolute cell keys."""
        from packages.api import drive_stats as ds
        from packages.utils import cell_raster as cr
        ax, ay = cr.key_cells(key)
        vals = ds.plane_lookup(self.plane, ax, ay)
        import numpy as np
        return np.clip(vals, 0, len(self.names_by_key)).astype(np.int64)


@dataclasses.dataclass
class Item:
    """One run: its cell summary and what run_legs says about it."""
    run_id: str
    started: datetime.datetime
    ended: datetime.datetime
    row: Any                                # run_cells_db.CellRow, source "track" only (else None)
    ids: Any = None                         # chart id per cell of the row
    expected_s: float = 0.0                 # sum over the run's legs of expected_s ...
    leg_duration_s: float = 0.0             # ... and of duration_s over the SAME legs


def _dt(value: Any) -> datetime.datetime:
    if isinstance(value, datetime.datetime):
        return value if value.tzinfo else value.replace(tzinfo=datetime.timezone.utc)
    return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def summarise_map(map_id: str, dumped: Dict[str, Any], factsheets: Dict[str, Any], owner: Owner) -> List[Item]:
    """The cell summaries of the map's runs (the production `summarise_rows`) with their chart ids."""
    from packages.api import run_cells as rc
    tracks: Dict[str, List[tuple]] = {}
    for t in dumped["track"]:
        if t["map"] == map_id:
            tracks.setdefault(t["run_id"], []).append((float(t["ts"]), float(t["x"]), float(t["y"]), t["session"],
                                                       t["epoch"], t["map"]))
    legs: Dict[str, List[Tuple[float, float]]] = {}
    for l in dumped["legs"]:
        if l["expected_s"] and l["expected_s"] > 0:
            legs.setdefault(l["run_id"], []).append((float(l["expected_s"]), float(l["duration_s"])))
    items: List[Item] = []
    for r in dumped["runs"]:
        started, ended = _dt(r["started_at"]), _dt(r["ended_at"])
        data = rc.RunData(r["run_id"], r["robot_name"], r["map_id"], started, ended, track=tracks.get(r["run_id"], []))
        data.tree = r.get("mission_tree")
        rows = [x for x in rc.summarise_rows(data, lambda m: {}, factsheets.get(r["robot_name"]))
                if x.map_id == map_id and x.source == "track" and len(x)]
        item = Item(r["run_id"], started, ended, rows[0] if rows else None)
        item.expected_s = sum(e for e, _ in legs.get(r["run_id"], []))
        item.leg_duration_s = sum(d for _, d in legs.get(r["run_id"], []))
        if item.row is not None:
            item.ids = owner.chart_ids(item.row.key)
        items.append(item)
    items.sort(key=lambda i: i.started)
    return items


def errors(pred: Sequence[float], actual: Sequence[float]) -> Dict[str, Any]:
    """Summary of predicted vs actual seconds: absolute error (s) median / p90, absolute error as a share of the actual,
    the median of actual / predicted, the bias (mean predicted - actual) and the ratio of the totals."""
    import numpy as np
    p, a = np.asarray(pred, float), np.asarray(actual, float)
    if not p.size:
        return {"n": 0}
    err = np.abs(p - a)
    ok = p > 0
    return {"n": int(p.size), "abs_err_median_s": float(np.median(err)), "abs_err_p90_s": float(np.percentile(err, 90)),
            "rel_err_median": float(np.median(err / a)), "rel_err_p90": float(np.percentile(err / a, 90)),
            "actual_over_pred_median": float(np.median(a[ok] / p[ok])) if ok.any() else None,
            "bias_s": float(np.mean(p - a)), "total_actual_over_pred": float(a.sum() / p.sum()) if p.sum() > 0 else None}


def stats_for(rows: List[Dict[str, Any]], map_id: str, version: int, cls: str):
    """RegionStats of the class and of the pool from `region_stats.aggregate` rows."""
    from packages.utils.region_stats_load import FIELDS, RegionStat, RegionStats
    out = []
    for c in (cls, "*"):
        charts = {r["chart_key"]: RegionStat(**{f: r[f] for f in FIELDS}) for r in rows if r["robot_class"] == c}
        out.append(RegionStats(map_id, version, c, charts, "replay", "ok", None))
    return out


def replay_map(map_id: str, items: List[Item], owner: Owner, *, min_history: int = 10, min_len_m: float = 1.0,
               min_actual_s: float = 3.0, window_days: Optional[int] = None, n0: Optional[float] = None,
               rho_cap: Optional[float] = None) -> Dict[str, Any]:
    """The replay of one map (see the module docstring); returns the samples and the metrics."""
    import numpy as np
    from packages import config
    from packages.api import region_stats as rs
    from packages.utils import region_cost as rcost
    from packages.utils import robot_class as rclass
    window = datetime.timedelta(days=window_days or config.REGION_STATS_WINDOW_DAYS)
    base = rcost.CostModel.from_config()
    kw = {"n0": base.n0 if n0 is None else n0, "rho_cap": base.rho_cap if rho_cap is None else rho_cap,
          "quantum": base.quantum}
    models = {"moving": rcost.CostModel(terms=(("moving", 1.0),), **kw),
              "moving+stop": rcost.CostModel(terms=(("moving", 1.0), ("stop", 1.0)), **kw)}
    runs: List[Dict[str, Any]] = []
    crossings: List[Dict[str, Any]] = []
    skipped = {"few_history": 0, "no_speed_max": 0, "no_cells": 0, "short": 0, "no_snapshot": 0}
    names = owner.names_by_key
    for i, it in enumerate(items):
        if it.row is None:
            skipped["no_cells"] += 1
            continue
        hist = [h for h in items[:i] if h.row is not None and h.ended <= it.started and it.started - h.started <= window]
        if len(hist) < min_history:
            skipped["few_history"] += 1
            continue
        row, ids = it.row, it.ids
        if row.speed_basis != rclass.SPEED_FACTSHEET or not row.speed_max:
            skipped["no_speed_max"] += 1
            continue
        inside = ids > 0
        length = float(row.len_m[inside].sum())
        moving_actual = float((row.time_s - row.stopped_s - row.dwell_s)[inside].sum())
        stopped_actual = float(row.stopped_s[inside].sum())
        total_actual = float(row.time_s[inside].sum())
        if length < min_len_m or moving_actual < min_actual_s:
            skipped["short"] += 1
            continue
        stat_rows = rs.aggregate(map_id, owner.version, [h.row for h in hist], [h.ids for h in hist], names, owner.cells,
                                 "replay")
        stats_class, stats_pooled = stats_for(stat_rows, map_id, owner.version, row.robot_class)
        v = float(row.speed_max)
        rec: Dict[str, Any] = {"run_id": it.run_id, "started": it.started.isoformat(), "len_m": length,
                               "moving_s": moving_actual, "stopped_s": stopped_actual, "total_s": total_actual,
                               "dwell_s": float(row.dwell_s[inside].sum()), "n_history": len(hist),
                               "free_s": length / v}
        snaps = {}
        for name, model in models.items():
            snap = rcost.build_snapshot(map_id, owner.version, row.robot_class, names, stats_class, stats_pooled, model, v,
                                        row.speed_basis)
            if not snap.active:
                skipped["no_snapshot"] += 1
                snaps = {}
                break
            snaps[name] = snap
        if not snaps:
            continue
        for name, snap in snaps.items():
            rho = snap.rho[ids[inside] - 1] / snap.scale
            rec["pred_" + name] = float((row.len_m[inside] * (1.0 + rho)).sum() / v)
        same = [h for h in hist if h.row.robot_class == row.robot_class] or hist
        tl = sum(float(h.row.time_s[h.ids > 0].sum()) for h in same)
        ll = sum(float(h.row.len_m[h.ids > 0].sum()) for h in same)
        rec["pred_pace_len"] = (tl / ll) * length if ll > 0 else 0.0
        dwells = [float(h.row.dwell_s[h.ids > 0].sum()) for h in same]
        rec["pred_total"] = rec["pred_moving+stop"] + float(np.median(dwells))
        rec["expected_s"], rec["leg_duration_s"] = it.expected_s, it.leg_duration_s
        runs.append(rec)
        # per chart crossing
        m_snap = snaps["moving"]
        for k in np.unique(ids[inside]):
            sel = ids == k
            c_len = float(row.len_m[sel].sum())
            if c_len < min_len_m:
                continue
            c_mov = float((row.time_s - row.stopped_s - row.dwell_s)[sel].sum())
            if c_mov <= 0:
                continue
            key = names[k - 1]
            st = stats_class.charts.get(key)
            crossings.append({"chart": key, "n_prior": int(st.norm_runs) if st else 0, "len_m": c_len,
                              "actual_s": c_mov, "pred_s": c_len * (1.0 + m_snap.rho[k - 1] / m_snap.scale) / v,
                              "free_s": c_len / v})
    return {"map": map_id, "graph_version": owner.version, "skipped": skipped, "runs": runs, "crossings": crossings,
            "metrics": metrics(runs, crossings)}


BUCKETS = ((0, 0, "0"), (1, 4, "1-4"), (5, 19, "5-19"), (20, 10 ** 9, "20+"))


def metrics(runs: List[Dict[str, Any]], crossings: List[Dict[str, Any]]) -> Dict[str, Any]:
    m: Dict[str, Any] = {}
    act = lambda k: [r[k] for r in runs]
    m["free vs moving"] = errors(act("free_s"), act("moving_s"))
    m["moving vs moving"] = errors(act("pred_moving"), act("moving_s"))
    m["pace x len vs total"] = errors(act("pred_pace_len"), act("total_s"))
    m["moving+stop vs moving+stopped"] = errors(act("pred_moving+stop"), [r["moving_s"] + r["stopped_s"] for r in runs])
    m["total (moving+stop+median dwell) vs total"] = errors(act("pred_total"), act("total_s"))
    legs = [r for r in runs if r["expected_s"] > 0 and r["leg_duration_s"] > 0]
    m["expected_s vs leg duration"] = errors([r["expected_s"] for r in legs], [r["leg_duration_s"] for r in legs])
    m["total vs leg duration (same runs)"] = errors([r["pred_total"] for r in legs], [r["leg_duration_s"] for r in legs])
    same_runs = [r for r in legs]
    m["free vs moving (runs with legs)"] = errors([r["free_s"] for r in same_runs], [r["moving_s"] for r in same_runs])
    m["moving vs moving (runs with legs)"] = errors([r["pred_moving"] for r in same_runs], [r["moving_s"] for r in same_runs])
    m["crossings: moving"] = errors([c["pred_s"] for c in crossings], [c["actual_s"] for c in crossings])
    m["crossings: free"] = errors([c["free_s"] for c in crossings], [c["actual_s"] for c in crossings])
    for lo, hi, name in BUCKETS:
        sub = [c for c in crossings if lo <= c["n_prior"] <= hi]
        m[f"crossings n_prior {name}: moving"] = errors([c["pred_s"] for c in sub], [c["actual_s"] for c in sub])
        m[f"crossings n_prior {name}: free"] = errors([c["free_s"] for c in sub], [c["actual_s"] for c in sub])
    return m


def table(metrics_: Dict[str, Any]) -> str:
    head = "| predictor vs actual | n | abs err median s | abs err p90 s | rel err median | rel err p90 | actual/pred median | sum actual/sum pred | bias s |"
    lines = [head, "|" + "---|" * 9]
    for name, e in metrics_.items():
        if not e.get("n"):
            lines.append(f"| {name} | 0 | | | | | | | |")
            continue
        f = lambda x, d=2: "" if x is None else f"{x:.{d}f}"
        lines.append(f"| {name} | {e['n']} | {f(e['abs_err_median_s'])} | {f(e['abs_err_p90_s'])} | "
                     f"{f(100 * e['rel_err_median'], 1)} % | {f(100 * e['rel_err_p90'], 1)} % | "
                     f"{f(e['actual_over_pred_median'])} | {f(e['total_actual_over_pred'])} | {f(e['bias_s'])} |")
    return "\n".join(lines)


def replay(args: argparse.Namespace) -> int:
    dumped = json.load(open(args.rows))
    owners = dict(o.split("=", 1) for o in args.owner)
    out = {}
    for map_id, path in owners.items():
        owner = Owner.load(path)
        items = summarise_map(map_id, dumped["maps"][map_id], dumped["factsheets"], owner)
        res = replay_map(map_id, items, owner, min_history=args.min_history, min_len_m=args.min_len,
                         min_actual_s=args.min_actual_s, n0=args.n0, rho_cap=args.rho_cap)
        out[map_id] = res
        n_rows = sum(1 for i in items if i.row is not None)
        print(f"\n## {map_id}: graph v{owner.version}, {len(items)} runs, {n_rows} with cells, "
              f"{len(res['runs'])} evaluated, {len(res['crossings'])} chart crossings; skipped {res['skipped']}\n")
        print(table(res["metrics"]))
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(out, fh, indent=1)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("dump", help="step 1: the raw rows of the maps to a JSON file (read only)")
    d.add_argument("--out", required=True)
    d.add_argument("--map", action="append", required=True)
    d.add_argument("--container", default="docker_compose-postgres-1")
    o = sub.add_parser("owner", help="step 2: print the owner raster of a map (run in the planner container)")
    o.add_argument("map")
    o.add_argument("box", nargs=4, type=float)
    r = sub.add_parser("replay", help="step 3: the analysis")
    r.add_argument("--rows", required=True)
    r.add_argument("--owner", action="append", required=True, metavar="MAP=FILE")
    r.add_argument("--min-history", type=int, default=10, help="earlier runs needed before a run is predicted")
    r.add_argument("--min-len", type=float, default=1.0, help="metres inside charts a run needs / a crossing needs")
    r.add_argument("--min-actual-s", type=float, default=3.0)
    r.add_argument("--n0", type=float, help="shrinkage strength (default: REGION_COST_N0)")
    r.add_argument("--rho-cap", type=float, help="cap of rho (default: REGION_COST_RHO_CAP)")
    r.add_argument("--json")
    args = p.parse_args(argv)
    if args.cmd == "dump":
        return dump(args)
    if args.cmd == "owner":
        print(json.dumps(owner_dump(args.map, args.box)))
        return 0
    return replay(args)


if __name__ == "__main__":
    sys.exit(main())
