"""Robot path efficiency: the path the robot DROVE against the path the planner computed.

Contract and rules: docs/path_efficiency_2026-10-15.md (and docs/API_REFERENCE.md). Read only.
Served as the `efficiency` block of GET /api/v1/maps/{map_id}/run-stats and as `driven_m` / `plan_m` per leg
of GET /api/v1/runs/{run_id}/legs?with_efficiency=true.

A LEG is one `run_legs` row: the move between two consecutive reached waypoints of a route node.
* planned_m  the length of the lattice plan path between those two waypoints: the polyline of
             `mission_tree[i].route.trajectory` (plan trajectory work, docs/plan_trajectory_2026-10-14.md) leg
             `from_index = k-1, to_index = k`, found from the leg's VDA node ids (`...-n{i}-s{2k+2}`). A leg
             without a (valid) trajectory falls back to the straight line between the two waypoints (a LOWER bound
             of the plan, so those legs are reported separately). Legs recorded since stage B of the edge
             statistics carry the answer (`run_legs.plan_source`, `planned_m`: dispatch compared the live route's
             trajectory with the leg when it ended, docs/path_efficiency_2026-10-15.md) and are read as recorded;
             older legs (plan_source NULL, `planned_m` = `straight_m`) are worked out here from the run's stored
             route snapshot. The rules are packages/utils/plan_length.py.
* driven_m   the path length of the robot's map-frame track (`robot_track_ts.x_map/y_map`) while the leg was in
             progress (`leg_seq`), with the drive_stats movement rules: a pair of consecutive rows of one run
             (same session and run epoch) farther apart than DRIVE_STATS_MAX_GAP_S or faster than
             DRIVE_STATS_MAX_SPEED_MS invalidates the leg; a pair slower than DRIVE_STATS_MIN_SPEED_MS is standing
             still (localization jitter) and adds nothing. A pair belongs to the leg of its END row (of its start
             row where the end row is between legs).

The sums are done in SQL (one row per leg comes back); numpy only reduces the legs.
"""
import asyncio
import dataclasses
import datetime
import json
import logging
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from packages import config
from packages.api import drive_stats, fleet_reads
from packages.api.result_cache import ResultCache
from packages.utils import plan_length as plan_length_mod
from packages.utils import run_legs

logger = logging.getLogger("ApiDelegationService.path_efficiency")

UTC = datetime.timezone.utc


# --- pure core -----------------------------------------------------------------------------------

parse_node = run_legs.parse_node     # (tree index, sequence) of "{prefix}-n{i}-s{seq}", else None


polyline_m = plan_length_mod.polyline_m
Trajectory = plan_length_mod.Trajectory
parse_trajectory = plan_length_mod.parse_trajectory


def plan_length(leg: Dict[str, Any], trajs: Dict[Tuple[Any, int], Trajectory]) -> Tuple[Optional[float], Optional[str]]:
    """(planned metres, source) of a leg: "trajectory" | "straight" | "stale" (a trajectory exists but does not
    match the leg: the route was replanned after the run's snapshot, straight is used) | None without any plan
    (the first leg of an order starts at the robot, not at a waypoint). A leg recorded with `plan_source`
    (stage B, dispatch worked it out from the live route) is read as recorded: "trajectory" with its stored
    `planned_m`; any other recorded source is "straight" / "stale" with the straight line, whatever the snapshot
    holds now. A leg recorded before (plan_source NULL) is worked out here from the run's stored snapshot
    (`mission_runs.mission_tree`), whose waypoints are compared with the leg's recorded `straight_m`."""
    straight = leg.get("straight_m")
    recorded = leg.get("plan_source")
    if recorded is not None:
        planned = leg.get("planned_m") if recorded == plan_length_mod.SOURCE_TRAJECTORY else straight
        return (None if planned is None else float(planned)), (recorded if planned is not None else None)
    src, dst = parse_node(leg.get("from_vda_node")), parse_node(leg.get("to_vda_node"))
    points, matches = None, False
    if src and dst and src[0] == dst[0] and dst[1] >= 4 and dst[1] % 2 == 0 and src[1] == dst[1] - 2:
        traj = trajs.get((leg["run_id"], dst[0]))
        k = dst[1] // 2 - 1
        points = traj.legs.get((k - 1, k)) if traj else None
        if points is not None and straight is not None and k < len(traj.waypoints):
            a, b = traj.waypoints[k - 1], traj.waypoints[k]
            matches = abs(math.hypot(b[0] - a[0], b[1] - a[1]) - straight) <= config.EFFICIENCY_STALE_TOL_M
    return plan_length_mod.resolve(points, straight, matches)


