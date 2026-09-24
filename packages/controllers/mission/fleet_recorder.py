"""Phase 0 recording inside mission-dispatch (docs/satinav-fleet-agent-phase0-v2.md §5.3).

What dispatch records, on top of what it already does:

- `mission_runs`: one row per dispatched mission, inserted together with
  MISSION.RUN_STARTED and closed together with MISSION.RUN_FINISHED, each pair in one
  transaction. Repeat passes stay inside the run (`passes_completed`).
- MISSION.NODE_FAILED / EDGE_BLOCKED / REROUTED where the dispatcher updates those
  status fields.
- From `state`, `connection` and `factsheet`: ROBOT.STATE_CHANGED, ONLINE/OFFLINE,
  ERROR_RAISED/CLEARED, SW_VERSION_CHANGED, BATTERY.LOW/OK, plus `robot_state_ts` rows
  (every 5 s per robot and at once on a state/order/error change) and `robot_latest`.
- A 1 Hz sweep for HEARTBEAT_LOST / HEARTBEAT_RESTORED.
- Orphan reconciliation of RUNNING runs at startup.

Failure isolation: the command path never waits on, or fails because of, anything here.

- Every hook the dispatcher calls is synchronous, only touches memory and the
  telemetry_ingest queue, and swallows (logs and counts) its own exceptions.
- Run rows are written by one background worker on the telemetry pool (not the
  dispatcher's own pool), strictly in submission order so a run's finish can never
  overtake its start. The mission object write stays exactly where it was, in its own
  transaction: a failed run/event write can neither roll back nor delay it. Transient
  failures are retried with backoff; if the run+event transaction is refused for any
  other reason it is retried once without the event, because the run row matters more.
  A run whose start never made it is written in full when it finishes, and anything
  still RUNNING after a crash is settled by the startup reconciliation.
- Telemetry and non-run events go through IngestQueue/TelemetryWriter (bounded queue,
  separate pool, spill file), which never blocks the caller.

Timestamps: events derived from a robot message carry the robot's own timestamp, so a
replay of the same messages produces the same event ids (and ON CONFLICT drops them).
The heartbeat uses the dispatcher's receive time, because it is about hearing the robot.
"""

import asyncio
import collections
import dataclasses
import datetime
import functools
import json
import logging
import re
import uuid
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional, Tuple

from packages.events import causes, detectors
from packages.events.codes import EventCode
from packages.events.emit import Event, emit
from packages.events.schemas import RecordingLevel, RunOutcome
from packages.telemetry_ingest import (
    IngestQueue, RecordingPolicy, SpillFile, TelemetryWriter, create_pool, load_latest,
)
from packages.telemetry_ingest import tables
from packages.telemetry_ingest.rehydrate import LatestRow

logger = logging.getLogger("Isaac Mission Dispatch.fleet_recorder")

# Namespace of mission_runs.run_id = uuid5(RUN_NAMESPACE, "<mission>|<first run id>").
# Part of the stored data: changing it changes the ids of retried/backfilled runs.
RUN_NAMESPACE = uuid.UUID("3f6c2a8e-91d4-4b57-a0e3-5d7b9c1e2f48")

# failure_reason the dispatcher sets when a mission times out (server.py); the run's
# outcome is then TIMEOUT rather than FAILED.
MISSION_TIMEOUT_REASON = "Mission timed out"

DEFAULT_SPILL_PATH = "/tmp/mission_dispatch/fleet_events_spill.jsonl"
STATE_ROW_INTERVAL_S = 5.0
BATTERY_LOW_PCT = 20.0
BATTERY_OK_PCT = 25.0
SWEEP_PERIOD_S = 1.0
# Robot NOTIFYs arrive with every status write, so the policy is reloaded at most this
# often after one, and at least this often regardless (sites/settings are not watched).
POLICY_REFRESH_MIN_S = 5.0
POLICY_REFRESH_MAX_S = 60.0
REHYDRATE_TIMEOUT_S = 10.0
REHYDRATE_RETRY_S = 10.0
REHYDRATE_ATTEMPTS = 30
OP_CONNECT_TIMEOUT_S = 5.0
OP_RETRY_DELAYS_S = (1.0, 2.0, 5.0, 10.0, 30.0)
MAX_PENDING_OPS = 1000
# Trajectory rows logged this long after the run ended still belong to it (graph-builder
# logs waypoints asynchronously from the robot's node updates).
TRAJECTORY_GRACE_S = 5

# Key inside robot_latest.state_msg holding dispatch's own detector baselines (the rest of
# state_msg is the robot's last VDA5050 state message, as received).
DISPATCH_KEY = "_dispatch"
# information[] entries that carry the robot's build id, and a bare protocol version
# ("2.0.0"), which the VDA5050 header `version` field normally holds.
SW_VERSION_INFO_TYPES = ("swVersion", "buildId", "softwareVersion")
_PROTOCOL_VERSION = re.compile(r"^\d+(\.\d+){0,2}$")

MISSION_TABLE = "missionobjectv1"
ACTIVE = "RUNNING"

