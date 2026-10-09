"""Process lifecycle of the dispatcher: single-instance lock, liveness heartbeat, shutdown.

- LeaderLock: a session-level Postgres advisory lock on a dedicated connection, taken before
  MQTT connects. A second dispatcher would share the fixed MQTT client id and double-publish
  orders, so it waits (the old one may still be shutting down) instead of running.
- Heartbeat: a file touched by a coroutine on the main event loop only while the database
  answers and MQTT is connected; the compose healthcheck fails when the file goes stale.
"""
import asyncio
import hashlib
import logging
import os
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("Isaac Mission Dispatch")

LEADER_LOCK_NAME = "mission_dispatch_leader"
LEADER_RETRY_S = 3.0
LEADER_PING_S = 5.0
LEADER_PING_TIMEOUT_S = 10.0

HEARTBEAT_FILE_ENV = "MISSION_DISPATCH_HEARTBEAT_FILE"
DEFAULT_HEARTBEAT_FILE = "/tmp/mission_dispatch/heartbeat"
HEARTBEAT_PERIOD_S = 5.0
# How long a successful SELECT 1 keeps counting as "the database is fine".
HEARTBEAT_DB_OK_MAX_AGE_S = 15.0
HEARTBEAT_DB_TIMEOUT_S = 5.0

# Whole graceful shutdown; compose stop_grace_period must exceed it.
SHUTDOWN_TIMEOUT_S = 13.0


def advisory_lock_key(name: str) -> int:
    """A stable signed bigint for a lock name (same scheme as packages/api/entrypoint.py)."""
    return int.from_bytes(hashlib.sha256(name.encode("utf-8")).digest()[:8], "big", signed=True)


LEADER_LOCK_KEY = advisory_lock_key(LEADER_LOCK_NAME)


class LeaderLockLost(Exception):
    """The connection holding the leader lock died: another instance may now run."""


class LeaderLock:
    """`connect` is an async factory returning an autocommit connection (execute/close)."""

    def __init__(self, connect: Callable[[], Awaitable[Any]],
                 retry_s: float = LEADER_RETRY_S, ping_s: float = LEADER_PING_S):
        self._connect = connect
        self._retry_s = retry_s
        self._ping_s = ping_s
        self._conn: Any = None

    async def _try_lock(self) -> bool:
        conn = await self._connect()
        try:
            cur = await conn.execute("SELECT pg_try_advisory_lock(%s)", (LEADER_LOCK_KEY,))
            row = await cur.fetchone()
        except BaseException:
            await self._close(conn)
            raise
        if row and row[0]:
            self._conn = conn
            return True
        await self._close(conn)
        return False

    async def acquire(self) -> None:
        """Block until this process holds the lock; Postgres being down is retried too."""
        warned = False
        while True:
            try:
                if await self._try_lock():
                    logger.info("Holding the dispatcher leader lock (key %d)", LEADER_LOCK_KEY)
                    return
                if not warned:
                    logger.warning("Another dispatcher instance holds the leader lock; "
                                   "waiting for it to exit")
                    warned = True
            except asyncio.CancelledError:
                raise
            except Exception as err:  # pylint: disable=broad-except
                logger.warning("Leader lock attempt failed (%s); retrying", err)
            await asyncio.sleep(self._retry_s)

    async def watch(self) -> None:
        """Runs for the process lifetime; raises LeaderLockLost when the connection is gone."""
        while True:
            await asyncio.sleep(self._ping_s)
            try:
                await asyncio.wait_for(self._conn.execute("SELECT 1"), LEADER_PING_TIMEOUT_S)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # pylint: disable=broad-except
                raise LeaderLockLost(f"leader lock connection lost: {err}") from err

    @staticmethod
    async def _close(conn: Any) -> None:
        try:
            await conn.close()
        except Exception:  # pylint: disable=broad-except
            pass

    async def release(self) -> None:
        """Closing the session releases the lock."""
        conn, self._conn = self._conn, None
        if conn is not None:
            await self._close(conn)


class Heartbeat:
    """Touches `path` every `period_s` while the database answered within `db_ok_max_age_s`
    and MQTT is connected. `ping_db` is an async callable that raises on failure."""

    def __init__(self, ping_db: Callable[[], Awaitable[Any]], mqtt_connected: Callable[[], bool],
                 path: Optional[str] = None, period_s: float = HEARTBEAT_PERIOD_S,
                 db_ok_max_age_s: float = HEARTBEAT_DB_OK_MAX_AGE_S,
                 clock: Callable[[], float] = None):
        import time
        self._ping_db = ping_db
        self._mqtt_connected = mqtt_connected
        self.path = path or os.getenv(HEARTBEAT_FILE_ENV, DEFAULT_HEARTBEAT_FILE)
        self._period_s = period_s
        self._db_ok_max_age_s = db_ok_max_age_s
        self._clock = clock or time.monotonic
        self._db_ok_at: Optional[float] = None

    async def beat(self) -> bool:
        """One iteration; True when the file was touched."""
        try:
            await asyncio.wait_for(self._ping_db(), HEARTBEAT_DB_TIMEOUT_S)
            self._db_ok_at = self._clock()
        except asyncio.CancelledError:
            raise
        except Exception as err:  # pylint: disable=broad-except
            logger.warning("Heartbeat: database check failed: %s", err)
        db_ok = self._db_ok_at is not None and \
            self._clock() - self._db_ok_at <= self._db_ok_max_age_s
        try:
            mqtt_ok = bool(self._mqtt_connected())
        except Exception:  # pylint: disable=broad-except
            mqtt_ok = False
        if not (db_ok and mqtt_ok):
            return False
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(self.path, "a", encoding="utf-8"):
                pass
            os.utime(self.path, None)
        except OSError as err:
            logger.warning("Heartbeat: cannot touch %s: %s", self.path, err)
            return False
        return True

    async def run(self) -> None:
        while True:
            await self.beat()
            await asyncio.sleep(self._period_s)
