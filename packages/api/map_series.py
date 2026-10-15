"""Time series of the per-map stats: `?series=day|run` of run-stats, `?series=day` of events-summary
(docs/map_series_2026-10-15.md). The drive-stats and mapping-stats series live in their own modules.

Every block is aligned with `t`; SQL does the bucketing (one grouped query per block, in the same read-only
transaction), Python only places the rows on the axis. Cached per (kind, map, request window, robot, series).
"""
import asyncio
import datetime
import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

from packages import config
from packages.api import fleet_reads, map_events, path_efficiency, run_stats, series_axis as ax
from packages.api.drive_stats import ResultCache
from packages.events.codes import EventCode

logger = logging.getLogger("ApiDelegationService.map_series")

UTC = datetime.timezone.utc
RUN_POINTS = 60
TOP_CODES = 6
OUTCOMES = ("completed", "failed", "timeout", "aborted", "canceled")
CACHE = ResultCache(config.EFFICIENCY_CACHE_ENTRIES, config.EFFICIENCY_CACHE_TTL_S)

_DAY = "(started_at AT TIME ZONE 'UTC')::date"


async def _rows(cur: Any, sql: str, params: Sequence[Any]) -> List[Sequence[Any]]:
    await cur.execute(sql, params)
    return await cur.fetchall()


def _f(value: Any, digits: int) -> Optional[float]:
    return None if value is None else round(float(value), digits)


# --- efficiency per run / per day ----------------------------------------------------------------

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


def efficiency_point(planned: float, driven: float) -> Tuple[Optional[float], Optional[float]]:
    """(ratio = planned / driven, extra_pct = (driven / planned - 1) * 100)."""
    if driven <= 0 or planned <= 0:
        return None, None
    return round(planned / driven, 4), round((driven / planned - 1.0) * 100.0, 2)


async def _efficiency(db: Any, map_id: str, start, end, robot) -> Dict[Any, Tuple[float, float, int]]:
    loaded = await path_efficiency.load(db, map_id, start, end, robot)
    recs = await asyncio.to_thread(path_efficiency.leg_records, loaded.legs, loaded.tracks, loaded.trajs)
    return run_efficiency(recs)


def _scope(robot: Optional[str]) -> str:
    return "WITH " + run_stats._SCOPE.format(robot=" AND robot_name = %s" if robot else "")  # pylint: disable=protected-access


# --- run-stats series ----------------------------------------------------------------------------

async def _safety_days(cur: Any, map_id: str, start, end, robot, pos: Dict[Any, int], n: int) -> List[int]:
    codes = sorted({c for c, _, _ in map_events.SAFETY_CLASSES})
    out = ax.zeros(n)
    for d, code, ekey, count in await map_events.daily_counts(cur, map_id, start, end, robot, codes):
        if d in pos and map_events.classify_event(code, ekey or None) is not None:
            out[pos[d]] += int(count)
    return out


