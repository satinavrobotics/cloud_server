"""Phase 0 ingest inside the API (docs/satinav-fleet-agent-phase0-v2.md §5.3 "api" items 2-4).

The API may run several uvicorn workers, and every worker has its own MQTT subscription to
`+/diagnostics` and `+/nav_supervisor`. Only one of them may write, so:

- `WriterElection`: each worker keeps one dedicated autocommit connection and retries
  `pg_try_advisory_lock(<'telemetry_writer'>)` on it every TELEMETRY_ELECTION_RETRY_S. The
  winner re-checks that connection every TELEMETRY_ELECTION_CHECK_S; if the check fails it
  stops writing at once and closes the connection. The lock is session-level, so it is
  released when the holder's connection ends for any reason (process killed, crash, network)
  and another worker takes it on its next retry. The key is derived like the migrations lock
  (packages/api/entrypoint.py: first 8 bytes of SHA-256, signed).
- `ApiTelemetry`: while this worker is the writer it owns one "term": a RecordingPolicy, an
  IngestQueue(host "api"), a TelemetryWriter on its own 2-connection pool, and the per-robot
  detectors of packages/api/telemetry_detectors.py, rehydrated from robot_latest when the term
  starts. Non-writers do nothing beyond the existing in-memory caches in diagnostics.py.

Failure isolation: nothing here raises into the MQTT handlers or the lifespan. Every error is
caught, counted and logged; if Postgres is unreachable the election just keeps retrying. Events
get deterministic IDs from the robot's own timestamp, so an overlap between an old and a new
writer (the old one has not noticed yet that its lock connection died) cannot duplicate events;
at worst a second or so of diagnostics_ts rows can be written twice.
"""

import asyncio
import datetime
import glob
import logging
import os
import time
import uuid
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional

from packages.api.entrypoint import advisory_lock_key
from packages.api.telemetry_detectors import (
    DEFAULT_THERMAL_HIGH_C, DEFAULT_THERMAL_OK_C, DiagnosticsDetector, NavSupervisorDetector,
    robot_ts, stamp_ts,
)
from packages.events.codes import Source
from packages.telemetry_ingest import (
    IngestQueue, LatestRow, Metrics, RecordingPolicy, SpillFile, TelemetryWriter, create_pool,
    load_latest,
)
from packages.telemetry_ingest.queue import decode_event

logger = logging.getLogger("ApiDelegationService.telemetry")

WRITER_LOCK_NAME = "telemetry_writer"
WRITER_LOCK_KEY = advisory_lock_key(WRITER_LOCK_NAME)

DEFAULT_RETRY_S = 5.0
DEFAULT_CHECK_S = 2.0
CONNECT_TIMEOUT_S = 5.0
QUERY_TIMEOUT_S = 5.0
WRITER_STOP_TIMEOUT_S = 3.0
# While writing: reload the dispatch-owned robot_latest columns used as event context, and
# mark the recording policy stale so the writer reloads it (until WP8 wires NOTIFYs to it).
CONTEXT_REFRESH_S = 10.0
POLICY_REFRESH_S = 30.0

SPILL_PREFIX = "api-"
SPILL_SUFFIX = ".jsonl"

Connect = Callable[[], Awaitable[Any]]


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class _Throttled:
    """Log the first few errors of a kind, then every 100th, so a bug at 10 Hz can't flood."""

    def __init__(self):
        self.count = 0

    def log(self, message: str, *args) -> None:
        self.count += 1
        if self.count <= 5 or self.count % 100 == 0:
            logger.exception(message + " (error #%d)", *args, self.count)


# --- election ------------------------------------------------------------------------------

