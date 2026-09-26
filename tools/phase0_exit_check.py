"""Phase 0 exit check (docs/satinav-fleet-agent-phase0-v2.md §8 + WP13).

Read-only checks against Postgres for the runs started in a time window (optionally one
robot). Prints a PASS/FAIL table and writes the same result as JSON. The runbook is
docs/satinav-fleet-agent-phase0-exit-test.md.

    python -m tools.phase0_exit_check --from 2026-09-27T08:00:00Z [--to ...] [--robot NAME]
        [--json out.json | --json -] [--no-scenario] [--no-disconnect] [--dsn DSN]

Run it where packages/ is importable and the API's environment is set, i.e. inside the API
container: `docker exec <api> python -m tools.phase0_exit_check ...` (the image ships
tools/). The database comes from --dsn, else the POSTGRES_DATABASE_* variables the API uses.

Read-only by construction: one connection, session default `READ ONLY`, everything in one
REPEATABLE READ READ ONLY transaction (a consistent snapshot) with a statement timeout; the
timelines are produced by the API's own code (packages/api/fleet_reads.run_timeline) on the
same transaction.

Checks (status PASS / FAIL / WARN / INFO / SKIP; the exit code is 1 if any FAIL):

  runs_in_window          at least one run started in the window
  one_row_per_run         every dispatched mission (missionobjectv1 with a run id) started in
                          the window has its mission_runs row (uuid5 of name + run id), and no
                          two rows of one mission overlap in time
  terminal_immutable      the immutability trigger is installed and enabled, and each
                          terminal run still matches its RUN_FINISHED event (outcome, cause)
  required_fields         site, recording level, and a cause for every non-COMPLETED
                          terminal run; sw_version may be null (robot-side step deferred,
                          counted); runs still RUNNING are a WARN (check again later)
  run_events_once         MISSION.RUN_STARTED and (terminal runs) MISSION.RUN_FINISHED exactly
                          once per run, unless the level was `off` at that moment
  no_duplicate_event_ids  no event_id twice in fleet_events (the key is (event_id, ts))
  no_duplicate_logical    no spurious repeats after restarts: paired codes (LOST/RESTORED,
                          ONLINE/OFFLINE, LOW/OK, RAISED/CLEARED by error type, ...) never
                          repeat the same side back to back, STATE_CHANGED never repeats the
                          same transition back to back, NODE_FAILED once per run and node
  timeseries_only_full    no robot_state_ts / diagnostics_ts rows while the robot's
                          effective level was not `full` (level history as the timeline
                          rebuilds it; GRACE_S around each change for NOTIFY propagation)
  timeline_not_recorded   per run, GET /runs/{id}/timeline's segments cover the window, its
                          not_recorded intervals match the segments, and no events / time
                          series appear inside an interval that says they were not recorded
  heartbeat_pairs         every HEARTBEAT_LOST is followed by a HEARTBEAT_RESTORED (a robot
                          still lost now is a WARN), and at least one pair exists (the
                          disconnect run; --no-disconnect turns that into INFO)
  unknown_cause_share     the baseline: share of non-COMPLETED terminal runs whose cause is
                          UNKNOWN (INFO, never fails)
  recorder_health         WP13: both processes reported within the staleness threshold and
                          the api row has no active alert; alert events in the window listed
  scenario_coverage       the exit scenario: >= 5 runs, >= 1 COMPLETED, >= 1 FAILED/TIMEOUT,
                          >= 1 CANCELED, >= 1 run with a heartbeat LOST/RESTORED inside it,
                          >= 1 run started at level `full` whose level changed during the run
                          (--no-scenario skips it)
"""

import argparse
import asyncio
import collections
import dataclasses
import datetime
import json
import sys
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

UTC = datetime.timezone.utc

PASS, FAIL, WARN, INFO, SKIP = "PASS", "FAIL", "WARN", "INFO", "SKIP"
_RANK = {FAIL: 4, WARN: 3, PASS: 2, INFO: 1, SKIP: 0}

# fleet_recorder.RUN_NAMESPACE (mission-dispatch; not importable from the API image). A unit
# test asserts they are equal.
RUN_NAMESPACE = uuid.UUID("3f6c2a8e-91d4-4b57-a0e3-5d7b9c1e2f48")
TERMINAL = ("COMPLETED", "FAILED", "CANCELED", "ABORTED", "TIMEOUT")
LEVELS = ("full", "events_only", "off")
# A recording level change reaches dispatch and the API within ~1 s (NOTIFY); rows this close
# to a boundary are not held against the level on either side.
GRACE_S = 5.0
STATEMENT_TIMEOUT_MS = 30000
DEFAULT_MAX_TIMELINES = 500
MAX_DETAILS = 20
RECORDER_STALE_S = 60.0   # config.RECORDER_HEALTH_STALE_S default

RUN_STARTED = "MISSION.RUN_STARTED"
RUN_FINISHED = "MISSION.RUN_FINISHED"
NODE_FAILED = "MISSION.NODE_FAILED"
STATE_CHANGED = "ROBOT.STATE_CHANGED"
HB_LOST = "ROBOT.HEARTBEAT_LOST"
HB_RESTORED = "ROBOT.HEARTBEAT_RESTORED"
RECORDING_CHANGED = "TELEMETRY.RECORDING_CHANGED"
ALERT_RAISED = "SYSTEM.RECORDER_ALERT_RAISED"
ALERT_CLEARED = "SYSTEM.RECORDER_ALERT_CLEARED"