INSERT_RUN_SQL = (
    "INSERT INTO mission_runs (run_id, mission_name, robot_name, site_id, map_id, sw_version, "
    "recording_level, state, abort_cause, abort_detail, passes_completed, created_by, "
    "mission_tree, started_at, ended_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s::jsonb, %s, %s) "
    "ON CONFLICT (run_id) DO NOTHING"
)
FINISH_RUN_SQL = (
    "UPDATE mission_runs SET state = %s, ended_at = %s, abort_cause = %s, "
    "abort_detail = %s::jsonb, passes_completed = %s "
    "WHERE run_id = %s AND state = 'RUNNING'"
)
RUN_STATE_SQL = "SELECT state FROM mission_runs WHERE run_id = %s"
ACTIVE_RUN_SQL = (
    "SELECT run_id, started_at FROM mission_runs "
    "WHERE mission_name = %s AND robot_name = %s AND state = 'RUNNING' "
    "ORDER BY started_at DESC LIMIT 1"
)
ORPHAN_CANDIDATES_SQL = (
    "SELECT run_id, mission_name, robot_name, started_at, recording_level FROM mission_runs "
    "WHERE state = 'RUNNING' AND started_at < %s ORDER BY started_at"
)
MISSION_SQL = f"SELECT lifecycle, spec->>'robot', status FROM {MISSION_TABLE} WHERE name = %s"
TRAJECTORY_SQL = (
    "UPDATE mission_trajectory SET run_id = %s "
    "WHERE mission_id = %s AND robot_name = %s AND run_id IS NULL "
    "AND ts >= %s AND ts <= %s::timestamptz + make_interval(secs => %s)"
)


# --- small helpers -------------------------------------------------------------------------

def utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def to_utc(ts: Optional[datetime.datetime]) -> Optional[datetime.datetime]:
    """Aware UTC; naive datetimes (the dispatcher's own timestamps) are taken to be UTC."""
    if ts is None:
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=datetime.timezone.utc)
    return ts.astimezone(datetime.timezone.utc)


def parse_robot_ts(value: Any, fallback: datetime.datetime) -> datetime.datetime:
    """A robot message's ISO-8601 `timestamp`, or `fallback` if it is missing/unreadable."""
    if isinstance(value, datetime.datetime):
        return to_utc(value)
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            return to_utc(datetime.datetime.fromisoformat(text))
        except ValueError:
            pass
    return fallback


def run_uuid(mission_name: str, run_id: Optional[str],
             start_timestamp: Optional[datetime.datetime] = None) -> uuid.UUID:
    """mission_runs.run_id for a mission's dispatcher run id (legacy missions without one
    are keyed by their start time)."""
    key = run_id if run_id else f"legacy:{to_utc(start_timestamp).isoformat() if start_timestamp else ''}"
    return uuid.uuid5(RUN_NAMESPACE, f"{mission_name}|{key}")


def error_key(error: Any, idx: int) -> str:
    """Same keying as server.vda5050_errors_to_status_dict."""
    return error.errorType or f"error_{idx}"


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def sw_version_from_message(message: Any) -> Optional[str]:
    """The robot's build id from a state or factsheet message, if it reports one: an
    information[] entry (SW_VERSION_INFO_TYPES), else a header `version` that is not a
    bare VDA5050 protocol version."""
    for info in getattr(message, "information", None) or []:
        if info.infoType in SW_VERSION_INFO_TYPES and info.infoDescription.strip():
            return info.infoDescription.strip()
    version = (getattr(message, "version", None) or "").strip()
    if version and not _PROTOCOL_VERSION.match(version):
        return version
    return None


def _json_or_none(value: Any) -> Optional[str]:
    return None if value is None else json.dumps(value, sort_keys=True, default=str)


def _is_transient(exc: BaseException) -> bool:
    """Connection-level failures (retry) as opposed to the database refusing the data."""
    if isinstance(exc, (OSError, asyncio.TimeoutError)):
        return True
    try:
        import psycopg
        import psycopg_pool
    except ImportError:  # pragma: no cover - the dispatcher always has them
        return False
    return isinstance(exc, (psycopg.OperationalError, psycopg_pool.PoolTimeout))