class WriterElection:
    """Session-level advisory lock election on a dedicated connection.

    `on_acquired(conn)` runs right after the lock is taken (it may use `conn` for reads);
    if it raises, the lock connection is closed (releasing the lock) and the next retry
    starts over. `on_lost()` runs when a held lock is given up for any reason.
    """

    def __init__(self, connect: Connect, on_acquired: Callable[[Any], Awaitable[None]],
                 on_lost: Callable[[], Awaitable[None]], *, key: int = WRITER_LOCK_KEY,
                 retry_s: float = DEFAULT_RETRY_S, check_s: float = DEFAULT_CHECK_S,
                 on_tick: Optional[Callable[[Any], Awaitable[None]]] = None,
                 sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
                 query_timeout_s: float = QUERY_TIMEOUT_S):
        self._connect = connect
        self._on_acquired = on_acquired
        self._on_lost = on_lost
        self._on_tick = on_tick
        self.key = key
        self._retry_s = retry_s
        self._check_s = check_s
        self._sleep = sleep
        self._timeout = query_timeout_s
        self._conn: Any = None
        self._task: Optional[asyncio.Task] = None
        self.holding = False
        self.acquisitions = 0
        self.losses = 0
        self.errors = 0

    def start(self) -> asyncio.Task:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(
                self._run(), name="api.telemetry.election")
        return self._task

    async def stop(self) -> None:
        """Stop electing, give up the lock and close the connection. Never raises."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except BaseException:  # noqa: BLE001 - CancelledError or a loop bug; both done
                pass
        await self._release()

    async def _run(self) -> None:
        while True:
            try:
                await self.step()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - never ends except through stop()
                self.errors += 1
                logger.exception("Telemetry writer election step failed")
            await self._sleep(self._check_s if self.holding else self._retry_s)

    async def step(self) -> None:
        """One election round: try to acquire, or verify the lock is still held."""
        if self.holding:
            if not await self._still_holding():
                logger.warning("Lost the telemetry writer lock connection; stopping writes")
                await self._release()
                return
            if self._on_tick is not None:
                try:
                    await self._on_tick(self._conn)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    self.errors += 1
                    logger.exception("Telemetry writer periodic refresh failed")
            return
        if not await self._try_acquire():
            return
        self.holding = True
        self.acquisitions += 1
        logger.info("This worker (pid %d) is the telemetry writer (lock key %d)",
                    os.getpid(), self.key)
        try:
            await self._on_acquired(self._conn)
        except asyncio.CancelledError:
            await self._release()
            raise
        except Exception:  # noqa: BLE001
            self.errors += 1
            logger.exception("Starting the telemetry writer failed; releasing the lock")
            await self._release()

    async def _query(self, sql: str, params: Any = None) -> Any:
        async def run():
            cursor = await self._conn.execute(sql, params)
            return await cursor.fetchone()
        return await asyncio.wait_for(run(), self._timeout)

    async def _try_acquire(self) -> bool:
        if self._conn is None or getattr(self._conn, "closed", False):
            self._conn = None
            try:
                self._conn = await asyncio.wait_for(self._connect(), CONNECT_TIMEOUT_S)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - Postgres down: retry later
                self.errors += 1
                logger.warning("Telemetry writer election: cannot connect to Postgres (%s)",
                               str(exc).strip() or type(exc).__name__)
                return False
        try:
            row = await self._query("SELECT pg_try_advisory_lock(%s)", (self.key,))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            self.errors += 1
            logger.exception("pg_try_advisory_lock failed; reconnecting next round")
            await self._close()
            return False
        return bool(row and row[0])

    async def _still_holding(self) -> bool:
        if self._conn is None or getattr(self._conn, "closed", False):
            return False
        try:
            await self._query("SELECT 1")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - timeout or broken connection
            return False
        return True

    async def _release(self) -> None:
        """Stop the term (if any), then close the connection, which frees the lock."""
        if self.holding:
            self.holding = False
            self.losses += 1
            try:
                await self._on_lost()
            except BaseException:  # noqa: BLE001 - must still close the connection
                logger.exception("Stopping the telemetry writer failed")
        await self._close()

    async def _close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            await asyncio.wait_for(conn.close(), CONNECT_TIMEOUT_S)
        except BaseException:  # noqa: BLE001 - closing a dead connection may fail; it is gone
            pass


# --- event context -------------------------------------------------------------------------

class LatestContext:
    """events.EventContext from the dispatch-owned robot_latest columns (refreshed every
    CONTEXT_REFRESH_S). It returns the robot's current run/site/version, not the ones at `ts`."""

    def __init__(self, rows: Optional[Mapping[str, LatestRow]] = None):
        self._rows: Dict[str, LatestRow] = dict(rows or {})

    def update(self, rows: Mapping[str, LatestRow]) -> None:
        self._rows = dict(rows)

    def run_for(self, robot_name: str, ts: datetime.datetime) -> Optional[uuid.UUID]:
        row = self._rows.get(robot_name)
        return row.active_run_id if row is not None else None

    def site_for(self, robot_name: str, ts: datetime.datetime) -> Optional[str]:
        row = self._rows.get(robot_name)
        return row.site_id if row is not None else None

    def sw_version_for(self, robot_name: str, ts: datetime.datetime) -> Optional[str]:
        row = self._rows.get(robot_name)
        return row.sw_version if row is not None else None