# code -> (group, side, payload keys that identify the thing the pair is about)
PAIRED: Dict[str, Tuple[str, str, Tuple[str, ...]]] = {
    HB_LOST: ("heartbeat", "on", ()), HB_RESTORED: ("heartbeat", "off", ()),
    "ROBOT.OFFLINE": ("connection", "on", ()), "ROBOT.ONLINE": ("connection", "off", ()),
    "BATTERY.LOW": ("battery", "on", ()), "BATTERY.OK": ("battery", "off", ()),
    "GNSS.RTK_LOST": ("rtk", "on", ()), "GNSS.RTK_RECOVERED": ("rtk", "off", ()),
    "SYSTEM.THERMAL_HIGH": ("thermal", "on", ()), "SYSTEM.THERMAL_OK": ("thermal", "off", ()),
    "NAV.RECOVERY_ENTERED": ("recovery", "on", ()), "NAV.RECOVERY_EXITED": ("recovery", "off", ()),
    "ROBOT.ERROR_RAISED": ("error", "on", ("error_type",)),
    "ROBOT.ERROR_CLEARED": ("error", "off", ("error_type",)),
    "SYSTEM.NODE_DOWN": ("node", "on", ("node",)), "SYSTEM.NODE_UP": ("node", "off", ("node",)),
    ALERT_RAISED: ("recorder_alert", "on", ("alert", "process")),
    ALERT_CLEARED: ("recorder_alert", "off", ("alert", "process")),
}


# --- data --------------------------------------------------------------------------------------

@dataclasses.dataclass
class Run:
    run_id: uuid.UUID
    mission_name: str
    robot_name: str
    site_id: Optional[str]
    sw_version: Optional[str]
    recording_level: str
    state: str
    abort_cause: Optional[str]
    started_at: datetime.datetime
    ended_at: Optional[datetime.datetime]

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL


@dataclasses.dataclass
class Ev:
    ts: datetime.datetime
    event_id: uuid.UUID
    robot_name: Optional[str]
    run_id: Optional[uuid.UUID]
    code: str
    payload: Dict[str, Any]


@dataclasses.dataclass
class Result:
    name: str
    status: str
    summary: str
    details: List[Any] = dataclasses.field(default_factory=list)
    metrics: Dict[str, Any] = dataclasses.field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "status": self.status, "summary": self.summary,
                "details": [_jsonable(d) for d in self.details[:MAX_DETAILS]],
                "details_total": len(self.details), "metrics": _jsonable(self.metrics)}


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime.datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return value


def iso(ts: Optional[datetime.datetime]) -> Optional[str]:
    return ts.astimezone(UTC).isoformat() if ts is not None else None


def parse_ts(value: str) -> datetime.datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    ts = datetime.datetime.fromisoformat(text)
    if ts.tzinfo is None:
        raise ValueError(f"timestamp needs a time zone: {value!r}")
    return ts.astimezone(UTC)


def expected_run_id(mission_name: str, run_token: str) -> uuid.UUID:
    """fleet_recorder.run_uuid() for a mission with a dispatcher run id."""
    return uuid.uuid5(RUN_NAMESPACE, f"{mission_name}|{run_token}")


def _short(run: Run) -> str:
    return f"{run.mission_name} ({run.run_id})"


# --- pure checks -------------------------------------------------------------------------------

def check_runs_present(runs: Sequence[Run]) -> Result:
    by_state = collections.Counter(r.state for r in runs)
    if not runs:
        return Result("runs_in_window", FAIL, "no run started in the window")
    return Result("runs_in_window", PASS,
                  f"{len(runs)} run(s): " + ", ".join(f"{n} {s}" for s, n in
                                                      sorted(by_state.items())),
                  metrics={"runs": len(runs), "by_state": dict(by_state)})


def check_one_row_per_run(missions: Sequence[Tuple[str, str, str, str]],
                          run_ids: Iterable[uuid.UUID], runs: Sequence[Run]) -> Result:
    """`missions`: (name, robot, run token, state) of dispatched missions started in the
    window; `run_ids`: every mission_runs.run_id that exists (any time)."""
    existing = set(run_ids)
    missing = [{"mission": name, "robot": robot, "state": state,
                "expected_run_id": str(expected_run_id(name, token))}
               for name, robot, token, state in missions
               if expected_run_id(name, token) not in existing]
    overlaps = []
    by_mission: Dict[Tuple[str, str], List[Run]] = collections.defaultdict(list)
    for run in runs:
        by_mission[(run.mission_name, run.robot_name)].append(run)
    for (mission, robot), group in by_mission.items():
        group.sort(key=lambda r: r.started_at)
        for a, b in zip(group, group[1:]):
            if a.ended_at is None or b.started_at < a.ended_at:
                overlaps.append({"mission": mission, "robot": robot,
                                 "runs": [str(a.run_id), str(b.run_id)]})
    details = [{"missing_row": m} for m in missing] + [{"overlapping_rows": o}
                                                       for o in overlaps]
    status = FAIL if details else PASS
    return Result("one_row_per_run", status,
                  f"{len(missions)} dispatched mission(s) checked; {len(missing)} without a "
                  f"row, {len(overlaps)} overlapping duplicate(s)", details,
                  {"missions": len(missions), "missing": len(missing),
                   "overlapping": len(overlaps)})