def _guarded(fn):
    """Recording hooks must never raise into mission handling."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except Exception:  # noqa: BLE001
            self.hook_errors += 1
            logger.exception("Fleet recording hook %s failed; mission handling is unaffected",
                             fn.__name__)
            return None
    return wrapper


# --- per-robot detector state --------------------------------------------------------------

class _Track:
    """Detectors and last values for one robot. Rehydrated from robot_latest at startup;
    `live` once a message from this process has fed it (then rehydration leaves it alone)."""

    def __init__(self, robot_name: str, timeout_s: float):
        self.robot_name = robot_name
        self.live = False
        self.robot_state = detectors.StateDiff()
        self.online = detectors.StateDiff()
        self.connection_state: Optional[str] = None
        self.errors = detectors.SetDiff()
        self.error_info: Dict[str, Dict[str, Any]] = {}
        self.sw = detectors.StateDiff()
        self.battery = detectors.Hysteresis(BATTERY_LOW_PCT, BATTERY_OK_PCT,
                                            detectors.Direction.BELOW)
        self.heartbeat = detectors.Timeout(timeout_s)
        self.last_raw: Optional[Dict[str, Any]] = None
        self.last_row_ts: Optional[datetime.datetime] = None
        self.last_row_key: Optional[Tuple[Any, ...]] = None

    def set_timeout(self, timeout_s: float) -> None:
        if timeout_s <= 0 or self.heartbeat.timeout.total_seconds() == timeout_s:
            return
        self.heartbeat = detectors.Timeout(timeout_s, last_seen=self.heartbeat.last_seen,
                                           lost=self.heartbeat.lost)

    def seed(self, row: LatestRow) -> None:
        """Baselines from the last stored values, so a restart reports only real changes."""
        state_msg = dict(row.state_msg or {})
        own = state_msg.pop(DISPATCH_KEY, None) or {}
        if row.state_msg is not None:
            self.last_raw = state_msg
            if isinstance(state_msg.get("errors"), list):
                info = {}
                for idx, err in enumerate(state_msg["errors"]):
                    key = err.get("errorType") or f"error_{idx}"
                    info[key] = {"error_level": err.get("errorLevel"),
                                 "description": err.get("errorDescription")}
                self.errors = detectors.SetDiff(info.keys())
                self.error_info = info
        active = own.get("battery_low")
        if not isinstance(active, bool):
            charge = (state_msg.get("batteryState") or {}).get("batteryCharge")
            active = None
            if isinstance(charge, (int, float)):
                if charge <= BATTERY_LOW_PCT:
                    active = True
                elif charge >= BATTERY_OK_PCT:
                    active = False
        self.battery = detectors.Hysteresis(BATTERY_LOW_PCT, BATTERY_OK_PCT,
                                            detectors.Direction.BELOW, active=active)
        if own.get("robot_state"):
            self.robot_state = detectors.StateDiff(own["robot_state"])
        if isinstance(own.get("online"), bool):
            self.online = detectors.StateDiff(own["online"])
        self.connection_state = own.get("connection_state")
        if row.sw_version:
            self.sw = detectors.StateDiff(row.sw_version)
        self.heartbeat = detectors.Timeout(
            self.heartbeat.timeout.total_seconds(), last_seen=to_utc(row.last_seen),
            lost=bool(own.get("heartbeat_lost", False)))

    def dispatch_state(self) -> Dict[str, Any]:
        return {
            "robot_state": self.robot_state.value,
            "online": self.online.value,
            "connection_state": self.connection_state,
            "battery_low": self.battery.active,
            "heartbeat_lost": self.heartbeat.lost,
        }


@dataclasses.dataclass
class RunInfo:
    """The run a robot is executing. `resolved` is False while an adopted run (a mission
    resumed after a restart) is still being looked up in mission_runs."""
    run_id: uuid.UUID
    mission_name: str
    robot_name: str
    started_at: datetime.datetime
    recording_level: str
    map_id: Optional[str]
    site_id: Optional[str]
    sw_version: Optional[str]
    mission_tree: List[Any]
    resolved: bool = True


class _Context:
    """events.EventContext for dispatch."""

    def __init__(self, recorder: "FleetRecorder"):
        self._recorder = recorder

    def run_for(self, robot_name: str, ts: datetime.datetime) -> Optional[uuid.UUID]:
        run = self._recorder._runs.get(robot_name)
        return run.run_id if run is not None and run.resolved else None

    def site_for(self, robot_name: str, ts: datetime.datetime) -> Optional[str]:
        return self._recorder.policy.site_for(robot_name)

    def sw_version_for(self, robot_name: str, ts: datetime.datetime) -> Optional[str]:
        track = self._recorder._tracks.get(robot_name)
        return track.sw.value if track is not None else None


# --- run writes (background worker) --------------------------------------------------------

class _Op:
    attempts = 0
    with_events = True

    async def run(self, recorder: "FleetRecorder") -> None:
        raise NotImplementedError

    def describe(self) -> str:
        return type(self).__name__


@dataclasses.dataclass
class _Finish:
    outcome: RunOutcome
    cause: Optional[str]
    detail: Optional[Dict[str, Any]]
    passes_completed: int
    ended_at: datetime.datetime


class _StartRun(_Op):
    def __init__(self, run: RunInfo, adopt: bool):
        self.info = run
        self.adopt = adopt

    def describe(self) -> str:
        return f"start of run {self.info.run_id} ({self.info.mission_name})"

    async def run(self, recorder: "FleetRecorder") -> None:
        info = self.info
        async with recorder._pool.connection(timeout=OP_CONNECT_TIMEOUT_S) as conn:
            async with conn.transaction():
                if self.adopt:
                    async with conn.cursor() as cursor:
                        await cursor.execute(ACTIVE_RUN_SQL, (info.mission_name, info.robot_name))
                        row = await cursor.fetchone()
                    if row is not None:
                        info.run_id = uuid.UUID(str(row[0]))
                        info.started_at = to_utc(row[1])
                        info.resolved = True
                        logger.info("Resumed run %s of mission %s", info.run_id, info.mission_name)
                        recorder._latest_changed(info.robot_name)
                        return
                inserted = await recorder._insert_run(conn, info, None)
                if inserted and self.with_events:
                    await recorder._emit_run_started(conn, info)
        info.resolved = True
        recorder._latest_changed(info.robot_name)


class _FinishRun(_Op):
    def __init__(self, run: RunInfo, finish: _Finish):
        self.info = run
        self.finish = finish

    def describe(self) -> str:
        return f"finish of run {self.info.run_id} ({self.info.mission_name})"

    async def run(self, recorder: "FleetRecorder") -> None:
        async with recorder._pool.connection(timeout=OP_CONNECT_TIMEOUT_S) as conn:
            async with conn.transaction():
                await recorder._close_run(conn, self.info, self.finish, self.with_events)


class _Reconcile(_Op):
    """Startup: settle RUNNING runs left by a previous dispatcher process."""

    def __init__(self, started_before: datetime.datetime):
        self.started_before = started_before

    async def run(self, recorder: "FleetRecorder") -> None:
        async with recorder._pool.connection(timeout=OP_CONNECT_TIMEOUT_S) as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(ORPHAN_CANDIDATES_SQL, (self.started_before,))
                candidates = await cursor.fetchall()
            await conn.commit()
            for run_id, mission_name, robot_name, started_at, level in candidates:
                run_id = uuid.UUID(str(run_id))
                active = recorder._runs.get(robot_name)
                if active is not None and active.run_id == run_id:
                    continue
                async with conn.cursor() as cursor:
                    await cursor.execute(MISSION_SQL, (mission_name,))
                    mission = await cursor.fetchone()
                await conn.commit()
                finish = recorder._reconcile_decision(robot_name, mission)
                if finish is None:
                    logger.info("Run %s (%s) stays RUNNING: the dispatcher resumes its mission",
                                run_id, mission_name)
                    continue
                info = RunInfo(run_id=run_id, mission_name=mission_name, robot_name=robot_name,
                               started_at=to_utc(started_at), recording_level=level,
                               map_id=None, site_id=None, sw_version=None, mission_tree=[])
                async with conn.transaction():
                    await recorder._close_run(conn, info, finish, True)
                logger.warning("Closed run %s (%s) at startup as %s (%s)", run_id, mission_name,
                               finish.outcome.value, finish.cause)


# --- the recorder --------------------------------------------------------------------------

class FleetRecorder:
    """Owns dispatch's Phase 0 recording. Hooks are synchronous and never raise; call them
    on the event-loop thread (the dispatcher's robot loops and MQTT consumer already run
    there)."""

    def __init__(self, conninfo: Optional[str] = None, *,
                 spill_path: str = DEFAULT_SPILL_PATH,
                 pool: Any = None,
                 queue: Optional[IngestQueue] = None,
                 policy: Optional[RecordingPolicy] = None,
                 clock: Callable[[], datetime.datetime] = utcnow,
                 sleep: Callable[[float], Any] = asyncio.sleep,
                 start_writer: bool = True):
        self._conninfo = conninfo
        self._pool = pool
        self.policy = policy or RecordingPolicy()
        self.queue = queue or IngestQueue("dispatch", SpillFile(spill_path), policy=self.policy)
        self._clock = clock
        self._sleep = sleep
        self._start_writer = start_writer
        self._writer: Optional[TelemetryWriter] = None
        self._ctx = _Context(self)
        self._tracks: Dict[str, _Track] = {}
        self._runs: Dict[str, RunInfo] = {}
        self._latest_rows: Dict[str, LatestRow] = {}
        self._ops: Deque[_Op] = collections.deque()
        self._ops_wakeup: Optional[asyncio.Event] = None
        self._ops_lock = asyncio.Lock()
        self._tasks: List[asyncio.Task] = []
        self._started_at: Optional[datetime.datetime] = None
        self._policy_dirty = False
        self._policy_invalidated_at: Optional[datetime.datetime] = None
        self.hook_errors = 0
        self.op_failures = 0
        self.ops_dropped = 0

    # --- lifecycle -----------------------------------------------------------------------
    async def start(self) -> None:
        """Open the pool, rehydrate the detectors, queue the orphan reconciliation and start
        the writer, run worker and heartbeat sweep. Never raises and never blocks for long:
        with the database unreachable the dispatcher starts anyway and this keeps retrying."""
        self._started_at = self._clock()
        try:
            if self._pool is None:
                self._pool = await create_pool(self._conninfo, name="mission_dispatch_recorder")
        except Exception:  # noqa: BLE001
            logger.exception("Fleet recording pool could not be created; recording disabled")
            return
        if not await self.rehydrate():
            self._spawn(self._rehydrate_later(), "rehydrate")
        # Load the recording levels now, so runs started right after startup already get
        # theirs (otherwise the writer loads them on its first tick). Never raises.
        try:
            await asyncio.wait_for(self.policy.refresh(self._pool), REHYDRATE_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.warning("Recording levels not loaded yet; the writer keeps retrying")
        self._ops.appendleft(_Reconcile(self._started_at))
        if self._start_writer:
            self._writer = TelemetryWriter(self._pool, self.queue, policy=self.policy)
            self._writer.start()
        self._spawn(self._run_worker(), "runs")
        self._spawn(self._sweep_loop(), "heartbeat_sweep")

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except BaseException:  # noqa: BLE001
                pass
        self._tasks = []
        if self._writer is not None:
            await self._writer.stop()

    def _spawn(self, coro, name: str) -> None:
        self._tasks.append(asyncio.get_running_loop().create_task(
            coro, name=f"fleet_recorder.{name}"))

    def snapshot(self) -> Dict[str, Any]:
        return {"hook_errors": self.hook_errors, "op_failures": self.op_failures,
                "ops_dropped": self.ops_dropped, "ops_pending": len(self._ops),
                "robots": len(self._tracks), "active_runs": len(self._runs),
                "ingest": self.queue.metrics.snapshot()}

    # --- rehydration ---------------------------------------------------------------------
    async def rehydrate(self) -> bool:
        """Seed every robot not yet fed live data from robot_latest. False on failure."""
        try:
            rows = await asyncio.wait_for(load_latest(self._pool, raise_errors=True),
                                          REHYDRATE_TIMEOUT_S)
        except Exception:  # noqa: BLE001
            logger.warning("Could not load robot_latest; detectors start unseeded for now",
                           exc_info=True)
            return False
        for name, row in rows.items():
            self._latest_rows[name] = row
            track = self._tracks.get(name)
            if track is None:
                track = self._tracks[name] = _Track(name, 30.0)
            if not track.live:
                track.seed(row)
        logger.info("Rehydrated %d robots from robot_latest", len(rows))
        return True

    async def _rehydrate_later(self) -> None:
        for _ in range(REHYDRATE_ATTEMPTS):
            await self._sleep(REHYDRATE_RETRY_S)
            if await self.rehydrate():
                return

    def knows(self, robot_name: str) -> bool:
        return robot_name in self._tracks

    def _track(self, robot_name: str, robot_object: Any = None) -> _Track:
        track = self._tracks.get(robot_name)
        timeout_s = None
        if robot_object is not None:
            timeout_s = robot_object.heartbeat_timeout.total_seconds()
        if track is None:
            track = self._tracks[robot_name] = _Track(robot_name, timeout_s or 30.0)
        elif timeout_s:
            track.set_timeout(timeout_s)
        if robot_object is not None and track.robot_state.value is None:
            track.robot_state = detectors.StateDiff(_enum_value(robot_object.status.state))
        return track

    # --- robot hooks ---------------------------------------------------------------------
    @_guarded
    def on_robot_object(self, robot_object: Any) -> None:
        """A robot object from the watcher (spec/status change)."""
        self._track(robot_object.name, robot_object)
        self._policy_dirty = True

    @_guarded
    def on_state(self, robot_name: str, message: Any, robot_object: Any = None,
                 received_at: Optional[datetime.datetime] = None) -> None:
        """A VDA5050 state message, after the dispatcher has processed it."""
        now = received_at or self._clock()
        ts = parse_robot_ts(message.timestamp, now)
        track = self._track(robot_name, robot_object)
        track.live = True

        restored = track.heartbeat.seen(now)
        if restored is not None:
            self._event(EventCode.ROBOT_HEARTBEAT_RESTORED, robot_name, now,
                        {"last_seen": restored.last_seen, "gap_s": restored.gap_s})

        current = {error_key(e, i): e for i, e in enumerate(message.errors)}
        change = track.errors.update(current.keys())
        if change is not None:
            for key in sorted(change.added):
                err = current[key]
                self._event(EventCode.ROBOT_ERROR_RAISED, robot_name, ts,
                            {"error_type": key, "error_level": _enum_value(err.errorLevel),
                             "description": err.errorDescription}, discriminator=key)
            for key in sorted(change.removed):
                self._event(EventCode.ROBOT_ERROR_CLEARED, robot_name, ts,
                            {"error_type": key,
                             "description": track.error_info.get(key, {}).get("description")},
                            discriminator=key)
        track.error_info = {key: {"error_level": _enum_value(e.errorLevel),
                                  "description": e.errorDescription}
                            for key, e in current.items()}

        if message.batteryState is not None:
            charge = message.batteryState.batteryCharge
            transition = track.battery.update(charge)
            if transition is detectors.Transition.ENTERED:
                self._event(EventCode.BATTERY_LOW, robot_name, ts,
                            {"battery_percent": charge, "threshold": BATTERY_LOW_PCT})
            elif transition is detectors.Transition.EXITED:
                self._event(EventCode.BATTERY_OK, robot_name, ts,
                            {"battery_percent": charge, "threshold": BATTERY_OK_PCT})

        self._sw_version(track, sw_version_from_message(message), ts)
        track.last_raw = json.loads(message.json(by_alias=True, exclude_none=True))

        robot_state = (_enum_value(robot_object.status.state) if robot_object is not None
                       else track.robot_state.value)
        self._state_row(track, message, ts, robot_state)
        self._put_latest(track, last_seen=now)

    @_guarded
    def on_connection(self, robot_name: str, message: Any,
                      received_at: Optional[datetime.datetime] = None) -> None:
        """A VDA5050 `connection` message (usually retained, so redelivered on reconnect)."""
        now = received_at or self._clock()
        ts = parse_robot_ts(message.timestamp, now)
        track = self._track(robot_name)
        track.live = True
        state = _enum_value(message.connectionState)
        track.connection_state = state
        change = track.online.update(state == "ONLINE")
        if change is not None:
            code = EventCode.ROBOT_ONLINE if change.new else EventCode.ROBOT_OFFLINE
            self._event(code, robot_name, ts, {"connection_state": state})
        self._put_latest(track)

    @_guarded
    def on_factsheet(self, robot_name: str, message: Any) -> None:
        track = self._track(robot_name)
        now = self._clock()
        if self._sw_version(track, sw_version_from_message(message),
                            parse_robot_ts(message.timestamp, now)):
            self._put_latest(track)

    @_guarded
    def on_robot_state(self, robot_name: str, old: Any, new: Any,
                       ts: Optional[datetime.datetime] = None) -> None:
        """The dispatcher changed the robot's RobotStateV1."""
        track = self._track(robot_name)
        if track.robot_state.value is None and old is not None:
            track.robot_state = detectors.StateDiff(_enum_value(old))
        change = track.robot_state.update(_enum_value(new))
        if change is None:
            return
        self._event(EventCode.ROBOT_STATE_CHANGED, robot_name, ts or self._clock(),
                    {"old": change.old, "new": change.new},
                    discriminator=f"{change.old}->{change.new}")
        self._put_latest(track)

    def _sw_version(self, track: _Track, version: Optional[str],
                    ts: datetime.datetime) -> bool:
        if not version:
            return False
        change = track.sw.update(version)
        if change is not None:
            self._event(EventCode.ROBOT_SW_VERSION_CHANGED, track.robot_name, ts,
                        {"old": change.old, "new": change.new})
        return change is not None or track.sw.value == version

    def _state_row(self, track: _Track, message: Any, ts: datetime.datetime,
                   robot_state: Optional[str]) -> None:
        key = (robot_state, message.orderId, track.errors.members)
        last = track.last_row_ts
        due = (last is None or ts < last or key != track.last_row_key or
               (ts - last).total_seconds() >= STATE_ROW_INTERVAL_S)
        if not due:
            return
        track.last_row_ts, track.last_row_key = ts, key
        pos = message.agvPosition
        self.queue.put_state({
            "ts": ts, "robot_name": track.robot_name,
            "run_id": self._ctx.run_for(track.robot_name, ts),
            "x": pos.x if pos else None, "y": pos.y if pos else None,
            "yaw": pos.theta if pos else None, "map_id": pos.mapId if pos else None,
            "battery": message.batteryState.batteryCharge if message.batteryState else None,
            "state": robot_state, "order_id": message.orderId or None,
            "last_node": message.lastNodeId or None, "driving": message.driving,
        })

    def _put_latest(self, track: _Track, **fields: Any) -> None:
        state_msg = dict(track.last_raw or {})
        state_msg[DISPATCH_KEY] = track.dispatch_state()
        fields.setdefault("active_run_id", self._ctx.run_for(track.robot_name, None))
        if track.sw.value is not None:
            fields.setdefault("sw_version", track.sw.value)
        site = self.policy.site_for(track.robot_name)
        if site is not None:
            fields.setdefault("site_id", site)
        self.queue.put_latest(track.robot_name, state_msg=state_msg, **fields)

    def _latest_changed(self, robot_name: str) -> None:
        try:
            track = self._tracks.get(robot_name)
            if track is not None:
                self._put_latest(track)
        except Exception:  # noqa: BLE001
            logger.exception("robot_latest update for %s failed", robot_name)

    def _event(self, code: EventCode, robot_name: str, ts: datetime.datetime,
               payload: Dict[str, Any], discriminator: Optional[str] = None) -> None:
        self.queue.put_event(Event(code, ts, robot_name=robot_name, payload=payload,
                                   discriminator=discriminator), self._ctx)

    # --- heartbeat sweep -----------------------------------------------------------------
    @_guarded
    def sweep(self, now: Optional[datetime.datetime] = None) -> None:
        """One heartbeat check of every robot (1 Hz). Right after startup each robot gets
        one heartbeat timeout of grace, so the dispatcher's own downtime is not reported
        as the robots going silent."""
        now = now or self._clock()
        for track in list(self._tracks.values()):
            if self._started_at is not None and now - self._started_at < track.heartbeat.timeout:
                continue
            lost = track.heartbeat.check(now)
            if lost is not None:
                timeout = track.heartbeat.timeout
                self._event(EventCode.ROBOT_HEARTBEAT_LOST, track.robot_name,
                            lost.last_seen + timeout,
                            {"last_seen": lost.last_seen, "timeout_s": timeout.total_seconds()})
                self._put_latest(track)
        self._maybe_invalidate_policy(now)

    def _maybe_invalidate_policy(self, now: datetime.datetime) -> None:
        last = self._policy_invalidated_at
        elapsed = None if last is None else (now - last).total_seconds()
        if (self._policy_dirty and (elapsed is None or elapsed >= POLICY_REFRESH_MIN_S)) or \
                (elapsed is not None and elapsed >= POLICY_REFRESH_MAX_S):
            self.policy.invalidate()
            self._policy_dirty = False
            self._policy_invalidated_at = now
        elif last is None:
            self._policy_invalidated_at = now

    async def _sweep_loop(self) -> None:
        while True:
            self.sweep()
            await self._sleep(SWEEP_PERIOD_S)

    # --- mission hooks -------------------------------------------------------------------
    @_guarded
    def run_started(self, robot_name: str, mission: Any, robot_object: Any = None) -> None:
        """The dispatcher is about to send the first order of `mission` (a fresh one, or one
        resumed after a restart, which adopts its existing run)."""
        current = self._runs.get(robot_name)
        if current is not None and current.mission_name == mission.name:
            return
        status = mission.status
        fresh = status.start_timestamp is None and status.passes_completed == 0
        now = self._clock()
        track = self._track(robot_name, robot_object)
        map_id = None
        if robot_object is not None:
            map_id = robot_object.current_map or robot_object.status.pose.map_id or None
        run = RunInfo(
            run_id=run_uuid(mission.name, status.run_id, status.start_timestamp),
            mission_name=mission.name, robot_name=robot_name,
            started_at=now if fresh else (to_utc(status.start_timestamp) or now),
            recording_level=self.policy.level_for(robot_name).value,
            map_id=map_id, site_id=self.policy.site_for(robot_name),
            sw_version=track.sw.value,
            mission_tree=[json.loads(node.json()) for node in mission.mission_tree],
            resolved=fresh)
        self._runs[robot_name] = run
        self._submit(_StartRun(run, adopt=not fresh))

    @_guarded
    def run_finished(self, robot_name: str, mission: Any, robot_object: Any = None) -> None:
        """The dispatcher is done with `mission` (after the last pass)."""
        run = self._runs.get(robot_name)
        if run is None or run.mission_name != mission.name:
            return
        del self._runs[robot_name]
        status = mission.status
        outcome = outcome_for(status)
        errors = dict(robot_object.status.errors) if robot_object is not None else {}
        nav = robot_object.status.nav_reasoning if robot_object is not None else None
        track = self._tracks.get(robot_name)
        finish = _Finish(
            outcome=outcome,
            cause=None if outcome is RunOutcome.COMPLETED else causes.classify(
                outcome.value, errors,
                " | ".join(t for t in (nav, status.failure_reason) if t),
                heartbeat_lost=track is not None and track.heartbeat.lost),
            detail=None if outcome is RunOutcome.COMPLETED else {
                "mission_state": _enum_value(status.state),
                "failure_reason": status.failure_reason,
                "failure_category": _enum_value(status.failure_category),
                "errors": errors, "nav_reasoning": nav},
            passes_completed=status.passes_completed,
            ended_at=self._clock())
        self._submit(_FinishRun(run, finish))
        if track is not None:
            self._put_latest(track, active_run_id=None)

    @_guarded
    def node_failed(self, robot_name: str, mission: Any, node_name: str,
                    ts: Optional[datetime.datetime] = None) -> None:
        node = next((n for n in mission.mission_tree if str(n.name) == node_name), None)
        node_status = mission.status.node_status.get(node_name)
        self._event(EventCode.MISSION_NODE_FAILED, robot_name, ts or self._clock(), {
            "mission_name": mission.name, "node_id": node_name,
            "node_type": _enum_value(node.type) if node is not None else None,
            "detail": (node_status.error_msg if node_status is not None else None)
            or mission.status.failure_reason,
        }, discriminator=f"{mission.name}|{mission.status.run_id}|{node_name}")

    @_guarded
    def edge_blocked(self, robot_name: str, mission: Any,
                     ts: Optional[datetime.datetime] = None) -> None:
        status = mission.status
        self._event(EventCode.MISSION_EDGE_BLOCKED, robot_name, ts or self._clock(), {
            "mission_name": mission.name, "edge_id": status.blocked_edge,
            "detail": status.block_reason,
        }, discriminator=f"{mission.name}|{status.run_id}|{status.blocked_node}|"
                         f"{status.blocked_edge}")

    @_guarded
    def rerouted(self, robot_name: str, mission: Any, blocked_node: Optional[str],
                 blocked_edge: Optional[str], ts: Optional[datetime.datetime] = None) -> None:
        self._event(EventCode.MISSION_REROUTED, robot_name, ts or self._clock(), {
            "mission_name": mission.name,
            "blocked_edges": [blocked_edge] if blocked_edge else [],
            "detail": f"block on node {blocked_node} cleared; mission resumed",
        }, discriminator=f"{mission.name}|{mission.status.run_id}")

    # --- run worker ----------------------------------------------------------------------
    def _submit(self, op: _Op) -> None:
        if len(self._ops) >= MAX_PENDING_OPS:
            self.ops_dropped += 1
            logger.error("Run write queue full; dropping %s", op.describe())
            return
        self._ops.append(op)
        if self._ops_wakeup is not None:
            self._ops_wakeup.set()

    async def _run_worker(self) -> None:
        self._ops_wakeup = asyncio.Event()
        while True:
            self._ops_wakeup.clear()
            await self.run_pending_ops()
            await self._ops_wakeup.wait()

    async def run_pending_ops(self) -> None:
        """Run queued run writes in order. A transient failure retries the head op with
        backoff (holding the ones behind it, to keep start before finish); anything else
        drops it after one retry without events."""
        async with self._ops_lock:
            await self._run_pending_ops()

    async def _run_pending_ops(self) -> None:
        while self._ops:
            op = self._ops[0]
            if self._pool is None:
                return
            try:
                await op.run(self)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                if _is_transient(exc) and op.attempts < len(OP_RETRY_DELAYS_S):
                    delay = OP_RETRY_DELAYS_S[op.attempts]
                    op.attempts += 1
                    logger.warning("Run write (%s) failed: %s; retry %d in %ss",
                                   op.describe(), exc, op.attempts, delay)
                    await self._sleep(delay)
                    continue
                if not _is_transient(exc) and op.with_events and \
                        isinstance(op, (_StartRun, _FinishRun)):
                    logger.exception("Run write (%s) refused; retrying without its event",
                                     op.describe())
                    op.with_events = False
                    continue
                self.op_failures += 1
                logger.exception("Giving up on run write (%s)", op.describe())
            self._ops.popleft()

    # --- SQL used by the ops -------------------------------------------------------------
    def _events_allowed(self, robot_name: str, code: EventCode) -> bool:
        return self.policy.allows(tables.EVENTS_TABLE, robot_name, code.value)

    async def _insert_run(self, conn: Any, info: RunInfo, finish: Optional[_Finish]) -> bool:
        params = (
            info.run_id, info.mission_name, info.robot_name, info.site_id, info.map_id,
            info.sw_version, info.recording_level,
            finish.outcome.value if finish else ACTIVE,
            finish.cause if finish else None,
            _json_or_none(finish.detail) if finish else None,
            finish.passes_completed if finish else 0,
            None, json.dumps(info.mission_tree, default=str), info.started_at,
            finish.ended_at if finish else None,
        )
        async with conn.cursor() as cursor:
            await cursor.execute(INSERT_RUN_SQL, params)
            return cursor.rowcount == 1

    async def _emit_run_started(self, conn: Any, info: RunInfo) -> None:
        if self._events_allowed(info.robot_name, EventCode.MISSION_RUN_STARTED):
            await emit(conn, Event(
                EventCode.MISSION_RUN_STARTED, info.started_at, robot_name=info.robot_name,
                run_id=info.run_id, site_id=info.site_id, sw_version=info.sw_version,
                discriminator=str(info.run_id),
                payload={"mission_name": info.mission_name, "map_id": info.map_id,
                         "recording_level": info.recording_level}))

    async def _close_run(self, conn: Any, info: RunInfo, finish: _Finish,
                         with_events: bool) -> None:
        """UPDATE the run to its terminal state + RUN_FINISHED, on `conn` inside the caller's
        transaction. A run whose start was never written is inserted whole."""
        async with conn.cursor() as cursor:
            await cursor.execute(FINISH_RUN_SQL, (
                finish.outcome.value, finish.ended_at, finish.cause,
                _json_or_none(finish.detail), finish.passes_completed, info.run_id))
            updated = cursor.rowcount == 1
            if not updated:
                await cursor.execute(RUN_STATE_SQL, (info.run_id,))
                existing = await cursor.fetchone()
                if existing is not None:
                    logger.info("Run %s is already %s; not finishing it again",
                                info.run_id, existing[0])
                    return
        if not updated:
            await self._insert_run(conn, info, finish)
            if with_events:
                await self._emit_run_started(conn, info)
        if with_events and self._events_allowed(info.robot_name, EventCode.MISSION_RUN_FINISHED):
            await emit(conn, Event(
                EventCode.MISSION_RUN_FINISHED, finish.ended_at, robot_name=info.robot_name,
                run_id=info.run_id, site_id=info.site_id, sw_version=info.sw_version,
                discriminator=str(info.run_id),
                payload={"mission_name": info.mission_name, "outcome": finish.outcome.value,
                         "cause": finish.cause, "passes_completed": finish.passes_completed,
                         "duration_s": max(0.0, (finish.ended_at - info.started_at)
                                           .total_seconds())}))
        # mission_trajectory is written by graph-builder, keyed by mission name; tag this
        # run's rows. A savepoint, so a problem here never costs the run row.
        try:
            async with conn.transaction():
                async with conn.cursor() as cursor:
                    await cursor.execute(TRAJECTORY_SQL, (
                        info.run_id, info.mission_name, info.robot_name, info.started_at,
                        finish.ended_at, TRAJECTORY_GRACE_S))
        except Exception as exc:  # noqa: BLE001
            if _is_transient(exc) or getattr(conn, "broken", False):
                raise
            logger.exception("Could not tag mission_trajectory rows with run %s", info.run_id)

    def _reconcile_decision(self, robot_name: str, mission_row: Any) -> Optional[_Finish]:
        """None if the dispatcher will resume this run's mission (an ALIVE, unfinished
        mission of the same robot: it resends the order and adopts the run); otherwise how
        to close the run. A mission that did finish keeps its real outcome; one that is
        gone, deleted or moved is an orphan: ABORTED with DISPATCH.ORPHANED."""
        now = self._clock()
        latest = self._latest_rows.get(robot_name)
        robot_order = ((latest.state_msg or {}).get("orderId") if latest is not None else None)
        if mission_row is None:
            return _orphan("mission_missing", None, robot_order, 0, now)
        lifecycle, mission_robot, status = mission_row
        if isinstance(status, str):
            status = json.loads(status)
        status = status or {}
        state = status.get("state")
        passes = int(status.get("passes_completed") or 0)
        done = state in ("COMPLETED", "FAILED", "CANCELED")
        if lifecycle == "ALIVE" and not done and mission_robot == robot_name:
            return None
        if not done:
            reason = "mission_moved" if mission_robot != robot_name else "mission_deleted"
            return _orphan(reason, state, robot_order, passes, now)
        failure_reason = status.get("failure_reason")
        outcome = _outcome(state, failure_reason)
        ended = status.get("end_timestamp")
        ended_at = parse_robot_ts(ended, now)
        return _Finish(
            outcome=outcome,
            cause=None if outcome is RunOutcome.COMPLETED else causes.classify(
                outcome.value, (), failure_reason),
            detail=None if outcome is RunOutcome.COMPLETED else {
                "mission_state": state, "failure_reason": failure_reason,
                "reconciled_at_startup": True},
            passes_completed=passes, ended_at=ended_at)


def _orphan(reason: str, mission_state: Optional[str], robot_order: Optional[str],
            passes: int, now: datetime.datetime) -> _Finish:
    return _Finish(outcome=RunOutcome.ABORTED, cause="DISPATCH.ORPHANED",
                   detail={"reason": reason, "mission_state": mission_state,
                           "robot_order_id": robot_order},
                   passes_completed=passes, ended_at=now)


def _outcome(state: Any, failure_reason: Optional[str]) -> RunOutcome:
    state = _enum_value(state)
    if state == "COMPLETED":
        return RunOutcome.COMPLETED
    if state == "CANCELED":
        return RunOutcome.CANCELED
    if state == "FAILED":
        return RunOutcome.TIMEOUT if failure_reason == MISSION_TIMEOUT_REASON \
            else RunOutcome.FAILED
    return RunOutcome.ABORTED


def outcome_for(status: Any) -> RunOutcome:
    """mission_runs terminal state for a mission status the dispatcher is done with."""
    return _outcome(status.state, status.failure_reason)
