"""Recorder health reports (docs/satinav-fleet-agent-phase0-v2.md WP13).

Each process that records (mission-dispatch's fleet_recorder, the API's elected telemetry
writer) periodically upserts ONE row, keyed by process name, into `recorder_health`
(migration 20260926_02_recorder_health). The API reads the rows for
GET /api/v1/health/recording and evaluates the alert rules (packages/api/recorder_health.py).

Why a table: mission-dispatch has no HTTP port and must not get one (no new ports or
containers), and Postgres is already the only interface between dispatch and the API. A
keyed row is readable by every API worker at any time, survives API restarts, and costs one
tiny upsert per report period; NOTIFY would be lost whenever no API worker is listening
and has no "last known value". If dispatch cannot reach the database its row simply stops
being refreshed, which the API reports as the `report_stale` alert.

Ages in a report are measured by the reporting process at report time (`*_age_s`), and
`reported_at` is the database's now() at upsert time, so readers compute current ages from
the database clock alone: no clock skew between hosts matters.
"""

import json
import os
import socket
import time
from typing import Any, Dict, Mapping, Optional

from packages.telemetry_ingest.metrics import Metrics

TABLE = "recorder_health"

PROCESS_DISPATCH = "dispatch"
PROCESS_API = "api"

# dispatch: never touches `alerts` (the API's evaluator owns them, on the api row).
UPSERT_SQL = (
    f"INSERT INTO {TABLE} (process, pid, hostname, role, started_at, reported_at, report) "
    "VALUES (%s, %s, %s, %s, %s, now(), %s::jsonb) "
    "ON CONFLICT (process) DO UPDATE SET pid = EXCLUDED.pid, hostname = EXCLUDED.hostname, "
    "role = EXCLUDED.role, started_at = EXCLUDED.started_at, reported_at = now(), "
    "report = EXCLUDED.report"
)
# api: the report plus the evaluator's active alerts, in the same statement.
UPSERT_WITH_ALERTS_SQL = (
    f"INSERT INTO {TABLE} (process, pid, hostname, role, started_at, reported_at, report, "
    "alerts) VALUES (%s, %s, %s, %s, %s, now(), %s::jsonb, %s::jsonb) "
    "ON CONFLICT (process) DO UPDATE SET pid = EXCLUDED.pid, hostname = EXCLUDED.hostname, "
    "role = EXCLUDED.role, started_at = EXCLUDED.started_at, reported_at = now(), "
    "report = EXCLUDED.report, alerts = EXCLUDED.alerts"
)
SELECT_SQL = (
    "SELECT process, pid, hostname, role, started_at, reported_at, "
    f"extract(epoch FROM now() - reported_at)::float8, report, alerts FROM {TABLE} "
    "ORDER BY process"
)
SELECT_COLUMNS = ("process", "pid", "hostname", "role", "started_at", "reported_at",
                  "report_age_s", "report", "alerts")


def hostname() -> str:
    try:
        return socket.gethostname()
    except Exception:  # noqa: BLE001
        return ""


def _age(at: Optional[float], now: float) -> Optional[float]:
    return None if at is None else round(max(0.0, now - at), 3)


def ingest_report(metrics: Metrics, *, queue_depth: int, queue_capacity: int,
                  spill: Any = None, writer_running: bool = False,
                  now: Optional[float] = None) -> Dict[str, Any]:
    """The part of a report every recording process has: its ingest queue, writer and spill
    file. `spill` is a telemetry_ingest.SpillFile (or None when there is none yet). JSON-safe."""
    now = time.time() if now is None else now
    capacity = max(0, int(queue_capacity))
    dropped_by_table = {t: dict(r) for t, r in metrics.rows_dropped.items()}
    pending = int(getattr(spill, "pending_lines", 0) or 0) if spill is not None else 0
    since = getattr(spill, "pending_since", None) if spill is not None else None
    return {
        "queue": {
            "depth": int(queue_depth),
            "capacity": capacity,
            "pct": round(100.0 * queue_depth / capacity, 2) if capacity else None,
            "depth_max": metrics.queue_depth_max,
        },
        "dropped": {
            "total": sum(sum(r.values()) for r in dropped_by_table.values()),
            "by_table": dropped_by_table,
        },
        "events_spilled": metrics.events_spilled,
        "events_replayed": metrics.events_replayed,
        "events_rejected": metrics.events_rejected,
        "events_lost": metrics.events_lost,
        "spill": {
            "pending": pending,
            "pending_age_s": _age(since, now) if pending else None,
        },
        "writer": {
            "running": bool(writer_running),
            "flushes": metrics.flushes,
            "flush_failures": metrics.flush_failures,
            "last_flush_age_s": _age(metrics.last_flush_at, now),
            "last_flush_ok_age_s": _age(metrics.last_flush_ok_at, now),
            "last_tick_age_s": _age(metrics.writer_tick_at, now),
            "flush_duration_last_s": metrics.flush_duration_last_s,
            "flush_duration_max_s": metrics.flush_duration_max_s,
            "loop_errors": metrics.loop_errors,
        },
    }


def row_params(process: str, role: str, started_at: Any, report: Mapping[str, Any],
               alerts: Optional[Any] = None) -> tuple:
    """Parameters for UPSERT_SQL (alerts None) or UPSERT_WITH_ALERTS_SQL."""
    params = (process, os.getpid(), hostname(), role, started_at,
              json.dumps(report, sort_keys=True, default=str))
    if alerts is None:
        return params
    return params + (json.dumps(alerts, sort_keys=True, default=str),)
