"""Sites and robot site assignments (docs/satinav-fleet-agent-phase0-v2.md §3.6, §5.5, WP9).

Sites are `siteobjectv1` objects (the object-class convention: table created at startup by
initialize_database, NOTIFY on the table's channel with "<publisher> <name> <lifecycle>").
Each write here is ONE transaction on a pooled connection (PostgresDatabase.connection()):
the object/assignment change, its NOTIFY and its TELEMETRY.RECORDING_CHANGED commit together
(the event in a savepoint, packages/api/recording.py, so a failing event never fails the
write).

Assignments live in `robot_site_assignments` (Alembic, 20260924_01_phase0_core): one row per
stay, `valid` = [from, to), at most one row per robot at any instant (EXCLUDE ... &&). A
robot's current site is its row with an unbounded upper end. `assign_robot()`:

1. checks the robot (404), takes a per-robot transaction-scoped advisory lock (so concurrent
   PUTs for one robot serialise instead of tripping the EXCLUDE constraint), and checks the
   site with FOR SHARE (404; this also blocks a concurrent site delete until commit);
2. reads the clock *after* the lock (`clock_timestamp()`, not the transaction-start `now()`:
   a PUT that waited on the lock must not close its predecessor's range before it began);
3. closes the open row (`upper = ts`) and opens `[ts, ∞)` for the new site (none for
   `site_id: null`); same site again is a no-op that writes nothing;
4. `pg_notify('robot_site_assignments', {"robot_name", "site_id"})` so dispatch and the API's
   telemetry writer switch the robot's site (and recording level) on commit, within ~1 s;
5. `recording.record_assignment_change()`: RECORDING_CHANGED (scope robot) if the robot's
   effective level changes.

An EXCLUDE violation that still happens (a writer bypassing this module) is a 409.
"""

import datetime
import json
import uuid
from typing import Any, Dict, List, Mapping, Optional, Tuple

import psycopg
import pydantic
from fastapi import HTTPException

from cloud_common.objects.object import ObjectLifecycleV1
from cloud_common.objects.robot import RobotObjectV1
from cloud_common.objects.site import SiteObjectV1, SiteSpecV1, SiteStatusV1, valid_site_id
from packages.api import recording
from packages.api.entrypoint import advisory_lock_key
from packages.telemetry_ingest.policy import (
    ASSIGNMENTS_CHANNEL, ASSIGNMENTS_TABLE, SPEC_FIELD, assignment_payload,
)

SITE_TABLE = SiteObjectV1.table_name()
ROBOT_TABLE = RobotObjectV1.table_name()
SPEC_FIELDS = frozenset(SiteSpecV1.__fields__)
# Keys of a site object that are accepted in a request body but never written from it, so a
# client can send back what GET returned.
IGNORED_KEYS = frozenset({"status", "lifecycle"})


def assignment_lock_key(robot_name: str) -> int:
    return advisory_lock_key(f"robot_site_assignment:{robot_name}")


# --- request validation ----------------------------------------------------------------------

def _unprocessable(errors: List[Dict[str, Any]]) -> HTTPException:
    return HTTPException(status_code=422, detail=errors)


def _validation_errors(exc: pydantic.ValidationError) -> List[Dict[str, Any]]:
    return [{"loc": ["body", *err["loc"]], "msg": err["msg"], "type": err["type"]}
            for err in exc.errors()]


def check_site_id(site_id: Any, loc: Tuple[str, ...] = ("body", "name")) -> str:
    if not valid_site_id(site_id):
        raise _unprocessable([{
            "loc": list(loc), "type": "value_error.str.regex",
            "msg": "site id must be 1-100 characters of letters, digits, '_', '.', ':' or "
                   "'-', starting with a letter or digit"}])
    return site_id


