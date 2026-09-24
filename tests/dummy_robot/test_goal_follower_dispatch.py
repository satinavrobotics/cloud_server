"""Closed loop: the real mission dispatcher's Robot controller against the dummy
robot's goal follower, with MQTT and Postgres mocked out (no broker, no database).

Every order/instantActions the dispatcher publishes is fed to the follower, and every
follower state goes back through JSON into Robot._on_client_message, so this checks
that the goal mode's states drive dispatch's behavior tree to COMPLETED (and to
CANCELED on a cancel). Skipped where the dispatcher's dependencies (py_trees,
psycopg) are not installed.
"""
import json

import pytest

pytest.importorskip("py_trees")
pytest.importorskip("psycopg")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import cloud_common.objects as api_objects  # noqa: E402
from cloud_common.objects import mission as mission_object  # noqa: E402
from packages.controllers.mission.server import Robot  # noqa: E402
from packages.controllers.mission.vda5050_types import vda5050_types as types  # noqa: E402
from packages.database.postgres import PostgresDatabase  # noqa: E402
from tests.dummy_robot.goal_follower import GoalFollower  # noqa: E402

pytestmark = pytest.mark.unit

State = mission_object.MissionStateV1


def _make_robot():
    db = AsyncMock(spec=PostgresDatabase)
    server = MagicMock()
    server.push_telemetry = False
    server.mission_ctrl_url = None
    server.delete_pending_mission = AsyncMock(return_value=False)
    # The message-queue loop is not needed: messages are handed to the handlers
    # directly, in order.
    with patch.object(Robot, "run", new=AsyncMock()):
        robot = Robot("r1", db, MagicMock(), "uagv/v2/test", server)
    robot._robot_object = api_objects.RobotObjectV1(name="r1", status={})
    robot._robot_object.status.online = True
    return robot


def _mission(name, tree):
    return api_objects.MissionObjectV1(name=name, robot="r1", mission_tree=tree,
                                       status={}, timeout=600)


async def _drive(robot, follower, mission, dt=0.5, max_ticks=400, on_tick=None):
    """Shuttle messages between dispatcher and follower until the mission is done.
    Returns the follower states that were sent."""
    delivered = 0
    sent = []
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
            return sent
        if on_tick is not None:
            await on_tick(tick)
        follower.step(dt)
        state = follower.to_state(header_id=tick, timestamp="t")
        sent.append(state)
        await robot._on_client_message(types.VDA5050State(**json.loads(state.json())))
    raise AssertionError(f"mission stuck in {mission.status.state}")


async def _start(robot, mission):
    robot._missions[mission.name] = mission
    await robot._try_start_mission()
    assert mission.status.state == State.RUNNING


async def test_route_action_move_mission_completes():
    robot = _make_robot()
    follower = GoalFollower(speed=2.0, action_duration=1.0)
    mission = _mission("m1", [
        # allowedDeviationXY 0 marks user waypoints, whose progress dispatch
        # records in task_status.
        {"name": "go", "route": {"waypoints": [
            {"x": 3.0, "y": 0.0, "theta": 0.0, "allowedDeviationXY": 0.0},
            {"x": 3.0, "y": 4.0, "theta": 0.0, "allowedDeviationXY": 0.0}]}},
        {"name": "scan", "action": {"action_type": "scan_area", "action_parameters": {}}},
        {"name": "back", "route": {"waypoints": [{"x": 0.0, "y": 0.0, "theta": 0.0}]}},
        {"name": "nudge", "move": {"distance": 1.0}},
    ])
    await _start(robot, mission)

    sent = await _drive(robot, follower, mission)

    assert mission.status.state == State.COMPLETED
    assert all(ns.state == State.COMPLETED for ns in mission.status.node_status.values())
    # The follower really went there: it passed through the route's endpoint and
    # finished at the move's goal, idle with nothing left.
    assert any((s.agvPosition.x, s.agvPosition.y) == (3.0, 4.0) for s in sent)
    assert follower.x == pytest.approx(1.0) and follower.y == pytest.approx(0.0)
    assert not follower.has_active_order and not sent[-1].driving
    # Waypoint progress was reported for the multi-waypoint route.
    assert mission.status.task_status["go"] == 1


async def test_cancelled_mission_ends_canceled_and_robot_stops():
    robot = _make_robot()
    follower = GoalFollower(speed=1.0)
    mission = _mission("m2", [
        {"name": "far", "route": {"waypoints": [{"x": 50.0, "y": 0.0, "theta": 0.0}]}},
    ])
    await _start(robot, mission)

    async def cancel_at_tick_3(tick):
        if tick == 3:
            # What _on_mission_change does for POST /mission/{name}/cancel.
            mission.needs_canceled = True
            await robot._send_cancel_order("m2-cancel")

    sent = await _drive(robot, follower, mission, dt=1.0, on_tick=cancel_at_tick_3)

    assert mission.status.state == State.CANCELED
    assert not follower.has_active_order
    assert sent[-1].driving is False and sent[-1].nodeStates == []
    assert follower.x < 50.0
    assert not robot._has_outstanding_cancel()
