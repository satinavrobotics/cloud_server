"""Bounded ingest queue and the event spill file (docs/satinav-fleet-agent-phase0-v2.md §5.2).

Hosts call the `put_*` methods from their MQTT handling code. They never block and
never await: the recording level is checked first (§4.3, "before enqueueing"), then
the row goes into a bounded asyncio.Queue. When the queue is full:

- time-series rows (robot_state_ts, diagnostics_ts) are dropped and counted;
- events are appended to a local JSONL spill file, which the writer replays on its
  next successful flush (dedupe by event_id + ON CONFLICT DO NOTHING).

robot_latest updates do not go through the queue at all: they are merged per robot
into a small pending map (bounded by the number of robots), so they are never
dropped and a 10 Hz state stream costs one upsert per flush.

The put_* methods must be called on the event-loop thread (asyncio.Queue is not
thread-safe); from a paho-mqtt thread use loop.call_soon_threadsafe(...).
"""

import asyncio
import datetime
import json
import logging
import os
import uuid
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple, Union

from packages.events import ids
from packages.events.emit import COLUMNS as EVENT_COLUMNS, Event, build_row
from packages.events.codes import Source
from packages.telemetry_ingest import tables
from packages.telemetry_ingest.metrics import Metrics

logger = logging.getLogger(__name__)

DEFAULT_MAXSIZE = 10_000

# Outcomes of a put_* call.
QUEUED = "queued"
SKIPPED = "skipped"   # recording level excludes it
DROPPED = "dropped"   # queue full, telemetry row lost (counted)
SPILLED = "spilled"   # queue full, event written to the spill file
LOST = "lost"         # queue full and the spill file failed (counted, logged)
MERGED = "merged"     # robot_latest update merged into the pending map

Item = Tuple[str, Any]  # (table, row): event rows are build_row() dicts, time series are tuples


# --- spill file ----------------------------------------------------------------------------

def encode_event(row: Mapping[str, Any]) -> str:
    """One JSONL line for a build_row() dict."""
    out = {}
    for column in EVENT_COLUMNS:
        value = row[column]
        if isinstance(value, datetime.datetime):
            value = value.isoformat()
        elif isinstance(value, uuid.UUID):
            value = str(value)
        out[column] = value
    return json.dumps(out, sort_keys=True, default=str)


def decode_event(line: str) -> Dict[str, Any]:
    """Inverse of encode_event(); raises ValueError/KeyError on a corrupt line."""
    data = json.loads(line)
    row = {column: data[column] for column in EVENT_COLUMNS}
    row["ts"] = ids.normalize_ts(datetime.datetime.fromisoformat(row["ts"]))
    row["event_id"] = uuid.UUID(row["event_id"])
    if row["run_id"] is not None:
        row["run_id"] = uuid.UUID(row["run_id"])
    return row


