"""Background writer (docs/satinav-fleet-agent-phase0-v2.md §5.2).

One asyncio task per host. It flushes when `batch_size` rows are queued or
`flush_interval_s` has passed since the last flush, on its own small
AsyncConnectionPool (`create_pool`, 2 connections) so ingest never competes with
the host's request pool. Each flush takes one connection and runs three
independent transactions:

1. fleet_events: spilled events (replay, oldest first) plus the batch's events,
   `executemany(INSERT … ON CONFLICT DO NOTHING)` (events.emit.INSERT_SQL).
   On success the replayed prefix is removed from the spill file; on a connection
   failure, or if the task is cancelled mid-write, the batch's events are appended to
   the spill file. If the database refuses the data on a healthy connection, the rows
   are retried one by one and the refused ones move to `<spill>.rejected`, so a poison
   row cannot block the replay forever. A write that committed just before a cancel is therefore replayed
   once more, which ON CONFLICT turns into a no-op: events are written exactly once.
2. robot_state_ts / diagnostics_ts: COPY. On failure the rows are dropped and counted.
3. robot_latest: one upsert per robot touching only this host's columns. On a
   connection failure the updates go back to the queue's pending map (under anything
   newer); if the database refuses them they are dropped and counted.

Every exception is caught and counted; the loop only ends through stop()/cancel.
Only `clock` (monotonic seconds) and `sleep` are used for timing, so tests can
drive the loop with a fake clock or call `flush_once()` directly.
"""

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Tuple

from packages.events.emit import INSERT_SQL, row_params
from packages.telemetry_ingest import tables
from packages.telemetry_ingest.metrics import Metrics
from packages.telemetry_ingest.queue import IngestQueue

logger = logging.getLogger(__name__)

POOL_SIZE = 2
DEFAULT_FLUSH_INTERVAL_S = 1.0
DEFAULT_BATCH_SIZE = 500
DEFAULT_REPLAY_LIMIT = 5000
DEFAULT_CONNECT_TIMEOUT_S = 5.0
DEFAULT_TICK_S = 0.1


async def create_pool(conninfo: str, *, name: str = "telemetry_ingest") -> Any:
    """Open the writer's own pool of POOL_SIZE connections. Does not wait for the
    database: connections are made in the background, and flushes fail (and spill)
    until it is reachable. The caller closes it after `TelemetryWriter.stop()`."""
    from psycopg_pool import AsyncConnectionPool  # imported lazily: tests need no driver

    pool = AsyncConnectionPool(conninfo, min_size=POOL_SIZE, max_size=POOL_SIZE,
                               name=name, open=False)
    await pool.open(wait=False)
    return pool


class _Batch:
    """What one flush still owes the database; settle() disposes of whatever is left."""

    def __init__(self, events: List[Dict[str, Any]],
                 timeseries: Dict[str, List[Tuple[Any, ...]]],
                 latest: Dict[str, Dict[str, Any]],
                 replay: List[Dict[str, Any]], replay_lines: int):
        self.events = events
        self.timeseries = timeseries
        self.latest = latest
        self.replay = replay
        self.replay_lines = replay_lines
        self.failed = False

    @property
    def empty(self) -> bool:
        return not (self.events or self.timeseries or self.latest or self.replay_lines)


