"""Operating metrics of one map: GET /api/v1/maps/{map_id}/run-stats (docs/map_run_stats_2026-10-14.md).

Everything is aggregated in SQL (a handful of grouped queries in ONE read-only transaction, see
fleet_reads.read_cursor); no per-row Python loop over a big table. The scope is the non-archived
`mission_runs` of the map (`map_id` = the map of the robot's open session at the run's start)
that STARTED in [from, to), optionally of one robot.

Definitions (all counts are runs unless stated):
  finished        runs that are not RUNNING
  success_rate    COMPLETED / (COMPLETED + FAILED + TIMEOUT + ABORTED). A cancel is not a failure.
  abort_rate      (FAILED + TIMEOUT + ABORTED) / finished
  interventions   cancels + teleop + takeovers
    takeovers       CANCELED runs that carry a ROBOT.ERROR_RAISED `operatorTakeover` event
    cancels         the other CANCELED runs (state-based: it does not depend on the
                    MISSION.CANCEL_REQUESTED event having been stored)
    teleop          TELEOP.SESSION_STARTED events of the map (payload.map_id) in the window
  free_success_rate  COMPLETED runs without a teleop session during the run / finished
  hours           operating time: the union of the intervals [started_at, ended_at] of the
                  finished runs, per robot (overlaps counted once), in hours; runs longer than
                  config.RUN_STATS_MAX_RUN_S (default 2 h: a hung run, not operating time) are
                  left out (their number is `data_quality.hours_excluded_runs`)
  rate_per_hour   interventions / hours (null without hours)
  duration_s      median and P95 of ended_at - started_at over COMPLETED runs, without the hung runs
                  over config.RUN_STATS_MAX_RUN_S (as `hours`; `n` still counts them, their number is
                  `data_quality.hours_excluded_runs`); `timeout` is reported separately (count, median)
  recovery        over the legs (run_legs) of the scope's runs: median / P95 of recovery_s over
                  legs with recoveries > 0; share_of_time = sum(recovery_s) / sum(duration_s)
  safety          safety EPISODES attributed to the map (map_events.py: the event's run, else the
                  robot's map session open at that time) with ts in [from, to) (by event time, not
                  by run start), classified by SAFETY_CLASSES; a repeated ROBOT.ERROR_RAISED
                  counts once until cleared. Nothing about collisions or geofences is derived:
                  only these recorded events are counted.
"""

import copy
import datetime
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException

from packages import config
from packages.api import fleet_reads, map_events
from packages.api.result_cache import ResultCache
from packages.events.codes import EventCode

UTC = datetime.timezone.utc

FAILURE_STATES: Tuple[str, ...] = ("FAILED", "TIMEOUT", "ABORTED")
TAKEOVER_ERROR = "operatorTakeover"
# Safety classes, attribution and episode counting live in map_events.py (one helper for run-stats
# and the events endpoints); re-exported here.
CLASSES = map_events.CLASSES
SAFETY_CLASSES = map_events.SAFETY_CLASSES
classify_event = map_events.classify_event


def _num(value: Any, digits: int = 3) -> Optional[float]:
    return None if value is None else round(float(value), digits)


def _ratio(num: float, den: float) -> Optional[float]:
    return round(num / den, 4) if den else None


def resolve_window(start: Optional[datetime.datetime], end: Optional[datetime.datetime]
                   ) -> Tuple[datetime.datetime, datetime.datetime]:
    start, end = fleet_reads.default_window(start, end, config.RUN_STATS_DEFAULT_DAYS)
    fleet_reads.check_window(start, end)
    return start, end


_SCOPE = ("scope AS (SELECT run_id, robot_name, state, abort_cause, started_at, ended_at "
          "FROM mission_runs WHERE map_id = %s AND started_at >= %s AND started_at < %s "
          "AND archived_at IS NULL{robot})")


