"""Recorder health: GET /api/v1/health/recording and its alerts (phase0-v2 WP13).

Who reports what
----------------
Both recording processes upsert one `recorder_health` row (packages/telemetry_ingest/health.py):

- `dispatch`: mission-dispatch's fleet_recorder, every fleet_recorder.HEALTH_REPORT_PERIOD_S
  (10 s): ingest queue, spill file, writer flush ages, the heartbeat sweep lag, run-write ops.
- `api`: the API worker that holds the telemetry writer lock (packages/api/telemetry.py), every
  config.RECORDER_HEALTH_EVAL_S (5 s): its ingest queue/spill/writer, the election role, and the
  active alerts.

Alert rules (thresholds in packages/config.py, env-overridable)
---------------------------------------------------------------
    writer_queue_high    queue depth > RECORDER_ALERT_QUEUE_PCT (80) % of capacity;
                         clears below RECORDER_ALERT_QUEUE_CLEAR_PCT (60) %.   api, dispatch
    spill_pending        spilled events waiting continuously > RECORDER_ALERT_SPILL_S (300 s);
                         clears when the spill file is empty.                  api, dispatch
    heartbeat_sweep_lag  sweep lag > RECORDER_ALERT_SWEEP_LAG_FACTOR (3) x the sweep period
                         (1 s); clears at <= RECORDER_ALERT_SWEEP_LAG_CLEAR_FACTOR (1.5) x.
                                                                               dispatch
    report_stale         the dispatch row is older than RECORDER_HEALTH_STALE_S (60 s), or
                         missing for that long after the evaluator started.   dispatch

Anti-flap: a raise condition must hold for RECORDER_ALERT_RAISE_S (10 s) before the alert
starts (spill_pending and report_stale measure a duration already and start at once), and
the clear condition for RECORDER_ALERT_CLEAR_S (30 s) before it ends. Between the raise and
clear thresholds the state holds. For dispatch those windows run on its reports' reported_at,
so a condition must show in reports spanning 10 s (two consecutive 10 s reports): one
report caught in a bad moment, re-read by several evaluations, does not raise. The rules of
a process whose report is stale are not evaluated (they hold) until it reports again.

Why 3 x the sweep period: the sweep sleeps SWEEP_PERIOD_S after each pass, so a healthy lag is
about 1 s. 3 s means at least two passes were missed in a row, i.e. dispatch's event loop was
blocked or the sweep task died, which is well beyond scheduling jitter and GC pauses, yet
still far below the robots' heartbeat timeouts (tens of seconds), so the alert fires before
HEARTBEAT_LOST detection itself becomes unreliable.

Emission
--------
Only the elected writer evaluates, so each transition is seen by exactly one process. Every
start and end emits one fleet_event (SYSTEM.RECORDER_ALERT_RAISED / _CLEARED, robot_name
null, payload {alert, process, value, threshold[, raised_at, duration_s]}) plus one WARNING
log line. The events are written with emit() directly on the writer's lock connection,
together with the api row (whose `alerts` column carries the active alerts) in ONE
transaction; they do not go through the ingest queue, whose backlog may be the very thing
being alerted on. If that write fails (database down), the log line has already been written,
the alert state stays in memory and in this endpoint's answer, and the events are retried on
the next evaluation (deterministic event_ids: ts = transition time, so a retry can't
duplicate). If the writer term ends first, the pending events go to the writer's spill file
and are replayed by the next writer. A restarted API restores the active alerts from the api
row, so an ongoing alert is not raised twice.
"""

import dataclasses
import datetime
import logging
import os
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from fastapi import HTTPException

from packages import config
from packages.events.codes import EventCode
from packages.events.emit import INSERT_SQL, Event, build_row, row_params
from packages.telemetry_ingest import health

logger = logging.getLogger("ApiDelegationService.recorder_health")

UTC = datetime.timezone.utc