def check_immutability(trigger: Optional[Tuple[str, str]], runs: Sequence[Run],
                       finished: Mapping[uuid.UUID, List[Ev]]) -> Result:
    """`trigger`: (name, pg_trigger.tgenabled) of the immutability trigger, None if absent."""
    details: List[Any] = []
    if trigger is None:
        details.append("trigger mission_runs_immutable_when_terminal is missing")
    elif trigger[1] == "D":
        details.append("trigger mission_runs_immutable_when_terminal is disabled")
    mismatched = 0
    for run in runs:
        if not run.terminal:
            continue
        for ev in finished.get(run.run_id, []):
            outcome, cause = ev.payload.get("outcome"), ev.payload.get("cause")
            if outcome != run.state or cause != run.abort_cause:
                mismatched += 1
                details.append({"run": _short(run), "row": [run.state, run.abort_cause],
                                "RUN_FINISHED": [outcome, cause]})
    return Result("terminal_immutable", FAIL if details else PASS,
                  "trigger enabled; terminal rows match their RUN_FINISHED" if not details
                  else f"{len(details)} problem(s)", details,
                  {"trigger": trigger[0] if trigger else None, "mismatched": mismatched})


def check_required_fields(runs: Sequence[Run],
                          assigned_at_start: Mapping[uuid.UUID, Optional[str]]) -> Result:
    """`assigned_at_start`: the robot's site assignment at started_at (None: unassigned)."""
    details: List[Any] = []
    running = [r for r in runs if not r.terminal]
    no_sw = sum(1 for r in runs if not r.sw_version)
    for run in runs:
        if not run.site_id:
            site = assigned_at_start.get(run.run_id)
            details.append({"run": _short(run), "missing": "site_id",
                            "robot_site_at_start": site,
                            "hint": "assign the robot to a site before the exit runs"
                            if site is None else "robot had a site; the run did not get it"})
        if run.recording_level not in LEVELS:
            details.append({"run": _short(run), "bad": "recording_level",
                            "value": run.recording_level})
        if run.terminal and run.state != "COMPLETED" and not run.abort_cause:
            details.append({"run": _short(run), "missing": "abort_cause", "state": run.state})
    metrics = {"runs": len(runs), "still_running": len(running),
               "sw_version_null": no_sw}
    if details:
        return Result("required_fields", FAIL, f"{len(details)} missing/invalid field(s)",
                      details, metrics)
    if running:
        return Result("required_fields", WARN,
                      f"{len(running)} run(s) still RUNNING; check again when they end "
                      f"(sw_version null on {no_sw}: robot-side step deferred)",
                      [_short(r) for r in running], metrics)
    return Result("required_fields", PASS,
                  f"site, level and cause present (sw_version null on {no_sw}/{len(runs)}: "
                  "robot-side step deferred)", metrics=metrics)


def check_run_events(runs: Sequence[Run], started: Mapping[uuid.UUID, List[Ev]],
                     finished: Mapping[uuid.UUID, List[Ev]],
                     level_at_end: Mapping[uuid.UUID, Optional[str]]) -> Result:
    details: List[Any] = []
    for run in runs:
        n_start, n_end = len(started.get(run.run_id, [])), len(finished.get(run.run_id, []))
        want_start = 0 if run.recording_level == "off" else 1
        if n_start > 1 or (want_start == 1 and n_start == 0):
            details.append({"run": _short(run), "RUN_STARTED": n_start,
                            "expected": want_start})
        if run.terminal:
            end_level = level_at_end.get(run.run_id) or run.recording_level
            want_end = 0 if end_level == "off" else 1
            if n_end > 1 or (want_end == 1 and n_end == 0):
                details.append({"run": _short(run), "RUN_FINISHED": n_end,
                                "expected": want_end, "level_at_end": end_level})
        elif n_end:
            details.append({"run": _short(run), "RUN_FINISHED": n_end,
                            "expected": 0, "state": run.state})
    return Result("run_events_once", FAIL if details else PASS,
                  f"{len(runs)} run(s): RUN_STARTED/RUN_FINISHED exactly once" if not details
                  else f"{len(details)} run(s) with a wrong RUN_STARTED/RUN_FINISHED count",
                  details)


def check_duplicate_event_ids(dups: Sequence[Tuple[uuid.UUID, int]]) -> Result:
    return Result("no_duplicate_event_ids", FAIL if dups else PASS,
                  "no event_id appears twice" if not dups
                  else f"{len(dups)} event_id(s) appear more than once",
                  [{"event_id": str(e), "count": n} for e, n in dups])


def _pair_key(ev: Ev) -> Optional[Tuple[Any, ...]]:
    meta = PAIRED.get(ev.code)
    if meta is None:
        return None
    group, _side, keys = meta
    return (ev.robot_name, group) + tuple(ev.payload.get(k) for k in keys)


