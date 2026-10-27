"""Events of one map: GET /api/v1/maps/{map_id}/events-summary and /events, and the safety counts of
run-stats (docs/map_events_2026-10-14.md).

ONE shared helper decides which events belong to a map and how many there are:
`_attributed_cte`. run-stats (`safety_counts`) and both endpoints use it, so their numbers agree.

Attribution of an event to a map (first match wins):
  1. run      the event's `run_id` names a non-archived mission run with a `map_id` -> that map
  2. session  the robot's `map_sessions` row (operate or mapping, paused or not) that was open at the
              event's ts: started_at <= ts and (ended_at is null or ts < ended_at) -> its map
  3. none     neither: the event belongs to no map (the robot was on no map)
Measured on 30 days of live data (2831 safety-relevant events): 631 by run, 1247 more by session,
953 none, and run and session never disagreed.

Counting (an "event" below is an *episode*): ROBOT.ERROR_RAISED counts once per raise episode of a
robot and error_type (a RAISED not preceded, within MAP_EVENTS_EPISODE_LOOKBACK_DAYS, by another
RAISED of the same robot and error_type without an ERROR_CLEARED in between); NAV.RECOVERY_ENTERED
likewise against NAV.RECOVERY_EXITED (the pair is robot-wide: one recovery at a time). Every other
code counts one per event. ROBOT.ERROR_RAISED of error_type edgeBlocked / nodeSkipped are left out:
they mirror MISSION.EDGE_BLOCKED / MISSION.NODE_SKIPPED, which are counted instead.
"""

import datetime
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple


from packages import config
from packages.api import fleet_reads
from packages.api.result_cache import ResultCache
from packages.events.codes import EventCode

UTC = datetime.timezone.utc

RAISED = EventCode.ROBOT_ERROR_RAISED.value
CLEARED = EventCode.ROBOT_ERROR_CLEARED.value
ENTERED = EventCode.NAV_RECOVERY_ENTERED.value
EXITED = EventCode.NAV_RECOVERY_EXITED.value
# Episode openers and the event that closes the episode.
CLOSERS = {RAISED: CLEARED, ENTERED: EXITED}

CLASSES: Tuple[str, ...] = ("safety_reflex", "blocked", "skipped", "link_loss")
ALL_CLASSES: Tuple[str, ...] = CLASSES + ("recovery", "error", "other")

# (event code, payload error_type or None for any, class). The only place that decides what
# counts as a safety event.
SAFETY_CLASSES: Tuple[Tuple[str, Optional[str], str], ...] = (
    (RAISED, "safetyReflexError", "safety_reflex"),
    (EventCode.MISSION_EDGE_BLOCKED.value, None, "blocked"),
    (EventCode.NAV_GOAL_BLOCKED.value, None, "blocked"),
    (EventCode.MISSION_NODE_SKIPPED.value, None, "skipped"),
    (EventCode.ROBOT_HEARTBEAT_LOST.value, None, "link_loss"),
)
_TYPED_CODES = {code for code, et, _ in SAFETY_CLASSES if et is not None}

# The summary universe: operational events of a map, in match order. Batteries, node up/down,
# state changes, run start/finish, map administration and the "cleared/exited/restored" halves are
# not "events of the map" (data_quality.notes says so).
CLASS_RULES: Tuple[Tuple[str, Optional[str], str], ...] = SAFETY_CLASSES + (
    (ENTERED, None, "recovery"),
    (RAISED, None, "error"),
    (EventCode.MISSION_NODE_FAILED.value, None, "error"),
    (EventCode.ROBOT_OFFLINE.value, None, "other"),
    (EventCode.MISSION_REROUTED.value, None, "other"),
    (EventCode.MISSION_CANCEL_REQUESTED.value, None, "other"),
    (EventCode.TELEOP_SESSION_STARTED.value, None, "other"),
)
SUMMARY_CODES: Tuple[str, ...] = tuple(dict.fromkeys(c for c, _, _ in CLASS_RULES))
MIRRORED_ERROR_TYPES: Tuple[str, ...] = ("edgeBlocked", "nodeSkipped")
_SEVERITY_RANK = {"debug": 0, "info": 1, "warning": 2, "error": 3, "critical": 4}
DETAIL_MAX = 200


def classify_event(code: str, error_type: Optional[str]) -> Optional[str]:
    """Safety class of an event (None = not a safety event)."""
    for c, et, cls in SAFETY_CLASSES:
        if c == code and (et is None or et == error_type):
            return cls
    return None


