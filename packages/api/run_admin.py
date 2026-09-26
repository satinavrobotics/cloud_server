"""Writes on the Phase 0 run tables: archiving runs and deleting a mission's runs.

    POST   /api/v1/runs/archive                    archive_runs()
    DELETE /api/v1/missions/{name}[?with_reruns=]   delete_mission()

The read routes (packages/api/fleet_reads.py) stay read-only; everything here is one pooled
connection = one transaction (PostgresDatabase.connection: commits on a clean exit, rolls back
on an exception), so a request either happens completely or not at all.

Archive (reversible, nothing lost)
----------------------------------
`mission_runs.archived_at` (migration 20260926_01_run_archive) is set to the transaction's
now() (archived=true) or back to NULL (archived=false). Archived runs are hidden from
GET /api/v1/runs unless `archived=include|only`; GET /runs/{id} and the timeline return them
as usual, and nothing else (events, trajectory, telemetry) is touched. Open runs
(ended_at IS NULL) are never archived: they are skipped and counted in `skipped_running`.
Only rows whose state actually changes are updated, so `updated` is exact. One RUN.ARCHIVED /
RUN.UNARCHIVED event per request that changed anything.

Mission delete (irreversible)
-----------------------------
Deletes the mission object(s) exactly as before (row removed from missionobjectv1 + the
`<publisher> <name> DELETED` NOTIFY the watchers expect) and, in the same transaction, the
mission's recorded runs:

- mission_runs WHERE mission_name = <name> (with_reruns: the `mission` family rule of
  GET /api/v1/runs, fleet_reads.family_filter);
- fleet_events WHERE run_id IN those runs (compressed chunks included: TimescaleDB 2.30 does
  DML on compressed chunks, tests/integration/run_admin covers it);
- mission_trajectory WHERE run_id IN those runs, or untagged rows of the mission name(s)
  (run_id IS NULL: rows of a run that was never tagged).

Robot telemetry (robot_state_ts, diagnostics_ts, their rollups, robot_latest) is per robot and
time, not per run, and is kept. One MISSION.DELETED event (robot_name and run_id NULL, so a
later run delete never removes it) records what was deleted.

Refusal (409, nothing deleted): a targeted mission object is RUNNING, or a targeted run is
still open (ended_at IS NULL). Cancel it and wait for it to finish first. A PENDING mission is
deleted as before (it has no run yet). The mission and run rows are locked (FOR UPDATE)
before the check, so a status write by the dispatcher cannot slip between check and delete.

404: without with_reruns, when the mission object does not exist (as before, even if runs of
that name exist; with_reruns=true cleans those). With with_reruns=true, only when neither a
mission object nor a run matches.

Late writes by mission-dispatch: open runs are refused, so dispatch has nothing in flight for
a deleted run except a finish whose start was never written; fleet_recorder._close_run skips
re-creating a run whose mission object no longer exists. Not guarded (rare, harmless, left as
is): events already in dispatch's spill file (written while its own database connection was
down) replayed after the delete are strays carrying a run_id no run row has; and a PENDING
mission deleted in the instant dispatch starts it can leave one run row, which the startup
reconciliation closes as ABORTED / DISPATCH.ORPHANED (mission_missing), as it does today.
"""

import logging
import uuid
from typing import Any, Dict, List, Optional, Sequence, Tuple

import psycopg
from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel

from packages.api.fleet_reads import family_filter
from packages.events.codes import EventCode, Source
from packages.events.emit import Event, emit

logger = logging.getLogger("ApiDelegationService.run_admin")

MISSION_TABLE = "missionobjectv1"
DELETED = "DELETED"
RUNNING = "RUNNING"
MAX_ARCHIVE_RUN_IDS = 500
# run_ids listed in a MISSION.DELETED / RUN.ARCHIVED payload (the counts are always exact).
EVENT_MAX_RUN_IDS = 500


def _body_invalid(loc: Sequence[Any], msg: str,
                  type_: str = "value_error") -> RequestValidationError:
    """A body error raised like FastAPI's own (bad uuid, extra field, ...), so every 422 of the
    route has the same shape (packages/utils/fastapi_helpers.py: `errors[].loc/msg/type`)."""
    return RequestValidationError([{"loc": ("body", *loc), "msg": msg, "type": type_}])


