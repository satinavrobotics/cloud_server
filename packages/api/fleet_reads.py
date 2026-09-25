"""Read endpoints over the Phase 0 tables (docs/satinav-fleet-agent-phase0-v2.md §5.5, WP10).

    GET /api/v1/runs                        list_runs()
    GET /api/v1/runs/{run_id}               get_run()
    GET /api/v1/runs/{run_id}/timeline      run_timeline()
    GET /api/v1/events                      list_events()
    GET /api/v1/robots/{name}/recording     effective_recording()
    GET /api/v1/recording                   effective_recording_all() (every robot)

Read-only by construction: every request is ONE pooled connection in a `READ ONLY`
transaction with `statement_timeout` (config.FLEET_READ_STATEMENT_TIMEOUT_MS); a cancelled
statement is a 503, missing Phase 0 tables are a 503. Timestamps in responses are ISO-8601 UTC.

Pagination (lists): newest first, keyset on (started_at, run_id) for runs and (ts, event_id)
for events, so pages are stable while new rows arrive. `next_cursor` is an opaque token
(base64url JSON of the last row's key); pass it back unchanged with the same filters. It is
null on the last page.

Recording level history and `not_recorded` (timeline, §4.3)
-------------------------------------------------------------
The level that applied to a robot at time t is §4.2 resolved over the values *configured* at t:
robot spec, else the spec of the site the robot was assigned to at t (robot_site_assignments),
else the global settings, else the default (events_only). The configured values over time
come from TELEMETRY.RECORDING_CHANGED events: scope `robot` (robot_name = the robot), scope
`site` (robot_name NULL, site_id = the site) and scope `global`. Their payload holds effective
levels, not the configured values, but the configured old/new values are part of the event's
discriminator and so of its deterministic event_id: `decode_change()` recovers them by
recomputing the id for each candidate (packages/api/recording.py owns the format). Robot-scope
events written for a site (re)assignment are recognised the same way and skipped (the
assignment table is the history of the site layer). An event that matches no candidate (not
written by this API) is used as an approximation (its payload levels, `approximate: true`).
A layer's value at t is the `new` value of its last change at or before t, else the `old`
value of its first later change, else its current configured value.

The first segment of a run's window uses `mission_runs.recording_level` (what dispatch
resolved at the start); if the reconstruction disagrees, the segment carries
`reconstructed_level`. Changes take effect in dispatch and the API within ~1 s of the event
(NOTIFY propagation), so interval boundaries are accurate to about a second.

What each level does NOT store (§4.1), i.e. `not_recorded[].missing`:
  full         -> nothing (no interval)
  events_only  -> ["time_series"]            (robot_state_ts, diagnostics_ts and their rollups)
  off          -> ["events", "time_series"]  (every fleet_events code except
                                              TELEMETRY.RECORDING_CHANGED, incl. the run's own
                                              MISSION.RUN_STARTED/RUN_FINISHED)
Always stored, at every level: mission_runs, robot_latest, TELEMETRY.RECORDING_CHANGED, and
mission_trajectory (graph-builder; not gated by the level).
"""

import base64
import binascii
import dataclasses
import datetime
import json
import logging
import math
import re
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import psycopg
from fastapi import HTTPException
try:
    from pydantic.datetime_parse import parse_datetime
except ImportError:  # pydantic 2 in the local test env (AUDIT_BACKLOG.md C1)
    from pydantic.v1.datetime_parse import parse_datetime

from packages import config
from packages.api import recording
from packages.events import ids
from packages.events.codes import EventCode, Severity
from packages.events.schemas import RecordingLevel, RecordingScope, RunOutcome
from packages.telemetry_ingest.policy import (
    ASSIGNMENTS_TABLE, DEFAULT_LEVEL, ROBOT_TABLE, SITE_TABLE, PolicySources, load_sources,
    parse_level,
)

logger = logging.getLogger("ApiDelegationService.fleet_reads")

UTC = datetime.timezone.utc
RUN_STATES: Tuple[str, ...] = ("RUNNING",) + tuple(o.value for o in RunOutcome)
SEVERITIES: Tuple[str, ...] = tuple(s.value for s in Severity)
EVENT_CODES: Tuple[str, ...] = tuple(c.value for c in EventCode)
CODE_PREFIXES: Tuple[str, ...] = tuple(sorted({c.split(".", 1)[0] for c in EVENT_CODES}))
CHANGED = EventCode.TELEMETRY_RECORDING_CHANGED.value
DEFAULT_LIMIT, MAX_LIMIT = 50, 500