# --- spill files ---------------------------------------------------------------------------

def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def adopt_orphan_spills(spill: SpillFile, directory: str, own_pid: int) -> int:
    """Move events from spill files of dead workers (api-<pid>.jsonl) into `spill`, so the new
    writer replays them. Returns the number of events adopted. Never raises."""
    adopted = 0
    for path in glob.glob(os.path.join(directory, f"{SPILL_PREFIX}*{SPILL_SUFFIX}")):
        name = os.path.basename(path)[len(SPILL_PREFIX):-len(SPILL_SUFFIX)]
        try:
            pid = int(name)
        except ValueError:
            continue
        if pid == own_pid or _pid_alive(pid):
            continue
        try:
            rows = []
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        rows.append(decode_event(line))
                    except (ValueError, KeyError, TypeError):
                        logger.error("Dropping corrupt line in orphaned spill %s", path)
            spill.append(rows)
            os.unlink(path)
            adopted += len(rows)
        except OSError:
            logger.exception("Could not adopt orphaned spill file %s", path)
    if adopted:
        logger.info("Adopted %d spilled events from dead workers", adopted)
    return adopted


# --- the writer term -----------------------------------------------------------------------

class _Term:
    """Everything that exists only while this worker is the writer."""

    def __init__(self, queue: IngestQueue, writer: TelemetryWriter, pool: Any,
                 policy: RecordingPolicy, ctx: LatestContext,
                 latest: Mapping[str, LatestRow], high_c: float, ok_c: float):
        self.queue = queue
        self.writer = writer
        self.pool = pool
        self.policy = policy
        self.ctx = ctx
        self._latest = dict(latest)
        self._high_c = high_c
        self._ok_c = ok_c
        self.diagnostics: Dict[str, DiagnosticsDetector] = {}
        self.nav: Dict[str, NavSupervisorDetector] = {}

    def diagnostics_detector(self, robot: str) -> DiagnosticsDetector:
        detector = self.diagnostics.get(robot)
        if detector is None:
            row = self._latest.get(robot)
            detector = DiagnosticsDetector.from_latest(
                robot, row.diagnostics if row is not None else None,
                high_c=self._high_c, ok_c=self._ok_c)
            self.diagnostics[robot] = detector
        return detector

    def nav_detector(self, robot: str) -> NavSupervisorDetector:
        detector = self.nav.get(robot)
        if detector is None:
            row = self._latest.get(robot)
            detector = NavSupervisorDetector.from_latest(
                robot, row.nav_supervisor if row is not None else None)
            self.nav[robot] = detector
        return detector

    async def stop(self) -> None:
        try:
            await self.writer.stop(timeout_s=WRITER_STOP_TIMEOUT_S)
        except BaseException:  # noqa: BLE001
            logger.exception("Telemetry writer stop failed")
        try:
            await asyncio.wait_for(self.pool.close(), WRITER_STOP_TIMEOUT_S)
        except BaseException:  # noqa: BLE001
            logger.exception("Closing the telemetry pool failed")


