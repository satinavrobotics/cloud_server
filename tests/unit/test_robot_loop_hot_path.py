"""Round-3 X4: the robot loop's error log is rate limited and has a traceback, the mission
update on each state message does not deep-copy the status, state payloads are parsed by the
consumer of the MQTT queue (and only when not replaced), the charging hook logs through the
robot and is tracked."""
import asyncio
import json
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("py_trees")
pytest.importorskip("psycopg")

import cloud_common.objects as api_objects  # noqa: E402
import cloud_common.objects.mission as mission_object  # noqa: E402
import cloud_common.objects.robot as robot_object  # noqa: E402
import packages.controllers.mission.server as server_module  # noqa: E402
from packages.controllers.mission import behavior_tree  # noqa: E402
from packages.controllers.mission.server import Robot, RobotServer  # noqa: E402
from packages.database.postgres import PostgresDatabase  # noqa: E402

pytestmark = pytest.mark.unit


def _robot():
    server = MagicMock()
    server.push_telemetry = False
    server.mission_ctrl_url = "http://mc"
    robot = Robot("r1", AsyncMock(spec=PostgresDatabase), MagicMock(), "p", server)
    robot._logger = MagicMock()
    robot._logger.isEnabledFor.return_value = True
    robot._robot_object = api_objects.RobotObjectV1(name="r1", status={})
    robot._robot_object.status.online = True
    return robot


# --- 1: loop error log ----------------------------------------------------------------------

