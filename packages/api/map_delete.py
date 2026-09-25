"""Map delete as a background saga (docs/satinav-fleet-agent-phase0-v2.md §6 F1, WP11).

DELETE /api/v1/maps/{map_id} only marks the map: one statement sets `lifecycle = 'DELETING'`
on its `mapobjectv1` row (inserting the row if there is none, so leftovers that exist only in
ArangoDB/MinIO can still be deleted) and the route returns 202. A background task then:

1. takes the per-map session advisory lock `map_delete:<map_id>` on a dedicated connection
   (`pg_try_advisory_lock`). Whoever holds it is the only runner across API workers; everyone
   else skips the map. The lock goes with the connection, so a crashed worker never keeps it;
2. deletes the map's graph from ArangoDB and its image bucket from MinIO. Both deletes are
   idempotent and treat "not there" as success, so a re-run after a partial success is safe;
3. on success deletes the row (only while it is still DELETING) and NOTIFYs `DELETED`;
4. on failure records the attempt in the map's status (`delete_attempts`, `delete_error`) and
   retries with exponential backoff. After MAP_DELETE_MAX_ATTEMPTS attempts in one round it
   writes MAP.DELETE_FAILED in the same transaction as the status and stops. The map stays
   DELETING (hidden) until the next API start or another DELETE, which starts a new round.

MAP.DELETE_FAILED: ts = `delete_requested_at`, discriminator `map:<map_id>:attempts:<total>`,
so the event_id is deterministic and each exhausted round writes exactly one event.

Not deleted: ROS bags (a robot's recordings; they only name the map in a sidecar) and base
models (not per map). Robots whose `current_map` is the deleted map keep it; the assign routes
refuse a DELETING map with 409 (packages/api/main.py).

At startup every worker lists the DELETING maps and starts a task for each; the lock makes
only one of them run it.
"""

import asyncio
import datetime
import json
import logging
import uuid
from typing import Any, Awaitable, Callable, Dict, List, Optional

from cloud_common.objects.map import MapObjectV1, MapSpecV1, MapStatusV1
from cloud_common.objects.object import ObjectLifecycleV1
from packages.api.entrypoint import advisory_lock_key
from packages.events.codes import EventCode, Source
from packages.events.emit import Event, emit

logger = logging.getLogger("ApiDelegationService.map_delete")

MAP_TABLE = MapObjectV1.table_name()
DELETING = ObjectLifecycleV1.DELETING.value
DELETED = ObjectLifecycleV1.DELETED.value

MARK_SQL = (
    f"INSERT INTO {MAP_TABLE} (name, lifecycle, spec, status) "
    f"VALUES (%s, '{DELETING}', %s::jsonb, %s::jsonb) "
    "ON CONFLICT (name) DO UPDATE "
    f"SET lifecycle = '{DELETING}', status = {MAP_TABLE}.status || %s::jsonb "
    f"WHERE {MAP_TABLE}.lifecycle <> '{DELETING}' "
    "RETURNING name")
LOAD_SQL = f"SELECT lifecycle, status FROM {MAP_TABLE} WHERE name = %s"
LIST_SQL = f"SELECT name FROM {MAP_TABLE} WHERE lifecycle = '{DELETING}' ORDER BY name"
RECORD_SQL = (f"UPDATE {MAP_TABLE} SET status = status || %s::jsonb "
              f"WHERE name = %s AND lifecycle = '{DELETING}'")
FINISH_SQL = f"DELETE FROM {MAP_TABLE} WHERE name = %s AND lifecycle = '{DELETING}'"
NOTIFY_SQL = "SELECT pg_notify(%s, %s)"


def lock_key(map_id: str) -> int:
    return advisory_lock_key(f"map_delete:{map_id}")