class TelemetryWriter:
    def __init__(self, pool: Any, queue: IngestQueue, *,
                 policy: Any = None,
                 metrics: Optional[Metrics] = None,
                 flush_interval_s: float = DEFAULT_FLUSH_INTERVAL_S,
                 batch_size: int = DEFAULT_BATCH_SIZE,
                 replay_limit: int = DEFAULT_REPLAY_LIMIT,
                 connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S,
                 tick_s: float = DEFAULT_TICK_S,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep):
        """`policy` (optional RecordingPolicy) is refreshed by the loop whenever it is
        stale, e.g. after the host's NOTIFY handler called `policy.invalidate()`."""
        if batch_size <= 0 or flush_interval_s <= 0:
            raise ValueError("batch_size and flush_interval_s must be positive")
        self._pool = pool
        self._queue = queue
        self._spill = queue.spill
        self._policy = policy
        self.metrics = metrics or queue.metrics
        self._interval = flush_interval_s
        self._batch_size = batch_size
        self._replay_limit = replay_limit
        self._connect_timeout = connect_timeout_s
        self._tick = tick_s
        self._clock = clock
        self._sleep = sleep
        self._last_flush: Optional[float] = None
        self._task: Optional[asyncio.Task] = None

    # --- lifecycle -----------------------------------------------------------------------
    def start(self) -> asyncio.Task:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(
                self._run(), name="telemetry_ingest.writer")
        return self._task

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def stop(self, timeout_s: float = 5.0) -> None:
        """Stop the loop, try one last flush within `timeout_s`, then spill whatever
        events are still queued. Never raises."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except BaseException:  # noqa: BLE001 - CancelledError or a loop bug; both done
                pass
        try:
            await asyncio.wait_for(self._drain(), timeout_s)
        except BaseException:  # noqa: BLE001 - timeout/cancel: flush_once already spilled
            logger.warning("Final telemetry flush did not finish; spilling queued events")
        self._queue.spill_queued()

    async def _drain(self) -> None:
        while self._queue.qsize() or self._queue.latest_pending or self._spill.pending:
            if not await self.flush_once():
                return

    # --- loop ----------------------------------------------------------------------------
    def due(self, now: Optional[float] = None) -> bool:
        """Should a flush run now? Full batch, or interval elapsed with work waiting."""
        if self._queue.qsize() >= self._batch_size:
            return True
        if not (self._queue.qsize() or self._queue.latest_pending or self._spill.pending):
            return False
        now = self._clock() if now is None else now
        return self._last_flush is None or now - self._last_flush >= self._interval

    async def _run(self) -> None:
        while True:
            try:
                if self._policy is not None and self._policy.stale:
                    if not await self._policy.refresh(self._pool):
                        self.metrics.policy_refresh_failures += 1
                if self.due():
                    await self.flush_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the writer must outlive any bug
                self.metrics.loop_errors += 1
                logger.exception("Telemetry writer loop error")
            await self._sleep(self._tick)

    # --- one flush -----------------------------------------------------------------------
    async def flush_once(self) -> bool:
        """Write one batch. Returns True if every step succeeded (or there was nothing
        to do). Never raises except CancelledError, and even then the batch's events
        are spilled first."""
        replay, replay_lines = ([], 0)
        if self._spill.pending:
            try:
                replay, replay_lines = self._spill.read(self._replay_limit)
            except OSError:
                logger.exception("Could not read spill file %s; replay skipped", self._spill.path)
        items = self._queue.drain(self._batch_size)
        events: List[Dict[str, Any]] = []
        timeseries: Dict[str, List[Tuple[Any, ...]]] = {}
        for table, row in items:
            if table == tables.EVENTS_TABLE:
                events.append(row)
            else:
                timeseries.setdefault(table, []).append(row)
        batch = _Batch(events, timeseries, self._queue.take_latest(), replay, replay_lines)
        if batch.empty:
            return True

        started = self._clock()
        self._last_flush = started
        try:
            async with self._pool.connection(timeout=self._connect_timeout) as conn:
                await self._write_events(conn, batch)
                await self._write_timeseries(conn, batch)
                await self._write_latest(conn, batch)
        except asyncio.CancelledError:
            batch.failed = True
            raise
        except Exception:  # noqa: BLE001 - e.g. PoolTimeout, connection lost
            batch.failed = True
            logger.exception("Telemetry flush failed")
        finally:
            self._settle(batch)
            self.metrics.flush_finished(self._clock() - started, batch.failed)
        return not batch.failed

    async def _write_events(self, conn: Any, batch: _Batch) -> None:
        rows = batch.replay + batch.events
        if not rows:
            if batch.replay_lines:  # the replayed prefix held only corrupt lines
                self._spill.consume(batch.replay_lines)
                batch.replay_lines = 0
            return
        rejected = 0
        try:
            async with conn.transaction():
                async with conn.cursor() as cursor:
                    await cursor.executemany(INSERT_SQL, [row_params(r) for r in rows])
        except Exception:  # noqa: BLE001
            if _broken(conn):
                batch.failed = True
                logger.exception("Writing %d events failed; spilling them", len(rows))
                return
            # The database rejected the data, not the connection: isolate the bad rows so
            # one poison row cannot block every later event through the replay.
            logger.exception("Batch insert of %d events failed; retrying one by one", len(rows))
            result = await self._write_events_one_by_one(conn, rows)
            if result is None:
                batch.failed = True
                return
            rejected = result
        self.metrics.written(tables.EVENTS_TABLE, len(rows) - rejected)
        if batch.replay_lines:
            self._spill.consume(batch.replay_lines)
            self.metrics.events_replayed += len(batch.replay)
        batch.events, batch.replay, batch.replay_lines = [], [], 0

    async def _write_events_one_by_one(self, conn: Any,
                                       rows: List[Dict[str, Any]]) -> Optional[int]:
        """Insert rows individually; rows the database refuses go to the rejected file.
        Returns how many were rejected, or None (nothing consumed) if the connection
        breaks on the way; rows already inserted are then written again later, which
        ON CONFLICT absorbs."""
        rejected = []
        for row in rows:
            try:
                async with conn.transaction():
                    async with conn.cursor() as cursor:
                        await cursor.execute(INSERT_SQL, row_params(row))
            except Exception:  # noqa: BLE001
                if _broken(conn):
                    logger.exception("Connection lost during one-by-one event insert")
                    return None
                logger.exception("Event %s rejected by the database", row.get("event_id"))
                rejected.append(row)
        if rejected:
            self._spill.reject(rejected)
        return len(rejected)

    async def _write_timeseries(self, conn: Any, batch: _Batch) -> None:
        for table in list(batch.timeseries):
            rows = batch.timeseries[table]
            try:
                async with conn.transaction():
                    async with conn.cursor() as cursor:
                        async with cursor.copy(tables.copy_sql(table)) as copy:
                            for row in rows:
                                await copy.write_row(row)
            except Exception:  # noqa: BLE001
                batch.failed = True
                logger.exception("COPY of %d %s rows failed; dropping them", len(rows), table)
                continue
            self.metrics.written(table, len(rows))
            del batch.timeseries[table]

    async def _write_latest(self, conn: Any, batch: _Batch) -> None:
        if not batch.latest:
            return
        grouped: Dict[Tuple[str, ...], List[Tuple[Any, ...]]] = {}
        for robot, fields in batch.latest.items():
            columns = tuple(sorted(fields))
            grouped.setdefault(columns, []).append(
                (robot,) + tuple(_latest_param(c, fields[c]) for c in columns))
        try:
            async with conn.transaction():
                async with conn.cursor() as cursor:
                    for columns, params in grouped.items():
                        await cursor.executemany(tables.latest_upsert_sql(columns), params)
        except Exception:  # noqa: BLE001
            batch.failed = True
            if _broken(conn):
                logger.exception("robot_latest upsert failed; retrying next flush")
            else:  # the data itself was refused: retrying it would fail forever
                logger.exception("robot_latest upsert refused; dropping %d updates",
                                 len(batch.latest))
                self.metrics.dropped(tables.LATEST_TABLE, "write_failed", len(batch.latest))
                batch.latest = {}
            return
        self.metrics.written(tables.LATEST_TABLE, len(batch.latest))
        batch.latest = {}

    def _settle(self, batch: _Batch) -> None:
        """Whatever the batch still holds was not committed: spill events, count dropped
        telemetry, and hand robot_latest updates back for the next flush. Replayed rows
        are still in the spill file, so nothing is done for them."""
        self._queue.spill_events(batch.events)
        for table, rows in batch.timeseries.items():
            self.metrics.dropped(table, "write_failed", len(rows))
        self._queue.restore_latest(batch.latest)
        batch.events, batch.timeseries, batch.latest = [], {}, {}


def _broken(conn: Any) -> bool:
    """True if the connection itself failed (psycopg sets `broken`/`closed`), as opposed
    to the database refusing the data on a healthy connection."""
    return bool(getattr(conn, "broken", False) or getattr(conn, "closed", False))


def _latest_param(column: str, value: Any) -> Any:
    if column in tables.LATEST_JSONB_COLUMNS and value is not None:
        return json.dumps(value, sort_keys=True, default=str)
    return value