class ApiTelemetry:
    """The API's Phase 0 ingest: election plus, while elected, the writer term.

    `on_diagnostics` / `on_nav_supervisor` are called on the event-loop thread by
    DiagnosticsService; they never raise and never block.
    """

    def __init__(self, conninfo: str, spill_dir: str, *,
                 retry_s: float = DEFAULT_RETRY_S, check_s: float = DEFAULT_CHECK_S,
                 high_c: float = DEFAULT_THERMAL_HIGH_C, ok_c: float = DEFAULT_THERMAL_OK_C,
                 connect: Optional[Connect] = None,
                 pool_factory: Optional[Callable[[str], Awaitable[Any]]] = None,
                 now: Callable[[], datetime.datetime] = _utcnow,
                 monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
                 writer_kwargs: Optional[Dict[str, Any]] = None):
        self._conninfo = conninfo
        self._spill_dir = spill_dir
        self._high_c = high_c
        self._ok_c = ok_c
        self._pool_factory = pool_factory or (lambda dsn: create_pool(dsn, name="api_telemetry"))
        self._now = now
        self._monotonic = monotonic
        self._writer_kwargs = dict(writer_kwargs or {})
        self.metrics = Metrics()
        self._spill: Optional[SpillFile] = None
        self._term: Optional[_Term] = None
        self._last_context_refresh = 0.0
        self._last_policy_refresh = 0.0
        self.handler_errors = _Throttled()
        self.election = WriterElection(
            connect or self._default_connect, self._start_term, self._stop_term,
            retry_s=retry_s, check_s=check_s, on_tick=self._tick, sleep=sleep)

    # --- lifecycle -----------------------------------------------------------------------
    async def _default_connect(self) -> Any:
        import psycopg
        return await psycopg.AsyncConnection.connect(
            self._conninfo, autocommit=True, connect_timeout=int(CONNECT_TIMEOUT_S),
            application_name=f"api-telemetry-election-{os.getpid()}")

    def start(self) -> None:
        """Start the election task on the running loop. Never raises."""
        try:
            self.election.start()
        except Exception:  # noqa: BLE001
            logger.exception("Could not start the telemetry writer election")

    async def stop(self) -> None:
        """Stop writing (final flush, bounded) and give up the lock. Never raises."""
        try:
            await self.election.stop()
        except BaseException:  # noqa: BLE001
            logger.exception("Stopping telemetry ingest failed")

    @property
    def is_writer(self) -> bool:
        return self._term is not None

    def snapshot(self) -> Dict[str, Any]:
        return {
            "pid": os.getpid(),
            "is_writer": self.is_writer,
            "lock_key": self.election.key,
            "acquisitions": self.election.acquisitions,
            "losses": self.election.losses,
            "election_errors": self.election.errors,
            "handler_errors": self.handler_errors.count,
            "ingest": self.metrics.snapshot(),
        }

    def _spill_file(self) -> SpillFile:
        if self._spill is None:
            path = os.path.join(self._spill_dir, f"{SPILL_PREFIX}{os.getpid()}{SPILL_SUFFIX}")
            self._spill = SpillFile(path, self.metrics)
        return self._spill

    async def _start_term(self, conn: Any) -> None:
        policy = RecordingPolicy()
        await policy.refresh(conn)  # never raises; unloaded -> default level until next refresh
        latest = await load_latest(conn)  # never raises; {} -> detectors start unseeded
        spill = self._spill_file()
        adopt_orphan_spills(spill, self._spill_dir, os.getpid())
        queue = IngestQueue(Source.API, spill, metrics=self.metrics, policy=policy)
        pool = await self._pool_factory(self._conninfo)
        try:
            writer = TelemetryWriter(pool, queue, policy=policy, metrics=self.metrics,
                                     **self._writer_kwargs)
            term = _Term(queue, writer, pool, policy, LatestContext(latest), latest,
                         self._high_c, self._ok_c)
            writer.start()
        except BaseException:
            await asyncio.wait_for(pool.close(), WRITER_STOP_TIMEOUT_S)
            raise
        now = self._monotonic()
        self._last_context_refresh = self._last_policy_refresh = now
        self._term = term
        logger.info("Telemetry writer started (%d robot_latest rows rehydrated)", len(latest))

    async def _stop_term(self) -> None:
        term, self._term = self._term, None
        if term is not None:
            await term.stop()
            logger.info("Telemetry writer stopped")

    async def _tick(self, conn: Any) -> None:
        term = self._term
        if term is None:
            return
        now = self._monotonic()
        if now - self._last_policy_refresh >= POLICY_REFRESH_S:
            self._last_policy_refresh = now
            term.policy.invalidate()
        if now - self._last_context_refresh >= CONTEXT_REFRESH_S:
            self._last_context_refresh = now
            rows = await load_latest(conn)
            if rows:
                term.ctx.update(rows)

    # --- MQTT handlers (event-loop thread) -----------------------------------------------
    def on_diagnostics(self, robot_name: str, robot_timestamp: Any,
                       diagnostics: Mapping[str, Any]) -> None:
        term = self._term
        if term is None:
            return
        try:
            ts = robot_ts(robot_timestamp, self._now())
            result = term.diagnostics_detector(robot_name).update(ts, diagnostics)
            for event in result.events:
                term.queue.put_event(event, term.ctx)
            term.queue.put_diagnostics(result.row)
            term.queue.put_latest(robot_name, diagnostics=result.latest)
        except Exception:  # noqa: BLE001 - never break the diagnostics cache/broadcast
            self.handler_errors.log("Telemetry ingest failed for %s/diagnostics", robot_name)

    def on_nav_supervisor(self, robot_name: str, supervisor: Any) -> None:
        term = self._term
        if term is None:
            return
        try:
            stamp = supervisor.get("stamp") if isinstance(supervisor, Mapping) else None
            ts = stamp_ts(stamp, self._now())
            result = term.nav_detector(robot_name).update(ts, supervisor)
            for event in result.events:
                term.queue.put_event(event, term.ctx)
            term.queue.put_latest(robot_name, nav_supervisor=result.latest)
        except Exception:  # noqa: BLE001
            self.handler_errors.log("Telemetry ingest failed for %s/nav_supervisor", robot_name)