def check_logical_duplicates(events: Sequence[Ev]) -> Result:
    """Events of the window (all robots in scope), any order."""
    ordered = sorted(events, key=lambda e: (e.ts, str(e.event_id)))
    details: List[Any] = []
    last_side: Dict[Tuple[Any, ...], Ev] = {}
    last_transition: Dict[Optional[str], Ev] = {}
    node_failed: Dict[Tuple[Any, ...], int] = collections.Counter()
    for ev in ordered:
        key = _pair_key(ev)
        if key is not None:
            side = PAIRED[ev.code][1]
            prev = last_side.get(key)
            if prev is not None and PAIRED[prev.code][1] == side:
                details.append({"repeated": ev.code, "robot": ev.robot_name,
                                "first": iso(prev.ts), "again": iso(ev.ts),
                                "key": [k for k in key[2:]]})
            last_side[key] = ev
        elif ev.code == STATE_CHANGED:
            prev = last_transition.get(ev.robot_name)
            pair = (ev.payload.get("old"), ev.payload.get("new"))
            if prev is not None and (prev.payload.get("old"), prev.payload.get("new")) == pair:
                details.append({"repeated": STATE_CHANGED, "robot": ev.robot_name,
                                "transition": list(pair), "first": iso(prev.ts),
                                "again": iso(ev.ts)})
            last_transition[ev.robot_name] = ev
        elif ev.code == NODE_FAILED:
            node_failed[(ev.run_id, ev.payload.get("node_id"))] += 1
    for (run_id, node), n in node_failed.items():
        if run_id is not None and n > 1:
            details.append({"repeated": NODE_FAILED, "run_id": str(run_id), "node": node,
                            "count": n})
    return Result("no_duplicate_logical", FAIL if details else PASS,
                  f"{len(events)} event(s): no spurious repeats" if not details
                  else f"{len(details)} spurious repeat(s)", details)


def check_timeseries_levels(violations: Sequence[Mapping[str, Any]], robots: int) -> Result:
    """`violations`: {robot, table, from, to, level, rows} for time-series rows found inside
    a non-`full` segment (grace already applied)."""
    return Result("timeseries_only_full", FAIL if violations else PASS,
                  f"{robots} robot(s): no time series outside level full" if not violations
                  else f"{sum(v['rows'] for v in violations)} row(s) written while the level "
                       "was not full", list(violations), {"robots": robots})


def _interior(start: datetime.datetime, end: datetime.datetime
              ) -> Optional[Tuple[datetime.datetime, datetime.datetime]]:
    grace = datetime.timedelta(seconds=GRACE_S)
    lo, hi = start + grace, end - grace
    return (lo, hi) if lo < hi else None


