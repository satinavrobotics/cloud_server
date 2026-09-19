"""
Robot teleop state in the mission dispatcher.

A robot in TELEOP (held by a pause_order or a startTeleop) only leaves it through
stopTeleop, and the dispatcher only sends stopTeleop while it believes the robot is in
TELEOP. So nothing but a teleop instant action may move the robot out of TELEOP --
neither an acknowledged cancelOrder / factsheetRequest, nor a mission ending.
"""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import cloud_common.objects as api_objects
import cloud_common.objects.mission as mission_object
import cloud_common.objects.robot as robot_object
import packages.controllers.mission.vda5050_types as types
from packages.controllers.mission.server import Robot
from packages.database.postgres import PostgresDatabase

pytestmark = pytest.mark.unit

TELEOP = robot_object.RobotStateV1.TELEOP


def _action(action_type, action_id="a0"):
    return types.VDA5050Action(actionType=action_type, actionId=action_id)


def _make_robot(state=robot_object.RobotStateV1.IDLE):
    """A Robot in ``state``; ``sent`` collects the instant actions it publishes."""
    sent = []
    db = AsyncMock(spec=PostgresDatabase)
    client = MagicMock()

    def _publish(topic, payload, *args, **kwargs):
        if topic.endswith("/instantActions"):
            sent.extend(a["actionType"] for a in json.loads(payload)["instantActions"])
    client.publish = MagicMock(side_effect=_publish)

    server = MagicMock()
    server.push_telemetry = False
    server.mission_ctrl_url = None
    server.disable_request_factsheet = True
    server.delete_pending_mission = AsyncMock(return_value=False)
    r = Robot("r1", db, client, "prefix", server)
    r._robot_object = api_objects.RobotObjectV1(name="r1", status={})
    r._robot_object.status.online = True
    r._robot_object.status.state = state
    return r, sent


def _with_mission(r, state=mission_object.MissionStateV1.RUNNING):
    mission = api_objects.MissionObjectV1(
        name="m1", robot="r1",
        mission_tree=[{"name": "0", "route": {"waypoints": [
            {"x": 1.0, "y": 1.0, "theta": 0.0}, {"x": 2.0, "y": 2.0, "theta": 0.0}]},
            "parent": "root"}],
        status={}, timeout=1000)
    mission.status.state = state
    r._missions[mission.name] = mission
    r._current_mission = mission
    return mission


@pytest.mark.parametrize("action_type", [
    types.VDA5050InstantActionType.CANCEL_ORDER,
    types.VDA5050InstantActionType.FACTSHEET_REQUEST,
])
def test_an_acknowledged_non_teleop_action_does_not_end_teleop(action_type):
    r, _ = _make_robot(TELEOP)
    r.update_robot_state([_action(action_type)])
    assert r._robot_object.status.state == TELEOP


def test_start_teleop_ack_enters_teleop():
    r, _ = _make_robot()
    r.update_robot_state([_action(types.NVInstantActionType.START_TELEOP)])
    assert r._robot_object.status.state == TELEOP


@pytest.mark.parametrize("has_mission,expected", [
    (False, robot_object.RobotStateV1.IDLE),
    (True, robot_object.RobotStateV1.ON_TASK),
])
def test_stop_teleop_ack_leaves_teleop(has_mission, expected):
    r, _ = _make_robot(TELEOP)
    if has_mission:
        _with_mission(r)
    r.update_robot_state([_action(types.NVInstantActionType.STOP_TELEOP)])
    assert r._robot_object.status.state == expected


def test_a_cancel_ack_batched_before_a_stop_teleop_ack_does_not_hide_it():
    # The old loop stopped at the first finished action, whatever its type.
    r, _ = _make_robot(TELEOP)
    r.update_robot_state([_action(types.VDA5050InstantActionType.CANCEL_ORDER, "c"),
                          _action(types.NVInstantActionType.STOP_TELEOP, "s")])
    assert r._robot_object.status.state == robot_object.RobotStateV1.IDLE


async def test_a_mission_ending_does_not_release_a_teleoperated_robot():
    r, _ = _make_robot(TELEOP)
    _with_mission(r, mission_object.MissionStateV1.CANCELED)
    await r.post_mission_completion()
    assert r._robot_object.status.state == TELEOP


async def test_a_mission_ending_still_idles_a_working_robot():
    r, _ = _make_robot(robot_object.RobotStateV1.ON_TASK)
    _with_mission(r, mission_object.MissionStateV1.COMPLETED)
    await r.post_mission_completion()
    assert r._robot_object.status.state == robot_object.RobotStateV1.IDLE


def test_a_mission_starting_does_not_override_teleop():
    r, _ = _make_robot(TELEOP)
    mission = _with_mission(r, mission_object.MissionStateV1.PENDING)
    r._set_mission_state(mission_object.MissionStateV1.RUNNING)
    assert mission.status.start_timestamp is not None
    assert r._robot_object.status.state == TELEOP


def test_a_mission_starting_puts_an_idle_robot_on_task():
    r, _ = _make_robot()
    _with_mission(r, mission_object.MissionStateV1.PENDING)
    r._set_mission_state(mission_object.MissionStateV1.RUNNING)
    assert r._robot_object.status.state == robot_object.RobotStateV1.ON_TASK


async def test_operator_stop_after_a_cancel_still_sends_stop_teleop():
    """The R1 scenario: the mission is cancelled while the robot is held in teleop
    and the robot stays paused after the cancelOrder. The operator's stop must still
    reach it."""
    r, sent = _make_robot(TELEOP)
    _with_mission(r, mission_object.MissionStateV1.CANCELED)
    r.update_robot_state([_action(types.VDA5050InstantActionType.CANCEL_ORDER)])
    await r.post_mission_completion()
    assert r._robot_object.status.state == TELEOP

    stop = api_objects.RobotObjectV1(name="r1", status={})
    stop.switch_teleop = False
    await r._on_robot_change(stop)

    assert sent == [types.NVInstantActionType.STOP_TELEOP.value]


async def test_teleop_released_after_a_mission_ended_returns_to_idle():
    r, _ = _make_robot(TELEOP)
    _with_mission(r, mission_object.MissionStateV1.CANCELED)
    await r.post_mission_completion()
    r.update_robot_state([_action(types.NVInstantActionType.STOP_TELEOP)])
    assert r._robot_object.status.state == robot_object.RobotStateV1.IDLE
    assert r._current_mission is None