def event_class(code: str, error_type: Optional[str]) -> Optional[str]:
    """Class of a summary-universe event (None = not in the universe)."""
    if code == RAISED and error_type in MIRRORED_ERROR_TYPES:
        return None
    for c, et, cls in CLASS_RULES:
        if c == code and (et is None or et == error_type):
            return cls
    return None


def event_key(code: str, error_type: Optional[str]) -> str:
    return f"{code}:{error_type}" if code == RAISED and error_type else code


def _class_sql(cls: str) -> Tuple[str, List[Any]]:
    """SQL (over the columns `code`, `ekey` of the attributed CTE) true for the events of `cls`,
    mirroring event_class (first-match rules)."""
    parts: List[str] = []
    params: List[Any] = []
    earlier: Dict[str, List[str]] = {}
    for code, et, c in CLASS_RULES:
        if c == cls:
            if et is not None:
                parts.append("(code = %s AND ekey = %s)")
                params += [code, et]
            elif earlier.get(code):
                parts.append("(code = %s AND NOT (ekey = ANY(%s)))")
                params += [code, earlier[code]]
            else:
                parts.append("code = %s")
                params.append(code)
        if et is not None:
            earlier.setdefault(code, []).append(et)
    return "(" + " OR ".join(parts) + ")", params


# --- the shared attribution + episode helper -----------------------------------------------------

def _attributed_cte(map_id: str, start: datetime.datetime, end: datetime.datetime,
                    robot: Optional[str], codes: Sequence[str]) -> Tuple[str, List[Any]]:
    """`WITH ... att AS (...)`: the episodes of `codes` in [start, end) of the robots that were on
    `map_id` in the window (a superset prefilter; the exact test is `att.map = map_id`), each with
    its attributed `map`, `via` (run | session | none), `session_id` and `run_mission`.

    Uses fleet_events_code_ts_idx (code, ts), mission_runs_pkey and map_sessions_one_open_per_robot /
    map_sessions_map_idx; no new index is needed (the window's rows are a few thousand)."""
    wanted = sorted(set(codes))
    fetched = sorted(set(wanted) | {CLOSERS[c] for c in wanted if c in CLOSERS})
    lookback = start - datetime.timedelta(days=config.MAP_EVENTS_EPISODE_LOOKBACK_DAYS)
    params: List[Any] = [fetched, lookback, end]
    robot_sql = ""
    if robot:
        robot_sql = " AND e.robot_name = %s"
        params.append(robot)
    params += [map_id, end, start, map_id, end, start, start, wanted, list(MIRRORED_ERROR_TYPES)]
    sql = (
        "WITH raw AS (SELECT e.event_id, e.ts, e.robot_name, e.run_id, e.code, e.severity, e.payload, "
        f"CASE WHEN e.code IN ('{RAISED}', '{CLEARED}') THEN 'E' "
        f"WHEN e.code IN ('{ENTERED}', '{EXITED}') THEN 'R' ELSE 'S' END AS fam, "
        f"CASE WHEN e.code IN ('{RAISED}', '{CLEARED}') "
        "THEN coalesce(e.payload->>'error_type', '') ELSE '' END AS ekey "
        "FROM fleet_events e WHERE e.code = ANY(%s) AND e.ts >= %s AND e.ts < %s" + robot_sql + " "
        "AND e.robot_name IN (SELECT robot_name FROM map_sessions WHERE map_name = %s "
        "AND started_at < %s AND coalesce(ended_at, 'infinity') > %s "
        "UNION SELECT robot_name FROM mission_runs WHERE map_id = %s AND started_at < %s "
        "AND coalesce(ended_at, now()) >= %s)), "
        "base AS (SELECT raw.*, lag(code) OVER (PARTITION BY robot_name, fam, ekey "
        "ORDER BY ts, event_id) AS prev FROM raw), "
        "ep AS (SELECT * FROM base WHERE ts >= %s AND code = ANY(%s) "
        f"AND (code NOT IN ('{RAISED}', '{ENTERED}') OR prev IS DISTINCT FROM code) "
        f"AND NOT (code = '{RAISED}' AND ekey = ANY(%s))), "
        "att AS (SELECT ep.*, r.mission_name AS run_mission, s.session_id, "
        "coalesce(r.map_id, s.map_name) AS map, "
        "CASE WHEN r.map_id IS NOT NULL THEN 'run' WHEN s.map_name IS NOT NULL THEN 'session' "
        "ELSE 'none' END AS via "
        "FROM ep LEFT JOIN mission_runs r ON r.run_id = ep.run_id AND r.archived_at IS NULL "
        "LEFT JOIN LATERAL (SELECT x.session_id, x.map_name FROM map_sessions x "
        "WHERE x.robot_name = ep.robot_name AND x.started_at <= ep.ts "
        "AND (x.ended_at IS NULL OR ep.ts < x.ended_at) ORDER BY x.started_at DESC LIMIT 1) s ON true) ")
    return sql, params