def spec_changes(data: Any, site_id: Optional[str] = None) -> Dict[str, Any]:
    """The spec fields in a request body; 422 on unknown keys or a bad level.

    `name` is allowed (and must equal `site_id` when that is given); status/lifecycle are
    ignored."""
    if not isinstance(data, Mapping):
        raise _unprocessable([{"loc": ["body"], "msg": "expected a JSON object",
                               "type": "type_error.dict"}])
    recording.check_level(data)
    unknown = sorted(k for k in data if k not in SPEC_FIELDS and k not in IGNORED_KEYS
                     and k != "name")
    if unknown:
        raise _unprocessable([{"loc": ["body", k], "msg": "extra fields not permitted",
                               "type": "value_error.extra"} for k in unknown])
    if site_id is not None and "name" in data and data["name"] != site_id:
        raise _unprocessable([{"loc": ["body", "name"], "type": "value_error.const",
                               "msg": f"name must be {site_id!r} (a site cannot be renamed)"}])
    return {k: v for k, v in data.items() if k in SPEC_FIELDS}


def build_spec(values: Mapping[str, Any]) -> SiteSpecV1:
    try:
        return SiteSpecV1(**values)
    except pydantic.ValidationError as exc:
        raise _unprocessable(_validation_errors(exc)) from exc


# --- helpers -----------------------------------------------------------------------------------

async def _notify_site(cursor: Any, name: str, lifecycle: str, publisher_id: uuid.UUID) -> None:
    # Same payload as PostgresDatabase._notify, so PostgresWatcher(SiteObjectV1) reads it.
    await cursor.execute("SELECT pg_notify(%s, %s)",
                         (SITE_TABLE, f"{publisher_id} {name} {lifecycle}"))


async def _notify_assignment(cursor: Any, robot_name: str, site_id: Optional[str]) -> None:
    await cursor.execute("SELECT pg_notify(%s, %s)",
                         (ASSIGNMENTS_CHANNEL, assignment_payload(robot_name, site_id)))


def _iso(ts: Optional[datetime.datetime]) -> Optional[str]:
    return ts.isoformat() if ts is not None else None


def assignment_dict(robot_name: str, site_id: str, valid_from: Optional[datetime.datetime],
                    valid_to: Optional[datetime.datetime],
                    assigned_by: Optional[str]) -> Dict[str, Any]:
    return {"robot_name": robot_name, "site_id": site_id, "valid_from": _iso(valid_from),
            "valid_to": _iso(valid_to), "assigned_by": assigned_by,
            "current": valid_to is None}


def _site(name: str, lifecycle: str, spec: Mapping[str, Any], status: Any) -> SiteObjectV1:
    return SiteObjectV1(name=name, lifecycle=ObjectLifecycleV1[lifecycle],
                        status=status or {}, **spec)


# --- sites -------------------------------------------------------------------------------------

async def create_site(db: Any, data: Any, publisher_id: uuid.UUID,
                      actor: Optional[str] = None) -> SiteObjectV1:
    """409 if the id is taken."""
    if not isinstance(data, Mapping) or "name" not in data:
        raise _unprocessable([{"loc": ["body", "name"], "msg": "field required",
                               "type": "value_error.missing"}])
    name = check_site_id(data["name"])
    spec = build_spec(spec_changes(data))
    spec_json = spec.json()
    async with db.connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                f"INSERT INTO {SITE_TABLE} (name, lifecycle, spec, status) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (name) DO NOTHING",
                (name, ObjectLifecycleV1.ALIVE.value, spec_json, SiteStatusV1().json()))
            if cursor.rowcount == 0:
                raise HTTPException(409, f"Site {name!r} already exists")
            await _notify_site(cursor, name, ObjectLifecycleV1.ALIVE.value, publisher_id)
        if spec.telemetry_recording is not None:
            await recording.record_change(conn, recording.RecordingScope.SITE, name, None,
                                          spec.telemetry_recording, actor)
    return _site(name, ObjectLifecycleV1.ALIVE.value, json.loads(spec_json), {})


