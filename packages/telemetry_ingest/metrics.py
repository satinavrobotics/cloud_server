"""In-process ingest counters (docs/satinav-fleet-agent-phase0-v2.md §5.2).

Plain counters, no exporter: the host exposes `snapshot()` however it likes (health
route, periodic log line). Everything here is touched only from the event loop thread.
"""

import collections
from typing import Any, Dict


class Metrics:
    """Counters for one ingest pipeline (queue + writer).

    rows_written[table]            rows sent to the database in a committed transaction
                                   (events count even when ON CONFLICT skipped them)
    rows_dropped[table][reason]    telemetry rows lost: "queue_full", "write_failed"
                                   or "shutdown"
    rows_skipped[table]            rows not enqueued because the recording level excludes them
    events_spilled                 events appended to the spill file (queue full or failed write)
    events_replayed                spilled events written back to the database
    events_rejected                events the database refused (bad data), parked in
                                   <spill>.rejected and never retried
    events_lost                    events that could not even be spilled (spill file I/O error)
    spill_corrupt_lines            unreadable spill lines skipped on replay
    flushes / flush_failures       flush attempts / attempts with at least one failed step
    flush_duration_*_s             wall time of flushes, from the writer's clock
    queue_depth / queue_depth_max  current and high-water queue size
    """

    def __init__(self):
        self.rows_written: Dict[str, int] = collections.Counter()
        self.rows_dropped: Dict[str, Dict[str, int]] = collections.defaultdict(collections.Counter)
        self.rows_skipped: Dict[str, int] = collections.Counter()
        self.events_spilled = 0
        self.events_replayed = 0
        self.events_rejected = 0
        self.events_lost = 0
        self.spill_corrupt_lines = 0
        self.flushes = 0
        self.flush_failures = 0
        self.loop_errors = 0
        self.policy_refresh_failures = 0
        self.flush_duration_last_s = 0.0
        self.flush_duration_max_s = 0.0
        self.flush_duration_total_s = 0.0
        self.queue_depth = 0
        self.queue_depth_max = 0

    def written(self, table: str, n: int) -> None:
        if n:
            self.rows_written[table] += n

    def dropped(self, table: str, reason: str, n: int = 1) -> None:
        if n:
            self.rows_dropped[table][reason] += n

    def skipped(self, table: str, n: int = 1) -> None:
        if n:
            self.rows_skipped[table] += n

    def set_queue_depth(self, depth: int) -> None:
        self.queue_depth = depth
        if depth > self.queue_depth_max:
            self.queue_depth_max = depth

    def flush_finished(self, duration_s: float, failed: bool) -> None:
        self.flushes += 1
        if failed:
            self.flush_failures += 1
        self.flush_duration_last_s = duration_s
        self.flush_duration_total_s += duration_s
        if duration_s > self.flush_duration_max_s:
            self.flush_duration_max_s = duration_s

    def snapshot(self) -> Dict[str, Any]:
        """A JSON-safe copy of every counter."""
        return {
            "rows_written": dict(self.rows_written),
            "rows_dropped": {t: dict(r) for t, r in self.rows_dropped.items()},
            "rows_skipped": dict(self.rows_skipped),
            "events_spilled": self.events_spilled,
            "events_replayed": self.events_replayed,
            "events_rejected": self.events_rejected,
            "events_lost": self.events_lost,
            "spill_corrupt_lines": self.spill_corrupt_lines,
            "flushes": self.flushes,
            "flush_failures": self.flush_failures,
            "loop_errors": self.loop_errors,
            "policy_refresh_failures": self.policy_refresh_failures,
            "flush_duration_last_s": self.flush_duration_last_s,
            "flush_duration_max_s": self.flush_duration_max_s,
            "flush_duration_total_s": self.flush_duration_total_s,
            "queue_depth": self.queue_depth,
            "queue_depth_max": self.queue_depth_max,
        }