ALERT_QUEUE = "writer_queue_high"
ALERT_SPILL = "spill_pending"
ALERT_SWEEP = "heartbeat_sweep_lag"
ALERT_STALE = "report_stale"
# Read-time only (the endpoint could not read recorder_health); never an event.
ALERT_DB = "health_db_unreachable"
ALERTS = (ALERT_QUEUE, ALERT_SPILL, ALERT_SWEEP, ALERT_STALE)

PROCESSES = (health.PROCESS_DISPATCH, health.PROCESS_API)
ROLE_WRITER = "writer"
ROLE_STANDBY = "standby"
MAX_PENDING_EVENTS = 200


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(UTC)


def _iso(ts: Optional[datetime.datetime]) -> Optional[str]:
    return ts.astimezone(UTC).isoformat() if ts is not None else None


def _parse_iso(value: Any) -> Optional[datetime.datetime]:
    if isinstance(value, datetime.datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value:
        try:
            ts = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return ts if ts.tzinfo else ts.replace(tzinfo=UTC)
    return None


def _num(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) \
        else None


# --- thresholds ------------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Thresholds:
    stale_s: float = 60.0
    queue_pct: float = 80.0
    queue_clear_pct: float = 60.0
    spill_s: float = 300.0
    sweep_factor: float = 3.0
    sweep_clear_factor: float = 1.5
    raise_s: float = 10.0
    clear_s: float = 30.0

    @classmethod
    def from_config(cls) -> "Thresholds":
        return cls(stale_s=config.RECORDER_HEALTH_STALE_S,
                   queue_pct=config.RECORDER_ALERT_QUEUE_PCT,
                   queue_clear_pct=config.RECORDER_ALERT_QUEUE_CLEAR_PCT,
                   spill_s=config.RECORDER_ALERT_SPILL_S,
                   sweep_factor=config.RECORDER_ALERT_SWEEP_LAG_FACTOR,
                   sweep_clear_factor=config.RECORDER_ALERT_SWEEP_LAG_CLEAR_FACTOR,
                   raise_s=config.RECORDER_ALERT_RAISE_S,
                   clear_s=config.RECORDER_ALERT_CLEAR_S)

    def as_dict(self) -> Dict[str, float]:
        return dataclasses.asdict(self)


# --- rules -----------------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Measure:
    """One rule's reading for one process. `bad`: the raise condition holds; `good`: the
    clear condition holds; neither: inside the hysteresis band (state holds)."""
    alert: str
    process: str
    value: Optional[float]
    threshold: float
    bad: bool
    good: bool
    raise_after_s: float
    clear_after_s: float
    # When the reading was taken (a stored report's reported_at); None = now. The raise and
    # clear windows are measured on this clock, so a condition must hold across reports
    # spanning raise_after_s: re-reading the same stored report does not count as "longer".
    observed_at: Optional[datetime.datetime] = None


def measures_for(process: str, report: Optional[Mapping[str, Any]], th: Thresholds, *,
                 report_age_s: Optional[float] = None, check_stale: bool = False,
                 missing_for_s: float = 0.0,
                 observed_at: Optional[datetime.datetime] = None) -> List[Measure]:
    """The rules' readings for one process. `report` None = no row. With `check_stale`, the
    report_stale rule is measured and, if the report is stale or missing, the other rules are
    skipped (they hold). `missing_for_s`: how long the evaluator has been looking for a row
    that does not exist (a missing row only counts as stale after th.stale_s of that).
    `observed_at`: the report's reported_at (see Measure); staleness is always judged now."""
    out: List[Measure] = []
    if check_stale:
        if report is None:
            bad = missing_for_s > th.stale_s
            out.append(Measure(ALERT_STALE, process, None, th.stale_s, bad, False,
                               0.0, th.clear_s))
            return out
        age = _num(report_age_s) or 0.0
        stale = age > th.stale_s
        out.append(Measure(ALERT_STALE, process, round(age, 3), th.stale_s, stale, not stale,
                           0.0, th.clear_s))
        if stale:
            return out
    if report is None:
        return out

    queue = report.get("queue") or {}
    pct = _num(queue.get("pct"))
    if pct is not None:
        out.append(Measure(ALERT_QUEUE, process, pct, th.queue_pct, pct > th.queue_pct,
                           pct < th.queue_clear_pct, th.raise_s, th.clear_s, observed_at))

    spill = report.get("spill") or {}
    pending = _num(spill.get("pending")) or 0.0
    age = _num(spill.get("pending_age_s")) or 0.0
    out.append(Measure(ALERT_SPILL, process, round(age, 3) if pending else 0.0, th.spill_s,
                       pending > 0 and age > th.spill_s, pending == 0, 0.0, th.clear_s,
                       observed_at))

    sweep = report.get("heartbeat_sweep")
    if isinstance(sweep, Mapping):
        lag, period = _num(sweep.get("lag_s")), _num(sweep.get("period_s"))
        if lag is not None and period:
            limit = th.sweep_factor * period
            out.append(Measure(ALERT_SWEEP, process, lag, limit, lag > limit,
                               lag <= th.sweep_clear_factor * period, th.raise_s, th.clear_s,
                               observed_at))
    return out


# --- evaluator -------------------------------------------------------------------------------

@dataclasses.dataclass
class _State:
    active: bool = False
    raised_at: Optional[datetime.datetime] = None
    bad_since: Optional[datetime.datetime] = None
    good_since: Optional[datetime.datetime] = None
    value: Optional[float] = None
    threshold: Optional[float] = None


@dataclasses.dataclass(frozen=True)
class Transition:
    raised: bool                       # True: RAISED, False: CLEARED
    alert: str
    process: str
    value: Optional[float]
    threshold: float
    at: datetime.datetime
    raised_at: Optional[datetime.datetime] = None   # CLEARED: when it was raised

    @property
    def code(self) -> EventCode:
        return (EventCode.SYSTEM_RECORDER_ALERT_RAISED if self.raised
                else EventCode.SYSTEM_RECORDER_ALERT_CLEARED)

    def event(self) -> Event:
        payload: Dict[str, Any] = {"alert": self.alert, "process": self.process,
                                   "value": self.value, "threshold": self.threshold}
        discriminator = f"{self.alert}|{self.process}"
        if not self.raised:
            payload["raised_at"] = self.raised_at
            payload["duration_s"] = (round((self.at - self.raised_at).total_seconds(), 3)
                                     if self.raised_at is not None else None)
            discriminator += f"|{_iso(self.raised_at)}"
        return Event(self.code, self.at, robot_name=None, payload=payload,
                     discriminator=discriminator)

    def log_line(self) -> str:
        what = "RAISED" if self.raised else "CLEARED"
        return (f"Recorder alert {what}: {self.alert} (process {self.process}) "
                f"value={self.value} threshold={self.threshold}")


class AlertEvaluator:
    """Pure hysteresis + minimum-duration state machine per (alert, process). Time is passed in."""

    def __init__(self):
        self._states: Dict[Tuple[str, str], _State] = {}

    def update(self, measures: Sequence[Measure], now: datetime.datetime) -> List[Transition]:
        out: List[Transition] = []
        for m in measures:
            st = self._states.setdefault((m.alert, m.process), _State())
            st.value, st.threshold = m.value, m.threshold
            seen = m.observed_at or now
            if not st.active:
                st.good_since = None
                if not m.bad:
                    st.bad_since = None
                    continue
                st.bad_since = st.bad_since or seen
                if (seen - st.bad_since).total_seconds() >= m.raise_after_s:
                    st.active, st.raised_at, st.bad_since = True, now, None
                    out.append(Transition(True, m.alert, m.process, m.value, m.threshold, now))
                continue
            st.bad_since = None
            if not m.good:
                st.good_since = None
                continue
            st.good_since = st.good_since or seen
            if (seen - st.good_since).total_seconds() >= m.clear_after_s:
                out.append(Transition(False, m.alert, m.process, m.value, m.threshold, now,
                                      raised_at=st.raised_at))
                st.active, st.raised_at, st.good_since = False, None, None
        return out

    def active(self) -> List[Dict[str, Any]]:
        return [{"alert": alert, "process": process, "value": st.value,
                 "threshold": st.threshold, "since": _iso(st.raised_at), "source": "evaluator"}
                for (alert, process), st in sorted(self._states.items()) if st.active]

    def restore(self, alerts: Sequence[Mapping[str, Any]]) -> int:
        """Re-activate alerts persisted by a previous writer (the api row's `alerts`), so an
        ongoing alert is not raised (and emitted) again. Keys already known are left alone."""
        restored = 0
        for item in alerts or ():
            if not isinstance(item, Mapping):
                continue
            alert, process = item.get("alert"), item.get("process")
            if alert not in ALERTS or process not in PROCESSES:
                continue
            key = (alert, process)
            if key in self._states:
                continue
            self._states[key] = _State(active=True, raised_at=_parse_iso(item.get("since")),
                                       value=_num(item.get("value")),
                                       threshold=_num(item.get("threshold")))
            restored += 1
        return restored


# --- the monitor inside the elected API writer -------------------------------------------------

class RecorderHealthMonitor:
    """Evaluates the rules and writes the api row + alert events on the writer's lock
    connection (ApiTelemetry calls restore() at term start and maybe_tick() on each election
    tick). Never raises."""

    def __init__(self, thresholds: Optional[Thresholds] = None, *,
                 eval_s: Optional[float] = None,
                 now=_utcnow, monotonic=time.monotonic):
        self.thresholds = thresholds or Thresholds.from_config()
        self.eval_s = config.RECORDER_HEALTH_EVAL_S if eval_s is None else eval_s
        self.evaluator = AlertEvaluator()
        self._now = now
        self._monotonic = monotonic
        self._started = monotonic()
        self._last_eval: Optional[float] = None
        self._pending: List[Dict[str, Any]] = []
        self.write_failures = 0
        self.read_failures = 0
        self.events_emitted = 0
        self.events_dropped = 0
        self.last_rows: Dict[str, Dict[str, Any]] = {}

    @property
    def pending_events(self) -> int:
        return len(self._pending)

    def take_pending(self) -> List[Dict[str, Any]]:
        rows, self._pending = self._pending, []
        return rows

    def active_alerts(self) -> List[Dict[str, Any]]:
        return self.evaluator.active()

    async def restore(self, conn: Any) -> int:
        """Load the active alerts a previous writer left in the api row. Never raises."""
        try:
            rows = await read_rows(conn)
        except Exception:  # noqa: BLE001
            logger.warning("Could not restore recorder alerts (recorder_health unreadable)",
                           exc_info=True)
            return 0
        api = rows.get(health.PROCESS_API)
        restored = self.evaluator.restore(api["alerts"] if api else [])
        if restored:
            logger.info("Restored %d active recorder alert(s) from recorder_health", restored)
        return restored

    def evaluate(self, api_report: Mapping[str, Any],
                 dispatch_row: Optional[Mapping[str, Any]], dispatch_known: bool,
                 now: datetime.datetime) -> List[Transition]:
        """One evaluation. `dispatch_known` False (recorder_health unreadable): dispatch's
        rules hold."""
        th = self.thresholds
        measures = measures_for(health.PROCESS_API, api_report, th)
        if dispatch_known:
            measures += measures_for(
                health.PROCESS_DISPATCH,
                dispatch_row["report"] if dispatch_row is not None else None, th,
                report_age_s=dispatch_row["report_age_s"] if dispatch_row is not None else None,
                check_stale=True, missing_for_s=self._monotonic() - self._started,
                observed_at=_parse_iso(dispatch_row.get("reported_at"))
                if dispatch_row is not None else None)
        transitions = self.evaluator.update(measures, now)
        for t in transitions:
            logger.warning(t.log_line())
            self._pending.append(build_row(t.event()))
        if len(self._pending) > MAX_PENDING_EVENTS:
            extra = len(self._pending) - MAX_PENDING_EVENTS
            self.events_dropped += extra
            logger.error("Dropping %d unwritten recorder alert events (backlog > %d)",
                         extra, MAX_PENDING_EVENTS)
            del self._pending[:extra]
        return transitions

    async def maybe_tick(self, conn: Any, api_report: Mapping[str, Any], *, role: str,
                         started_at: Optional[datetime.datetime], force: bool = False) -> bool:
        """Evaluate and write if RECORDER_HEALTH_EVAL_S has passed. Never raises (except
        CancelledError). Returns True if an evaluation ran."""
        mono = self._monotonic()
        if not force and self._last_eval is not None and mono - self._last_eval < self.eval_s:
            return False
        self._last_eval = mono
        rows: Optional[Dict[str, Dict[str, Any]]] = None
        try:
            rows = await read_rows(conn)
            self.last_rows = rows
        except Exception as exc:  # noqa: BLE001
            self.read_failures += 1
            if self.read_failures <= 3 or self.read_failures % 60 == 0:
                logger.warning("Reading recorder_health failed (#%d): %s", self.read_failures,
                               str(exc).strip() or type(exc).__name__)
        now = self._now()
        self.evaluate(api_report, (rows or {}).get(health.PROCESS_DISPATCH), rows is not None,
                      now)
        await self._write(conn, api_report, role, started_at)
        return True

    async def _write(self, conn: Any, api_report: Mapping[str, Any], role: str,
                     started_at: Optional[datetime.datetime]) -> bool:
        pending = list(self._pending)
        try:
            async with conn.transaction():
                async with conn.cursor() as cursor:
                    await cursor.execute(health.UPSERT_WITH_ALERTS_SQL, health.row_params(
                        health.PROCESS_API, role, started_at, api_report,
                        alerts=self.evaluator.active()))
                    for row in pending:
                        await cursor.execute(INSERT_SQL, row_params(row))
        except Exception as exc:  # noqa: BLE001
            self.write_failures += 1
            if self.write_failures <= 3 or self.write_failures % 60 == 0:
                logger.error("Writing recorder health failed (#%d): %s; alert state is kept in "
                             "memory, %d alert event(s) pending (retried; spilled if the writer "
                             "stops)", self.write_failures, str(exc).strip() or
                             type(exc).__name__, len(pending))
            return False
        written = {id(r) for r in pending}
        self._pending = [r for r in self._pending if id(r) not in written]
        self.events_emitted += len(pending)
        return True


async def read_rows(conn: Any) -> Dict[str, Dict[str, Any]]:
    """recorder_health rows by process (raises on any database error)."""
    async with conn.cursor() as cursor:
        await cursor.execute(health.SELECT_SQL)
        rows = await cursor.fetchall()
    return {row[0]: dict(zip(health.SELECT_COLUMNS, row)) for row in rows}


# --- the endpoint ------------------------------------------------------------------------------

def _process_view(row: Mapping[str, Any], th: Thresholds) -> Dict[str, Any]:
    report = dict(row.get("report") or {})
    age = _num(row.get("report_age_s"))
    view: Dict[str, Any] = {
        "present": True,
        "source": row.get("source", "database"),
        "pid": row.get("pid"),
        "hostname": row.get("hostname"),
        "role": row.get("role"),
        "started_at": _iso(row.get("started_at")) if isinstance(row.get("started_at"),
                                                              datetime.datetime)
        else row.get("started_at"),
        "reported_at": _iso(row.get("reported_at")) if isinstance(row.get("reported_at"),
                                                                datetime.datetime)
        else row.get("reported_at"),
        "report_age_s": None if age is None else round(age, 3),
        "stale": age is not None and age > th.stale_s,
    }
    view.update(report)
    # Ages in the report were measured when it was written; add how old the report is.
    spill = dict(view.get("spill") or {})
    if spill.get("pending_age_s") is not None and age:
        spill["pending_age_s"] = round(spill["pending_age_s"] + age, 3)
    if spill:
        view["spill"] = spill
    sweep = view.get("heartbeat_sweep")
    if isinstance(sweep, Mapping) and _num(sweep.get("period_s")):
        view["heartbeat_sweep"] = {**sweep,
                                   "lag_threshold_s": th.sweep_factor * sweep["period_s"]}
    return view


async def recording_health(db: Any, telemetry: Any = None,
                           thresholds: Optional[Thresholds] = None) -> Dict[str, Any]:
    """GET /api/v1/health/recording. Read-only; always 200 (the answer is the health).

    `telemetry` is the worker's ApiTelemetry (or None when ingest is disabled). The worker that
    is the writer answers for `api` and the alerts from memory (fresh even when the database
    is down); any other worker reads the api row and its stored alerts. Staleness is also
    checked at read time for every process, so a dead evaluator shows too."""
    from packages.api import fleet_reads  # the READ ONLY + statement_timeout cursor
    th = thresholds or Thresholds.from_config()
    rows: Dict[str, Dict[str, Any]] = {}
    db_error: Optional[str] = None
    try:
        async with fleet_reads.read_cursor(db) as cur:
            await cur.execute(health.SELECT_SQL)
            for row in await cur.fetchall():
                rows[row[0]] = dict(zip(health.SELECT_COLUMNS, row))
    except HTTPException as exc:
        db_error = str(exc.detail)
    except Exception as exc:  # noqa: BLE001
        db_error = str(exc).strip() or type(exc).__name__
        logger.warning("GET /api/v1/health/recording: reading recorder_health failed: %s",
                       db_error)

    local = telemetry is not None and getattr(telemetry, "is_writer", False)
    processes: Dict[str, Any] = {}
    for process in PROCESSES:
        if local and process == health.PROCESS_API:
            live = telemetry.health_row()
            processes[process] = _process_view({**live, "report_age_s": 0.0,
                                                "source": "memory"}, th)
        elif process in rows:
            processes[process] = _process_view(rows[process], th)
        else:
            processes[process] = {"present": False, "source": None, "stale": db_error is None}

    if local:
        alerts = [dict(a) for a in telemetry.health.active_alerts()]
    else:
        stored = (rows.get(health.PROCESS_API) or {}).get("alerts") or []
        alerts = [{**a, "source": "stored"} for a in stored if isinstance(a, Mapping)]
    have = {(a.get("alert"), a.get("process")) for a in alerts}
    if db_error is None:
        for process, view in processes.items():
            if view.get("source") == "memory" or (ALERT_STALE, process) in have:
                continue
            if not view.get("present") or view.get("stale"):
                alerts.append({"alert": ALERT_STALE, "process": process,
                               "value": view.get("report_age_s"), "threshold": th.stale_s,
                               "since": None, "source": "read"})
    else:
        alerts.append({"alert": ALERT_DB, "process": health.PROCESS_API, "value": None,
                       "threshold": None, "since": None, "source": "read",
                       "detail": db_error})

    return {
        "status": "alerting" if alerts else "ok",
        "generated_at": _iso(_utcnow()),
        "served_by": {"pid": os.getpid(), "is_writer": bool(local),
                      "telemetry_enabled": telemetry is not None},
        "database": {"ok": db_error is None, "error": db_error},
        "thresholds": th.as_dict(),
        "processes": processes,
        "alerts": alerts,
    }