def _valid_name(value: Optional[str]) -> bool:
    return isinstance(value, str) and value != "" and "\x00" not in value


def _unavailable(exc: BaseException) -> HTTPException:
    return HTTPException(503, "Fleet history is not available (database migrations not "
                              "applied)")


def _run_id_payload(run_ids: Sequence[Any]) -> Dict[str, Any]:
    return {"run_ids": [str(r) for r in run_ids[:EVENT_MAX_RUN_IDS]],
            "run_ids_truncated": len(run_ids) > EVENT_MAX_RUN_IDS}


async def _trace(conn: Any, event: Event) -> None:
    """Write the trace event in its own savepoint: a failed event write never undoes the
    change it describes."""
    try:
        async with conn.transaction():
            await emit(conn, event)
    except Exception:  # noqa: BLE001
        logger.exception("Could not write %s", event.code.value)


# --- archive ---------------------------------------------------------------------------------

class ArchiveRequest(BaseModel):
    """Body of POST /api/v1/runs/archive: exactly one of `run_ids` / `mission`."""
    run_ids: Optional[List[uuid.UUID]] = None
    mission: Optional[str] = None
    archived: bool = True

    class Config:
        extra = "forbid"


def check_archive_request(request: ArchiveRequest) -> None:
    """422 unless exactly one selector is given, run_ids has 1..MAX_ARCHIVE_RUN_IDS entries
    and mission is a non-empty name."""
    if (request.run_ids is None) == (request.mission is None):
        raise _body_invalid([], "exactly one of `run_ids` or `mission` is required")
    if request.run_ids is not None and not 1 <= len(request.run_ids) <= MAX_ARCHIVE_RUN_IDS:
        raise _body_invalid(["run_ids"], f"run_ids must contain 1 to {MAX_ARCHIVE_RUN_IDS} "
                                         "run ids")
    if request.mission is not None and not _valid_name(request.mission):
        raise _body_invalid(["mission"], "mission must be a non-empty mission name")


async def archive_runs(db: Any, request: ArchiveRequest) -> Dict[str, int]:
    """Archive (archived=true) or restore (false) the selected runs.
    `{"updated": n, "skipped_running": m}`: n rows whose archive state changed; m open runs
    the selector matched that were not archived (always 0 when restoring)."""
    check_archive_request(request)
    if request.run_ids is not None:
        selector, params = "run_id = ANY(%s::uuid[])", (sorted(set(request.run_ids)),)
    else:
        selector, params = family_filter("mission_name"), (request.mission,) * 3
    try:
        async with db.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT now()")
                now = (await cur.fetchone())[0]
                if request.archived:
                    await cur.execute(
                        f"UPDATE mission_runs SET archived_at = %s WHERE {selector} "
                        "AND ended_at IS NOT NULL AND archived_at IS NULL RETURNING run_id",
                        (now, *params))
                    changed = [row[0] for row in await cur.fetchall()]
                    await cur.execute(f"SELECT count(*) FROM mission_runs WHERE {selector} "
                                      "AND ended_at IS NULL", params)
                    skipped = (await cur.fetchone())[0]
                else:
                    await cur.execute(
                        f"UPDATE mission_runs SET archived_at = NULL WHERE {selector} "
                        "AND archived_at IS NOT NULL RETURNING run_id", params)
                    changed = [row[0] for row in await cur.fetchall()]
                    skipped = 0
            if changed:
                changed.sort(key=str)
                code = EventCode.RUN_ARCHIVED if request.archived else EventCode.RUN_UNARCHIVED
                await _trace(conn, Event(
                    code, now, source=Source.API,
                    discriminator=f"runs:{len(changed)}:{changed[0]}",
                    payload={"count": len(changed), "mission": request.mission,
                             **_run_id_payload(changed)}))
    except psycopg.errors.UndefinedTable as exc:
        raise _unavailable(exc) from exc
    return {"updated": len(changed), "skipped_running": int(skipped)}


# --- mission delete --------------------------------------------------------------------------

def _selectors(name: str, with_reruns: bool) -> Tuple[Tuple[str, str, str], Tuple[str, ...]]:
    """WHERE clauses on (missionobjectv1.name, mission_runs.mission_name,
    mission_trajectory.mission_id) and their parameters (the same for all three)."""
    if with_reruns:
        return ((family_filter("name"), family_filter("mission_name"),
                 family_filter("mission_id")), (name,) * 3)
    return ("name = %s", "mission_name = %s", "mission_id = %s"), (name,)