# §4.1: data kinds a level does not store.
MISSING_BY_LEVEL: Dict[str, List[str]] = {
    RecordingLevel.FULL.value: [],
    RecordingLevel.EVENTS_ONLY.value: ["time_series"],
    RecordingLevel.OFF.value: ["events", "time_series"],
}


# --- request validation ----------------------------------------------------------------------

def _invalid(loc: str, msg: str, type_: str = "value_error") -> HTTPException:
    """422 in FastAPI's request-validation shape."""
    return HTTPException(status_code=422, detail=[{"loc": ["query", loc], "msg": msg,
                                                   "type": type_}])


_ISO_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")
_SPACE_OFFSET = re.compile(r"(?<=\d)\s(\d{2}:?\d{2})$")


def parse_ts(value: Optional[str], name: str) -> Optional[datetime.datetime]:
    """ISO-8601 with a time zone (`Z` or an offset) -> aware UTC datetime; 422 otherwise."""
    if value is None:
        return None
    # An unencoded "+hh:mm" in a query string arrives as " hh:mm"; take it as meant.
    text = _SPACE_OFFSET.sub(r"+\1", value.strip())
    try:
        if not _ISO_DATETIME.match(text):
            raise ValueError(text)
        ts = parse_datetime(text)
    except (ValueError, TypeError):
        raise _invalid(name, "invalid timestamp; expected ISO-8601 with a time zone, e.g. "
                             "2026-09-24T12:00:00Z", "value_error.datetime") from None
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise _invalid(name, "timestamp must include a time zone (Z or +hh:mm)",
                       "value_error.datetime")
    return ts.astimezone(UTC)


def check_window(start: Optional[datetime.datetime],
                 end: Optional[datetime.datetime]) -> None:
    if start is not None and end is not None and start >= end:
        raise _invalid("to", "`to` must be after `from`")


def check_choice(value: Optional[str], name: str, choices: Sequence[str]) -> Optional[str]:
    if value is not None and value not in choices:
        raise _invalid(name, f"unexpected value {value!r}; permitted: {', '.join(choices)}",
                       "type_error.enum")
    return value


def parse_uuid(value: Optional[str], name: str) -> Optional[uuid.UUID]:
    if value is None:
        return None
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise _invalid(name, "value is not a valid uuid", "type_error.uuid") from None


def expand_codes(values: Optional[Iterable[str]]) -> Optional[List[str]]:
    """`code` filter: each value is an exact code (`NAV.GOAL_BLOCKED`) or a category prefix
    (`NAV.*`); expanded to the exact known codes (so the (code, ts) index applies). 422 on
    anything unknown."""
    if not values:
        return None
    codes: List[str] = []
    for value in values:
        if value.endswith(".*"):
            prefix = value[:-2]
            if prefix not in CODE_PREFIXES:
                raise _invalid("code", f"unknown code category {prefix!r}; permitted: "
                                       f"{', '.join(p + '.*' for p in CODE_PREFIXES)}",
                               "type_error.enum")
            codes.extend(c for c in EVENT_CODES if c.startswith(prefix + "."))
        elif value in EVENT_CODES:
            codes.append(value)
        else:
            raise _invalid("code", f"unknown event code {value!r} (use an exact code from "
                                   f"packages/events/codes.py or a prefix such as NAV.*)",
                           "type_error.enum")
    return sorted(set(codes))


# --- cursors -----------------------------------------------------------------------------------

