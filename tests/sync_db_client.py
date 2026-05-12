import asyncio
import uuid
import os
from packages.database.postgres import PostgresDatabase
from cloud_common.objects import ObjectLifecycleV1

class SyncDatabaseClient:
    """Synchronous wrapper for PostgresDatabase testing.

    Uses a fresh psycopg connection per call.

    psycopg AsyncConnection objects are bound to the event loop they were
    created in.  asyncio.run() creates and fully closes a new event loop on
    every call (cancelling any pending I/O callbacks), so the cached
    connection becomes stale after the first call.  Reusing it in subsequent
    calls causes psycopg's internal state machine to hang waiting on I/O from
    a closed loop.

    The fix: reset _connection to None before each operation so that
    _get_connection() always creates a fresh connection on the current loop.
    The previous connection is closed explicitly inside the new loop; if that
    fails (because its original loop is already gone) the exception is
    suppressed.
    """
    def __init__(self, host=None, port=None, dbname=None, user=None, password=None):
        host = host or os.getenv("POSTGRES_HOST", "localhost")
        port = int(port or os.getenv("POSTGRES_PORT", "5432"))
        dbname = dbname or os.getenv("POSTGRES_DB", "mission_dispatch")
        user = user or os.getenv("POSTGRES_USER", "postgres")
        password = password or os.getenv("POSTGRES_PASSWORD", "postgres")
        self._db = PostgresDatabase(dbname, user, password, host, port, max_retries=1)
        self._publisher_id = uuid.uuid4()

    def _run(self, coro):
        old_conn = self._db._connection
        self._db._connection = None

        async def _exec():
            if old_conn is not None:
                try:
                    await old_conn.close()
                except Exception:
                    pass
            return await coro

        return asyncio.run(_exec())

    def is_running(self, timeout=None):
        try:
            if not self._db.is_running():
                asyncio.run(asyncio.wait_for(self._db.async_init(), timeout=timeout or 2.0))
            return self._db.is_running()
        except Exception:
            return False

    def create(self, obj):
        return self._run(self._db.create_object(obj, self._publisher_id))

    def update_spec(self, obj):
        return self._run(self._db.update_spec(type(obj), obj.name, obj.spec, self._publisher_id))

    def update_status(self, obj):
        return self._run(self._db.update_status(type(obj), obj.name, obj.status, self._publisher_id))

    def get(self, obj_class, name):
        return self._run(self._db.get_object(obj_class, name))

    def delete(self, obj_class, name):
        return self._run(self._db.set_lifecycle(obj_class, name, ObjectLifecycleV1.DELETED, self._publisher_id))

    def list(self, obj_class, params=None):
        if params is not None and isinstance(params, dict):
            params = list(params.items())
        return self._run(self._db.list_objects(obj_class, params))
