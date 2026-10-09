"""Dispatcher lifecycle: heartbeat gating, graceful shutdown, single-instance leader lock."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from packages.controllers.mission import lifecycle
from packages.controllers.mission import server as server_module


async def _ok():
    return None


async def _fail():
    raise RuntimeError("db down")


@pytest.mark.unit
async def test_heartbeat_touched_only_when_db_and_mqtt_ok(tmp_path):
    path = tmp_path / "hb" / "beat"
    t = [0.0]
    mqtt = [True]
    ping = [_ok]
    hb = lifecycle.Heartbeat(lambda: ping[0](), lambda: mqtt[0], path=str(path),
                             db_ok_max_age_s=15, clock=lambda: t[0])
    assert await hb.beat() is True and path.exists()
    path.unlink()
    mqtt[0] = False
    assert await hb.beat() is False and not path.exists()
    mqtt[0] = True
    ping[0] = _fail
    t[0] = 100.0  # last DB success is stale
    assert await hb.beat() is False and not path.exists()


@pytest.mark.unit
async def test_heartbeat_not_touched_when_db_never_ok(tmp_path):
    path = tmp_path / "beat"
    hb = lifecycle.Heartbeat(_fail, lambda: True, path=str(path))
    assert await hb.beat() is False and not path.exists()


@pytest.mark.unit
async def test_graceful_shutdown_disconnects_mqtt_and_stops_recorder():
    srv = server_module.RobotServer.__new__(server_module.RobotServer)
    srv._logger = MagicMock()
    srv._shutdown_done = False
    srv._robots = {}
    srv._mqtt_client = MagicMock()
    srv.fleet_recorder = MagicMock()
    srv.fleet_recorder.stop = AsyncMock()
    srv.fleet_recorder.close = AsyncMock()
    srv._database = MagicMock()
    srv._database.close_pool = AsyncMock()
    srv._leader = MagicMock()
    srv._leader.release = AsyncMock()
    await srv.graceful_shutdown()
    srv._mqtt_client.disconnect.assert_called_once()
    srv.fleet_recorder.stop.assert_awaited_once()
    srv._database.close_pool.assert_awaited_once()
    srv._leader.release.assert_awaited_once()
    await srv.graceful_shutdown()  # idempotent
    srv._mqtt_client.disconnect.assert_called_once()


@pytest.mark.unit
async def test_graceful_shutdown_survives_failing_steps():
    srv = server_module.RobotServer.__new__(server_module.RobotServer)
    srv._logger = MagicMock()
    srv._shutdown_done = False
    srv._robots = {}
    srv._mqtt_client = MagicMock()
    srv._mqtt_client.disconnect.side_effect = RuntimeError("boom")
    srv.fleet_recorder = None
    srv._database = MagicMock()
    srv._database.close_pool = AsyncMock()
    srv._leader = MagicMock()
    srv._leader.release = AsyncMock()
    await srv.graceful_shutdown()
    srv._leader.release.assert_awaited_once()


class _Cur:
    def __init__(self, got):
        self.got = got

    async def fetchone(self):
        return (self.got,)


class _Conn:
    def __init__(self, free):
        self.free = free
        self.closed = False

    async def execute(self, sql, params=None):
        return _Cur(self.free())

    async def close(self):
        self.closed = True


@pytest.mark.unit
async def test_second_instance_waits_while_lock_is_held():
    held = [True]
    conns = []

    async def connect():
        c = _Conn(lambda: not held[0])
        conns.append(c)
        return c

    lock = lifecycle.LeaderLock(connect, retry_s=0.01)
    task = asyncio.ensure_future(lock.acquire())
    await asyncio.sleep(0.1)
    assert not task.done()
    assert len(conns) > 2 and all(c.closed for c in conns)  # failed attempts leave nothing open
    held[0] = False
    await asyncio.wait_for(task, 1)
    assert not conns[-1].closed


@pytest.mark.unit
async def test_leader_lost_raises():
    class Dead(_Conn):
        async def execute(self, sql, params=None):
            if "pg_try" in sql:
                return _Cur(True)
            raise RuntimeError("connection closed")

    async def connect():
        return Dead(lambda: True)

    lock = lifecycle.LeaderLock(connect, ping_s=0.01)
    await lock.acquire()
    with pytest.raises(lifecycle.LeaderLockLost):
        await asyncio.wait_for(lock.watch(), 1)