async def _all(cur: Any, sql: str, params: Sequence[Any]) -> List[Sequence[Any]]:
    await cur.execute(sql, params)
    return await cur.fetchall()


_GROUP_SQL = ("SELECT code, ekey, severity, count(*), max(ts), count(DISTINCT run_id), "
              "array_agg(DISTINCT robot_name) FROM att WHERE map = %s GROUP BY code, ekey, severity")


async def grouped(cur: Any, map_id: str, start: datetime.datetime, end: datetime.datetime,
                  robot: Optional[str], codes: Sequence[str]) -> List[Sequence[Any]]:
    """Episodes of the map in [start, end) grouped by (code, error_type, severity):
    rows (code, error_type, severity, n, last_ts, runs, robots)."""
    cte, params = _attributed_cte(map_id, start, end, robot, codes)
    return await _all(cur, cte + _GROUP_SQL, params + [map_id])


async def daily_counts(cur: Any, map_id: str, start: datetime.datetime, end: datetime.datetime,
                       robot: Optional[str], codes: Sequence[str]) -> List[Sequence[Any]]:
    """Episodes of the map in [start, end) per UTC day: rows (date, code, error_type, n). One query."""
    cte, params = _attributed_cte(map_id, start, end, robot, codes)
    return await _all(cur, cte + "SELECT (ts AT TIME ZONE 'UTC')::date, code, ekey, count(*) FROM att "
                      "WHERE map = %s GROUP BY 1, 2, 3", params + [map_id])


async def attribution(cur: Any, map_id: str, start: datetime.datetime, end: datetime.datetime,
                      robot: Optional[str], codes: Sequence[str]) -> Dict[str, int]:
    """How the map's episodes were attributed, plus `unattributed`: episodes of the robots that
    were on the map in the window which fall in no run and no session."""
    cte, params = _attributed_cte(map_id, start, end, robot, codes)
    rows = await _all(cur, cte + "SELECT via, count(*) FROM att WHERE map = %s OR via = 'none' "
                      "GROUP BY via", params + [map_id])
    got = {via: int(n) for via, n in rows}
    return {"by_run": got.get("run", 0), "by_session": got.get("session", 0),
            "unattributed": got.get("none", 0)}


async def safety_counts(cur: Any, map_id: str, start: datetime.datetime, end: datetime.datetime,
                        robot: Optional[str]) -> Tuple[Dict[str, int], Dict[str, int]]:
    """(by_class, by_code) of the safety episodes of the map (run-stats' `safety`)."""
    codes = sorted({c for c, _, _ in SAFETY_CLASSES})
    by_class = {c: 0 for c in CLASSES}
    by_code: Dict[str, int] = {}
    for code, error_type, _sev, n, *_ in await grouped(cur, map_id, start, end, robot, codes):
        cls = classify_event(code, error_type or None)
        if cls is None:
            continue
        by_class[cls] += int(n)
        key = event_key(code, error_type) if code in _TYPED_CODES else code
        by_code[key] = by_code.get(key, 0) + int(n)
    return by_class, by_code


# --- detail text ---------------------------------------------------------------------------------

_SPACE = re.compile(r"\s+")