async def update_site(db: Any, name: str, data: Any, publisher_id: uuid.UUID,
                      actor: Optional[str] = None) -> SiteObjectV1:
    """Partial update: only the given spec fields change. 404 if unknown."""
    changes = spec_changes(data, site_id=name)
    async with db.connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                f"SELECT lifecycle, spec, status FROM {SITE_TABLE} WHERE name = %s FOR UPDATE",
                (name,))
            row = await cursor.fetchone()
            if row is None:
                raise HTTPException(404, f"Did not find \"site\" with name \"{name}\"")
            lifecycle, old_spec, status = row
            spec = build_spec({**old_spec, **changes})
            spec_json = spec.json()
            await cursor.execute(f"UPDATE {SITE_TABLE} SET spec = %s WHERE name = %s",
                                 (spec_json, name))
            await _notify_site(cursor, name, lifecycle, publisher_id)
        if SPEC_FIELD in changes:
            await recording.record_change(conn, recording.RecordingScope.SITE, name,
                                          old_spec.get(SPEC_FIELD), spec.telemetry_recording,
                                          actor)
    return _site(name, lifecycle, json.loads(spec_json), status)


async def delete_site(db: Any, name: str, publisher_id: uuid.UUID,
                      actor: Optional[str] = None) -> List[str]:
    """404 if unknown; 409 while an existing robot is assigned to it. Open assignments of
    robots that no longer exist are closed with it. Returns those robots' names."""
    try:
        return await _delete_site(db, name, publisher_id, actor)
    except psycopg.errors.UndefinedTable as exc:
        raise HTTPException(503, "Site assignments are not available (database migrations "
                                 "not applied)") from exc


async def _delete_site(db: Any, name: str, publisher_id: uuid.UUID,
                       actor: Optional[str]) -> List[str]:
    async with db.connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                f"SELECT spec FROM {SITE_TABLE} WHERE name = %s FOR UPDATE", (name,))
            row = await cursor.fetchone()
            if row is None:
                raise HTTPException(404, f"Did not find \"site\" with name \"{name}\"")
            old_spec = row[0]
            await cursor.execute(
                f"SELECT a.robot_name FROM {ASSIGNMENTS_TABLE} a "
                "WHERE a.site_id = %s AND upper_inf(a.valid) "
                f"AND EXISTS (SELECT 1 FROM {ROBOT_TABLE} r WHERE r.name = a.robot_name) "
                "ORDER BY a.robot_name", (name,))
            assigned = [r[0] for r in await cursor.fetchall()]
            if assigned:
                raise HTTPException(409, {
                    "message": f"Site {name!r} still has robots assigned; move or unassign "
                               "them first (PUT /api/v1/robots/{name}/site)",
                    "robots": assigned})
            await cursor.execute(
                f"UPDATE {ASSIGNMENTS_TABLE} "
                "SET valid = tstzrange(lower(valid), greatest(lower(valid), clock_timestamp()), "
                "'[)') WHERE site_id = %s AND upper_inf(valid) RETURNING robot_name", (name,))
            orphans = [r[0] for r in await cursor.fetchall()]
            for robot_name in orphans:
                await _notify_assignment(cursor, robot_name, None)
            await cursor.execute(f"DELETE FROM {SITE_TABLE} WHERE name = %s", (name,))
            await _notify_site(cursor, name, ObjectLifecycleV1.DELETED.value, publisher_id)
        if old_spec.get(SPEC_FIELD) is not None:
            await recording.record_change(conn, recording.RecordingScope.SITE, name,
                                          old_spec.get(SPEC_FIELD), None, actor)
    return orphans


# --- assignments -------------------------------------------------------------------------------

