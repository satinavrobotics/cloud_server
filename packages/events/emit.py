"""Build and write `fleet_events` rows (docs/satinav-fleet-agent-phase0-v2.md §3.2).

`build_row()` is pure. `emit()` writes one row on a psycopg3 AsyncConnection the
caller owns: it neither commits nor rolls back, so the event lands in the
caller's transaction.
"""

import dataclasses
import datetime
import json
import uuid
from typing import Any, Dict, Mapping, Optional, Protocol, Tuple, Union

from packages.events import ids, schemas
from packages.events.codes import EventCode, Severity, Source, meta_for

TABLE = "fleet_events"
COLUMNS: Tuple[str, ...] = (
    "ts", "event_id", "robot_name", "run_id", "site_id",
    "code", "severity", "sw_version", "payload", "source",
)
INSERT_SQL = (
    f"INSERT INTO {TABLE} ({', '.join(COLUMNS)}) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s) "
    "ON CONFLICT DO NOTHING"
)


class EventContext(Protocol):
    """Host-provided lookups, resolved at write time for the event's robot and time."""

    def site_for(self, robot_name: str, ts: datetime.datetime) -> Optional[str]:
        ...

    def run_for(self, robot_name: str, ts: datetime.datetime) -> Optional[uuid.UUID]:
        ...

    def sw_version_for(self, robot_name: str, ts: datetime.datetime) -> Optional[str]:
        ...


@dataclasses.dataclass(frozen=True)
class Event:
    """One event. `run_id`, `site_id`, `sw_version`, `severity` and `source`
    override the context and code defaults when set."""
    code: EventCode
    ts: datetime.datetime
    robot_name: Optional[str] = None
    payload: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    discriminator: Optional[str] = None
    severity: Optional[Severity] = None
    run_id: Optional[Union[uuid.UUID, str]] = None
    site_id: Optional[str] = None
    sw_version: Optional[str] = None
    source: Optional[Source] = None


def build_row(event: Event, ctx: Optional[EventContext] = None, *,
              strict: Optional[bool] = None) -> Dict[str, Any]:
    code = EventCode(event.code)
    meta = meta_for(code)
    if meta.discriminator_required and not event.discriminator:
        raise ValueError(f"{code.value} requires a discriminator")
    ts = ids.normalize_ts(event.ts)
    robot = event.robot_name

    run_id, site_id, sw_version = event.run_id, event.site_id, event.sw_version
    if ctx is not None and robot:
        if run_id is None:
            run_id = ctx.run_for(robot, ts)
        if site_id is None:
            site_id = ctx.site_for(robot, ts)
        if sw_version is None:
            sw_version = ctx.sw_version_for(robot, ts)

    return {
        "ts": ts,
        "event_id": ids.event_id(code, robot, ts, event.discriminator),
        "robot_name": robot,
        "run_id": uuid.UUID(str(run_id)) if run_id is not None else None,
        "site_id": site_id,
        "code": code.value,
        "severity": Severity(event.severity or meta.severity).value,
        "sw_version": sw_version,
        "payload": schemas.validate_payload(meta.payload_model, event.payload, strict),
        "source": Source(event.source or meta.source).value,
    }


def row_params(row: Mapping[str, Any]) -> Tuple[Any, ...]:
    """Positional parameters for INSERT_SQL (also usable with executemany)."""
    return tuple(
        json.dumps(row[c], sort_keys=True, default=str) if c == "payload" else row[c]
        for c in COLUMNS
    )


async def emit(conn: Any, event: Event, ctx: Optional[EventContext] = None, *,
               strict: Optional[bool] = None) -> bool:
    """Insert `event` on `conn` (a psycopg.AsyncConnection, never a pool).

    Returns True if a row was inserted, False if it already existed.
    """
    if not hasattr(conn, "cursor"):
        raise TypeError("emit() needs a connection, not a pool; the caller owns the transaction")
    row = build_row(event, ctx, strict=strict)
    async with conn.cursor() as cursor:
        await cursor.execute(INSERT_SQL, row_params(row))
        return cursor.rowcount == 1
