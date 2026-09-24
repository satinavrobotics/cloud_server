"""Startup rehydration from `robot_latest` (docs/satinav-fleet-agent-phase0-v2.md §3.5).

Detectors built without a previous state treat their first sample as a silent
baseline; seeding them from the last stored values keeps continuity across a
restart, so a change that happened while the host was down is still reported and
an unchanged value is not reported again. This module only loads the rows; each
host maps them onto its own detectors (state_msg for dispatch, diagnostics and
nav_supervisor for the API).
"""

import dataclasses
import datetime
import json
import logging
import uuid
from typing import Any, Dict, Iterable, Optional

from packages.telemetry_ingest import tables
from packages.telemetry_ingest._db import connection_scope

logger = logging.getLogger(__name__)

SELECT_SQL = f"SELECT {', '.join(tables.LATEST_COLUMNS)} FROM {tables.LATEST_TABLE}"


@dataclasses.dataclass(frozen=True)
class LatestRow:
    robot_name: str
    state_msg: Optional[Dict[str, Any]] = None
    diagnostics: Optional[Dict[str, Any]] = None
    nav_supervisor: Optional[Dict[str, Any]] = None
    active_run_id: Optional[uuid.UUID] = None
    site_id: Optional[str] = None
    sw_version: Optional[str] = None
    last_seen: Optional[datetime.datetime] = None
    updated_at: Optional[datetime.datetime] = None


def _json(value: Any) -> Any:
    # psycopg returns jsonb already decoded; accept text too (other adapters, fakes).
    if isinstance(value, (str, bytes)):
        return json.loads(value)
    return value


def row_from_record(record: Iterable[Any]) -> LatestRow:
    """A LatestRow from one SELECT_SQL result tuple."""
    values = dict(zip(tables.LATEST_COLUMNS, record))
    for column in tables.LATEST_JSONB_COLUMNS:
        values[column] = _json(values[column])
    if values["active_run_id"] is not None and not isinstance(values["active_run_id"], uuid.UUID):
        values["active_run_id"] = uuid.UUID(str(values["active_run_id"]))
    return LatestRow(**values)


async def load_latest(pool_or_conn: Any, robots: Optional[Iterable[str]] = None, *,
                      raise_errors: bool = False) -> Dict[str, LatestRow]:
    """All robot_latest rows (or only `robots`), keyed by robot name.

    With raise_errors=False (the default) a failure is logged and returns {}: the
    host then starts with unseeded detectors rather than not starting at all.
    """
    query, params = SELECT_SQL, None
    if robots is not None:
        query, params = SELECT_SQL + " WHERE robot_name = ANY(%s)", (list(robots),)
    try:
        async with connection_scope(pool_or_conn) as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(query, params)
                records = await cursor.fetchall()
    except Exception:  # noqa: BLE001
        if raise_errors:
            raise
        logger.exception("Could not load robot_latest; detectors start unseeded")
        return {}
    rows = {}
    for record in records:
        try:
            row = row_from_record(record)
        except (ValueError, TypeError):
            logger.exception("Skipping unreadable robot_latest row %r", record[:1])
            continue
        rows[row.robot_name] = row
    return rows