def failed_discriminator(map_id: str, attempts: int) -> str:
    """Part of MAP.DELETE_FAILED's event_id (stored data: never change the format)."""
    return f"map:{map_id}:attempts:{attempts}"


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _parse_ts(value: Any) -> Optional[datetime.datetime]:
    if isinstance(value, datetime.datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


class MapDeleter:
    """Runs map deletes in the background. `delete_graph` / `delete_images` are the blocking
    ArangoDB / MinIO deletes (return True when the data is gone, including "was not there");
    they run in a thread."""

    def __init__(self, db: Any, delete_graph: Callable[[str], bool],
                 delete_images: Callable[[str], bool], *, max_attempts: int = 5,
                 backoff_s: float = 2.0, backoff_max_s: float = 60.0,
                 sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
                 now: Callable[[], datetime.datetime] = _utcnow):
        self._db = db
        self._steps = (("graph_db", delete_graph), ("image_db", delete_images))
        self.max_attempts = max(1, int(max_attempts))
        self._backoff_s = backoff_s
        self._backoff_max_s = backoff_max_s
        self._sleep = sleep
        self._now = now
        self._tasks: Dict[str, asyncio.Task] = {}
        self._resume_task: Optional[asyncio.Task] = None
        self._publisher_id = uuid.uuid4()

    def backoff(self, attempt: int) -> float:
        """Seconds to wait after the `attempt`-th failed attempt of a round (1-based)."""
        return min(self._backoff_max_s, self._backoff_s * (2 ** max(0, attempt - 1)))

    # --- entry points ------------------------------------------------------------------------
    async def request(self, map_id: str) -> Dict[str, Any]:
        """Mark `map_id` DELETING (idempotent) and start its cleanup; the 202 body."""
        now = self._now()
        patch = {"delete_requested_at": now.isoformat(), "delete_attempts": 0,
                 "delete_error": None}
        status = json.loads(MapStatusV1().json())
        status.update(patch)
        async with self._db.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(MARK_SQL, (map_id, MapSpecV1().json(), json.dumps(status),
                                                json.dumps(patch)))
                marked = await cursor.fetchone() is not None
                if marked:
                    await cursor.execute(NOTIFY_SQL, (MAP_TABLE,
                                                      f"{self._publisher_id} {map_id} {DELETING}"))
        if marked:
            logger.info("Map %s marked DELETING", map_id)
        self.start(map_id)
        return {"success": True, "map_id": map_id, "lifecycle": DELETING,
                "message": f"Map {map_id} is being deleted"}

    def start(self, map_id: str) -> asyncio.Task:
        """Start the cleanup task for `map_id` unless this worker already runs one."""
        task = self._tasks.get(map_id)
        if task is None or task.done():
            task = asyncio.get_running_loop().create_task(
                self._run(map_id), name=f"api.map_delete.{map_id}")
            self._tasks[map_id] = task
            task.add_done_callback(lambda t, m=map_id: self._forget(m, t))
        return task

    def _forget(self, map_id: str, task: asyncio.Task) -> None:
        if self._tasks.get(map_id) is task:
            del self._tasks[map_id]

    def task_for(self, map_id: str) -> Optional[asyncio.Task]:
        return self._tasks.get(map_id)

    async def resume(self) -> List[str]:
        """Start a task for every map left DELETING (e.g. by an API restart)."""
        async with self._db.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(LIST_SQL)
                names = [row[0] for row in await cursor.fetchall()]
        for name in names:
            self.start(name)
        if names:
            logger.info("Resuming the delete of %d map(s): %s", len(names), ", ".join(names))
        return names

    def start_resume(self) -> None:
        """resume() in the background, for startup. Never raises."""
        async def _resume():
            try:
                await self.resume()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the API must start regardless
                logger.exception("Could not resume pending map deletes")
        try:
            self._resume_task = asyncio.get_running_loop().create_task(
                _resume(), name="api.map_delete.resume")
        except Exception:  # noqa: BLE001
            logger.exception("Could not start resuming pending map deletes")

    async def stop(self) -> None:
        """Cancel running cleanups (their maps stay DELETING and resume at the next start)."""
        tasks = list(self._tasks.values())
        if self._resume_task is not None:
            tasks.append(self._resume_task)
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except BaseException:  # noqa: BLE001 - CancelledError or the task's own error
                pass
        self._tasks.clear()
        self._resume_task = None

    # --- the saga ----------------------------------------------------------------------------
    async def _run(self, map_id: str) -> None:
        try:
            conn = await self._db.dedicated_connection()
        except Exception:  # noqa: BLE001
            logger.exception("Map %s: no lock connection; the delete resumes at the next start "
                             "or DELETE", map_id)
            return
        try:
            cursor = await conn.execute("SELECT pg_try_advisory_lock(%s)", (lock_key(map_id),))
            if not (await cursor.fetchone())[0]:
                logger.info("Map %s: another worker runs its delete", map_id)
                return
            await self._round(map_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Map %s: delete stopped on an error; it resumes at the next start "
                             "or DELETE", map_id)
        finally:
            try:
                await conn.close()  # releases the advisory lock
            except Exception:  # noqa: BLE001
                pass

    async def _round(self, map_id: str) -> None:
        for attempt in range(1, self.max_attempts + 1):
            row = await self._load(map_id)
            if row is None or row[0] != DELETING:
                return  # finished by another runner, or never marked
            status = row[1] or {}
            error = await self._attempt(map_id)
            if error is None:
                await self._finish(map_id)
                return
            total = int(status.get("delete_attempts") or 0) + 1
            requested_at = _parse_ts(status.get("delete_requested_at")) or self._now()
            exhausted = attempt >= self.max_attempts
            logger.warning("Map %s: delete attempt %d/%d failed: %s", map_id, attempt,
                           self.max_attempts, error)
            await self._record_failure(map_id, total, error, requested_at, exhausted)
            if exhausted:
                return
            await self._sleep(self.backoff(attempt))

    async def _attempt(self, map_id: str) -> Optional[str]:
        """One pass over both stores; None if everything is gone, else the error text."""
        errors = []
        for name, delete in self._steps:
            try:
                result = await asyncio.to_thread(delete, map_id)
                # The stores return a bool; a {"success": ..., "error": ...} dict is accepted too.
                if isinstance(result, dict):
                    ok, detail = bool(result.get("success")), result.get("error") or "delete failed"
                else:
                    ok, detail = bool(result), "delete failed"
            except Exception as exc:  # noqa: BLE001
                ok, detail = False, str(exc) or type(exc).__name__
            if not ok:
                errors.append(f"{name}: {detail}")
        return "; ".join(errors) or None

    async def _load(self, map_id: str) -> Optional[tuple]:
        async with self._db.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(LOAD_SQL, (map_id,))
                return await cursor.fetchone()

    async def _finish(self, map_id: str) -> None:
        async with self._db.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(FINISH_SQL, (map_id,))
                if cursor.rowcount:
                    await cursor.execute(NOTIFY_SQL, (MAP_TABLE,
                                                      f"{self._publisher_id} {map_id} {DELETED}"))
        logger.info("Map %s deleted", map_id)

    async def _record_failure(self, map_id: str, total: int, error: str,
                              requested_at: datetime.datetime, exhausted: bool) -> None:
        """The attempt count and error in the map's status; MAP.DELETE_FAILED in the same
        transaction when the round is exhausted (its own savepoint: a failing event write
        never loses the status)."""
        try:
            async with self._db.connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(RECORD_SQL, (json.dumps(
                        {"delete_attempts": total, "delete_error": error}), map_id))
                    if not cursor.rowcount or not exhausted:
                        return
                try:
                    async with conn.transaction():
                        await emit(conn, Event(
                            EventCode.MAP_DELETE_FAILED, requested_at, source=Source.API,
                            discriminator=failed_discriminator(map_id, total),
                            payload={"map_name": map_id, "attempts": total, "error": error}))
                except Exception:  # noqa: BLE001
                    logger.exception("Map %s: could not write MAP.DELETE_FAILED", map_id)
                    return
            logger.error("Map %s: delete failed after %d attempts (%s); it stays DELETING",
                         map_id, total, error)
        except Exception:  # noqa: BLE001 - a status write must not end the retries
            logger.exception("Map %s: could not record the failed delete attempt", map_id)