def _refusal(name: str, running: List[str], open_runs: List[Tuple[Any, str]]) -> HTTPException:
    parts = []
    if running:
        parts.append("mission(s) RUNNING: " + ", ".join(running))
    if open_runs:
        parts.append("run(s) still open: " + ", ".join(f"{run_id} ({mission})"
                                                         for run_id, mission in open_runs))
    return HTTPException(409, f"Cannot delete mission {name}: {'; '.join(parts)}. Cancel it "
                              "and wait for the run to finish; nothing was deleted")


async def delete_mission(db: Any, name: str, *, with_reruns: bool = False,
                         publisher_id: Optional[uuid.UUID] = None) -> Dict[str, Any]:
    """Delete the mission object (with_reruns: every mission of the family) and its runs,
    their run_id-tagged events and trajectory rows, in one transaction. See the module
    docstring for the refusal and 404 rules."""
    publisher_id = publisher_id or uuid.uuid4()
    (mission_where, run_where, trajectory_where), params = _selectors(name, with_reruns)
    try:
        async with db.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT now()")
                now = (await cur.fetchone())[0]
                await cur.execute(
                    f"SELECT name, spec->>'robot', status->>'state' FROM {MISSION_TABLE} "
                    f"WHERE {mission_where} ORDER BY name FOR UPDATE", params)
                missions = await cur.fetchall()
                await cur.execute(
                    f"SELECT run_id, mission_name, robot_name, ended_at IS NULL "
                    f"FROM mission_runs WHERE {run_where} ORDER BY started_at, run_id "
                    "FOR UPDATE", params)
                runs = await cur.fetchall()
                if not missions and not with_reruns:
                    raise HTTPException(404, f"Could not find object {name}")
                if not missions and not runs:
                    raise HTTPException(404, f"Did not find a mission or run matching "
                                             f"\"{name}\" (with reruns)")
                running = [m[0] for m in missions if m[2] == RUNNING]
                open_runs = [(r[0], r[1]) for r in runs if r[3]]
                if running or open_runs:
                    raise _refusal(name, running, open_runs)

                run_ids = [r[0] for r in runs]
                await cur.execute(
                    "DELETE FROM mission_trajectory WHERE run_id = ANY(%s::uuid[]) "
                    f"OR (run_id IS NULL AND {trajectory_where})", (run_ids, *params))
                deleted_trajectory = cur.rowcount
                await cur.execute("DELETE FROM fleet_events WHERE run_id = ANY(%s::uuid[])",
                                  (run_ids,))
                deleted_events = cur.rowcount
                await cur.execute("DELETE FROM mission_runs WHERE run_id = ANY(%s::uuid[])",
                                  (run_ids,))
                deleted_runs = cur.rowcount
                names = [m[0] for m in missions]
                if names:
                    await cur.execute(f"DELETE FROM {MISSION_TABLE} WHERE name = ANY(%s::text[])",
                                      (names,))
                    for mission in names:  # what PostgresDatabase.set_lifecycle notifies
                        await cur.execute("SELECT pg_notify(%s, %s)", (
                            MISSION_TABLE, f"{publisher_id} {mission} {DELETED}"))
            robots = sorted({m[1] for m in missions if m[1]} | {r[2] for r in runs})
            await _trace(conn, Event(
                EventCode.MISSION_DELETED, now, source=Source.API,
                discriminator=f"mission:{name}:{'family' if with_reruns else 'one'}",
                payload={"mission_name": name, "with_reruns": with_reruns,
                         "deleted_missions": names, "deleted_runs": deleted_runs,
                         "deleted_events": deleted_events,
                         "deleted_trajectory": deleted_trajectory, "robots": robots,
                         **_run_id_payload(run_ids)}))
    except psycopg.errors.UndefinedTable as exc:
        raise _unavailable(exc) from exc
    result: Dict[str, Any] = {
        "success": True,
        "message": (f"Mission {name} and its reruns deleted" if with_reruns
                    else f"Mission {name} deleted"),
        "deleted_runs": deleted_runs, "deleted_events": deleted_events,
        "deleted_trajectory": deleted_trajectory,
    }
    if with_reruns:
        result["deleted_missions"] = names
    return result
