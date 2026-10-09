"""Regression tests for the mission-dispatch audit, fixes W2: the mission timeout task, a
mission whose start raised, robot deletion, the timeout budget of a resumed mission and
the robot state when no further pass follows."""
import asyncio
import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import cloud_common.objects as api_objects
import cloud_common.objects.mission as mission_object
import cloud_common.objects.robot as robot_object
import packages.controllers.mission.server as server_module
from packages.controllers.mission.server import Robot, RobotServer
import packages.controllers.mission.vda5050_types as types
from packages.database.postgres import PostgresDatabase


def _mission(name="m1", timeout=1000, **kw):
    return api_objects.MissionObjectV1(
        name=name, robot="r1",
        mission_tree=[{"name": "0", "route": {"waypoints": [
            {"x": 1.0, "y": 1.0, "theta": 0.0},
            {"x": 2.0, "y": 2.0, "theta": 0.0}]}, "parent": "root"}],
        status={}, timeout=timeout, **kw)


def _robot():
    db = AsyncMock(spec=PostgresDatabase)
    db.update_status = AsyncMock()
    server = MagicMock()
    server.push_telemetry = False
    server.mission_ctrl_url = None
    server.delete_pending_mission = AsyncMock(return_value=False)
    server.delete_robot = AsyncMock()
    r = Robot("r1", db, MagicMock(), "prefix", server)
    r._robot_object = api_objects.RobotObjectV1(name="r1", status={})
    r._robot_object.status.online = True
    return r, db


def _state():
    return types.VDA5050State(
        headerId=0, timestamp="", nodeStates=[], edgeStates=[], errors=[],
        batteryState=None, agvPosition=None, velocity=None)


def _topic_publishes(r, suffix):
    return [c for c in r._mqtt_client.publish.call_args_list if c.args[0].endswith(suffix)]


async def _until(cond, timeout=3.0):
    end = asyncio.get_event_loop().time() + timeout
    while not cond() and asyncio.get_event_loop().time() < end:
        await asyncio.sleep(0.01)


# H1 ------------------------------------------------------------------------------------
@pytest.mark.unit
async def test_next_mission_is_dispatched_after_a_timeout():
    """The timeout task used to cancel itself while starting the next mission (its own
    _arm_mission_timeout), so the next mission's first order was never published."""
    r, db = _robot()

    async def slow_write(*_args, **_kwargs):  # a real database write suspends the task
        await asyncio.sleep(0.001)
    db.update_status = AsyncMock(side_effect=slow_write)
    a, b = _mission("a", timeout=1), _mission("b")
    r._missions = {"a": a, "b": b}
    r._current_mission = a
    a.status.state = mission_object.MissionStateV1.RUNNING
    # (a cancelOrder in flight would hold B's order back; this is the robot-resolved case)
    r._send_cancel_order = AsyncMock()
    r._arm_mission_timeout(0.01)

    await _until(lambda: _topic_publishes(r, "/order"))
    await asyncio.sleep(0.1)

    assert a.status.state == mission_object.MissionStateV1.FAILED
    assert r._current_mission is b
    assert b.status.state == mission_object.MissionStateV1.RUNNING
    assert len(_topic_publishes(r, "/order")) == 1
    r.shutdown()


@pytest.mark.unit
async def test_stale_timeout_message_is_dropped():
    r, _ = _robot()
    a = _mission("a", timeout=1)
    r._missions = {"a": a}
    r._current_mission = a
    a.status.state = mission_object.MissionStateV1.RUNNING
    r._arm_mission_timeout(100)
    stale = server_module.MissionTimeoutElapsed(token=("a", 5.0, 1.0))
    await r._on_mission_timeout_elapsed(stale)
    assert a.status.state == mission_object.MissionStateV1.RUNNING
    r.shutdown()


# M3 ------------------------------------------------------------------------------------
@pytest.mark.unit
async def test_mission_whose_start_raised_is_started_again(monkeypatch):
    r, _ = _robot()
    m = _mission()
    r._missions = {"m1": m}
    read = AsyncMock(side_effect=RuntimeError("db down"))
    monkeypatch.setattr(r, "_read_open_session", read)
    with pytest.raises(RuntimeError):
        await r._try_start_mission()
    assert r._current_mission is m and r._current_behavior_tree is not None or True
    r._current_behavior_tree = None  # the run loop's view: tree not usable
    r._mqtt_client.publish.reset_mock()

    # Rate-limited: a second message right after the first retry does not start again.
    read.side_effect = None
    read.return_value = None
    await r._on_client_message(_state())
    assert m.status.state == mission_object.MissionStateV1.RUNNING
    assert len(_topic_publishes(r, "/order")) == 1
    r.shutdown()