@dataclasses.dataclass
class Track:
    """What the SQL summed for one leg."""
    n_good: int = 0
    n_bad: int = 0
    all_m: float = 0.0       # every valid pair
    moving_m: float = 0.0    # pairs at or above min speed: the driven distance
    recovery_m: float = 0.0  # share of moving_m that lies inside NAV.RECOVERY_EXITED intervals


def leg_records(legs: Sequence[Dict[str, Any]], tracks: Dict[Tuple[Any, int], Track],
                trajs: Dict[Tuple[Any, int], Trajectory]) -> List[Dict[str, Any]]:
    """One record per leg: plan, driven and the reason it is not counted ("excluded") when it is not."""
    out = []
    for leg in legs:
        plan, src = plan_length(leg, trajs)
        tr = tracks.get((leg["run_id"], leg["seq"]))
        rec: Dict[str, Any] = {"run_id": leg["run_id"], "seq": leg["seq"], "plan_m": plan,
                               "plan_source": src, "driven_m": None, "driven_excl_recovery_m": None,
                               "recovery_m": None, "excluded": None,
                               "actual_s": leg.get("duration_s"), "expected_s": leg.get("expected_s"),
                               "recovery_s": leg.get("recovery_s") or 0.0}
        straight = leg.get("straight_m")
        if tr is None or (tr.n_good == 0 and tr.n_bad == 0):
            rec["excluded"] = "no_track"
        elif tr.n_bad:
            rec["excluded"] = "gap"
        elif straight is not None and tr.all_m < config.EFFICIENCY_MIN_COVERAGE * straight:
            rec["excluded"] = "incomplete_track"
        else:
            rec["driven_m"] = tr.moving_m
            rec["recovery_m"] = min(tr.recovery_m, tr.moving_m)
            rec["driven_excl_recovery_m"] = tr.moving_m - rec["recovery_m"]
        if rec["excluded"] is None and (plan is None or src is None):
            rec["excluded"] = "no_plan"
        elif rec["excluded"] is None and plan < config.EFFICIENCY_MIN_LEG_M:
            rec["excluded"] = "short"
        elif rec["excluded"] is None and rec["driven_m"] <= 0:
            rec["excluded"] = "incomplete_track"
        out.append(rec)
    return out


def _r(value: Optional[float], digits: int = 3) -> Optional[float]:
    return None if value is None else round(float(value), digits)


def _pair(driven: float, planned: float, ok: bool) -> Dict[str, Optional[float]]:
    return {"driven_m": _r(driven), "planned_m": _r(planned),
            "ratio": _r(planned / driven, 4) if ok and driven > 0 else None,
            "extra_pct": _r((driven / planned - 1.0) * 100.0, 2) if ok and planned > 0 else None}


