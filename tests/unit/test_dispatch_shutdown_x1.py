"""Round 3 X1: robot delete retry (R1), one controller per robot (R3), shutdown order (R6)."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from cloud_common import objects as api_objects
from packages.controllers.mission import fleet_recorder as fr
from packages.controllers.mission import server as server_module
from packages.controllers.mission.server import Robot, RobotServer


def _server():
    srv = RobotServer.__new__(RobotServer)
    srv._logger = MagicMock()
    srv._robots = {}
    srv._database = MagicMock()
    srv._mqtt_client = MagicMock()
    srv._mqtt_prefix = "p"
    srv._shutdown_done = False
    srv.fleet_recorder = None
    srv._leader = MagicMock()
    srv._leader.release = AsyncMock()
    srv._database.close_pool = AsyncMock()
    srv.push_telemetry = False
    srv.mission_ctrl_url = None
    return srv


def _robot_obj(name="r1"):
    return api_objects.RobotObjectV1(name=name, status={})


# R1 --------------------------------------------------------------------------------------
@pytest.mark.unit
async def test_failed_db_delete_leaves_no_zombie_and_is_retried_by_a_fresh_controller():
    srv = _server()
    calls = []

    async def set_lifecycle(*a):
        calls.append(a)
        if len(calls) == 1:
            raise RuntimeError("db down")
    srv._database.set_lifecycle = set_lifecycle
    srv._database.get_object = AsyncMock(return_value=_robot_obj())
    r = srv._get_or_create_robot("r1")
    r._robot_object = _robot_obj()
    await r._delete_robot_object()            # must not raise
    assert "r1" not in srv._robots            # no zombie with a dead run loop
    assert r._alive is False
    # the row is still PENDING_DELETE: its next delivery builds a fresh controller
    fresh = srv._get_or_create_robot("r1")
    assert fresh is not r and fresh._alive
    fresh._robot_object = _robot_obj()
    await fresh._delete_robot_object()
    assert len(calls) == 2 and "r1" not in srv._robots
    fresh.shutdown()


# R3 --------------------------------------------------------------------------------------
@pytest.mark.unit
async def test_mqtt_creation_race_reuses_the_controller_created_meanwhile():
    srv = _server()
    srv._unknown_robots = {}
    srv.fleet_recorder = None
    created = []

    async def get_object(cls, name):
        # a resync-driven robot change creates the controller while we await the lookup
        created.append(srv._get_or_create_robot(name))
        await asyncio.sleep(0)
        return _robot_obj(name)
    srv._database.get_object = get_object
    msg = SimpleNamespace(name="r1", payload=object())
    await srv._process_mqtt_message(msg)
    assert srv._robots["r1"] is created[0]
    assert len(created) == 1
    for r in srv._robots.values():
        r.shutdown()


@pytest.mark.unit
async def test_all_creation_sites_share_one_controller():
    srv = _server()
    a = srv._get_or_create_robot("r1")
    await srv._process_robot_change(_robot_obj("r1"))
    assert srv._robots["r1"] is a
    a.shutdown()


# R6 --------------------------------------------------------------------------------------
class _Rec:
    def __init__(self, order, stop_s=0.0):
        self.order, self.stop_s = order, stop_s

    async def drain(self, t):
        self.order.append("drain")

    async def stop(self):
        self.order.append("rec.stop")
        await asyncio.sleep(self.stop_s)

    async def close(self):
        self.order.append("rec.close")


@pytest.mark.unit
async def test_shutdown_order_and_robots_shut_down():
    srv = _server()
    order = []
    srv._mqtt_client.disconnect = lambda: order.append("mqtt")
    srv.fleet_recorder = _Rec(order)
    srv._database.close_pool = AsyncMock(side_effect=lambda: order.append("db"))
    srv._leader.release = AsyncMock(side_effect=lambda: order.append("lock"))
    r = srv._get_or_create_robot("r1")
    notify = asyncio.ensure_future(asyncio.sleep(60))
    r._notify_task = notify
    hook = asyncio.ensure_future(asyncio.sleep(60))
    r._background_tasks.add(hook)
    orig = r.shutdown
    r.shutdown = lambda: (order.append("robot"), orig())
    await srv.graceful_shutdown()
    assert order == ["mqtt", "robot", "drain", "rec.stop", "rec.close", "db", "lock"]
    await asyncio.sleep(0)
    assert notify.cancelled() and hook.cancelled() and r._alive is False


@pytest.mark.unit
async def test_shutdown_waits_for_status_flush_and_slow_recorder_does_not_starve_it(
        monkeypatch):
    monkeypatch.setattr(server_module, "SHUTDOWN_RECORDER_STOP_S", 0.05)
    srv = _server()
    order = []
    srv.fleet_recorder = _Rec(order, stop_s=5.0)      # hangs, is cut at 0.05 s
    srv._database.close_pool = AsyncMock(side_effect=lambda: order.append("db"))
    r = srv._get_or_create_robot("r1")

    async def write():
        await asyncio.sleep(0.1)
        order.append("status written")
    task = asyncio.ensure_future(write())
    r._status_write_tasks.add(task)
    await srv.graceful_shutdown()
    assert order.index("status written") < order.index("rec.stop")
    assert "db" in order and srv._leader.release.await_count == 1


@pytest.mark.unit
async def test_recorder_drain_writes_pending_finish_and_close_closes_pool():
    wrote = []

    class FinishOp(fr._Op):
        lifecycle = True

        async def run(self, recorder):
            wrote.append("finish")
    pool = MagicMock()
    pool.close = AsyncMock()
    rec = fr.FleetRecorder(pool=pool, start_writer=False)
    rec._ops.append(FinishOp())
    await rec.drain(2.0)
    assert wrote == ["finish"] and not rec._ops
    await rec.close()
    pool.close.assert_awaited_once()


@pytest.mark.unit
async def test_recorder_drain_is_bounded():
    class Stuck(fr._Op):
        lifecycle = True

        async def run(self, recorder):
            await asyncio.sleep(30)
    rec = fr.FleetRecorder(pool=MagicMock(), start_writer=False)
    rec._ops.append(Stuck())
    await asyncio.wait_for(rec.drain(0.05), 2.0)
    assert len(rec._ops) == 1
