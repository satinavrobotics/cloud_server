"""The real mission dispatcher `Robot` wired to the Phase 0 recorder (WP6): runs, mission
events and robot events come out of normal mission execution, and a recorder that fails
in any way leaves mission execution exactly as it is without one.

Closed loop against the dummy robot's goal follower, MQTT and the mission database
mocked, the recorder on a fake database (tests/unit/fleet_recorder_fakes.py).
"""
import asyncio
import datetime
import json

import pytest

pytest.importorskip("py_trees")
pytest.importorskip("psycopg")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import cloud_common.objects as api_objects  # noqa: E402
from cloud_common.objects import mission as mission_object  # noqa: E402
from packages.controllers.mission import fleet_recorder as fr  # noqa: E402
from packages.controllers.mission import server as mission_server  # noqa: E402
from packages.controllers.mission.server import Robot  # noqa: E402
from packages.controllers.mission.vda5050_types import vda5050_types as types  # noqa: E402
from packages.database.postgres import PostgresDatabase  # noqa: E402
from packages.telemetry_ingest import tables  # noqa: E402
from tests.dummy_robot.goal_follower import GoalFollower  # noqa: E402
from tests.unit.fleet_recorder_fakes import (  # noqa: E402
    T0, codes, connection, make_recorder, queued,
)

pytestmark = pytest.mark.unit

State = mission_object.MissionStateV1


def _make_robot(recorder):
    db = AsyncMock(spec=PostgresDatabase)
    server = MagicMock()
    server.push_telemetry = False
    server.mission_ctrl_url = None
    server.delete_pending_mission = AsyncMock(return_value=False)
    server.fleet_recorder = recorder
    with patch.object(Robot, "run", new=AsyncMock()):
        robot = Robot("r1", db, MagicMock(), "uagv/v2/test", server)
    robot._robot_object = api_objects.RobotObjectV1(name="r1", status={})
    robot._robot_object.status.online = True
    return robot, db


def _mission(name="m1", far=3.0):
    return api_objects.MissionObjectV1(
        name=name, robot="r1", status={}, timeout=600,
        mission_tree=[{"name": "go", "route": {"waypoints": [
            {"x": far, "y": 0.0, "theta": 0.0}]}}])


async def _drive(robot, follower, mission, dt=0.5, max_ticks=200, on_tick=None):
    delivered = 0
    for tick in range(max_ticks):
        calls = robot._mqtt_client.publish.call_args_list
        for call in calls[delivered:]:
            topic, payload = call.args[0], json.loads(call.args[1])
            if topic.endswith("/order"):
                follower.handle_order(types.VDA5050Order(**payload))
            elif topic.endswith("/instantActions"):
                follower.handle_instant_actions(
                    types.VDA5050InstantActions(**payload).instantActions)
        delivered = len(calls)
        if mission.status.state.done:
            return
        if on_tick is not None:
            await on_tick(tick)
        follower.step(dt)
        ts = T0.replace(tzinfo=None) + datetime.timedelta(seconds=tick)
        state = follower.to_state(header_id=tick, timestamp=ts.isoformat(),
                                  batteryState=types.VDA5050BatteryState(
                                      batteryCharge=80.0, charging=False, batteryVoltage=None,
                                      batteryHealth=None, reach=None))
        await robot._on_state_message(types.VDA5050State(**json.loads(state.json())))
    raise AssertionError(f"mission stuck in {mission.status.state}")


async def _start(robot, mission):
    robot._missions[mission.name] = mission
    await robot._try_start_mission()
    assert mission.status.state == State.RUNNING


def _mission_states_written(db):
    return [json.loads(c.args[2].json())["state"] for c in db.update_status.call_args_list
            if c.args and c.args[0] is api_objects.MissionObjectV1]


async def test_completed_mission_records_run_events_and_telemetry(tmp_path):
    recorder, fdb, clock = make_recorder(tmp_path, global_level="full")
    robot, _ = _make_robot(recorder)
    mission = _mission()
    await _start(robot, mission)
    await _drive(robot, GoalFollower(speed=2.0), mission)
    await recorder.run_pending_ops()

    [run] = fdb.runs.values()
    assert run["run_id"] == fr.run_uuid("m1", mission.status.run_id)
    assert run["state"] == "COMPLETED" and run["passes_completed"] == 1
    assert run["recording_level"] == "full"
    assert sorted(codes(fdb.events_by_code())) == ["MISSION.RUN_FINISHED", "MISSION.RUN_STARTED"]
    assert robot._current_mission is None

    events = queued(recorder)
    changes = [(e["payload"]["old"], e["payload"]["new"]) for e in events
               if e["code"] == "ROBOT.STATE_CHANGED"]
    assert changes == [("IDLE", "ON_TASK"), ("ON_TASK", "IDLE")]
    # Both robot state changes happen inside the run and are attributed to it.
    assert all(e["run_id"] == run["run_id"] for e in events
               if e["code"] == "ROBOT.STATE_CHANGED")


async def test_state_rows_follow_the_messages_at_full(tmp_path):
    recorder, _, _ = make_recorder(tmp_path, global_level="full")
    robot, _ = _make_robot(recorder)
    mission = _mission(far=6.0)
    await _start(robot, mission)
    await _drive(robot, GoalFollower(speed=1.0), mission)
    rows = queued(recorder, tables.ROBOT_STATE_TABLE)
    run_col = tables.ROBOT_STATE_COLUMNS.index("run_id")
    state_col = tables.ROBOT_STATE_COLUMNS.index("state")
    assert rows and rows[0][run_col] == fr.run_uuid("m1", mission.status.run_id)
    assert rows[0][state_col] == "ON_TASK"