async def _run_stats_day(db: Any, map_id: str, start, end, robot) -> Dict[str, Any]:
    axis = ax.day_axis(start, end)
    n = len(axis)
    pos = ax.index_of(axis)
    scope = _scope(robot)
    sp: Tuple[Any, ...] = (map_id, start, end) + ((robot,) if robot else ())
    ev_end = end + datetime.timedelta(days=1)
    cap = config.RUN_STATS_MAX_RUN_S
    teleop_code = EventCode.TELEOP_SESSION_STARTED.value
    outcomes = {o: ax.zeros(n) for o in OUTCOMES}
    median = ax.nulls(n)
    takeovers, teleop = ax.zeros(n), ax.zeros(n)
    hours = [0.0] * n
    rec_legs, rec_median, rec_share = ax.zeros(n), ax.nulls(n), ax.nulls(n)
    run_day: Dict[Any, int] = {}

    async with fleet_reads.read_cursor(db) as cur:
        for d, state, count, med in await _rows(cur, scope + (
                f" SELECT {_DAY}, state, count(*), percentile_cont(0.5) WITHIN GROUP (ORDER BY "
                "extract(epoch FROM ended_at - started_at)) FILTER (WHERE state = 'COMPLETED' "
                "AND ended_at IS NOT NULL) FROM scope GROUP BY 1, 2"), sp):
            i = pos.get(d)
            if i is None:
                continue
            key = str(state).lower()
            if key in outcomes:
                outcomes[key][i] += int(count)
            if state == "COMPLETED":
                median[i] = _f(med, 1)
        for d, count in await _rows(cur, scope + (
                f" SELECT {_DAY}, count(*) FROM scope s WHERE s.state = 'CANCELED' AND EXISTS (SELECT 1 FROM "
                "fleet_events e WHERE e.run_id = s.run_id AND e.code = %s AND e.ts >= %s AND e.ts < %s "
                "AND e.payload->>'error_type' = %s) GROUP BY 1"),
                sp + (EventCode.ROBOT_ERROR_RAISED.value, start, ev_end, run_stats.TAKEOVER_ERROR)):
            if d in pos:
                takeovers[pos[d]] += int(count)
        for d, count in await _rows(cur, (
                "SELECT (ts AT TIME ZONE 'UTC')::date, count(*) FROM fleet_events WHERE code = %s AND ts >= %s "
                "AND ts < %s AND payload->>'map_id' = %s" + (" AND robot_name = %s" if robot else "")
                + " GROUP BY 1"), (teleop_code, start, end, map_id) + ((robot,) if robot else ())):
            if d in pos:
                teleop[pos[d]] += int(count)
        # operating time: the union of the finished runs' intervals per robot, each interval's new part credited
        # to the day its run started (the same rule and cap as the headline `hours`)
        for d, secs in await _rows(cur, scope + (
                ", f AS (SELECT robot_name, started_at AS s, ended_at AS e FROM scope "
                "WHERE ended_at IS NOT NULL AND extract(epoch FROM ended_at - started_at) <= %s), "
                "o AS (SELECT s, e, max(e) OVER (PARTITION BY robot_name ORDER BY s, e "
                "ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS pe FROM f) "
                "SELECT (s AT TIME ZONE 'UTC')::date, coalesce(sum(greatest(0, extract(epoch FROM "
                "e - greatest(s, coalesce(pe, s))))), 0) FROM o GROUP BY 1"), sp + (cap,)):
            if d in pos:
                hours[pos[d]] += float(secs) / 3600.0
        for d, with_rec, med, total_rec, total_dur in await _rows(cur, scope + (
                " SELECT (s.started_at AT TIME ZONE 'UTC')::date, count(*) FILTER (WHERE l.recoveries > 0), "
                "percentile_cont(0.5) WITHIN GROUP (ORDER BY l.recovery_s) FILTER (WHERE l.recoveries > 0), "
                "coalesce(sum(l.recovery_s), 0), coalesce(sum(l.duration_s), 0) "
                "FROM run_legs l JOIN scope s ON s.run_id = l.run_id GROUP BY 1"), sp):
            if d in pos:
                i = pos[d]
                rec_legs[i] = int(with_rec)
                rec_median[i] = _f(med, 2)
                rec_share[i] = ax.ratio(float(total_rec), float(total_dur))
        for rid, d in await _rows(cur, scope + f" SELECT run_id, {_DAY} FROM scope", sp):
            if d in pos:
                run_day[rid] = pos[d]
        episodes = await _safety_days(cur, map_id, start, end, robot, pos, n)
    per_run = await _efficiency(db, map_id, start, end, robot)
    planned, driven = [0.0] * n, [0.0] * n
    for rid, (pl, dr, _legs) in per_run.items():
        i = run_day.get(rid)
        if i is not None:
            planned[i] += pl
            driven[i] += dr
    eff = [efficiency_point(planned[i], driven[i]) for i in range(n)]
    fail = [outcomes["failed"][i] + outcomes["timeout"][i] + outcomes["aborted"][i] for i in range(n)]
    return {
        "bucket": "day", "t": ax.iso_axis(axis), "outcomes": outcomes,
        "success_rate": [ax.ratio(outcomes["completed"][i], outcomes["completed"][i] + fail[i]) for i in range(n)],
        "interventions": [max(outcomes["canceled"][i] - takeovers[i], 0) + teleop[i] + takeovers[i]
                          for i in range(n)],
        "hours": [round(h, 3) for h in hours],
        "duration_median_s": median,
        "recovery": {"legs_with_recovery": rec_legs, "median_s": rec_median, "share_of_time": rec_share},
        "efficiency": {"ratio": [e[0] for e in eff], "extra_pct": [e[1] for e in eff]},
        "safety": {"episodes": episodes},
    }


