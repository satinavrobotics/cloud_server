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
    view once its own __anext__() is timed out via asyncio.wait_for().

    notifies() deliberately takes no arguments: psycopg 3.0.15 (the version
    actually pinned in requirements.txt) has no `timeout`/`stop_after` kwargs
    on this method at all. The previous version of this fake accepted
    `timeout=`/`stop_after=` because it was written to match watch()'s (buggy)
    call, not the real library — which is exactly how a production TypeError
    (raised on every single call, immediately) went undetected by this test:
    the mock never round-tripped through the real signature.
    """

    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _FakeCursor(self._rows)

    def notifies(self):
        async def _empty():
            return
            yield  # noqa: unreachable — makes this an async generator

        return _empty()


class _StrictSignatureConnection(_FakeConnection):
    """Raises exactly as psycopg 3.0.15 does if called with any argument —
    guards against watch() regressing back to `notifies(timeout=...)`, which
    silently busy-looped (2026-09-13 live incident: one CPU core pinned at
    100%, ~200 full-table resyncs/second, every one of them propagated as a
    change to every mission and robot watching this table) because the
    resulting TypeError was swallowed by watch()'s broad `except Exception`.
    """

    def notifies(self, *args, **kwargs):
        if args or kwargs:
            raise TypeError(
                f"notifies() takes no arguments (got args={args!r}, kwargs={kwargs!r})")
        return super().notifies()


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


@pytest.mark.unit
@pytest.mark.asyncio
async def test_watch_calls_notifies_with_no_arguments(caplog):
    # 2026-09-13 regression: watch() called notifies(timeout=...), which psycopg
    # 3.0.15 doesn't accept at all. That TypeError was swallowed by the broad
    # `except Exception` below (reconnect + continue) on every single iteration,
    # producing a tight busy loop instead of ever timing anything out. A fake
    # whose notifies() raises on any argument catches this the way the real
    # library would, instead of silently accepting whatever watch() happens to
    # pass it.
    connections = [_StrictSignatureConnection(rows=[]),
                   _StrictSignatureConnection(rows=[("robot1", "ALIVE", {}, {})])]
    get_connection = AsyncMock(side_effect=connections)

    watcher = PostgresWatcher("dbname=x user=x host=x", objects.RobotObjectV1, uuid.uuid4())
    watcher._get_connection = get_connection

    caplog.set_level(logging.WARNING)

    result = await watcher.watch().__anext__()

    assert result.name == "robot1"
    # Exactly one reconnect (the stalled first pass) -- not a busy-loop of
    # reconnects from notifies() raising on every call.
    assert get_connection.call_count == 2