def build_from_config() -> Optional[ApiTelemetry]:
    """ApiTelemetry from packages.config, or None when TELEMETRY_INGEST_ENABLED is off.
    Never raises (a misconfiguration disables ingest, it does not stop the API)."""
    try:
        from psycopg.conninfo import make_conninfo
        from packages.config import (
            POSTGRES_DATABASE_HOST, POSTGRES_DATABASE_NAME, POSTGRES_DATABASE_PASSWORD,
            POSTGRES_DATABASE_PORT, POSTGRES_DATABASE_USERNAME, TELEMETRY_ELECTION_CHECK_S,
            TELEMETRY_ELECTION_RETRY_S, TELEMETRY_INGEST_ENABLED, TELEMETRY_SPILL_DIR,
            THERMAL_HIGH_C, THERMAL_OK_C)
        if not TELEMETRY_INGEST_ENABLED:
            logger.info("Telemetry ingest disabled (TELEMETRY_INGEST_ENABLED=false)")
            return None
        conninfo = make_conninfo(
            dbname=POSTGRES_DATABASE_NAME, user=POSTGRES_DATABASE_USERNAME,
            password=POSTGRES_DATABASE_PASSWORD, host=POSTGRES_DATABASE_HOST,
            port=POSTGRES_DATABASE_PORT, application_name=f"api-telemetry-{os.getpid()}")
        return ApiTelemetry(conninfo, TELEMETRY_SPILL_DIR,
                            retry_s=TELEMETRY_ELECTION_RETRY_S, check_s=TELEMETRY_ELECTION_CHECK_S,
                            high_c=THERMAL_HIGH_C, ok_c=THERMAL_OK_C)
    except Exception:  # noqa: BLE001
        logger.exception("Telemetry ingest misconfigured; continuing without it")
        return None