class SpillFile:
    """Append-only JSONL of fleet_events rows waiting to be (re)written.

    Lines are only ever appended at the end and removed from the front, so the writer
    can read a prefix, await the database, and then drop exactly that prefix even if
    more lines were appended meanwhile. Writes are flushed to the OS (they survive a
    process crash) but not fsync'ed.
    """

    def __init__(self, path: Union[str, os.PathLike], metrics: Optional[Metrics] = None):
        self.path = os.fspath(path)
        self.metrics = metrics or Metrics()
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._has_data = False
        self._repair()

    def _repair(self) -> None:
        """Terminate a partial last line left by a crash, so appends start on a new line."""
        try:
            size = os.path.getsize(self.path)
        except FileNotFoundError:
            return
        if size == 0:
            return
        self._has_data = True
        with open(self.path, "rb+") as f:
            f.seek(-1, os.SEEK_END)
            if f.read(1) != b"\n":
                f.write(b"\n")

    @property
    def pending(self) -> bool:
        return self._has_data

    def append(self, rows: Iterable[Mapping[str, Any]]) -> int:
        """Append rows; returns how many were written. Raises OSError on I/O failure."""
        lines = [encode_event(row) + "\n" for row in rows]
        if not lines:
            return 0
        with open(self.path, "a", encoding="utf-8") as f:
            f.writelines(lines)
            f.flush()
        self._has_data = True
        self.metrics.events_spilled += len(lines)
        return len(lines)

    @property
    def rejected_path(self) -> str:
        return self.path + ".rejected"

    def reject(self, rows: Iterable[Mapping[str, Any]]) -> None:
        """Park rows the database refused (kept for inspection, never replayed)."""
        lines = [encode_event(row) + "\n" for row in rows]
        try:
            with open(self.rejected_path, "a", encoding="utf-8") as f:
                f.writelines(lines)
        except OSError:
            logger.exception("Could not write %d rejected events to %s",
                             len(lines), self.rejected_path)
        self.metrics.events_rejected += len(lines)

    def read(self, limit: int) -> Tuple[List[Dict[str, Any]], int]:
        """Up to `limit` decoded rows from the front, and the number of lines they span
        (corrupt lines are skipped, counted, and still consumed)."""
        rows: List[Dict[str, Any]] = []
        consumed = 0
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    if len(rows) >= limit:
                        break
                    consumed += 1
                    if not line.strip():
                        continue
                    try:
                        rows.append(decode_event(line))
                    except (ValueError, KeyError, TypeError):
                        self.metrics.spill_corrupt_lines += 1
                        logger.error("Skipping corrupt spill line in %s: %r", self.path, line[:200])
        except FileNotFoundError:
            self._has_data = False
        return rows, consumed

    def consume(self, n_lines: int) -> None:
        """Drop the first `n_lines` lines (after they were written to the database)."""
        if n_lines <= 0:
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                remaining = f.readlines()[n_lines:]
        except FileNotFoundError:
            self._has_data = False
            return
        if not any(line.strip() for line in remaining):
            os.unlink(self.path)
            self._has_data = False
            return
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(remaining)
            f.flush()
        os.replace(tmp, self.path)
        self._has_data = True


# --- queue ---------------------------------------------------------------------------------

