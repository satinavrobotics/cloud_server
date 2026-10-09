"""Mission-dispatch audit W1: H3 (crash paths), H4 (bad row in the watcher), M8 (unknown robots)."""
import asyncio
import logging
import uuid

import fastapi
import psycopg
import pytest

pytest.importorskip("py_trees")

from unittest.mock import AsyncMock, MagicMock  # noqa: E402

import cloud_common.objects as objects  # noqa: E402
from packages.controllers.mission import server as dispatch_server  # noqa: E402
from packages.database import postgres  # noqa: E402
from packages.database.postgres import PostgresDatabase, PostgresWatcher  # noqa: E402

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


_real_sleep = asyncio.sleep


def _connecting(make_conn):
    """A _get_connection that yields to the loop, so a hot reconnect loop times out instead of
    starving the test's wait_for."""
    mock = AsyncMock()

    async def connect():
        await _real_sleep(0)
        return make_conn()
    mock.side_effect = connect
    return mock


# --- fakes ------------------------------------------------------------------------------

class _Cursor:
    def __init__(self, rows=(), rowcount=1, one=None, fail=None):
        self._rows, self.rowcount, self._one, self._fail = rows, rowcount, one, fail
        self.executed = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, query, params=None):
        self.executed.append(query)
        if self._fail is not None:
            raise self._fail

    async def fetchall(self):
        return self._rows

    async def fetchone(self):
        return self._one


class _Conn:
    def __init__(self, cursor, notifies=None):
        self._cursor, self._notifies = cursor, notifies or []

    def cursor(self):
        return self._cursor

    def notifies(self):
        items = self._notifies

        async def gen():
            for item in items:
                yield item
        return gen()


def _db(cursor=None, pool_error=None):
    db = PostgresDatabase("d", "u", "p", "h", 1)
    pool = MagicMock()
    if pool_error is not None:
        pool.connection.side_effect = pool_error
    else:
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=_Conn(cursor))
        ctx.__aexit__ = AsyncMock(return_value=False)
        pool.connection.return_value = ctx
    db._pool = pool
    return db


# --- H3 a/b: set_lifecycle ----------------------------------------------------------------

async def test_set_lifecycle_operational_error_raises_not_system_exit():
    db = _db(pool_error=psycopg.OperationalError("pool timeout"))
    with pytest.raises(psycopg.OperationalError):
        await db.set_lifecycle(objects.MissionObjectV1, "m", objects.ObjectLifecycleV1.DELETED,
                               uuid.uuid4())


async def test_set_lifecycle_deleted_on_a_missing_row_is_a_noop():
    cursor = _Cursor(rowcount=0)
    db = _db(cursor)
    await db.set_lifecycle(objects.MissionObjectV1, "gone", objects.ObjectLifecycleV1.DELETED,
                           uuid.uuid4())
    assert not any(q.startswith("DELETE") or "DELETE FROM" in q for q in cursor.executed)


async def test_set_lifecycle_other_lifecycle_on_a_missing_row_is_still_404():
    db = _db(_Cursor(rowcount=0))
    with pytest.raises(fastapi.HTTPException) as err:
        await db.set_lifecycle(objects.MissionObjectV1, "gone",
                               objects.ObjectLifecycleV1.PENDING_DELETE, uuid.uuid4())
    assert err.value.status_code == 404


# --- H4: watcher --------------------------------------------------------------------------

def _robot_row(name):
    return (name, "ALIVE", {}, {})


async def test_resync_with_a_malformed_row_still_yields_the_good_rows(caplog):
    rows = [_robot_row("a"), ("bad", "NOT_A_LIFECYCLE", {}, {}), _robot_row("b")]
    watcher = PostgresWatcher("x", objects.RobotObjectV1, uuid.uuid4())
    watcher._get_connection = _connecting(lambda: _Conn(_Cursor(rows=rows)))
    caplog.set_level(logging.ERROR)
    gen = watcher.watch()
    got = [(await asyncio.wait_for(gen.__anext__(), 2)).name,
           (await asyncio.wait_for(gen.__anext__(), 2)).name]
    assert got == ["a", "b"]
    assert watcher._get_connection.await_count == 1          # no reconnect/resync loop
    assert any("'bad'" in r.getMessage() for r in caplog.records if r.levelname == "ERROR")


async def test_a_notification_whose_row_does_not_parse_is_skipped():
    pub = uuid.uuid4()
    notes = [MagicMock(payload=f"{uuid.uuid4()} bad ALIVE"),
             MagicMock(payload=f"{uuid.uuid4()} good ALIVE")]
    rows = {"bad": ({"nonsense_field_xyz": 1}, "not a dict"), "good": ({}, {})}

    class Cur(_Cursor):
        async def execute(self, query, params=None):
            self._one = rows.get(params[0]) if params else None

    watcher = PostgresWatcher("x", objects.RobotObjectV1, pub)
    watcher._get_connection = AsyncMock(return_value=_Conn(Cur(rows=[]), notifies=notes))
    gen = watcher.watch()
    assert (await asyncio.wait_for(gen.__anext__(), 2)).name == "good"


async def test_notification_name_with_spaces_is_parsed():
    notes = [MagicMock(payload=f"{uuid.uuid4()} my robot ALIVE")]

    class Cur(_Cursor):
        async def execute(self, query, params=None):
            self._one = ({}, {}) if params else None

    watcher = PostgresWatcher("x", objects.RobotObjectV1, uuid.uuid4())
    watcher._get_connection = AsyncMock(return_value=_Conn(Cur(rows=[]), notifies=notes))
    assert (await asyncio.wait_for(watcher.watch().__anext__(), 2)).name == "my robot"