def summarize(records: Sequence[Dict[str, Any]], truncated: bool = False,
              notes: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    """The `efficiency` block; None when there are no legs at all."""
    if not records:
        return None
    notes = list(notes or [])
    min_legs = config.EFFICIENCY_MIN_LEGS
    counted = [r for r in records if r["excluded"] is None]
    excluded: Dict[str, int] = {}
    for r in records:
        if r["excluded"]:
            excluded[r["excluded"]] = excluded.get(r["excluded"], 0) + 1
    stale = sum(1 for r in counted if r["plan_source"] == "stale")
    # The headline compares with the lattice plan only: legs planned by a stored trajectory. A straight-line plan
    # is a lower bound of the real plan and would read as a detour (`by_plan.straight` reports those apart).
    head = [r for r in counted if r["plan_source"] == "trajectory"]
    enough = len(head) >= min_legs
    driven = sum(r["driven_m"] for r in head)
    planned = sum(r["plan_m"] for r in head)
    ratios = np.array([r["plan_m"] / r["driven_m"] for r in head], float)
    q = (np.percentile(ratios, [10, 50, 90]) if enough else [None, None, None])
    ex = sum(r["driven_excl_recovery_m"] for r in head)
    rec_m = sum(r["recovery_m"] for r in head)

    def by(source: Sequence[str]) -> Dict[str, Any]:
        sel = [r for r in counted if r["plan_source"] in source]
        d = {"legs": len(sel)}
        d.update(_pair(sum(r["driven_m"] for r in sel), sum(r["plan_m"] for r in sel), len(sel) >= min_legs))
        return d

    timed = [r for r in records if r["expected_s"] is not None and (r["actual_s"] or 0) > 0]
    actual = sum(r["actual_s"] for r in timed)
    expected = sum(r["expected_s"] for r in timed)
    actual_x = sum(max(0.0, r["actual_s"] - r["recovery_s"]) for r in timed)
    t_ok = len(timed) >= min_legs
    trajectory_legs = len(head)
    straight_legs = len(counted) - trajectory_legs
    if straight_legs:
        notes.append(f"{straight_legs} leg(s) with a track have no usable plan trajectory (older runs, or the route "
                     "was replanned/rerouted mid-run); they are not in the headline, `by_plan.straight` compares "
                     "them with the straight line between waypoints, a lower bound of the plan")
    if not enough:
        notes.append(f"fewer than {min_legs} legs with a plan trajectory and a track: ratios are null")
    if truncated:
        notes.append("the leg cap cut older legs")
    return {
        "basis": "trajectory",
        "runs": len({r["run_id"] for r in head}),
        "legs": len(head),
        **_pair(driven, planned, enough),
        "p10": _r(q[0], 4), "median": _r(q[1], 4), "p90": _r(q[2], 4),
        "excluding_recovery": {"driven_m": _r(ex), "recovery_m": _r(rec_m),
                               "ratio": _r(planned / ex, 4) if enough and ex > 0 else None,
                               "extra_pct": _r((ex / planned - 1.0) * 100.0, 2) if enough and planned > 0 else None},
        "time": {"legs": len(timed), "actual_s": _r(actual, 1), "expected_s": _r(expected, 1),
                 "ratio": _r(expected / actual, 4) if t_ok and actual > 0 else None,
                 "ratio_excl_recovery": _r(expected / actual_x, 4) if t_ok and actual_x > 0 else None},
        "by_plan": {"trajectory": by(("trajectory",)), "straight": by(("straight", "stale"))},
        "source": {"track_legs": len(counted),   # legs with a usable track (either plan source)
                   "leg_only": excluded.get("no_track", 0),
                   "trajectory_legs": trajectory_legs,
                   "straight_legs": straight_legs,
                   "stale_trajectory_legs": stale},
        "excluded": {k: excluded.get(k, 0) for k in ("no_track", "gap", "incomplete_track", "no_plan", "short")},
        "data_quality": {"legs_read": len(records), "truncated": truncated, "notes": notes},
    }


# --- SQL -----------------------------------------------------------------------------------------

_SCOPE_MAP = ("scope AS (SELECT run_id FROM mission_runs WHERE map_id = %s AND started_at >= %s "
              "AND started_at < %s AND archived_at IS NULL{robot})")
_SCOPE_RUN = "scope AS (SELECT run_id FROM mission_runs WHERE run_id = %s)"

TRACK_SQL = (
    "WITH {scope}, "
    "rec AS (SELECT e.run_id, extract(epoch FROM e.ts)::float8 AS t1, (e.payload->>'duration_s')::float8 AS dur "
    "FROM fleet_events e JOIN scope s ON s.run_id = e.run_id WHERE e.code = 'NAV.RECOVERY_EXITED' "
    "AND e.ts >= %s AND e.ts < %s AND e.payload->>'duration_s' IS NOT NULL), "
    "rows AS (SELECT t.run_id, t.leg_seq, t.ts, t.x_map AS x, t.y_map AS y, "
    "lag(t.ts) OVER w AS pts, lag(t.x_map) OVER w AS px, lag(t.y_map) OVER w AS py, lag(t.leg_seq) OVER w AS pseq "
    "FROM robot_track_ts t JOIN scope s ON s.run_id = t.run_id WHERE t.x_map IS NOT NULL AND t.y_map IS NOT NULL "
    "AND t.ts >= %s AND t.ts < %s "
    "WINDOW w AS (PARTITION BY t.run_id, t.map_session_id, t.run_epoch ORDER BY t.ts)), "
    "seg AS (SELECT run_id, coalesce(leg_seq, pseq) AS seq, extract(epoch FROM ts)::float8 AS t1, "
    "extract(epoch FROM pts)::float8 AS t0, sqrt(power(x - px, 2) + power(y - py, 2)) AS dist "
    "FROM rows WHERE pts IS NOT NULL), "
    "ok AS (SELECT g.run_id, g.seq, g.dist, g.t1 - g.t0 AS dt, "
    "(g.t1 > g.t0 AND g.t1 - g.t0 <= %s AND g.dist <= %s * (g.t1 - g.t0)) AS good, "
    "(g.t1 - g.t0 > %s OR (g.t1 > g.t0 AND g.dist > %s * (g.t1 - g.t0))) AS bad, "
    "(SELECT coalesce(sum(greatest(0, least(g.t1, r.t1) - greatest(g.t0, r.t1 - r.dur))), 0) "
    "FROM rec r WHERE r.run_id = g.run_id) AS rec_s "
    "FROM seg g WHERE g.seq IS NOT NULL) "
    "SELECT run_id, seq, count(*) FILTER (WHERE good), count(*) FILTER (WHERE bad), "
    "coalesce(sum(dist) FILTER (WHERE good), 0), "
    "coalesce(sum(dist) FILTER (WHERE good AND dist >= %s * dt), 0), "
    "coalesce(sum(dist * least(1, rec_s / dt)) FILTER (WHERE good AND dist >= %s * dt), 0) "
    "FROM ok GROUP BY run_id, seq")

LEGS_SQL = ("SELECT l.run_id, l.seq, l.from_vda_node, l.to_vda_node, l.duration_s, l.straight_m, l.planned_m, "
            "l.plan_source, l.expected_s, l.recovery_s FROM run_legs l "
            "WHERE l.run_id IN (SELECT run_id FROM mission_runs WHERE {where}) ORDER BY l.run_id, l.seq LIMIT %s")

TRAJ_SQL = ("SELECT r.run_id, (t.i - 1)::int, "
            "(SELECT jsonb_agg(jsonb_build_array(w->'x', w->'y')) "
            "FROM jsonb_array_elements(t.n->'route'->'waypoints') w), t.n->'route'->'trajectory' "
            "FROM mission_runs r CROSS JOIN LATERAL jsonb_array_elements(r.mission_tree) WITH ORDINALITY AS t(n, i) "
            "WHERE r.run_id IN (SELECT run_id FROM mission_runs WHERE {where}) "
            "AND jsonb_typeof(t.n->'route'->'trajectory') = 'array'")

LEG_COLUMNS = ("run_id", "seq", "from_vda_node", "to_vda_node", "duration_s", "straight_m", "planned_m",
               "plan_source", "expected_s", "recovery_s")


@dataclasses.dataclass
class Loaded:
    legs: List[Dict[str, Any]]
    tracks: Dict[Tuple[Any, int], Track]
    trajs: Dict[Tuple[Any, int], Trajectory]
    truncated: bool


async def load(db: Any, map_id: Optional[str], start: Optional[datetime.datetime],
               end: Optional[datetime.datetime], robot: Optional[str],
               run_id: Optional[Any] = None) -> Loaded:
    """The legs, their summed tracks and the stored trajectories of one map window or of one run."""
    if run_id is not None:
        where, wargs = "run_id = %s", (run_id,)
        scope, sargs = _SCOPE_RUN, (run_id,)
        t_lo = t_hi = None
    else:
        robot_sql = " AND robot_name = %s" if robot else ""
        where = ("map_id = %s AND started_at >= %s AND started_at < %s AND archived_at IS NULL" + robot_sql)
        wargs = (map_id, start, end) + ((robot,) if robot else ())
        scope, sargs = _SCOPE_MAP.format(robot=robot_sql), wargs
        t_lo, t_hi = start, end + datetime.timedelta(days=1)
    if run_id is not None:
        t_lo = datetime.datetime(2000, 1, 1, tzinfo=UTC)
        t_hi = datetime.datetime(2100, 1, 1, tzinfo=UTC)
    p = drive_stats.Params()
    cap = config.EFFICIENCY_MAX_LEGS
    async with fleet_reads.read_cursor(db) as cur:
        await cur.execute(LEGS_SQL.format(where=where), wargs + (cap + 1,))
        leg_rows = await cur.fetchall()
        await cur.execute(TRACK_SQL.format(scope=scope), sargs + (
            t_lo, t_hi, t_lo, t_hi, p.max_gap_s, p.max_speed, p.max_gap_s, p.max_speed, p.min_speed, p.min_speed))
        track_rows = await cur.fetchall()
        await cur.execute(TRAJ_SQL.format(where=where), wargs)
        traj_rows = await cur.fetchall()
    trajs: Dict[Tuple[Any, int], Trajectory] = {}
    for rid, idx, wps, traj in traj_rows:
        parsed = parse_trajectory(_json(wps), _json(traj))
        if parsed is not None:
            trajs[(rid, int(idx))] = parsed
    tracks = {(r[0], int(r[1])): Track(int(r[2]), int(r[3]), float(r[4]), float(r[5]), float(r[6]))
              for r in track_rows}
    legs = [dict(zip(LEG_COLUMNS, r)) for r in leg_rows[:cap]]
    return Loaded(legs, tracks, trajs, len(leg_rows) > cap)


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, (str, bytes)) else value