def check_timeline(run: Run, tl: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Problems in one GET /runs/{id}/timeline answer (empty list = consistent)."""
    from packages.api import fleet_reads
    problems: List[Dict[str, Any]] = []
    rec = tl.get("recording") or {}
    segments = [{**s, "from": parse_ts(s["from"]), "to": parse_ts(s["to"])}
                for s in rec.get("segments") or []]
    w_from, w_to = parse_ts(tl["window"]["from"]), parse_ts(tl["window"]["to"])
    if not segments:
        return [{"run": _short(run), "problem": "no recording segments"}]
    if segments[0]["from"] != w_from or segments[-1]["to"] != w_to:
        problems.append({"run": _short(run), "problem": "segments do not cover the window"})
    for a, b in zip(segments, segments[1:]):
        if a["to"] != b["from"]:
            problems.append({"run": _short(run), "problem": "gap between segments",
                             "at": iso(a["to"])})
    if segments[0]["level"] != run.recording_level:
        problems.append({"run": _short(run), "problem": "first segment level differs from "
                         "mission_runs.recording_level", "segment": segments[0]["level"],
                         "run": run.recording_level})
    expected = [{**n, "from": iso(n["from"]), "to": iso(n["to"])}
                for n in fleet_reads.not_recorded(segments)]
    got = [dict(n) for n in rec.get("not_recorded") or []]
    if [(n["from"], n["to"], n["level"]) for n in expected] != \
            [(parse_ts(n["from"]).isoformat(), parse_ts(n["to"]).isoformat(), n["level"])
             for n in got]:
        problems.append({"run": _short(run), "problem": "not_recorded does not match the "
                         "segments", "expected": expected, "got": got})
    for gap in got:
        inner = _interior(parse_ts(gap["from"]), parse_ts(gap["to"]))
        if inner is None:
            continue
        lo, hi = inner
        if "events" in gap.get("missing", []):
            leaked = [e for e in tl.get("events") or []
                      if e.get("robot_name") == run.robot_name
                      and e.get("code") != RECORDING_CHANGED
                      and lo <= parse_ts(e["ts"]) <= hi]
            if leaked:
                problems.append({"run": _short(run), "problem": "events inside a not_recorded "
                                 "(events) interval", "count": len(leaked),
                                 "codes": sorted({e["code"] for e in leaked})})
        if "time_series" in gap.get("missing", []):
            for name, track in (tl.get("tracks") or {}).items():
                if track.get("source") != "raw":
                    continue
                inside = [p for p in track.get("points") or [] if lo <= parse_ts(p["ts"]) <= hi]
                if inside:
                    problems.append({"run": _short(run), "problem": f"{name} points inside a "
                                     "not_recorded (time_series) interval",
                                     "count": len(inside)})
    reconstructed = [s for s in segments if s.get("reconstructed_level")]
    if reconstructed:
        problems.append({"run": _short(run), "warning": "the level history disagrees with "
                         "mission_runs.recording_level at the start",
                         "reconstructed_level": reconstructed[0]["reconstructed_level"]})
    return problems


def check_timelines(results: Sequence[Tuple[Run, List[Dict[str, Any]]]], skipped: int) -> Result:
    problems = [p for _run, ps in results for p in ps]
    hard = [p for p in problems if "problem" in p]
    status = FAIL if hard else (WARN if problems else PASS)
    summary = (f"{len(results)} timeline(s) consistent" if not problems
               else f"{len(hard)} inconsistency(ies), {len(problems) - len(hard)} warning(s)")
    if skipped:
        summary += f"; {skipped} run(s) not checked (--max-timelines)"
    return Result("timeline_not_recorded", status, summary, problems,
                  {"timelines": len(results), "skipped": skipped})


def check_heartbeat_pairs(events: Sequence[Ev], still_lost: Iterable[str],
                          expect_disconnect: bool = True) -> Result:
    lost_now = set(still_lost)
    by_robot: Dict[Optional[str], List[Ev]] = collections.defaultdict(list)
    for ev in events:
        if ev.code in (HB_LOST, HB_RESTORED):
            by_robot[ev.robot_name].append(ev)
    pairs: List[Dict[str, Any]] = []
    details: List[Any] = []
    warn: List[Any] = []
    for robot, evs in sorted(by_robot.items(), key=lambda kv: str(kv[0])):
        evs.sort(key=lambda e: e.ts)
        pending: Optional[Ev] = None
        for ev in evs:
            if ev.code == HB_LOST:
                if pending is not None:
                    details.append({"robot": robot, "problem": "HEARTBEAT_LOST twice without "
                                    "RESTORED", "at": iso(ev.ts)})
                pending = ev
            else:
                if pending is None:
                    continue  # its LOST is before the window
                pairs.append({"robot": robot, "lost": iso(pending.ts), "restored": iso(ev.ts),
                              "gap_s": ev.payload.get("gap_s"),
                              "run_id": str(pending.run_id) if pending.run_id else None})
                pending = None
        if pending is not None:
            item = {"robot": robot, "lost": iso(pending.ts), "problem": "no HEARTBEAT_RESTORED"}
            (warn if robot in lost_now else details).append(
                {**item, "note": "robot is still lost now"} if robot in lost_now else item)
    metrics = {"pairs": len(pairs), "pairs_list": pairs}
    if details:
        return Result("heartbeat_pairs", FAIL, f"{len(details)} unpaired/repeated LOST",
                      details + warn, metrics)
    if not pairs:
        status = FAIL if expect_disconnect else INFO
        return Result("heartbeat_pairs", status, "no HEARTBEAT_LOST/RESTORED pair in the "
                      "window (the disconnect run should produce one)", warn, metrics)
    if warn:
        return Result("heartbeat_pairs", WARN, f"{len(pairs)} pair(s); a robot is still lost",
                      warn, metrics)
    return Result("heartbeat_pairs", PASS, f"{len(pairs)} LOST/RESTORED pair(s)",
                  pairs, metrics)


def unknown_cause_share(runs: Sequence[Run]) -> Result:
    terminal = [r for r in runs if r.terminal]
    failed = [r for r in terminal if r.state != "COMPLETED"]
    causes = collections.Counter(r.abort_cause or "(none)" for r in failed)
    unknown = causes.get("UNKNOWN", 0)
    share = (unknown / len(failed)) if failed else None
    summary = (f"UNKNOWN on {unknown}/{len(failed)} non-COMPLETED run(s)"
               + (f" = {share:.0%}" if share is not None else "")
               + f" ({unknown}/{len(terminal)} of all terminal runs)")
    return Result("unknown_cause_share", INFO, summary,
                  [{"cause": c, "runs": n} for c, n in causes.most_common()],
                  {"unknown": unknown, "non_completed": len(failed), "terminal": len(terminal),
                   "share_of_non_completed": share,
                   "share_of_terminal": (unknown / len(terminal)) if terminal else None})


def check_recorder_health(rows: Optional[Sequence[Mapping[str, Any]]],
                          alert_events: Sequence[Ev],
                          stale_s: float = RECORDER_STALE_S) -> Result:
    """`rows`: recorder_health rows (process, report_age_s, alerts), None if the table is
    missing (WP13 not deployed)."""
    if rows is None:
        return Result("recorder_health", SKIP, "recorder_health table missing (WP13 not "
                      "deployed)")
    by_process = {r["process"]: r for r in rows}
    details: List[Any] = []
    for process in ("dispatch", "api"):
        row = by_process.get(process)
        if row is None:
            details.append({"process": process, "problem": "never reported"})
        elif (row.get("report_age_s") or 0) > stale_s:
            details.append({"process": process, "problem": "stale",
                            "report_age_s": row["report_age_s"]})
    active = list((by_process.get("api") or {}).get("alerts") or [])
    for alert in active:
        details.append({"active_alert": alert})
    raised = [e for e in alert_events if e.code == ALERT_RAISED]
    cleared = [e for e in alert_events if e.code == ALERT_CLEARED]
    metrics = {"alerts_raised_in_window": len(raised),
               "alerts_cleared_in_window": len(cleared), "active": len(active),
               "events": [{"code": e.code, "ts": iso(e.ts), **e.payload} for e in alert_events]}
    if details:
        return Result("recorder_health", FAIL, f"{len(details)} problem(s)", details, metrics)
    return Result("recorder_health", PASS,
                  f"both processes reporting, no active alert ({len(raised)} raised / "
                  f"{len(cleared)} cleared in the window)", metrics=metrics)


def check_scenario(runs: Sequence[Run], hb_pairs: Sequence[Mapping[str, Any]],
                   level_changed_runs: Iterable[uuid.UUID]) -> Result:
    changed = set(level_changed_runs)
    have = {
        ">=5 runs": len(runs) >= 5,
        "COMPLETED": any(r.state == "COMPLETED" for r in runs),
        "FAILED or TIMEOUT": any(r.state in ("FAILED", "TIMEOUT") for r in runs),
        "CANCELED": any(r.state == "CANCELED" for r in runs),
        "disconnect mid-run": any(_pair_in_run(p, r) for p in hb_pairs for r in runs),
        "full + level change mid-run": any(r.recording_level == "full" and r.run_id in changed
                                           for r in runs),
    }
    missing = [k for k, ok in have.items() if not ok]
    return Result("scenario_coverage", FAIL if missing else PASS,
                  "all exit-test runs present" if not missing
                  else "missing: " + ", ".join(missing), missing, {"have": have})


def _pair_in_run(pair: Mapping[str, Any], run: Run) -> bool:
    if pair.get("robot") != run.robot_name:
        return False
    lost = parse_ts(pair["lost"])
    end = run.ended_at or datetime.datetime.max.replace(tzinfo=UTC)
    return run.started_at <= lost <= end


def overall(results: Sequence[Result]) -> str:
    return FAIL if any(r.status == FAIL for r in results) else PASS


# --- database ----------------------------------------------------------------------------------

class _Db:
    """What packages/api/fleet_reads expects of PostgresDatabase: connection() on our one
    read-only connection (so the timelines read the same snapshot)."""

    def __init__(self, conn: Any):
        self.conn = conn

    @asynccontextmanager
    async def connection(self):
        yield self.conn


async def _fetch(conn: Any, sql: str, params: Any = None) -> List[Tuple[Any, ...]]:
    async with conn.cursor() as cur:
        await cur.execute(sql, params)
        return await cur.fetchall()


def _ev(row: Sequence[Any]) -> Ev:
    ts, event_id, robot, run_id, code, payload = row
    return Ev(ts, uuid.UUID(str(event_id)), robot, uuid.UUID(str(run_id)) if run_id else None,
              code, payload or {})


_RUN_SQL = ("SELECT run_id, mission_name, robot_name, site_id, sw_version, recording_level, "
            "state, abort_cause, started_at, ended_at FROM mission_runs "
            "WHERE started_at >= %s AND started_at < %s AND (%s::text IS NULL OR "
            "robot_name = %s) ORDER BY started_at, run_id")
_EVENT_COLS = "ts, event_id, robot_name, run_id, code, payload"


async def gather(conn: Any, start: datetime.datetime, end: datetime.datetime,
                 robot: Optional[str], *, expect_scenario: bool = True,
                 expect_disconnect: bool = True,
                 max_timelines: int = DEFAULT_MAX_TIMELINES) -> List[Result]:
    """Every check, on `conn` (already inside the read-only transaction)."""
    from packages.api import fleet_reads

    runs = [Run(uuid.UUID(str(r[0])), *r[1:]) for r in
            await _fetch(conn, _RUN_SQL, (start, end, robot, robot))]
    results = [check_runs_present(runs)]

    missions = [tuple(r) for r in await _fetch(
        conn,
        "SELECT name, spec->>'robot', status->>'run_id', status->>'state' "
        "FROM missionobjectv1 WHERE lifecycle <> 'DELETED' AND status->>'run_id' IS NOT NULL "
        "AND status->>'start_timestamp' ~ '^\\d{4}-\\d{2}-\\d{2}' "
        "AND (status->>'start_timestamp')::timestamptz >= %s "
        "AND (status->>'start_timestamp')::timestamptz < %s "
        "AND (%s::text IS NULL OR spec->>'robot' = %s)", (start, end, robot, robot))]
    ids = [uuid.UUID(str(r[0])) for r in await _fetch(
        conn, "SELECT run_id FROM mission_runs WHERE mission_name = ANY(%s)",
        ([m[0] for m in missions] or [""],))]
    results.append(check_one_row_per_run(missions, ids, runs))

    run_ids = [r.run_id for r in runs]
    run_events = [_ev(r) for r in await _fetch(
        conn, f"SELECT {_EVENT_COLS} FROM fleet_events WHERE run_id = ANY(%s) AND code IN "
              "(%s, %s)", (run_ids or [uuid.UUID(int=0)], RUN_STARTED, RUN_FINISHED))]
    started = collections.defaultdict(list)
    finished = collections.defaultdict(list)
    for ev in run_events:
        (started if ev.code == RUN_STARTED else finished)[ev.run_id].append(ev)

    trig = await _fetch(conn, "SELECT tgname, tgenabled FROM pg_trigger WHERE tgname = "
                              "'mission_runs_immutable_when_terminal' AND NOT tgisinternal")
    results.append(check_immutability(tuple(trig[0]) if trig else None, runs, finished))

    assigned = {}
    for run in runs:
        rows = await _fetch(conn, "SELECT site_id FROM robot_site_assignments WHERE "
                                  "robot_name = %s AND valid @> %s::timestamptz",
                            (run.robot_name, run.started_at))
        assigned[run.run_id] = rows[0][0] if rows else None
    results.append(check_required_fields(runs, assigned))

    # timelines (the API's own code), reused for the level at each run's end
    timelines: List[Tuple[Run, List[Dict[str, Any]]]] = []
    level_at_end: Dict[uuid.UUID, Optional[str]] = {}
    level_changed: List[uuid.UUID] = []
    db = _Db(conn)
    for run in runs[:max_timelines]:
        tl = await fleet_reads.run_timeline(db, run.run_id)
        timelines.append((run, check_timeline(run, tl)))
        segs = (tl.get("recording") or {}).get("segments") or []
        if segs:
            level_at_end[run.run_id] = segs[-1]["level"]
        if len({s["level"] for s in segs}) > 1 or any(
                c.get("payload", {}).get("new_level") != c.get("payload", {}).get("old_level")
                for c in (tl.get("recording") or {}).get("changes") or []):
            level_changed.append(run.run_id)
    results.append(check_run_events(runs, started, finished, level_at_end))

    dups = [(uuid.UUID(str(e)), n) for e, n in await _fetch(
        conn, "SELECT event_id, count(*) FROM fleet_events WHERE ts >= %s AND ts < %s "
              "AND (%s::text IS NULL OR robot_name = %s OR robot_name IS NULL) "
              "GROUP BY event_id HAVING count(*) > 1", (start, end, robot, robot))]
    results.append(check_duplicate_event_ids(dups))

    window_events = [_ev(r) for r in await _fetch(
        conn, f"SELECT {_EVENT_COLS} FROM fleet_events WHERE ts >= %s AND ts < %s AND "
              "(%s::text IS NULL OR robot_name = %s OR robot_name IS NULL) ORDER BY ts",
        (start, end, robot, robot))]
    results.append(check_logical_duplicates(window_events))

    # time series vs level, per robot with runs or time series in the window
    robots = sorted({r[0] for r in await _fetch(
        conn, "SELECT robot_name FROM robot_state_ts WHERE ts >= %s AND ts < %s UNION "
              "SELECT robot_name FROM diagnostics_ts WHERE ts >= %s AND ts < %s UNION "
              "SELECT robot_name FROM mission_runs WHERE started_at >= %s AND started_at < %s",
        (start, end) * 3)} - {None})
    if robot is not None:
        robots = [r for r in robots if r == robot]
    violations = []
    for name in robots:
        async with fleet_reads.read_cursor(db) as cur:
            rec = await fleet_reads._recording(cur, name, start, end, None)
        for seg in rec["segments"]:
            if seg["level"] == "full":
                continue
            inner = _interior(parse_ts(seg["from"]), parse_ts(seg["to"]))
            if inner is None:
                continue
            for table in ("robot_state_ts", "diagnostics_ts"):
                n, first, last = (await _fetch(
                    conn, f"SELECT count(*), min(ts), max(ts) FROM {table} WHERE "
                          "robot_name = %s AND ts > %s AND ts < %s", (name, *inner)))[0]
                if n:
                    violations.append({"robot": name, "table": table, "level": seg["level"],
                                       "from": seg["from"], "to": seg["to"], "rows": n,
                                       "first": iso(first), "last": iso(last)})
    results.append(check_timeseries_levels(violations, len(robots)))
    results.append(check_timelines(timelines, max(0, len(runs) - max_timelines)))

    still_lost = [r[0] for r in await _fetch(
        conn, "SELECT robot_name FROM robot_latest WHERE "
              "(state_msg->'_dispatch'->>'heartbeat_lost')::boolean IS TRUE")]
    hb = check_heartbeat_pairs(window_events, still_lost, expect_disconnect)
    results.append(hb)
    results.append(unknown_cause_share(runs))

    health_rows: Optional[List[Dict[str, Any]]] = None
    if (await _fetch(conn, "SELECT to_regclass('recorder_health') IS NOT NULL"))[0][0]:
        health_rows = [{"process": p, "report_age_s": age, "alerts": alerts}
                       for p, age, alerts in await _fetch(
                           conn, "SELECT process, extract(epoch FROM now() - reported_at)"
                                 "::float8, alerts FROM recorder_health")]
    alert_events = [e for e in window_events if e.code in (ALERT_RAISED, ALERT_CLEARED)]
    results.append(check_recorder_health(health_rows, alert_events, _stale_s()))

    if expect_scenario:
        results.append(check_scenario(runs, hb.metrics.get("pairs_list", []), level_changed))
    else:
        results.append(Result("scenario_coverage", SKIP, "--no-scenario"))
    return results


def _stale_s() -> float:
    try:
        from packages import config
        return float(config.RECORDER_HEALTH_STALE_S)
    except Exception:  # noqa: BLE001
        return RECORDER_STALE_S


def default_dsn() -> str:
    """The API's own database settings (packages/config.py POSTGRES_DATABASE_*)."""
    from psycopg.conninfo import make_conninfo
    from packages import config
    return make_conninfo(host=config.POSTGRES_DATABASE_HOST, port=config.POSTGRES_DATABASE_PORT,
                         dbname=config.POSTGRES_DATABASE_NAME,
                         user=config.POSTGRES_DATABASE_USERNAME,
                         password=config.POSTGRES_DATABASE_PASSWORD or "",
                         application_name="phase0_exit_check")


async def run_checks(dsn: str, start: datetime.datetime, end: datetime.datetime,
                     robot: Optional[str], **kwargs: Any) -> List[Result]:
    import psycopg
    conn = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
    try:
        # Belt and braces: the session default is read-only, and so is the transaction.
        await conn.execute("SET default_transaction_read_only = on")
        await conn.execute("SET TIME ZONE 'UTC'")
        await conn.execute(f"SET statement_timeout = {int(STATEMENT_TIMEOUT_MS)}")
        async with conn.transaction():
            await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            return await gather(conn, start, end, robot, **kwargs)
    finally:
        await conn.close()


# --- output ------------------------------------------------------------------------------------

def render_table(results: Sequence[Result], start: datetime.datetime,
                 end: datetime.datetime, robot: Optional[str]) -> str:
    width = max(len(r.name) for r in results) if results else 10
    lines = [f"Phase 0 exit check  window [{iso(start)}, {iso(end)})  robot={robot or 'all'}",
             "", f"{'CHECK'.ljust(width)}  STATUS  SUMMARY", f"{'-' * width}  ------  -------"]
    for r in results:
        lines.append(f"{r.name.ljust(width)}  {r.status.ljust(6)}  {r.summary}")
        if r.status in (FAIL, WARN):
            for d in r.details[:5]:
                lines.append(f"{' ' * width}          - {json.dumps(_jsonable(d), default=str)}")
            if len(r.details) > 5:
                lines.append(f"{' ' * width}          ... {len(r.details) - 5} more (see JSON)")
    counts = collections.Counter(r.status for r in results)
    lines += ["", f"OVERALL: {overall(results)}  (" + ", ".join(
        f"{counts[s]} {s}" for s in (PASS, FAIL, WARN, INFO, SKIP) if counts[s]) + ")"]
    return "\n".join(lines)


def to_json(results: Sequence[Result], start: datetime.datetime, end: datetime.datetime,
            robot: Optional[str]) -> Dict[str, Any]:
    share = next((r.metrics for r in results if r.name == "unknown_cause_share"), {})
    return {"window": {"from": iso(start), "to": iso(end)}, "robot": robot,
            "generated_at": iso(datetime.datetime.now(UTC)), "overall": overall(results),
            "baseline": {"unknown_cause_share_of_non_completed":
                         share.get("share_of_non_completed"),
                         "unknown_cause_share_of_terminal": share.get("share_of_terminal"),
                         "unknown": share.get("unknown"),
                         "non_completed": share.get("non_completed")},
            "checks": [r.as_dict() for r in results]}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--from", dest="start", required=True,
                        help="window start, ISO-8601 with a time zone")
    parser.add_argument("--to", dest="end", default=None, help="window end (default: now)")
    parser.add_argument("--robot", default=None, help="only this robot")
    parser.add_argument("--json", dest="json_path", default=None,
                        help="write the JSON result here ('-' = stdout, table to stderr)")
    parser.add_argument("--dsn", default=None, help="libpq connection string "
                        "(default: POSTGRES_DATABASE_* / PG* environment)")
    parser.add_argument("--no-scenario", action="store_true",
                        help="do not require the 5-run exit scenario")
    parser.add_argument("--no-disconnect", action="store_true",
                        help="a window without a HEARTBEAT_LOST/RESTORED pair is not a failure")
    parser.add_argument("--max-timelines", type=int, default=DEFAULT_MAX_TIMELINES)
    args = parser.parse_args(argv)
    try:
        start = parse_ts(args.start)
        end = parse_ts(args.end) if args.end else datetime.datetime.now(UTC)
    except ValueError as exc:
        parser.error(str(exc))
    if end <= start:
        parser.error("--to must be after --from")
    try:
        results = asyncio.run(run_checks(
            args.dsn or default_dsn(), start, end, args.robot,
            expect_scenario=not args.no_scenario, expect_disconnect=not args.no_disconnect,
            max_timelines=args.max_timelines))
    except Exception as exc:  # noqa: BLE001
        print(f"phase0_exit_check: error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    table = render_table(results, start, end, args.robot)
    payload = json.dumps(to_json(results, start, end, args.robot), indent=2, default=str)
    if args.json_path == "-":
        print(table, file=sys.stderr)
        print(payload)
    else:
        print(table)
        if args.json_path:
            with open(args.json_path, "w", encoding="utf-8") as f:
                f.write(payload + "\n")
            print(f"\nJSON: {args.json_path}")
    return 1 if overall(results) == FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