async def run_stats(db: Any, map_id: str, start: Optional[datetime.datetime] = None,
                    end: Optional[datetime.datetime] = None,
                    robot: Optional[str] = None) -> Dict[str, Any]:
    start, end = resolve_window(start, end)
    robot_sql = " AND robot_name = %s" if robot else ""
    scope = "WITH " + _SCOPE.format(robot=robot_sql)
    sp: Tuple[Any, ...] = (map_id, start, end) + ((robot,) if robot else ())
    ev_end = end + datetime.timedelta(days=1)   # events of runs that began before `end`
    cap = config.RUN_STATS_MAX_RUN_S

    async def rows(cur, sql, params):
        await cur.execute(sql, params)
        return await cur.fetchall()

    async with fleet_reads.read_cursor(db) as cur:
        by_state = dict(await rows(cur, scope + " SELECT state, count(*) FROM scope "
                                   "GROUP BY state", sp))
        (dur,) = await rows(cur, scope + (
            ", d AS (SELECT state, extract(epoch FROM ended_at - started_at) AS secs "
            "FROM scope WHERE ended_at IS NOT NULL) SELECT "
            "percentile_cont(0.5) WITHIN GROUP (ORDER BY secs) FILTER (WHERE state = 'COMPLETED' AND secs <= %s), "
            "percentile_cont(0.95) WITHIN GROUP (ORDER BY secs) FILTER (WHERE state = 'COMPLETED' AND secs <= %s), "
            "percentile_cont(0.5) WITHIN GROUP (ORDER BY secs) FILTER (WHERE state = 'TIMEOUT'), "
            "count(*) FILTER (WHERE secs > %s) FROM d"), sp + (cap, cap, cap))
        causes = await rows(cur, scope + (
            " SELECT coalesce(abort_cause, 'UNKNOWN'), count(*) FROM scope "
            "WHERE state NOT IN ('RUNNING', 'COMPLETED') GROUP BY 1 ORDER BY 2 DESC, 1"), sp)
        ((hours_s,),) = await rows(cur, scope + (
            ", f AS (SELECT robot_name, started_at AS s, ended_at AS e FROM scope "
            "WHERE ended_at IS NOT NULL AND extract(epoch FROM ended_at - started_at) <= %s), "
            "o AS (SELECT s, e, max(e) OVER (PARTITION BY robot_name ORDER BY s, e "
            "ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS pe FROM f) "
            "SELECT coalesce(sum(greatest(0, extract(epoch FROM e - greatest(s, coalesce(pe, s))))), "
            "0) FROM o"), sp + (cap,))
        ((takeovers,),) = await rows(cur, scope + (
            " SELECT count(*) FROM scope s WHERE s.state = 'CANCELED' AND EXISTS (SELECT 1 FROM "
            "fleet_events e WHERE e.run_id = s.run_id AND e.code = %s AND e.ts >= %s AND e.ts < %s "
            "AND e.payload->>'error_type' = %s)"),
            sp + (EventCode.ROBOT_ERROR_RAISED.value, start, ev_end, TAKEOVER_ERROR))
        teleop_code = EventCode.TELEOP_SESSION_STARTED.value
        ((teleop,),) = await rows(cur, (
            "SELECT count(*) FROM fleet_events WHERE code = %s AND ts >= %s AND ts < %s "
            "AND payload->>'map_id' = %s" + (" AND robot_name = %s" if robot else "")),
            (teleop_code, start, end, map_id) + ((robot,) if robot else ()))
        ((first_teleop,),) = await rows(cur, "SELECT min(ts) FROM fleet_events WHERE code = %s",
                                        (teleop_code,))
        ((free_runs,),) = await rows(cur, scope + (
            " SELECT count(*) FROM scope s WHERE s.state = 'COMPLETED' AND NOT EXISTS "
            "(SELECT 1 FROM fleet_events e WHERE e.code = %s AND e.robot_name = s.robot_name "
            "AND e.ts >= s.started_at AND e.ts <= s.ended_at AND e.ts >= %s AND e.ts < %s)"),
            sp + (teleop_code, start, ev_end))
        (rec,) = await rows(cur, scope + (
            " SELECT count(*), count(*) FILTER (WHERE l.recoveries > 0), "
            "percentile_cont(0.5) WITHIN GROUP (ORDER BY l.recovery_s) FILTER (WHERE l.recoveries > 0), "
            "percentile_cont(0.95) WITHIN GROUP (ORDER BY l.recovery_s) FILTER (WHERE l.recoveries > 0), "
            "coalesce(sum(l.recovery_s), 0), coalesce(sum(l.duration_s), 0) "
            "FROM run_legs l JOIN scope s ON s.run_id = l.run_id"), sp)
        by_class, by_code = await map_events.safety_counts(cur, map_id, start, end, robot)
        ((no_map,),) = await rows(cur, (
            "SELECT count(*) FROM mission_runs WHERE map_id IS NULL AND started_at >= %s "
            "AND started_at < %s AND archived_at IS NULL" + (" AND robot_name = %s" if robot else "")),
            (start, end) + ((robot,) if robot else ()))

    n = {s: int(by_state.get(s, 0)) for s in
         ("COMPLETED", "FAILED", "TIMEOUT", "ABORTED", "CANCELED", "RUNNING")}
    failures = sum(n[s] for s in FAILURE_STATES)
    finished = sum(v for s, v in n.items() if s != "RUNNING")
    hours = float(hours_s) / 3600.0
    takeovers = int(takeovers)
    cancels = max(n["CANCELED"] - takeovers, 0)
    teleop = int(teleop)
    interventions = cancels + teleop + takeovers

    recorded = first_teleop is not None and first_teleop <= start
    notes: List[str] = []
    if not recorded:
        notes.append("TELEOP.SESSION_STARTED is recorded only from "
                     + (first_teleop.astimezone(UTC).isoformat() if first_teleop
                        else "a later release (none stored yet)")
                     + ": teleop interventions before that are not in `intervention.teleop`")
    notes.append("teleop sessions are counted when a robot joins the operator's video room "
                 "(its token request), an upper bound of actual driving")
    if no_map:
        notes.append(f"{no_map} run(s) in the window have no map and belong to no map's stats")

    return {
        "map_id": map_id,
        "window": {"from": start.astimezone(UTC).isoformat(),
                   "to": end.astimezone(UTC).isoformat()},
        "robot": robot,
        "runs": {"finished": finished, "completed": n["COMPLETED"], "failed": n["FAILED"],
                 "timeout": n["TIMEOUT"], "aborted": n["ABORTED"], "canceled": n["CANCELED"],
                 "running": n["RUNNING"]},
        "success_rate": _ratio(n["COMPLETED"], n["COMPLETED"] + failures),
        "intervention": {"count": interventions, "cancels": cancels, "teleop": teleop,
                         "takeovers": takeovers,
                         "rate_per_hour": _ratio(interventions, hours),
                         "free_success_rate": _ratio(int(free_runs), finished),
                         "free_runs": int(free_runs)},
        "hours": round(hours, 3),
        "duration_s": {"median": _num(dur[0], 1), "p95": _num(dur[1], 1), "n": n["COMPLETED"]},
        "timeout": {"count": n["TIMEOUT"], "median_s": _num(dur[2], 1)},
        "abort_rate": _ratio(failures, finished),
        "recovery": {"legs": int(rec[0]), "legs_with_recovery": int(rec[1]),
                     "median_s": _num(rec[2], 2), "p95_s": _num(rec[3], 2),
                     "total_s": _num(rec[4], 1), "share_of_time": _ratio(float(rec[4]), float(rec[5])),
                     "share_of_legs": _ratio(int(rec[1]), int(rec[0]))},
        "safety": {"by_class": by_class, "by_code": dict(sorted(by_code.items())),
                   "total": sum(by_class.values())},
        "by_cause": {cause: int(count) for cause, count in causes},
        "data_quality": {"runs_without_map": int(no_map), "teleop_recorded": recorded,
                         "hours_excluded_runs": int(dur[3] or 0),
                         "max_run_s": cap, "notes": notes},
    }


# The headline is cached with the same TTL as its `efficiency` and `series` blocks (path_efficiency, map_series),
# so the three agree to within one TTL. Keys start with the map id (ResultCache.discard_map).
CACHE = ResultCache(config.EFFICIENCY_CACHE_ENTRIES, config.EFFICIENCY_CACHE_TTL_S)


async def run_stats_cached(db: Any, map_id: str, start: Optional[datetime.datetime] = None,
                           end: Optional[datetime.datetime] = None, robot: Optional[str] = None,
                           cache: Optional[ResultCache] = None) -> Dict[str, Any]:
    """`run_stats` through the cache (key: the REQUEST window, as the other blocks). Returns a private copy: the
    caller adds `efficiency` / `series` to it."""
    cache = CACHE if cache is None else cache
    out = await cache.get((map_id, "run-stats", start, end, robot or None),
                          lambda: run_stats(db, map_id, start, end, robot))
    return copy.deepcopy(out)


def require_map_id(map_id: str) -> str:
    if not map_id or "\x00" in map_id:
        raise HTTPException(status_code=422, detail="map_id must be a non-empty map name")
    return map_id