def compute(loaded: Loaded) -> Optional[Dict[str, Any]]:
    return summarize(leg_records(loaded.legs, loaded.tracks, loaded.trajs), loaded.truncated)


CACHE = ResultCache(config.EFFICIENCY_CACHE_ENTRIES, config.EFFICIENCY_CACHE_TTL_S)


def run_efficiency(records: Sequence[Dict[str, Any]]) -> Dict[Any, Tuple[float, float, int]]:
    """run_id -> (planned_m, driven_m, legs) over the legs counted in the headline (a stored plan trajectory
    and a usable track)."""
    out: Dict[Any, List[float]] = {}
    for r in records:
        if r["excluded"] is None and r["plan_source"] == "trajectory":
            acc = out.setdefault(r["run_id"], [0.0, 0.0, 0])
            acc[0] += r["plan_m"]
            acc[1] += r["driven_m"]
            acc[2] += 1
    return {k: (v[0], v[1], int(v[2])) for k, v in out.items()}


def _analyse(loaded: Loaded) -> Dict[str, Any]:
    recs = leg_records(loaded.legs, loaded.tracks, loaded.trajs)
    return {"block": summarize(recs, loaded.truncated), "per_run": run_efficiency(recs)}


async def map_analysis(db: Any, map_id: str, start: datetime.datetime, end: datetime.datetime,
                       robot: Optional[str], key: Any = None, cache: Optional[ResultCache] = None
                       ) -> Dict[str, Any]:
    """`{"block": the efficiency block, "per_run": run_efficiency}` of a map window. Cached; the one cached load
    (TRACK_SQL is the heaviest query of run-stats) serves the headline and the series (map_series)."""
    cache = CACHE if cache is None else cache

    async def run() -> Dict[str, Any]:
        loaded = await load(db, map_id, start, end, robot)
        return await asyncio.to_thread(_analyse, loaded)
    return await cache.get(key if key is not None else (map_id, start, end, robot or None), run)


