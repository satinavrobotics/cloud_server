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
  duration_s      median and P95 of ended_at - started_at over COMPLETED runs; `timeout` is
                  reported separately (count, median)
  recovery        over the legs (run_legs) of the scope's runs: median / P95 of recovery_s over
                  legs with recoveries > 0; share_of_time = sum(recovery_s) / sum(duration_s)
  safety          events attributed to the map by run_id, or (run-less events) by robot and time
                  overlap with a run of the scope, classified by SAFETY_CLASSES. Nothing about
                  collisions or geofences is derived: only these recorded events are counted.
"""

import datetime
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException

from packages import config
from packages.api import fleet_reads
from packages.events.codes import EventCode

UTC = datetime.timezone.utc

FAILURE_STATES: Tuple[str, ...] = ("FAILED", "TIMEOUT", "ABORTED")
TAKEOVER_ERROR = "operatorTakeover"
CLASSES: Tuple[str, ...] = ("safety_reflex", "blocked", "skipped", "link_loss")

# (event code, payload error_type or None for any, class). The only place that decides what
# counts as a safety event.
SAFETY_CLASSES: Tuple[Tuple[str, Optional[str], str], ...] = (
    (EventCode.ROBOT_ERROR_RAISED.value, "safetyReflexError", "safety_reflex"),
    (EventCode.MISSION_EDGE_BLOCKED.value, None, "blocked"),
    (EventCode.NAV_GOAL_BLOCKED.value, None, "blocked"),
    (EventCode.MISSION_NODE_SKIPPED.value, None, "skipped"),
    (EventCode.ROBOT_HEARTBEAT_LOST.value, None, "link_loss"),
)
_TYPED_CODES = {code for code, et, _ in SAFETY_CLASSES if et is not None}


def classify_event(code: str, error_type: Optional[str]) -> Optional[str]:
    for c, et, cls in SAFETY_CLASSES:
        if c == code and (et is None or et == error_type):
            return cls
    return None


def _num(value: Any, digits: int = 3) -> Optional[float]:
    return None if value is None else round(float(value), digits)


def _ratio(num: float, den: float) -> Optional[float]:
    return round(num / den, 4) if den else None


def resolve_window(start: Optional[datetime.datetime], end: Optional[datetime.datetime]
                   ) -> Tuple[datetime.datetime, datetime.datetime]:
    end = end or datetime.datetime.now(UTC)
    start = start or end - datetime.timedelta(days=config.RUN_STATS_DEFAULT_DAYS)
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

    typed = sorted(_TYPED_CODES)
    safety_codes = sorted({c for c, _, _ in SAFETY_CLASSES})
    safety_types = sorted({et for _, et, _ in SAFETY_CLASSES if et})
    safety_filter = ("e.code = ANY(%s) AND (NOT (e.code = ANY(%s)) "
                     "OR e.payload->>'error_type' = ANY(%s)) AND e.ts >= %s AND e.ts < %s")
    safety_params = (safety_codes, typed, safety_types, start, ev_end)

    async with fleet_reads.read_cursor(db) as cur:
        by_state = dict(await rows(cur, scope + " SELECT state, count(*) FROM scope "
                                   "GROUP BY state", sp))
        (dur,) = await rows(cur, scope + (
            ", d AS (SELECT state, extract(epoch FROM ended_at - started_at) AS secs "
            "FROM scope WHERE ended_at IS NOT NULL) SELECT "
            "percentile_cont(0.5) WITHIN GROUP (ORDER BY secs) FILTER (WHERE state = 'COMPLETED'), "
            "percentile_cont(0.95) WITHIN GROUP (ORDER BY secs) FILTER (WHERE state = 'COMPLETED'), "
            "percentile_cont(0.5) WITHIN GROUP (ORDER BY secs) FILTER (WHERE state = 'TIMEOUT'), "
            "count(*) FILTER (WHERE secs > %s) FROM d"), sp + (cap,))
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
        safety_rows = await rows(cur, scope + (
            " SELECT code, error_type, count(*) FROM ("
            "SELECT e.code, e.payload->>'error_type' AS error_type FROM fleet_events e "
            "JOIN scope s ON e.run_id = s.run_id WHERE " + safety_filter
            + " UNION ALL "
            "SELECT e.code, e.payload->>'error_type' FROM fleet_events e JOIN scope s "
            "ON e.run_id IS NULL AND e.robot_name = s.robot_name AND e.ts >= s.started_at "
            "AND e.ts <= coalesce(s.ended_at, now()) WHERE " + safety_filter
            + ") x GROUP BY code, error_type"), sp + safety_params + safety_params)
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

    by_class = {c: 0 for c in CLASSES}
    by_code: Dict[str, int] = {}
    for code, error_type, count in safety_rows:
        cls = classify_event(code, error_type)
        if cls is None:
            continue
        key = f"{code}:{error_type}" if code in _TYPED_CODES else code
        by_class[cls] += int(count)
        by_code[key] = by_code.get(key, 0) + int(count)

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


def require_map_id(map_id: str) -> str:
    if not map_id or "\x00" in map_id:
        raise HTTPException(status_code=422, detail="map_id must be a non-empty map name")
    return map_id