class IngestQueue:
    """Where hosts put rows. `source` ("dispatch" or "api") fixes which robot_latest
    columns this host may write (§3.5). `policy` is a RecordingPolicy (or anything with
    `allows(table, robot_name, code)`); without one everything is recorded."""

    def __init__(self, source: Union[Source, str], spill: SpillFile, *,
                 maxsize: int = DEFAULT_MAXSIZE, metrics: Optional[Metrics] = None,
                 policy: Any = None):
        if maxsize <= 0:
            raise ValueError("maxsize must be positive (the queue has to be bounded)")
        self.source = Source(source)
        self.spill = spill
        self.metrics = metrics or spill.metrics
        spill.metrics = self.metrics
        self.policy = policy
        self._queue: "asyncio.Queue[Item]" = asyncio.Queue(maxsize=maxsize)
        self._latest: Dict[str, Dict[str, Any]] = {}
        self._owned = tables.LATEST_OWNED_COLUMNS[self.source]

    # --- producers (sync, never block) -------------------------------------------------
    def put_event(self, event: Union[Event, Mapping[str, Any]], ctx: Any = None, *,
                  strict: Optional[bool] = None) -> str:
        """Enqueue an events.Event (built here with build_row) or a prebuilt row dict.

        Raises only for programming errors that build_row() raises (bad code, missing
        discriminator, invalid payload in strict mode).
        """
        row = build_row(event, ctx, strict=strict) if isinstance(event, Event) else dict(event)
        if not self._allowed(tables.EVENTS_TABLE, row["robot_name"], row["code"]):
            return SKIPPED
        try:
            self._queue.put_nowait((tables.EVENTS_TABLE, row))
        except asyncio.QueueFull:
            return self._spill_one(row)
        self._depth()
        return QUEUED

    def put_state(self, row: Mapping[str, Any]) -> str:
        """Enqueue one robot_state_ts row (dict with tables.ROBOT_STATE_COLUMNS keys)."""
        return self._put_timeseries(tables.ROBOT_STATE_TABLE, row)

    def put_diagnostics(self, row: Mapping[str, Any]) -> str:
        """Enqueue one diagnostics_ts row (dict with tables.DIAGNOSTICS_COLUMNS keys)."""
        return self._put_timeseries(tables.DIAGNOSTICS_TABLE, row)

    def put_latest(self, robot_name: str, **fields: Any) -> str:
        """Merge an update of this host's robot_latest columns for `robot_name`.

        Always recorded, whatever the level. Only the columns passed are written; pass
        None to clear one. jsonb columns take plain Python objects.
        """
        if not robot_name:
            raise ValueError("robot_name is required")
        if not fields:
            raise ValueError("put_latest needs at least one column")
        foreign = set(fields) - self._owned
        if foreign:
            raise ValueError(f"{self.source.value} may not write robot_latest columns "
                             f"{sorted(foreign)}; it owns {sorted(self._owned)}")
        if fields.get("active_run_id") is not None:
            fields["active_run_id"] = uuid.UUID(str(fields["active_run_id"]))
        if fields.get("last_seen") is not None:
            fields["last_seen"] = ids.normalize_ts(fields["last_seen"])
        self._latest.setdefault(robot_name, {}).update(fields)
        return MERGED

    # --- consumer side (writer) --------------------------------------------------------
    def qsize(self) -> int:
        return self._queue.qsize()

    @property
    def latest_pending(self) -> bool:
        return bool(self._latest)

    def drain(self, limit: int) -> List[Item]:
        items: List[Item] = []
        while len(items) < limit:
            try:
                items.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        self._depth()
        return items

    def take_latest(self) -> Dict[str, Dict[str, Any]]:
        latest, self._latest = self._latest, {}
        return latest

    def restore_latest(self, failed: Mapping[str, Mapping[str, Any]]) -> None:
        """Put back updates whose write failed, underneath anything newer."""
        for robot, fields in failed.items():
            newer = self._latest.get(robot, {})
            self._latest[robot] = {**fields, **newer}

    def spill_events(self, rows: List[Mapping[str, Any]]) -> None:
        """Spill event rows the writer could not commit. Never raises."""
        if not rows:
            return
        try:
            self.spill.append(rows)
        except OSError:
            self.metrics.events_lost += len(rows)
            logger.exception("Could not spill %d events to %s; they are lost",
                             len(rows), self.spill.path)

    def spill_queued(self) -> int:
        """At shutdown: move queued events to the spill file and count queued telemetry
        as dropped. Returns the number of events spilled."""
        items = self.drain(self._queue.qsize())
        events = [row for table, row in items if table == tables.EVENTS_TABLE]
        for table, _ in items:
            if table != tables.EVENTS_TABLE:
                self.metrics.dropped(table, "shutdown")
        self.spill_events(events)
        return len(events)

    # --- internals ---------------------------------------------------------------------
    def _allowed(self, table: str, robot_name: Optional[str], code: Optional[str] = None) -> bool:
        if self.policy is None or self.policy.allows(table, robot_name, code):
            return True
        self.metrics.skipped(table)
        return False

    def _put_timeseries(self, table: str, row: Mapping[str, Any]) -> str:
        columns = tables.TIMESERIES_COLUMNS[table]
        unknown = set(row) - set(columns)
        if unknown:
            raise ValueError(f"unknown {table} columns {sorted(unknown)}")
        if row.get("ts") is None or not row.get("robot_name"):
            raise ValueError(f"{table} rows need ts and robot_name")
        if not self._allowed(table, row["robot_name"]):
            return SKIPPED
        values = dict(row)
        values["ts"] = ids.normalize_ts(values["ts"])
        if values.get("run_id") is not None:
            values["run_id"] = uuid.UUID(str(values["run_id"]))
        try:
            self._queue.put_nowait((table, tuple(values.get(c) for c in columns)))
        except asyncio.QueueFull:
            self.metrics.dropped(table, "queue_full")
            return DROPPED
        self._depth()
        return QUEUED

    def _spill_one(self, row: Mapping[str, Any]) -> str:
        try:
            self.spill.append([row])
        except OSError:
            self.metrics.events_lost += 1
            logger.exception("Queue full and spill file %s failed; event lost", self.spill.path)
            return LOST
        return SPILLED

    def _depth(self) -> None:
        self.metrics.set_queue_depth(self._queue.qsize())