async def map_efficiency(db: Any, map_id: str, start: datetime.datetime, end: datetime.datetime,
                         robot: Optional[str], key: Any = None, cache: Optional[ResultCache] = None
                         ) -> Optional[Dict[str, Any]]:
    """The `efficiency` block of a map window (cached; the SQL and numpy work do not block the event loop)."""
    return (await map_analysis(db, map_id, start, end, robot, key, cache))["block"]


async def attach(out: Dict[str, Any], db: Any, map_id: str, start: Optional[datetime.datetime],
                 end: Optional[datetime.datetime], robot: Optional[str]) -> Dict[str, Any]:
    """Add `efficiency` to a run-stats answer; a failure leaves it null with a note (never fails run-stats)."""
    win = out.get("window") or {}
    s = datetime.datetime.fromisoformat(win["from"]) if win.get("from") else start
    e = datetime.datetime.fromisoformat(win["to"]) if win.get("to") else end
    try:
        # the cache key is the REQUEST window (a default `to` of "now" moves; the TTL bounds the staleness)
        out["efficiency"] = await map_efficiency(db, map_id, s, e, robot, key=(map_id, start, end, robot or None))
    except Exception as exc:  # noqa: BLE001
        logger.warning("path efficiency of %s unavailable: %s", map_id, exc)
        out["efficiency"] = None
        out.setdefault("data_quality", {}).setdefault("notes", []).append(
            "efficiency unavailable: the computation failed (see the API log)")
    return out


async def annotate_legs(db: Any, run_id: Any, items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Add plan_m / plan_source / driven_m / driven_excl_recovery_m / path_ratio / efficiency_excluded to the
    leg dicts of one run (in place); returns the top-level `{"efficiency": block}`."""
    loaded = await load(db, None, None, None, None, run_id=run_id)
    recs = await asyncio.to_thread(leg_records, loaded.legs, loaded.tracks, loaded.trajs)
    by_seq = {r["seq"]: r for r in recs}
    for item in items:
        r = by_seq.get(item.get("seq"))
        ok = r is not None and r["excluded"] is None
        item.update(
            plan_m=_r(r["plan_m"]) if r else None, plan_source=r["plan_source"] if r else None,
            driven_m=_r(r["driven_m"]) if r else None,
            driven_excl_recovery_m=_r(r["driven_excl_recovery_m"]) if r else None,
            path_ratio=_r(r["plan_m"] / r["driven_m"], 4) if ok else None,
            efficiency_excluded=r["excluded"] if r else None)
    return {"efficiency": await asyncio.to_thread(summarize, recs, loaded.truncated)}