def encode_cursor(kind: str, ts: datetime.datetime, key: Any) -> str:
    raw = json.dumps([kind, ts.astimezone(UTC).isoformat(), str(key)], separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(kind: str, token: Optional[str]) -> Optional[Tuple[datetime.datetime,
                                                                     uuid.UUID]]:
    if token is None:
        return None
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        got_kind, ts, key = json.loads(raw)
        if got_kind != kind:
            raise ValueError(kind)
        ts = datetime.datetime.fromisoformat(ts)
        if ts.tzinfo is None:
            raise ValueError(ts)
        return ts.astimezone(UTC), uuid.UUID(key)
    except (ValueError, TypeError, binascii.Error, UnicodeDecodeError):
        raise _invalid("cursor", "invalid cursor; pass back `next_cursor` unchanged") from None


# --- serialisation -----------------------------------------------------------------------------

def iso(ts: Optional[datetime.datetime]) -> Optional[str]:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC).isoformat()


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime.datetime):
        return iso(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def _row(columns: Sequence[str], values: Sequence[Any]) -> Dict[str, Any]:
    return {c: _jsonable(v) for c, v in zip(columns, values)}


RUN_COLUMNS = ("run_id", "mission_name", "robot_name", "site_id", "map_id", "sw_version",
               "recording_level", "state", "abort_cause", "abort_detail", "passes_completed",
               "created_by", "started_at", "ended_at", "summary_metrics")
EVENT_COLUMNS = ("event_id", "ts", "robot_name", "run_id", "site_id", "code", "severity",
                 "sw_version", "source", "payload")
_RUN_SELECT = ", ".join(RUN_COLUMNS)
_EVENT_SELECT = ", ".join(EVENT_COLUMNS)


def run_dict(values: Sequence[Any]) -> Dict[str, Any]:
    run = _row(RUN_COLUMNS, values)
    started, ended = values[RUN_COLUMNS.index("started_at")], values[
        RUN_COLUMNS.index("ended_at")]
    run["duration_s"] = (round((ended - started).total_seconds(), 3)
                         if started is not None and ended is not None else None)
    return run


def event_dict(values: Sequence[Any]) -> Dict[str, Any]:
    return _row(EVENT_COLUMNS, values)


# --- database access ---------------------------------------------------------------------------

@asynccontextmanager
async def read_cursor(db: Any):
    """A cursor on a pooled connection in a READ ONLY transaction with a statement timeout.
    Timeouts -> 503; Phase 0 tables missing -> 503."""
    try:
        async with db.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SET TRANSACTION READ ONLY")
                await cursor.execute("SELECT set_config('statement_timeout', %s, true)",
                                     (str(int(config.FLEET_READ_STATEMENT_TIMEOUT_MS)),))
                yield cursor
    except psycopg.errors.QueryCanceled as exc:
        logger.warning("Fleet read cancelled after %s ms: %s",
                       config.FLEET_READ_STATEMENT_TIMEOUT_MS, exc)
        raise HTTPException(503, "Query took too long; narrow the filters (time window, "
                                 "robot) and retry") from exc
    except psycopg.errors.UndefinedTable as exc:
        raise HTTPException(503, "Fleet history is not available (database migrations not "
                                 "applied)") from exc


def _page(rows: List[Sequence[Any]], limit: int, kind: str, ts_idx: int, key_idx: int,
          convert) -> Dict[str, Any]:
    more = len(rows) > limit
    rows = rows[:limit]
    cursor = encode_cursor(kind, rows[-1][ts_idx], rows[-1][key_idx]) if more else None
    return {"items": [convert(r) for r in rows], "next_cursor": cursor}


# `mission` filter: the base name, or the base followed by one or more `-rerun-<digits>`
# (the client names a rerun `${name}-rerun-${Date.now()}`; reruns of reruns chain). The base
# is free text, so it never goes into a regex: it is compared as a plain string (equality /
# starts_with) and only the remainder is matched against this constant pattern.
RERUN_SUFFIX_RE = "^(-rerun-[0-9]+)+$"
_MISSION_FILTER = ("(mission_name = %s::text OR (starts_with(mission_name, %s::text) AND "
                   f"substr(mission_name, char_length(%s::text) + 1) ~ '{RERUN_SUFFIX_RE}'))")


def check_mission(value: Optional[str]) -> Optional[str]:
    if value is not None and (value == "" or "\x00" in value):
        raise _invalid("mission", "mission must be a non-empty mission name")
    return value


async def list_runs(db: Any, *, robot: Optional[str] = None, site: Optional[str] = None,
                    state: Optional[str] = None, sw_version: Optional[str] = None,
                    mission: Optional[str] = None,
                    start: Optional[datetime.datetime] = None,
                    end: Optional[datetime.datetime] = None, cursor: Optional[str] = None,
                    limit: int = DEFAULT_LIMIT) -> Dict[str, Any]:
    """Runs newest first (by started_at); `from`/`to` bound started_at: [from, to).
    `mission`: runs of that mission and of its reruns (`<mission>-rerun-<n>[-rerun-<n>...]`)."""
    check_choice(state, "state", RUN_STATES)
    check_mission(mission)
    check_window(start, end)
    after = decode_cursor("runs", cursor)
    where, params = [], []
    for column, value in (("robot_name", robot), ("site_id", site), ("state", state),
                          ("sw_version", sw_version)):
        if value is not None:
            where.append(f"{column} = %s")
            params.append(value)
    if mission is not None:
        where.append(_MISSION_FILTER)
        params.extend((mission, mission, mission))
    if start is not None:
        where.append("started_at >= %s")
        params.append(start)
    if end is not None:
        where.append("started_at < %s")
        params.append(end)
    if after is not None:
        where.append("(started_at, run_id) < (%s, %s)")
        params.extend(after)
    sql = (f"SELECT {_RUN_SELECT} FROM mission_runs"
           + (" WHERE " + " AND ".join(where) if where else "")
           + " ORDER BY started_at DESC, run_id DESC LIMIT %s")
    async with read_cursor(db) as cur:
        await cur.execute(sql, (*params, limit + 1))
        rows = await cur.fetchall()
    return _page(rows, limit, "runs", RUN_COLUMNS.index("started_at"),
                 RUN_COLUMNS.index("run_id"), run_dict)


async def list_events(db: Any, *, robot: Optional[str] = None, site: Optional[str] = None,
                      codes: Optional[Sequence[str]] = None,
                      severities: Optional[Sequence[str]] = None,
                      run: Optional[uuid.UUID] = None,
                      start: Optional[datetime.datetime] = None,
                      end: Optional[datetime.datetime] = None, cursor: Optional[str] = None,
                      limit: int = DEFAULT_LIMIT) -> Dict[str, Any]:
    """Events newest first (by ts); `from`/`to` bound ts: [from, to). `codes` already
    expanded (expand_codes); several severities / codes are OR-ed."""
    for severity in severities or ():
        check_choice(severity, "severity", SEVERITIES)
    check_window(start, end)
    after = decode_cursor("events", cursor)
    where, params = [], []
    for column, value in (("robot_name", robot), ("site_id", site), ("run_id", run)):
        if value is not None:
            where.append(f"{column} = %s")
            params.append(value)
    if codes:
        where.append("code = ANY(%s)")
        params.append(list(codes))
    if severities:
        where.append("severity = ANY(%s)")
        params.append(sorted(set(severities)))
    if start is not None:
        where.append("ts >= %s")
        params.append(start)
    if end is not None:
        where.append("ts < %s")
        params.append(end)
    if after is not None:
        where.append("(ts, event_id) < (%s, %s)")
        params.extend(after)
    sql = (f"SELECT {_EVENT_SELECT} FROM fleet_events"
           + (" WHERE " + " AND ".join(where) if where else "")
           + " ORDER BY ts DESC, event_id DESC LIMIT %s")
    async with read_cursor(db) as cur:
        await cur.execute(sql, (*params, limit + 1))
        rows = await cur.fetchall()
    return _page(rows, limit, "events", EVENT_COLUMNS.index("ts"),
                 EVENT_COLUMNS.index("event_id"), event_dict)


async def _fetch_run(cur: Any, run_id: uuid.UUID) -> Sequence[Any]:
    await cur.execute(f"SELECT {_RUN_SELECT}, mission_tree FROM mission_runs "
                      "WHERE run_id = %s", (run_id,))
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(404, f"Did not find \"run\" with id \"{run_id}\"")
    return row


async def get_run(db: Any, run_id: uuid.UUID) -> Dict[str, Any]:
    """The run (with mission_tree) and the events tagged with its run_id, oldest first
    (at most FLEET_TIMELINE_MAX_EVENTS; `events_truncated` says whether more exist)."""
    max_events = config.FLEET_TIMELINE_MAX_EVENTS
    async with read_cursor(db) as cur:
        row = await _fetch_run(cur, run_id)
        await cur.execute(f"SELECT {_EVENT_SELECT} FROM fleet_events WHERE run_id = %s "
                          "ORDER BY ts, event_id LIMIT %s", (run_id, max_events + 1))
        events = await cur.fetchall()
    run = run_dict(row[:len(RUN_COLUMNS)])
    run["mission_tree"] = row[len(RUN_COLUMNS)]
    return {"run": run, "events": [event_dict(e) for e in events[:max_events]],
            "events_truncated": len(events) > max_events}


# --- recording level history (pure) ------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class LevelChange:
    """A configured-value change at one scope (None = unset), decoded from an event."""
    ts: datetime.datetime
    scope: str                    # robot | site | global
    scope_id: Optional[str]
    old: Optional[str]
    new: Optional[str]
    approximate: bool = False


_TOKENS = tuple(level.value for level in RecordingLevel) + (recording.UNSET,)


def _level_or_none(token: Optional[str]) -> Optional[RecordingLevel]:
    return None if token in (None, recording.UNSET) else RecordingLevel(token)


def decode_change(ts: datetime.datetime, event_id: uuid.UUID, robot_name: Optional[str],
                  payload: Mapping[str, Any],
                  robot_sites: Iterable[str] = ()) -> Optional[LevelChange]:
    """The configured change behind a RECORDING_CHANGED row (see the module docstring);
    None for a robot-scope event written for a site assignment, or an unreadable row."""
    try:
        scope = RecordingScope(payload.get("scope"))
    except ValueError:
        return None
    scope_id = payload.get("scope_id")
    id_robot = robot_name if scope is RecordingScope.ROBOT else None
    code = EventCode.TELEMETRY_RECORDING_CHANGED
    event_id = uuid.UUID(str(event_id))
    for old in _TOKENS:
        for new in _TOKENS:
            if old == new:
                continue
            disc = recording.change_discriminator(scope, scope_id, _level_or_none(old),
                                                  _level_or_none(new))
            if ids.event_id(code, id_robot, ts, disc) == event_id:
                return LevelChange(ts, scope.value, scope_id,
                                   None if old == recording.UNSET else old,
                                   None if new == recording.UNSET else new)
    if scope is RecordingScope.ROBOT and robot_name:
        sites = [None] + sorted(set(robot_sites))
        for old_site in sites:
            for new_site in sites:
                if old_site != new_site and ids.event_id(
                        code, robot_name, ts, recording.assignment_discriminator(
                            robot_name, old_site, new_site)) == event_id:
                    return None
    old_level, new_level = payload.get("old_level"), payload.get("new_level")
    if parse_level(new_level) is None:
        return None
    return LevelChange(ts, scope.value, scope_id, old_level, new_level, approximate=True)


class _Layer:
    """One configured value over time (see the module docstring for the rule)."""

    def __init__(self, changes: Sequence[LevelChange], current: Optional[str]):
        self._changes = sorted(changes, key=lambda c: c.ts)
        self._current = current

    def at(self, t: datetime.datetime) -> Optional[str]:
        value, seen = None, False
        for change in self._changes:
            if change.ts <= t:
                value, seen = change.new, True
            else:
                return value if seen else change.old
        return value if seen else self._current


def resolve_with_source(robot_value: Any, site_value: Any, global_value: Any
                        ) -> Tuple[RecordingLevel, str]:
    for source, value in (("robot", robot_value), ("site", site_value),
                          ("global", global_value)):
        level = parse_level(value)
        if level is not None:
            return level, source
    return DEFAULT_LEVEL, "default"


Assignment = Tuple[str, Optional[datetime.datetime], Optional[datetime.datetime]]


def site_at(assignments: Sequence[Assignment], t: datetime.datetime) -> Optional[str]:
    for site, lower, upper in assignments:
        if (lower is None or lower <= t) and (upper is None or t < upper):
            return site
    return None


def level_segments(start: datetime.datetime, end: datetime.datetime, run_level: Optional[str],
                   changes: Sequence[LevelChange], assignments: Sequence[Assignment],
                   current_robot: Optional[str], current_sites: Mapping[str, Optional[str]],
                   current_global: Optional[str]) -> List[Dict[str, Any]]:
    """The robot's effective level over [start, end]: consecutive segments
    {from, to, level, source, site_id} (source: robot | site | global | default). The first
    segment's level is `run_level` when given (mission_runs.recording_level)."""
    robot_layer = _Layer([c for c in changes if c.scope == "robot"], current_robot)
    global_layer = _Layer([c for c in changes if c.scope == "global"], current_global)
    site_ids = {c.scope_id for c in changes if c.scope == "site"} | {a[0] for a in assignments}
    site_layers = {s: _Layer([c for c in changes if c.scope == "site" and c.scope_id == s],
                             current_sites.get(s)) for s in site_ids}

    def evaluate(t: datetime.datetime) -> Dict[str, Any]:
        site = site_at(assignments, t)
        site_value = site_layers[site].at(t) if site is not None else None
        level, source = resolve_with_source(robot_layer.at(t), site_value, global_layer.at(t))
        return {"level": level.value, "source": source, "site_id": site}

    points = {start}
    points.update(c.ts for c in changes if start < c.ts < end)
    for _site, lower, upper in assignments:
        points.update(b for b in (lower, upper) if b is not None and start < b < end)
    ordered = sorted(points)
    segments: List[Dict[str, Any]] = []
    for i, t in enumerate(ordered):
        state = evaluate(t)
        if i == 0 and run_level is not None and parse_level(run_level) is not None \
                and run_level != state["level"]:
            state = {"level": run_level, "source": "run", "site_id": state["site_id"],
                     "reconstructed_level": state["level"]}
        if segments and all(segments[-1].get(k) == state.get(k)
                            for k in ("level", "source", "site_id", "reconstructed_level")):
            continue
        if segments:
            segments[-1]["to"] = t
        segments.append({"from": t, "to": end, **state})
    return segments


def not_recorded(segments: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Intervals where some data kind was not stored, from level_segments(); adjacent
    segments with the same level are merged."""
    out: List[Dict[str, Any]] = []
    for seg in segments:
        missing = MISSING_BY_LEVEL.get(seg["level"], [])
        if not missing:
            continue
        if out and out[-1]["level"] == seg["level"] and out[-1]["to"] == seg["from"]:
            out[-1]["to"] = seg["to"]
            continue
        out.append({"from": seg["from"], "to": seg["to"], "level": seg["level"],
                    "missing": list(missing)})
    return out


# --- downsampling ------------------------------------------------------------------------------

def bucket_seconds(start: datetime.datetime, end: datetime.datetime, max_points: int) -> int:
    return max(1, math.ceil((end - start).total_seconds() / max(1, max_points)))


def stride(points: List[Any], max_points: int) -> List[Any]:
    """Every k-th point (keeping the last) so at most max_points remain."""
    if len(points) <= max_points:
        return points
    kept = points[::math.ceil(len(points) / max_points)]
    if kept[-1] is not points[-1]:
        if len(kept) >= max_points:
            kept[-1] = points[-1]
        else:
            kept.append(points[-1])
    return kept


# --- timeline ----------------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class _Track:
    raw_table: str
    raw_columns: Tuple[str, ...]          # selected as-is; point keys
    rollup_view: str
    rollup_columns: Tuple[Tuple[str, str], ...]  # (expression, point key)


STATE_TRACK = _Track(
    "robot_state_ts",
    ("x", "y", "yaw", "map_id", "battery", "state", "order_id", "last_node", "driving"),
    "robot_state_1m",
    (("x", "x"), ("y", "y"), ("yaw", "yaw"), ("map_id", "map_id"), ("battery_avg", "battery"),
     ("battery_min", "battery_min"), ("state", "state"), ("order_id", "order_id"),
     ("last_node", "last_node"), ("driving", "driving"), ("samples", "samples")))
DIAGNOSTICS_TRACK = _Track(
    "diagnostics_ts",
    ("cpu", "gpu", "ram", "temp_max", "power_w", "nodes_down", "gnss_fix", "gnss_sats",
     "gnss_h_acc_m", "gnss_corr_age_s"),
    "diagnostics_1m",
    (("cpu_avg", "cpu"), ("cpu_max", "cpu_max"), ("gpu_avg", "gpu"), ("gpu_max", "gpu_max"),
     ("ram_avg", "ram"), ("ram_max", "ram_max"), ("temp_max", "temp_max"),
     ("power_w_avg", "power_w"), ("nodes_down_max", "nodes_down"), ("gnss_fix", "gnss_fix"),
     ("gnss_sats_min", "gnss_sats"), ("gnss_h_acc_m_max", "gnss_h_acc_m"),
     ("gnss_corr_age_s_max", "gnss_corr_age_s"), ("samples", "samples")))


# Index of the equal-width bucket (of %s seconds from the window start) a row falls in.
_BUCKET = "floor(extract(epoch FROM {col} - %s::timestamptz) / %s)"


async def _track(cur: Any, track: _Track, robot: str, start: datetime.datetime,
                 end: datetime.datetime, max_points: int) -> Dict[str, Any]:
    """Raw rows in [start, end]; the 1-minute rollup when no raw row is left (retention).
    More than max_points: the last sample of each of ~max_points equal buckets."""
    bucket_s = bucket_seconds(start, end, max_points)
    keys = ("ts",) + track.raw_columns
    await cur.execute(f"SELECT count(*) FROM {track.raw_table} "
                      "WHERE robot_name = %s AND ts >= %s AND ts <= %s", (robot, start, end))
    count = (await cur.fetchone())[0]
    if count:
        cols = ", ".join(keys)
        where = "WHERE robot_name = %s AND ts >= %s AND ts <= %s"
        if count <= max_points:
            await cur.execute(f"SELECT {cols} FROM {track.raw_table} {where} ORDER BY ts",
                              (robot, start, end))
            return {"source": "raw", "downsampled": False, "bucket_s": None,
                    "points": [_row(keys, r) for r in await cur.fetchall()]}
        await cur.execute(
            f"SELECT {cols} FROM (SELECT DISTINCT ON (b) {cols} FROM (SELECT {cols}, "
            f"{_BUCKET.format(col='ts')} AS b FROM {track.raw_table} {where}) x "
            "ORDER BY b, ts DESC) s ORDER BY ts",
            (start, bucket_s, robot, start, end))
        return {"source": "raw", "downsampled": True, "bucket_s": bucket_s,
                "points": [_row(keys, r) for r in await cur.fetchall()]}

    keys = ("ts",) + tuple(key for _expr, key in track.rollup_columns)
    cols = ", ".join(["bucket AS ts"] + [f"{expr} AS {key}" for expr, key in
                                         track.rollup_columns])
    lo = start - datetime.timedelta(minutes=1)
    bucket_s = max(60, bucket_s)
    if bucket_s <= 60:
        await cur.execute(f"SELECT {cols} FROM {track.rollup_view} WHERE robot_name = %s "
                          "AND bucket > %s AND bucket <= %s ORDER BY bucket",
                          (robot, lo, end))
        downsampled = False
    else:
        await cur.execute(
            f"SELECT {', '.join(keys)} FROM (SELECT DISTINCT ON (b) * FROM (SELECT {cols}, "
            f"{_BUCKET.format(col='bucket')} AS b FROM {track.rollup_view} "
            "WHERE robot_name = %s AND bucket > %s AND bucket <= %s) x "
            "ORDER BY b, ts DESC) s ORDER BY ts",
            (start, bucket_s, robot, lo, end))
        downsampled = True
    points = [_row(keys, r) for r in await cur.fetchall()]
    if not points:
        return {"source": "none", "downsampled": False, "bucket_s": None, "points": []}
    return {"source": "rollup_1m", "downsampled": downsampled,
            "bucket_s": bucket_s if downsampled else 60, "points": points}


TRAJECTORY_KEYS = ("ts", "seq", "node_id", "x", "y", "yaw", "map_id")
TRAJECTORY_GRACE = datetime.timedelta(seconds=5)  # fleet_recorder TRAJECTORY_GRACE_S


async def _trajectory(cur: Any, run: Mapping[str, Any], start: datetime.datetime,
                      end: datetime.datetime, max_points: int) -> Dict[str, Any]:
    """mission_trajectory rows tagged with the run (dispatch tags them when the run
    finishes); for an untagged (running or not yet tagged) run, the mission's untagged rows
    in the run window, as dispatch would tag them."""
    cols = ", ".join(TRAJECTORY_KEYS)
    await cur.execute(f"SELECT {cols} FROM mission_trajectory WHERE run_id = %s "
                      "ORDER BY ts, seq", (run["run_id"],))
    rows, source = await cur.fetchall(), "run_id"
    if not rows:
        await cur.execute(
            f"SELECT {cols} FROM mission_trajectory WHERE mission_id = %s AND robot_name = %s "
            "AND run_id IS NULL AND ts >= %s AND ts <= %s ORDER BY ts, seq",
            (run["mission_name"], run["robot_name"], start, end + TRAJECTORY_GRACE))
        rows, source = await cur.fetchall(), "mission_window"
    if not rows:
        return {"source": "none", "downsampled": False, "points": []}
    kept = stride(rows, max_points)
    return {"source": source, "downsampled": len(kept) < len(rows),
            "points": [_row(TRAJECTORY_KEYS, r) for r in kept]}


async def _recording(cur: Any, robot: str, start: datetime.datetime, end: datetime.datetime,
                     run_level: Optional[str]) -> Dict[str, Any]:
    await cur.execute(
        f"SELECT site_id, lower(valid), upper(valid) FROM {ASSIGNMENTS_TABLE} "
        "WHERE robot_name = %s AND NOT isempty(valid) ORDER BY lower(valid)", (robot,))
    assignments = [tuple(r) for r in await cur.fetchall()]
    robot_sites = sorted({a[0] for a in assignments})
    await cur.execute(
        "SELECT ts, event_id, robot_name, site_id, payload FROM fleet_events "
        "WHERE code = %s AND (robot_name = %s OR (robot_name IS NULL AND "
        "(payload->>'scope' = 'global' OR (payload->>'scope' = 'site' "
        "AND payload->>'scope_id' = ANY(%s))))) ORDER BY ts, event_id",
        (CHANGED, robot, robot_sites))
    rows = await cur.fetchall()
    changes = [c for c in (decode_change(ts, eid, rname, payload or {}, robot_sites)
                           for ts, eid, rname, _site, payload in rows) if c is not None]
    await cur.execute("SELECT now()")
    now = (await cur.fetchone())[0]
    sources = await load_sources(cur.connection, now)
    segments = level_segments(start, end, run_level, changes, assignments,
                              sources.robot_levels.get(robot), sources.site_levels,
                              sources.global_level)
    in_window = [r for r in rows if start <= r[0] <= end]
    return {
        "level_at_start": segments[0]["level"],
        "segments": [{**s, "from": iso(s["from"]), "to": iso(s["to"])} for s in segments],
        "not_recorded": [{**n, "from": iso(n["from"]), "to": iso(n["to"])}
                         for n in not_recorded(segments)],
        "changes": [{"ts": iso(ts), "event_id": str(eid), "robot_name": rname,
                     "site_id": site, "payload": payload}
                    for ts, eid, rname, site, payload in in_window],
        "approximate": any(c.approximate for c in changes),
    }


async def run_timeline(db: Any, run_id: uuid.UUID) -> Dict[str, Any]:
    """Events, coarse tracks, trajectory and recording level history for the run's window
    [started_at, ended_at] (ended_at = now for a running run)."""
    max_points = config.FLEET_TIMELINE_MAX_POINTS
    max_events = config.FLEET_TIMELINE_MAX_EVENTS
    async with read_cursor(db) as cur:
        row = await _fetch_run(cur, run_id)
        values = dict(zip(RUN_COLUMNS, row[:len(RUN_COLUMNS)]))
        start, ended = values["started_at"], values["ended_at"]
        if ended is None:
            await cur.execute("SELECT now()")
            end = (await cur.fetchone())[0]
        else:
            end = ended
        end = max(start, end)
        robot = values["robot_name"]
        # the run's own events plus everything the robot logged in the window
        await cur.execute(
            f"SELECT {_EVENT_SELECT} FROM fleet_events WHERE run_id = %s "
            f"UNION SELECT {_EVENT_SELECT} FROM fleet_events WHERE robot_name = %s "
            "AND ts >= %s AND ts <= %s ORDER BY ts, event_id LIMIT %s",
            (run_id, robot, start, end, max_events + 1))
        events = await cur.fetchall()
        tracks = {"robot_state": await _track(cur, STATE_TRACK, robot, start, end, max_points),
                  "diagnostics": await _track(cur, DIAGNOSTICS_TRACK, robot, start, end,
                                              max_points)}
        trajectory = await _trajectory(cur, values, start, end, max_points)
        rec = await _recording(cur, robot, start, end, values["recording_level"])
    return {
        "run": run_dict(row[:len(RUN_COLUMNS)]),
        "window": {"from": iso(start), "to": iso(end), "open": ended is None},
        "events": [event_dict(e) for e in events[:max_events]],
        "events_truncated": len(events) > max_events,
        "tracks": tracks,
        "trajectory": trajectory,
        "recording": rec,
    }


# --- effective level ---------------------------------------------------------------------------

def _value(raw: Any) -> Optional[str]:
    level = parse_level(raw)
    return level.value if level is not None else None


def effective_level(robot_name: str, sources: PolicySources) -> Dict[str, Any]:
    """The one resolution rule for both effective-level routes: §4.2 over `sources`
    (packages/telemetry_ingest/policy.py load_sources, the same snapshot dispatch and the API
    writer use)."""
    site_id = sources.robot_sites.get(robot_name)
    robot_value = sources.robot_levels.get(robot_name)
    site_value = sources.site_levels.get(site_id) if site_id is not None else None
    level, source = resolve_with_source(robot_value, site_value, sources.global_level)
    return {"robot_name": robot_name, "level": level.value, "source": source,
            "site_id": site_id,
            "configured": {"robot": _value(robot_value), "site": _value(site_value),
                           "global": _value(sources.global_level)}}


async def _sources_now(cur: Any) -> PolicySources:
    await cur.execute("SELECT now()")
    now = (await cur.fetchone())[0]
    return await load_sources(cur.connection, now)


async def effective_recording(db: Any, robot_name: str) -> Dict[str, Any]:
    """The robot's recording level now and which layer it comes from. 404 if unknown."""
    async with read_cursor(db) as cur:
        await cur.execute(f"SELECT 1 FROM {ROBOT_TABLE} WHERE name = %s "
                          "AND lifecycle <> 'DELETED'", (robot_name,))
        if await cur.fetchone() is None:
            raise HTTPException(404, f"Did not find \"robot\" with name \"{robot_name}\"")
        sources = await _sources_now(cur)
    return {**effective_level(robot_name, sources), "default": DEFAULT_LEVEL.value}


async def effective_recording_all(db: Any) -> List[Dict[str, Any]]:
    """effective_level() for every non-deleted robot, by name, plus `site_name` (the site's
    display_name, else its id; null when unassigned). A fixed number of queries (the
    load_sources snapshot + site names), whatever the fleet size."""
    async with read_cursor(db) as cur:
        sources = await _sources_now(cur)
        await cur.execute("SELECT to_regclass(%s) IS NOT NULL", (SITE_TABLE,))
        names: Dict[str, Optional[str]] = {}
        if (await cur.fetchone())[0]:
            await cur.execute(f"SELECT name, spec->>'display_name' FROM {SITE_TABLE} "
                              "WHERE lifecycle <> 'DELETED'")
            names = {name: display for name, display in await cur.fetchall()}
    out = []
    for robot_name in sorted(sources.robot_levels):
        item = effective_level(robot_name, sources)
        site_id = item["site_id"]
        out.append({"robot_name": robot_name, "level": item["level"],
                    "source": item["source"], "site_id": site_id,
                    "site_name": (names.get(site_id) or site_id) if site_id else None})
    return out