async def _run_stats_runs(db: Any, map_id: str, start, end, robot) -> Dict[str, Any]:
    scope = _scope(robot)
    sp: Tuple[Any, ...] = (map_id, start, end) + ((robot,) if robot else ())
    async with fleet_reads.read_cursor(db) as cur:
        runs = await _rows(cur, scope + (
            " SELECT run_id, state, started_at, ended_at FROM scope WHERE state <> 'RUNNING' "
            "ORDER BY started_at DESC, run_id LIMIT %s"), sp + (RUN_POINTS,))
        ids = [r[0] for r in runs]
        rec = dict(await _rows(cur, "SELECT run_id, coalesce(sum(recovery_s), 0) FROM run_legs "
                                    "WHERE run_id = ANY(%s) GROUP BY run_id", (ids,))) if ids else {}
        flagged = {r[0] for r in await _rows(cur, (
            "SELECT r.run_id FROM mission_runs r WHERE r.run_id = ANY(%s) AND (r.state = 'CANCELED' OR EXISTS "
            "(SELECT 1 FROM fleet_events e WHERE e.code = %s AND e.robot_name = r.robot_name "
            "AND e.ts >= r.started_at AND e.ts <= r.ended_at))"),
            (ids, EventCode.TELEOP_SESSION_STARTED.value))} if ids else set()
    per_run = await _efficiency(db, map_id, start, end, robot) if ids else {}
    runs = sorted(runs, key=lambda r: (r[2], str(r[0])))        # oldest first: a chart reads left to right
    out: Dict[str, Any] = {"bucket": "run", "t": [], "run_id": [], "outcome": [], "duration_s": [],
                           "recovery_s": [], "efficiency_ratio": [], "intervention": []}
    for rid, state, s, e in runs:
        pl, dr, _ = per_run.get(rid, (0.0, 0.0, 0))
        out["t"].append(fleet_reads.iso(s))
        out["run_id"].append(str(rid))
        out["outcome"].append(str(state).lower())
        out["duration_s"].append(None if e is None else round((e - s).total_seconds(), 1))
        out["recovery_s"].append(_f(rec.get(rid, 0), 2))
        out["efficiency_ratio"].append(efficiency_point(pl, dr)[0])
        out["intervention"].append(rid in flagged)
    return out


async def attach_run_stats(out: Dict[str, Any], db: Any, map_id: str, start: Optional[datetime.datetime],
                           end: Optional[datetime.datetime], robot: Optional[str], series: str,
                           cache: Optional[ResultCache] = None) -> Dict[str, Any]:
    """Add `series` to a run-stats answer (over the window of that answer). A 422 (window too long for
    series=day) is raised; any other failure leaves `series` null with a note, run-stats itself still answers."""
    cache = CACHE if cache is None else cache
    win = out.get("window") or {}
    s = datetime.datetime.fromisoformat(win["from"]) if win.get("from") else start
    e = datetime.datetime.fromisoformat(win["to"]) if win.get("to") else end
    fn = _run_stats_day if series == "day" else _run_stats_runs

    async def run() -> Dict[str, Any]:
        return await fn(db, map_id, s, e, robot)
    try:
        out["series"] = await cache.get((map_id, "run-stats", start, end, robot or None, series), run)
    except Exception as exc:  # noqa: BLE001
        if getattr(exc, "status_code", None) == 422:
            raise
        logger.warning("run-stats series of %s unavailable: %s", map_id, exc)
        out["series"] = None
        out.setdefault("data_quality", {}).setdefault("notes", []).append(
            "series unavailable: the computation failed (see the API log)")
    return out


# --- events-summary series -----------------------------------------------------------------------

async def events_series(db: Any, map_id: str, start: datetime.datetime, end: datetime.datetime,
                        robot: Optional[str]) -> Dict[str, Any]:
    axis = ax.day_axis(start, end)
    n = len(axis)
    pos = ax.index_of(axis)
    async with fleet_reads.read_cursor(db) as cur:
        rows = await map_events.daily_counts(cur, map_id, start, end, robot, map_events.SUMMARY_CODES)
    total, safety = ax.zeros(n), ax.zeros(n)
    per_code: Dict[str, List[int]] = {}
    for d, code, ekey, count in rows:
        et = ekey or None
        if d not in pos or map_events.event_class(code, et) is None:
            continue
        i, c = pos[d], int(count)
        total[i] += c
        if map_events.classify_event(code, et) is not None:
            safety[i] += c
        per_code.setdefault(map_events.event_key(code, et), ax.zeros(n))[i] += c
    top = sorted(per_code, key=lambda k: (-sum(per_code[k]), k))[:TOP_CODES]
    return {"bucket": "day", "t": ax.iso_axis(axis), "total": total, "safety": safety,
            "by_code": {k: per_code[k] for k in top}}


async def attach_events(out: Dict[str, Any], db: Any, map_id: str, start: Optional[datetime.datetime],
                        end: Optional[datetime.datetime], robot: Optional[str],
                        cache: Optional[ResultCache] = None) -> Dict[str, Any]:
    """`out` (a cached events-summary answer, not mutated) plus `series` over the window of that answer."""
    cache = CACHE if cache is None else cache
    win = out["window"]
    s, e = datetime.datetime.fromisoformat(win["from"]), datetime.datetime.fromisoformat(win["to"])

    async def run() -> Dict[str, Any]:
        return await events_series(db, map_id, s, e, robot)
    return {**out, "series": await cache.get((map_id, "events-summary", start, end, robot or None, "day"), run)}