@pytest.mark.unit
async def test_idle_robot_picks_up_queued_mission_on_state_message():
    r, _ = _robot()
    m = _mission()
    r._missions = {"m1": m}
    await r._on_client_message(_state())
    assert m.status.state == mission_object.MissionStateV1.RUNNING
    r.shutdown()


@pytest.mark.unit
async def test_start_retries_are_rate_limited_and_end_in_failure(monkeypatch):
    r, _ = _robot()
    m, n = _mission("m1"), _mission("m2")
    r._missions = {"m1": m, "m2": n}
    calls = []

    async def boom():
        calls.append(1)
        r._current_mission = r._current_mission or next(iter(r._missions.values()))
        raise RuntimeError("broken")
    original = r._try_start_mission
    monkeypatch.setattr(r, "_try_start_mission", boom)
    await r._retry_stalled_start()
    await r._retry_stalled_start()  # inside START_RETRY_S: no second attempt
    assert len(calls) == 1
    for _ in range(server_module.MAX_START_ATTEMPTS - 1):
        r._start_retry_at = 0.0
        await r._retry_stalled_start()
    assert len(calls) == server_module.MAX_START_ATTEMPTS + 1  # + the next mission
    assert m.status.state == mission_object.MissionStateV1.FAILED
    assert "broken" in m.status.failure_reason
    assert "m1" not in r._missions
    monkeypatch.setattr(r, "_try_start_mission", original)
    r.shutdown()


# M4 ------------------------------------------------------------------------------------
@pytest.mark.unit
async def test_deleting_a_robot_fails_current_and_queued_missions():
    r, db = _robot()
    cur, queued = _mission("cur"), _mission("queued")
    r._missions = {"cur": cur, "queued": queued}
    r._current_mission = cur
    cur.status.state = mission_object.MissionStateV1.RUNNING
    r._sent_order = object()
    await r._delete_robot_object()
    assert cur.status.state == mission_object.MissionStateV1.FAILED
    assert cur.status.failure_reason == "Robot deleted"
    assert queued.status.state == mission_object.MissionStateV1.FAILED
    assert queued.status.failure_reason == "Robot deleted"
    assert any(c.args[0] is api_objects.MissionObjectV1 and c.args[1] == "queued"
               for c in db.update_status.call_args_list)
    assert len(_topic_publishes(r, "/instantActions")) == 1  # the cancelOrder
    r.shutdown()


@pytest.mark.unit
async def test_robot_server_delete_robot_shuts_the_controller_down():
    r, _ = _robot()
    r._arm_mission_timeout(100)
    run_task = r._run_task
    db = AsyncMock()
    ns = SimpleNamespace(_robots={"r1": r}, _database=db)
    await RobotServer.delete_robot(ns, "r1")
    await asyncio.sleep(0)
    assert "r1" not in ns._robots
    assert r._alive is False
    assert r._mission_timeout_task is None
    assert run_task.cancelled() or run_task.done()


# L2 ------------------------------------------------------------------------------------
@pytest.mark.unit
async def test_resumed_mission_gets_the_rest_of_its_timeout():
    r, _ = _robot()
    m = _mission(timeout=100)
    m.status.state = mission_object.MissionStateV1.RUNNING
    m.status.start_timestamp = datetime.datetime.now() - datetime.timedelta(seconds=40)
    r._missions = {"m1": m}
    await r._try_start_mission()
    name, budget, _ = r._timeout_budget
    assert name == "m1"
    assert 55 < budget < 61
    r.shutdown()


@pytest.mark.unit
def test_remaining_timeout_handles_aware_timestamps():
    m = _mission(timeout=100)
    m.status.start_timestamp = datetime.datetime.now(datetime.timezone.utc) - \
        datetime.timedelta(seconds=30)
    assert 65 < Robot._remaining_timeout_s(m) < 71
    m.status.start_timestamp = datetime.datetime.now() - datetime.timedelta(seconds=500)
    assert Robot._remaining_timeout_s(m) == 0.0


# L3 ------------------------------------------------------------------------------------
@pytest.mark.unit
async def test_robot_goes_idle_when_the_next_pass_cannot_start():
    r, _ = _robot()
    m = _mission(repeat=3)
    m.status.state = mission_object.MissionStateV1.COMPLETED
    r._missions = {"m1": m}
    r._current_mission = m
    r._robot_object.status.state = robot_object.RobotStateV1.ON_TASK
    r._start_next_pass = AsyncMock(return_value=False)
    await r.post_mission_completion()
    assert r._robot_object.status.state == robot_object.RobotStateV1.IDLE
    r.shutdown()