async def assign_robot(db: Any, robot_name: str, site_id: Optional[str],
                       actor: Optional[str] = None) -> Dict[str, Any]:
    """Move `robot_name` to `site_id` (None: unassign). See the module docstring."""
    if site_id is not None:
        check_site_id(site_id, ("body", "site_id"))
    try:
        async with db.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(f"SELECT 1 FROM {ROBOT_TABLE} WHERE name = %s",
                                     (robot_name,))
                if await cursor.fetchone() is None:
                    raise HTTPException(404, f"Did not find \"robot\" with name "
                                             f"\"{robot_name}\"")
                await cursor.execute("SELECT pg_advisory_xact_lock(%s)",
                                     (assignment_lock_key(robot_name),))
                if site_id is not None:
                    await cursor.execute(
                        f"SELECT 1 FROM {SITE_TABLE} WHERE name = %s "
                        "AND lifecycle <> 'DELETED' FOR SHARE", (site_id,))
                    if await cursor.fetchone() is None:
                        raise HTTPException(404, f"Did not find \"site\" with name "
                                                 f"\"{site_id}\"")
                await cursor.execute("SELECT clock_timestamp()")
                ts = (await cursor.fetchone())[0]
                await cursor.execute(
                    f"SELECT site_id, lower(valid), assigned_by FROM {ASSIGNMENTS_TABLE} "
                    "WHERE robot_name = %s AND upper_inf(valid) FOR UPDATE", (robot_name,))
                current = await cursor.fetchone()
                old_site = current[0] if current is not None else None
                if old_site == site_id:
                    return {"robot_name": robot_name, "site_id": site_id, "changed": False,
                            "assignment": assignment_dict(robot_name, *current[:2], None,
                                                          current[2])
                            if current is not None else None,
                            "previous": None}
                previous = None
                if current is not None:
                    if current[1] is not None and current[1] > ts:
                        ts = current[1]  # clock stepped back: never end before the start
                    await cursor.execute(
                        f"UPDATE {ASSIGNMENTS_TABLE} SET valid = tstzrange(lower(valid), %s, "
                        "'[)') WHERE robot_name = %s AND upper_inf(valid)", (ts, robot_name))
                    previous = assignment_dict(robot_name, old_site, current[1], ts, current[2])
                assignment = None
                if site_id is not None:
                    await cursor.execute(
                        f"INSERT INTO {ASSIGNMENTS_TABLE} (robot_name, site_id, valid, "
                        "assigned_by) VALUES (%s, %s, tstzrange(%s, NULL, '[)'), %s)",
                        (robot_name, site_id, ts, actor))
                    assignment = assignment_dict(robot_name, site_id, ts, None, actor)
                await _notify_assignment(cursor, robot_name, site_id)
            await recording.record_assignment_change(conn, robot_name, old_site, site_id,
                                                     actor, ts)
    except psycopg.errors.ExclusionViolation as exc:
        raise HTTPException(409, f"Overlapping site assignment for robot {robot_name!r}; "
                                 "retry") from exc
    except psycopg.errors.UndefinedTable as exc:
        raise HTTPException(503, "Site assignments are not available (database migrations "
                                 "not applied)") from exc
    return {"robot_name": robot_name, "site_id": site_id, "changed": True,
            "assignment": assignment, "previous": previous}


async def list_assignments(db: Any, robot_name: str) -> List[Dict[str, Any]]:
    """The robot's assignment history, newest first. 404 if the robot is unknown and has no
    history (a deleted robot's history is still returned)."""
    try:
        async with db.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    f"SELECT site_id, lower(valid), upper(valid), assigned_by "
                    f"FROM {ASSIGNMENTS_TABLE} WHERE robot_name = %s AND NOT isempty(valid) "
                    "ORDER BY lower(valid) DESC", (robot_name,))
                rows = await cursor.fetchall()
                if not rows:
                    await cursor.execute(f"SELECT 1 FROM {ROBOT_TABLE} WHERE name = %s",
                                         (robot_name,))
                    if await cursor.fetchone() is None:
                        raise HTTPException(404, f"Did not find \"robot\" with name "
                                                 f"\"{robot_name}\"")
    except psycopg.errors.UndefinedTable as exc:
        raise HTTPException(503, "Site assignments are not available (database migrations "
                                 "not applied)") from exc
    return [assignment_dict(robot_name, site, lower, upper, by)
            for site, lower, upper, by in rows]