async def test_persistent_watcher_error_backs_off_instead_of_hot_looping(monkeypatch):
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)
        if len(sleeps) >= 6:
            raise asyncio.CancelledError
        await _real_sleep(0)
    monkeypatch.setattr(postgres.asyncio, "sleep", fake_sleep)
    watcher = PostgresWatcher("x", objects.RobotObjectV1, uuid.uuid4())
    watcher._get_connection = _connecting(lambda: _Conn(_Cursor(fail=RuntimeError("boom"))))
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(watcher.watch().__anext__(), 2)
    assert sleeps[:5] == [1, 2, 4, 8, 16]
    assert max(sleeps) <= postgres.WATCHER_ERROR_BACKOFF_MAX_S


# --- H3 c/d, M8: RobotServer ----------------------------------------------------------------

def _server():
    server = dispatch_server.RobotServer.__new__(dispatch_server.RobotServer)
    server._logger = logging.getLogger("test-dispatch")
    server._robots = {}
    server.fleet_recorder = None
    server._mqtt_client = MagicMock()
    server._mqtt_prefix = "p"
    server._database = AsyncMock()
    server._mission_changes = asyncio.Queue()
    server._robot_changes = asyncio.Queue()
    server._mqtt_messages = asyncio.Queue()
    return server


def _mission(name, state_done=True):
    m = MagicMock()
    m.name = name
    m.lifecycle = objects.ObjectLifecycleV1.PENDING_DELETE
    m.status.state.done = state_done
    return m


async def test_mission_handler_loop_survives_an_exception():
    server = _server()
    deleted = []

    async def delete_pending_mission(mission):
        deleted.append(mission.name)
        if mission.name == "m1":
            raise fastapi.HTTPException(404, "gone")
    server.delete_pending_mission = delete_pending_mission
    for name in ("m1", "m2"):
        await server._mission_changes.put(_mission(name))
    task = asyncio.ensure_future(server._handle_mission_changes())
    await asyncio.sleep(0.05)
    assert deleted == ["m1", "m2"]
    assert not task.done()
    task.cancel()


async def test_robot_handler_loop_survives_an_exception():
    server = _server()
    calls = []

    def remove_robot(name):
        calls.append(name)
        if name == "a":
            raise RuntimeError("boom")
    server.remove_robot = remove_robot
    for name in ("a", "b"):
        r = MagicMock()
        r.name = name
        r.lifecycle = objects.ObjectLifecycleV1.DELETED
        await server._robot_changes.put(r)
    task = asyncio.ensure_future(server._handle_robot_changes())
    await asyncio.sleep(0.05)
    assert calls == ["a", "b"]
    assert not task.done()
    task.cancel()


async def test_mqtt_handler_loop_survives_an_exception():
    server = _server()
    bad = MagicMock()
    bad.name = "r1"
    server._robots["r1"] = MagicMock(send_message=AsyncMock(side_effect=RuntimeError("boom")))
    good = MagicMock()
    good.name = "r2"
    server._robots["r2"] = MagicMock(send_message=AsyncMock())
    await server._mqtt_messages.put(bad)
    await server._mqtt_messages.put(good)
    task = asyncio.ensure_future(server._handle_mqtt_messages())
    await asyncio.sleep(0.05)
    server._robots["r2"].send_message.assert_awaited_once_with(good.payload)
    assert not task.done()
    task.cancel()


async def test_watch_changes_restarts_after_a_failure_instead_of_stopping(monkeypatch):
    monkeypatch.setattr(dispatch_server, "WATCH_CHANGES_RETRY_MIN_S", 0.01)
    server = _server()
    server.stop = AsyncMock()
    server._event_loop = asyncio.get_event_loop()
    attempts = []

    class Watcher:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        async def watch(self):
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("boom")
            yield "update"

    server._database.get_watcher = AsyncMock(return_value=Watcher())
    queue = asyncio.Queue()
    task = asyncio.ensure_future(server._watch_changes(objects.MissionObjectV1, queue))
    assert await asyncio.wait_for(queue.get(), 2) == "update"
    assert len(attempts) >= 2
    server._mqtt_client.disconnect.assert_not_called()
    task.cancel()


async def test_unknown_robot_is_looked_up_once_per_ttl_and_warned_once(caplog):
    server = _server()
    server._database.get_object = AsyncMock(side_effect=fastapi.HTTPException(404, "no"))
    caplog.set_level(logging.WARNING)
    for _ in range(5):
        msg = MagicMock()
        msg.name = "ghost"
        await server._process_mqtt_message(msg)
    assert server._database.get_object.await_count == 1
    assert len([r for r in caplog.records if "unknown robot" in r.getMessage()]) == 1


async def test_unknown_robot_entry_expires_and_is_cleared_by_the_robot_watcher(monkeypatch):
    server = _server()
    server._database.get_object = AsyncMock(side_effect=fastapi.HTTPException(404, "no"))
    msg = MagicMock()
    msg.name = "ghost"
    await server._process_mqtt_message(msg)
    # the robot watcher delivers the robot: the next message is looked up again
    robot = MagicMock()
    robot.name = "ghost"
    robot.lifecycle = objects.ObjectLifecycleV1.ALIVE
    monkeypatch.setattr(dispatch_server, "Robot", MagicMock(return_value=MagicMock(
        send_message=AsyncMock())))
    await server._process_robot_change(robot)
    server._robots.pop("ghost")
    await server._process_mqtt_message(msg)
    assert server._database.get_object.await_count == 2
