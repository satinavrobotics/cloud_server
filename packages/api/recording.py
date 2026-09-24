"""Recording-level changes made through the API (docs/satinav-fleet-agent-phase0-v2.md §4, WP8).

The level lives in the `telemetry_recording` field of the robot spec and of the global
settings (the site layer arrives with siteobjectv1 in WP9). The robot and settings routes:

- validate the field with `check_level()` (422 on an unknown value);
- pass `change_hook(...)` to `create_object`/`update_spec`. It runs on the route's own
  connection after the object write and before the commit, so `TELEMETRY.RECORDING_CHANGED`
  commits together with the change it describes.

The event is written straight to `fleet_events` (not through the telemetry queue), so the
§4.1 level gate never applies to it: it is written even at level `off`.

Failure isolation: the event write runs in a savepoint. Any error (fleet_events missing, a
bad payload, a database hiccup) rolls back only the savepoint, is logged and counted, and the
object change still commits; the route's response is unchanged.
"""

import datetime
import logging
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional

from fastapi import HTTPException

from cloud_common.objects.common import TELEMETRY_RECORDING_LEVELS
from packages.events.codes import EventCode, Source
from packages.events.emit import Event, emit
from packages.events.schemas import RecordingLevel, RecordingScope
from packages.telemetry_ingest.policy import SPEC_FIELD, load_sources, parse_level, resolve

logger = logging.getLogger("ApiDelegationService.recording")

# Counters for health/debugging: events written, and event writes that failed (the object
# change was committed without its event).
stats: Dict[str, int] = {"written": 0, "failed": 0}


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def check_level(data: Mapping[str, Any]) -> None:
    """Raise 422 if `data` sets `telemetry_recording` to anything but a level or None.

    The detail has FastAPI's request-validation shape. Only this field is checked here; the
    other fields keep their existing handling."""
    if not isinstance(data, Mapping) or SPEC_FIELD not in data:
        return
    value = data[SPEC_FIELD]
    if value is None or (isinstance(value, str) and value in TELEMETRY_RECORDING_LEVELS):
        return
    permitted = ", ".join(repr(v) for v in TELEMETRY_RECORDING_LEVELS)
    raise HTTPException(status_code=422, detail=[{
        "loc": ["body", SPEC_FIELD],
        "msg": f"unexpected value; permitted: {permitted} or null",
        "type": "value_error.const",
        "ctx": {"given": value, "permitted": list(TELEMETRY_RECORDING_LEVELS) + [None]},
    }])


def request_actor(request: Any = None) -> Optional[str]:
    """The authenticated identity behind a request, for audit-style fields.

    The API has no authentication today (AUDIT_BACKLOG.md B2), so there is no identity to
    report and this returns None. Nothing client-supplied (headers, body) is trusted as an
    actor. F4/F5 (WP11) replace this once auth exists."""
    return None


async def record_change(conn: Any, scope: RecordingScope, scope_id: Optional[str],
                        old_value: Any, new_value: Any, actor: Optional[str],
                        ts: Optional[datetime.datetime] = None) -> bool:
    """Write TELEMETRY.RECORDING_CHANGED on `conn` if the configured value at `scope` changed.

    `old_level`/`new_level` in the payload are the levels in effect for the scope before and
    after (robot: robot -> site -> global; global: the global value or the default), read in
    the same transaction. Returns True if an event was written. Never raises."""
    old, new = parse_level(old_value), parse_level(new_value)
    if old == new:
        return False
    ts = ts or _utcnow()
    try:
        async with conn.transaction():  # a savepoint inside the route's transaction
            sources = await load_sources(conn, ts)
            robot_name = site_id = None
            if scope is RecordingScope.GLOBAL:
                old_level, new_level = resolve(None, None, old), resolve(None, None, new)
            else:
                robot_name = scope_id
                site_id = sources.robot_sites.get(robot_name)
                site_level = sources.site_levels.get(site_id) if site_id is not None else None
                old_level = resolve(old, site_level, sources.global_level)
                new_level = resolve(new, site_level, sources.global_level)
            event = Event(
                EventCode.TELEMETRY_RECORDING_CHANGED, ts, robot_name=robot_name,
                site_id=site_id, source=Source.API,
                discriminator=f"{scope.value}:{scope_id or ''}:"
                              f"{old.value if old else 'unset'}->{new.value if new else 'unset'}",
                payload={"old_level": old_level.value, "new_level": new_level.value,
                         "scope": scope.value, "scope_id": scope_id, "actor": actor})
            await emit(conn, event)
    except Exception:  # noqa: BLE001 - the object change must commit regardless
        stats["failed"] += 1
        logger.exception("Could not write TELEMETRY.RECORDING_CHANGED for %s %s (%s -> %s); "
                         "the change itself is saved", scope.value, scope_id or "",
                         old_value, new_value)
        return False
    stats["written"] += 1
    logger.info("Recording level %s %s: %s -> %s (effective %s -> %s, actor %s)",
                scope.value, scope_id or "", old_value, new_value, old_level.value,
                new_level.value, actor)
    return True


def change_hook(scope: RecordingScope, scope_id: Optional[str], actor: Optional[str] = None,
                ) -> Callable[[Any, Optional[Mapping[str, Any]], Mapping[str, Any]],
                              Awaitable[bool]]:
    """A PostgresDatabase before-commit hook that records a change of `telemetry_recording`
    between the stored spec (None on create) and the written one."""
    async def hook(conn: Any, old_spec: Optional[Mapping[str, Any]],
                   new_spec: Mapping[str, Any]) -> bool:
        old_value = (old_spec or {}).get(SPEC_FIELD)
        return await record_change(conn, scope, scope_id, old_value,
                                   (new_spec or {}).get(SPEC_FIELD), actor)
    return hook


__all__ = ["RecordingLevel", "RecordingScope", "change_hook", "check_level", "record_change",
           "request_actor", "stats"]
