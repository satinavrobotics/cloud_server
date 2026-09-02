"""Unit test for PostgresWatcher.watch()'s notify-timeout recovery.

Context: a mission was stuck (client cancel request accepted by the REST API,
but mission-dispatch never processed it) with nothing in the logs pointing at
why. Root cause: watch()'s `async for notification in self._connection.notifies()`
has no timeout, so if Postgres silently stops delivering NOTIFYs on an
otherwise-open connection (observed in practice, no exception raised), the
watcher hangs forever with nothing logged — it never reaches the
except-and-reconnect path below it, because nothing ever raises.

This test drives watch() with a fully mocked connection whose notifies() ends
immediately (simulating "no notification arrived"), and asserts the watcher
logs a warning and reconnects rather than hanging.
"""
import logging
import uuid

import pytest
from unittest.mock import AsyncMock

import cloud_common.objects as objects
from packages.database.postgres import PostgresWatcher


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, *args, **kwargs):
        return None

    async def fetchall(self):
        return self._rows

    async def fetchone(self):
        return None


class _FakeConnection:
    """A connection whose notifies() ends immediately with nothing yielded —
    exactly what a stalled LISTEN channel looks like from watch()'s point of
    view once notifies() is given a timeout.
    """

    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _FakeCursor(self._rows)

    def notifies(self, *, timeout=None, stop_after=None):
        async def _empty():
            return
            yield  # noqa: unreachable — makes this an async generator

        return _empty()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_watch_reconnects_and_warns_after_a_stalled_notify_timeout(caplog):
    # First connection has nothing to resync and its notifies() stalls out
    # immediately; the second (post-reconnect) connection has one row to
    # resync, which is where watch() should finally yield.
    connections = [_FakeConnection(rows=[]),
                   _FakeConnection(rows=[("robot1", "ALIVE", {}, {})])]
    get_connection = AsyncMock(side_effect=connections)

    watcher = PostgresWatcher("dbname=x user=x host=x", objects.RobotObjectV1, uuid.uuid4())
    watcher._get_connection = get_connection

    caplog.set_level(logging.WARNING)

    result = await watcher.watch().__anext__()

    assert result.name == "robot1"
    # Initial connect + one reconnect triggered by the stalled first pass.
    assert get_connection.call_count == 2
    stall_warnings = [r for r in caplog.records if "no notification" in r.message]
    assert len(stall_warnings) == 1