async def test_cancelled_mission_run_is_canceled(tmp_path):
    recorder, fdb, _ = make_recorder(tmp_path)
    robot, _ = _make_robot(recorder)
    mission = _mission(far=50.0)
    await _start(robot, mission)

    async def cancel(tick):
        if tick == 3:
            mission.needs_canceled = True
            await robot._send_cancel_order("m1-cancel")
    await _drive(robot, GoalFollower(speed=1.0), mission, dt=1.0, on_tick=cancel)
    await recorder.run_pending_ops()
    [run] = fdb.runs.values()
    assert (run["state"], run["abort_cause"]) == ("CANCELED", "OPERATOR.CANCELED")


async def test_timeout_run_is_timeout(tmp_path):
    recorder, fdb, _ = make_recorder(tmp_path)
    robot, _ = _make_robot(recorder)
    mission = _mission(far=50.0)
    await _start(robot, mission)
    await robot._wait_mission_timeout(0, "m1")
    await recorder.run_pending_ops()
    [run] = fdb.runs.values()
    assert (run["state"], run["abort_cause"]) == ("TIMEOUT", "DISPATCH.TIMEOUT")


async def test_node_failed_and_edge_block_events_from_the_dispatcher(tmp_path):
    recorder, _, _ = make_recorder(tmp_path)
    robot, _ = _make_robot(recorder)
    mission = _mission()
    await _start(robot, mission)
    blocked = types.VDA5050Error(
        errorType="edgeBlocked", errorDescription="Edge blocked",
        errorLevel=types.VDA5050ErrorLevel.WARNING,
        errorReferences=[types.VDA5050ErrorReference(referenceKey="edgeId",
                                                     referenceValue="e3")])
    msg = types.VDA5050State(headerId=1, timestamp=T0.isoformat(), nodeStates=[],
                             edgeStates=[], errors=[blocked], batteryState=None,
                             agvPosition=None, velocity=None)
    robot._event_ts = T0
    assert robot._handle_edge_blocked(msg)
    assert robot._handle_edge_blocked(msg)       # repeated warning: one event
    robot._handle_edge_blocked(msg.copy(update={"errors": []}))
    robot.set_mission_node_state("go", State.FAILED)
    assert [e["code"] for e in queued(recorder) if e["code"].startswith("MISSION.")] == [
        "MISSION.EDGE_BLOCKED", "MISSION.REROUTED", "MISSION.NODE_FAILED"]


class _Exploding:
    """A recorder whose every hook raises."""

    def __getattr__(self, name):
        def hook(*args, **kwargs):
            raise RuntimeError(f"recorder.{name} exploded")
        return hook


async def _run_mission(recorder):
    robot, db = _make_robot(recorder)
    mission = _mission()
    await _start(robot, mission)
    await _drive(robot, GoalFollower(speed=2.0), mission)
    await asyncio.sleep(0)                        # let fire-and-forget status writes run
    return mission, robot, db


@pytest.mark.parametrize("breakage", ["exploding", "database_down", "queue_broken"])
async def test_recording_failures_do_not_change_mission_execution(tmp_path, breakage):
    reference, ref_robot, ref_db = await _run_mission(None)

    if breakage == "exploding":
        recorder = _Exploding()
    else:
        recorder, fdb, _ = make_recorder(tmp_path)
        if breakage == "database_down":
            fdb.unavailable = True
        else:
            def boom(*a, **k):
                raise RuntimeError("queue broken")
            recorder.queue.put_event = boom
            recorder.queue.put_state = boom
            recorder.queue.put_latest = boom
    mission, robot, db = await _run_mission(recorder)
    if breakage != "exploding":
        await recorder.run_pending_ops()           # retries exhaust against the fake

    assert mission.status.state == reference.status.state == State.COMPLETED
    assert _mission_states_written(db) == _mission_states_written(ref_db)
    assert [c.args[0] for c in robot._mqtt_client.publish.call_args_list] == \
        [c.args[0] for c in ref_robot._mqtt_client.publish.call_args_list]
    assert robot._robot_object.status.state == ref_robot._robot_object.status.state


async def test_connection_messages_reach_the_recorder_only(tmp_path):
    recorder, _, _ = make_recorder(tmp_path)
    server = mission_server.RobotServer.__new__(mission_server.RobotServer)
    server._logger = MagicMock()
    server._mqtt_messages = asyncio.Queue()
    server._robots = {"r1": MagicMock(send_message=AsyncMock())}
    server._database = AsyncMock()
    server.fleet_recorder = recorder
    server._mqtt_prefix = "uagv/v2/test"

    msg = MagicMock(topic="uagv/v2/test/r1/connection",
                    payload=connection("ONLINE").json().encode())
    with patch.object(server, "_enqueue",
                      side_effect=lambda q, obj: q.put_nowait(obj)):
        server._mqtt_on_message(None, None, msg)
        server._mqtt_on_message(None, None, MagicMock(
            topic="uagv/v2/test/ghost/connection",
            payload=connection("OFFLINE").json().encode()))
        server._mqtt_on_message(None, None, MagicMock(
            topic="uagv/v2/test/r1/connection",
            payload=connection("OFFLINE", T0.replace(minute=1)).json().encode()))
    task = asyncio.get_running_loop().create_task(server._handle_mqtt_messages())
    await asyncio.sleep(0.05)
    task.cancel()

    [event] = queued(recorder)
    assert (event["code"], event["robot_name"]) == ("ROBOT.OFFLINE", "r1")
    server._robots["r1"].send_message.assert_not_called()
    server._database.get_object.assert_not_called()