def test_a_repeating_loop_error_is_logged_once_with_a_traceback_then_summarised(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(server_module.time, "monotonic", lambda: clock[0])
    r = _robot()
    err = ValueError("same every time")
    for _ in range(500):
        r._log_loop_error(err)
        clock[0] += 0.1                                  # 50 s in all
    assert r._logger.error.call_count == 1
    assert r._logger.error.call_args.kwargs["exc_info"] is err
    clock[0] += 20                                       # past the interval
    r._log_loop_error(err)
    assert r._logger.error.call_count == 2
    summary = r._logger.error.call_args.args[-1]
    assert "repeated 500 more" in summary
    # a different exception is a new kind: logged at once with its traceback
    r._log_loop_error(KeyError("other"))
    assert r._logger.error.call_count == 3
    assert r._logger.error.call_args.kwargs["exc_info"] is not False


def test_the_tracked_error_kinds_are_bounded():
    r = _robot()
    for i in range(server_module.LOOP_ERROR_MAX_KINDS * 3):
        r._log_loop_error(ValueError(f"v{i}"))
    assert len(r._loop_errors) <= server_module.LOOP_ERROR_MAX_KINDS


async def test_run_logs_a_failing_handler_with_traceback_once():
    r = _robot()
    n = []

    async def boom(message):
        n.append(1)
        if len(n) >= 20:
            r._alive = False
        raise RuntimeError("deterministic")
    r._on_robot_change = boom
    for _ in range(20):
        await r._messages.put(api_objects.RobotObjectV1(name="a", status={}))
    await asyncio.wait_for(Robot.run(r), timeout=2)
    assert r._logger.error.call_count == 1
    assert r._logger.error.call_args.kwargs["exc_info"] is not False


# --- debug is lazy --------------------------------------------------------------------------

def test_debug_formats_nothing_when_debug_is_off():
    r = _robot()
    r._logger.isEnabledFor.return_value = False

    class Boom:
        def __str__(self):
            raise AssertionError("formatted")
    r.debug("[%s] x", Boom())
    r._logger.debug.assert_not_called()
    r._logger.isEnabledFor.return_value = True
    r.debug("[%s] x", 7)
    assert r._logger.debug.call_args.args[-1] == "[7] x"


# --- 2: charging hook -----------------------------------------------------------------------

async def test_charging_hook_logs_through_the_robot_and_is_tracked(monkeypatch):
    r = _robot()
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_post():
        started.set()
        await release.wait()
    r._post_charging_mission = fake_post
    r._robot_object.battery.recommended_minimum = 50
    r._robot_object.status.battery_level = 10.0
    r._robot_object.status.state = robot_object.RobotStateV1.IDLE
    import packages.controllers.mission.vda5050_types as types
    await r._on_client_message(types.VDA5050State(
        headerId=1, timestamp="", nodeStates=[], edgeStates=[], errors=[]))
    await asyncio.wait_for(started.wait(), 1)
    assert len(r._background_tasks) == 1
    release.set()
    await asyncio.sleep(0.05)
    assert not r._background_tasks


async def test_charging_hook_failures_use_the_robot_logger(monkeypatch, caplog):
    r = _robot()
    r._charging_hook_request = MagicMock(side_effect=RuntimeError("down"))
    r._charging_hook_busy = True
    with caplog.at_level(logging.DEBUG):
        r._logger = logging.getLogger("Isaac Mission Dispatch")
        await r._post_charging_mission()
    assert r._charging_hook_busy is False
    assert any("[r1]" in rec.getMessage() and "down" in rec.getMessage()
               for rec in caplog.records)
    assert not any(rec.name == "root" for rec in caplog.records)


# --- 3: ON_TASK through the teleop helper ---------------------------------------------------

def test_mission_start_does_not_leave_teleop():
    r = _robot()
    r._current_mission = _mission()
    r._robot_object.status.state = robot_object.RobotStateV1.TELEOP
    r._set_mission_state(mission_object.MissionStateV1.RUNNING)
    assert r._robot_object.status.state == robot_object.RobotStateV1.TELEOP
    r2 = _robot()
    r2._current_mission = _mission()
    r2._set_mission_state(mission_object.MissionStateV1.RUNNING)
    assert r2._robot_object.status.state == robot_object.RobotStateV1.ON_TASK


# --- 5a: mission status writes without a deep copy ------------------------------------------

def _mission():
    return api_objects.MissionObjectV1(
        name="m1", robot="r1",
        mission_tree=[
            {"name": "0", "route": {"waypoints": [{"x": 1.0, "y": 1.0, "theta": 0.0}]},
             "parent": "root"},
            {"name": "1", "route": {"waypoints": [{"x": 2.0, "y": 2.0, "theta": 0.0}]},
             "parent": "root"}],
        status={}, timeout=1000)


def _running_robot():
    r = _robot()
    r._current_mission = _mission()
    r._current_mission.status.node_status = {
        n: mission_object.MissionNodeStatusV1() for n in ("root", "0", "1")}
    r._current_behavior_tree = behavior_tree.MissionBehaviorTree(r._current_mission)
    assert r._current_behavior_tree.create_behavior_tree()
    r._queue_status_write = MagicMock()
    return r


def _mission_writes(r):
    return [c for c in r._queue_status_write.call_args_list
            if c.args[0] is api_objects.MissionObjectV1]


def test_mission_status_is_written_exactly_when_it_changed():
    """Differential against the old deep-copy comparison over a scripted run."""
    r = _running_robot()
    status = r._current_mission.status
    script = [
        lambda: None,                                                     # pending -> running
        lambda: None,                                                     # nothing happens
        lambda: None,
        lambda: setattr(status.node_status["0"], "state",
                        mission_object.MissionStateV1.COMPLETED),         # next node
        lambda: None,
        lambda: setattr(status.node_status["1"], "state",
                        mission_object.MissionStateV1.COMPLETED),         # mission done
        lambda: None,
    ]
    for step in script:
        step()
        before = status.copy(deep=True)
        r._queue_status_write.reset_mock()
        state_before = status.state
        r.update_mission_from_behavior_tree()
        state_changed = status.state != state_before
        # a mission-state change is written by _set_mission_state's own path
        expected = status != before
        wrote_here = bool(_mission_writes(r))
        if not state_changed:
            assert wrote_here == expected, (status, before)
        elif not wrote_here:
            assert expected      # reported through _set_mission_state's return value only


def test_a_node_change_with_the_mission_state_unchanged_is_written():
    r = _running_robot()
    status = r._current_mission.status
    r.update_mission_from_behavior_tree()                 # -> RUNNING
    r._queue_status_write.reset_mock()
    status.node_status["0"].state = mission_object.MissionStateV1.COMPLETED
    r.update_mission_from_behavior_tree()
    assert status.state == mission_object.MissionStateV1.RUNNING
    assert status.current_node == 1
    assert len(_mission_writes(r)) == 1


def test_an_idle_update_writes_nothing_and_copies_nothing(monkeypatch):
    r = _running_robot()
    r.update_mission_from_behavior_tree()
    r._queue_status_write.reset_mock()
    monkeypatch.setattr(mission_object.MissionStatusV1, "copy",
                        lambda *a, **k: pytest.fail("deep copy on the hot path"))
    for _ in range(5):
        r.update_mission_from_behavior_tree()
    assert not _mission_writes(r)


# --- 5c: raw payloads, parsed by the consumer -----------------------------------------------

def _mqtt_server():
    srv = RobotServer.__new__(RobotServer)
    srv._mqtt_messages = asyncio.Queue()
    srv._state_slots = {}
    srv._mqtt_prefix = "uagv"
    srv._event_loop = MagicMock()
    srv._event_loop.call_soon_threadsafe = lambda f, *a: f(*a)
    srv._logger = MagicMock()
    return srv


def _msg(robot, payload):
    m = MagicMock()
    m.topic = f"uagv/{robot}/state"
    m.payload = payload
    return m


def _state(i):
    return json.dumps({"headerId": i, "timestamp": "", "nodeStates": [], "edgeStates": [],
                       "errors": []}).encode()


async def test_state_payloads_are_coalesced_unparsed_and_parsed_by_the_consumer():
    srv = _mqtt_server()
    parsed = []
    orig = server_module.RawStateMessage.parse
    server_module.RawStateMessage.parse = lambda self: (parsed.append(1), orig(self))[1]
    try:
        for i in range(50):
            srv._mqtt_on_message(None, None, _msg("a", _state(i)))
        assert srv._mqtt_messages.qsize() == 1
        assert not parsed                                  # nothing parsed in the paho thread
        seen = []
        srv._process_mqtt_message = AsyncMock(side_effect=lambda m: seen.append(m))
        task = asyncio.ensure_future(srv._handle_mqtt_messages())
        await asyncio.sleep(0.05)
        task.cancel()
    finally:
        server_module.RawStateMessage.parse = orig
    assert len(parsed) == 1 and len(seen) == 1
    assert seen[0].payload.headerId == 49 and seen[0].name == "a"


async def test_a_malformed_state_is_logged_and_skipped():
    srv = _mqtt_server()
    srv._mqtt_on_message(None, None, _msg("a", b"{not json"))
    srv._mqtt_on_message(None, None, _msg("b", b'{"headerId": "x"}'))
    seen = []
    srv._process_mqtt_message = AsyncMock(side_effect=lambda m: seen.append(m))
    task = asyncio.ensure_future(srv._handle_mqtt_messages())
    srv._mqtt_on_message(None, None, _msg("c", _state(1)))
    await asyncio.sleep(0.05)
    task.cancel()
    assert [m.name for m in seen] == ["c"]
    assert srv._logger.warning.call_count == 2