def event_detail(code: str, payload: Any) -> Optional[str]:
    """A short human string from the payload (the failure reason or trigger text). Only these
    descriptive keys are read; nothing else of the payload leaves the server."""
    if not isinstance(payload, dict):
        return None
    if code == ENTERED:
        text = payload.get("cause")
        text = f"recovery: {text}" if text else None
    elif code == EventCode.ROBOT_HEARTBEAT_LOST.value:
        text = f"no heartbeat for {payload['timeout_s']:g} s" if isinstance(
            payload.get("timeout_s"), (int, float)) else "no heartbeat"
    else:
        text = None
        for key in ("detail", "description", "cause", "reason", "block_reason"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                text = value
                break
    if not text:
        return None
    text = _SPACE.sub(" ", str(text)).strip()
    return text if len(text) <= DETAIL_MAX else text[:DETAIL_MAX - 1] + "…"


# --- request validation --------------------------------------------------------------------------

def resolve_window(start: Optional[datetime.datetime], end: Optional[datetime.datetime]
                   ) -> Tuple[datetime.datetime, datetime.datetime]:
    end = end or datetime.datetime.now(UTC)
    start = start or end - datetime.timedelta(days=config.RUN_STATS_DEFAULT_DAYS)
    fleet_reads.check_window(start, end)
    return start, end


def _limit(value: Optional[int], default: int, maximum: int) -> int:
    if value is None:
        return default
    if not 1 <= value <= maximum:
        raise fleet_reads._invalid("limit", f"limit must be between 1 and {maximum}")
    return int(value)


def split_code(value: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """`code` filter: `NAV.GOAL_BLOCKED` or `ROBOT.ERROR_RAISED[:error_type]`."""
    if value is None:
        return None, None
    code, _, error_type = value.partition(":")
    if code not in SUMMARY_CODES or (error_type and code != RAISED):
        raise fleet_reads._invalid("code", f"unknown code {value!r}; permitted: "
                                           f"{', '.join(SUMMARY_CODES)} (ROBOT.ERROR_RAISED may "
                                           "carry :<error_type>)", "type_error.enum")
    return code, error_type or None


# --- cache ---------------------------------------------------------------------------------------

CACHE = ResultCache(config.MAP_EVENTS_CACHE_ENTRIES, config.MAP_EVENTS_CACHE_TTL_S)


# --- GET /api/v1/maps/{map_id}/events-summary ----------------------------------------------------

def _top(rows: Sequence[Sequence[Any]], total: int, limit: int, universe: str) -> List[Dict[str, Any]]:
    """Merge (code, ekey, severity) rows into one entry per code[:error_type], most frequent first."""
    merged: Dict[str, Dict[str, Any]] = {}
    for code, ekey, severity, n, last_ts, runs, robots in rows:
        et = ekey or None
        cls = event_class(code, et) if universe == "all" else classify_event(code, et)
        if cls is None:
            continue
        key = event_key(code, et)
        m = merged.setdefault(key, {"code": key, "severity": severity, "class": cls, "count": 0,
                                    "last_ts": None, "runs": 0, "robots": set()})
        m["count"] += int(n)
        m["runs"] += int(runs or 0)
        m["robots"].update(r for r in (robots or []) if r)
        if _SEVERITY_RANK.get(severity, 0) > _SEVERITY_RANK.get(m["severity"], 0):
            m["severity"] = severity
        if last_ts is not None and (m["last_ts"] is None or last_ts > m["last_ts"]):
            m["last_ts"] = last_ts
    out = sorted(merged.values(), key=lambda m: (-m["count"], m["code"]))[:limit]
    return [{"code": m["code"], "severity": m["severity"], "class": m["class"], "count": m["count"],
             "share": round(m["count"] / total, 4) if total else None,
             "last_ts": fleet_reads.iso(m["last_ts"]), "runs": m["runs"],
             "robots": sorted(m["robots"])} for m in out]


async def events_summary(db: Any, map_id: str, start: Optional[datetime.datetime] = None,
                         end: Optional[datetime.datetime] = None, robot: Optional[str] = None,
                         limit: Optional[int] = None, cache: Optional[ResultCache] = None
                         ) -> Dict[str, Any]:
    limit = _limit(limit, 10, 50)
    start_r, end_r = resolve_window(start, end)
    cache = CACHE if cache is None else cache

    async def run() -> Dict[str, Any]:
        async with fleet_reads.read_cursor(db) as cur:
            rows = await grouped(cur, map_id, start_r, end_r, robot, SUMMARY_CODES)
            att = await attribution(cur, map_id, start_r, end_r, robot, SUMMARY_CODES)
        by_severity: Dict[str, int] = {}
        total = 0
        safety_rows = []
        for row in rows:
            code, ekey, severity, n = row[0], row[1] or None, row[2], int(row[3])
            if event_class(code, ekey) is None:
                continue
            total += n
            by_severity[severity] = by_severity.get(severity, 0) + n
            if classify_event(code, ekey) is not None:
                safety_rows.append(row)
        by_class = {c: 0 for c in CLASSES}
        for row in safety_rows:
            by_class[classify_event(row[0], row[1] or None)] += int(row[3])
        safety_total = sum(by_class.values())
        notes = [
            "episodes, not repeats: ROBOT.ERROR_RAISED counts once per raise (until its ROBOT.ERROR_CLEARED) "
            "and NAV.RECOVERY_ENTERED once per recovery (until NAV.RECOVERY_EXITED)",
            "an event belongs to the map of its run, else of the robot's map session open at that time; "
            "events of a robot on no map are in no map's counts",
            "not counted: battery, node up/down, state changes, run start/finish, map administration, "
            "and the cleared/exited/restored halves of a pair",
        ]
        if att["unattributed"]:
            notes.append(f"{att['unattributed']} event(s) of robots seen on this map in the window happened "
                         "while the robot was in no run and no map session (on no map); not counted")
        return {
            "map_id": map_id,
            "window": {"from": fleet_reads.iso(start_r), "to": fleet_reads.iso(end_r)},
            "robot": robot,
            "total_episodes": total,
            "by_severity": dict(sorted(by_severity.items())),
            "top": _top(rows, total, limit, "all"),
            "safety": {"episodes": safety_total, "by_class": by_class,
                       "top": _top(safety_rows, safety_total, limit, "safety")},
            "attribution": att,
            "data_quality": {"notes": notes},
        }
    return await cache.get(("summary", map_id, start, end, robot, limit), run)


# --- GET /api/v1/maps/{map_id}/events ------------------------------------------------------------

_ITEM_COLUMNS = ("ts", "event_id", "code", "ekey", "severity", "robot_name", "run_id", "session_id",
                 "run_mission", "payload", "via")


def _item(row: Sequence[Any]) -> Dict[str, Any]:
    r = dict(zip(_ITEM_COLUMNS, row))
    et = r["ekey"] or None
    payload = r["payload"] if isinstance(r["payload"], dict) else {}
    return {"ts": fleet_reads.iso(r["ts"]), "event_id": str(r["event_id"]), "code": r["code"],
            "error_type": et if r["code"] == RAISED else None, "severity": r["severity"],
            "class": event_class(r["code"], et), "robot": r["robot_name"],
            "run_id": str(r["run_id"]) if r["run_id"] else None,
            "session_id": str(r["session_id"]) if r["session_id"] else None,
            "mission_name": r["run_mission"] or payload.get("mission_name"),
            "detail": event_detail(r["code"], payload), "attribution": r["via"]}


async def list_events(db: Any, map_id: str, start: Optional[datetime.datetime] = None,
                      end: Optional[datetime.datetime] = None, code: Optional[str] = None,
                      cls: Optional[str] = None, robot: Optional[str] = None,
                      limit: Optional[int] = None, cursor: Optional[str] = None,
                      scope: Optional[str] = None, cache: Optional[ResultCache] = None
                      ) -> Dict[str, Any]:
    """`scope=safety` restricts to the safety classes (CLASSES); `class` filters further."""
    limit = _limit(limit, 50, 200)
    fleet_reads.check_choice(scope, "scope", ("all", "safety"))
    safety_only = scope == "safety"
    start_r, end_r = resolve_window(start, end)
    fleet_reads.check_choice(cls, "class", ALL_CLASSES)
    code_only, error_type = split_code(code)
    after = fleet_reads.decode_cursor("map_events", cursor)
    cache = CACHE if cache is None else cache
    where, wparams = ["map = %s"], [map_id]
    if code_only:
        where.append("code = %s")
        wparams.append(code_only)
        if error_type:
            where.append("ekey = %s")
            wparams.append(error_type)
    if cls:
        sql, p = _class_sql(cls)
        where.append(sql)
        wparams += p
    if after is not None:
        where.append("(ts, event_id) < (%s, %s)")
        wparams += list(after)
    if safety_only:
        parts = [_class_sql(c) for c in CLASSES]
        where.append("(" + " OR ".join(p for p, _ in parts) + ")")
        wparams += [x for _, ps in parts for x in ps]
    allowed = {c for c, _, k in CLASS_RULES if (not cls or k == cls) and (not safety_only or k in CLASSES)}
    codes = sorted(allowed & {code_only} if code_only else allowed)

    async def run() -> Dict[str, Any]:
        cte, params = _attributed_cte(map_id, start_r, end_r, robot, codes)
        sql = (cte + f"SELECT {', '.join(_ITEM_COLUMNS[:3])}, ekey, {', '.join(_ITEM_COLUMNS[4:])} "
               "FROM att WHERE " + " AND ".join(where) + " ORDER BY ts DESC, event_id DESC LIMIT %s")
        async with fleet_reads.read_cursor(db) as cur:
            rows = await _all(cur, sql, params + wparams + [limit + 1])
        more = len(rows) > limit
        rows = rows[:limit]
        nxt = fleet_reads.encode_cursor("map_events", rows[-1][0], rows[-1][1]) if more else None
        return {"items": [_item(r) for r in rows], "next": nxt}
    return await cache.get(("events", map_id, start, end, code, cls, robot, limit, cursor, scope), run)
