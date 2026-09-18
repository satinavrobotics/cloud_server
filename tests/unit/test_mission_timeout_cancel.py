"""Unit test for the mission-timeout cancelOrder fix.

Field incident: a mission timed out (`_wait_mission_timeout`) and was marked FAILED,
but the robot was never told to abandon its order -- it kept reporting the timed-out
mission's orderId indefinitely. Any subsequent mission dispatched to that robot then
got rejected ("An order is running") and failed the same way after
MAX_ORDER_MISMATCHES, with the operator seeing "Robot did not accept the dispatched
order (still reporting <stale orderId>)" and no way to recover short of manually
publishing a cancelOrder or restarting the robot's VDA5050 client.

`_wait_mission_timeout` must now send a cancelOrder instant action to the robot before
moving on to the next mission, mirroring the explicit-cancel code path in
`_on_mission_change` ("Update a RUNNING mission").
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

import cloud_common.objects as api_objects
import cloud_common.objects.mission as mission_object
import cloud_common.objects.robot as robot_object
import packages.controllers.mission.vda5050_types as types
from packages.controllers.mission.server import Robot
from packages.database.postgres import PostgresDatabase


def _make_robot():
    db = AsyncMock(spec=PostgresDatabase)
    db.update_status = AsyncMock()
    client = MagicMock()
    server = MagicMock()
    server.push_telemetry = False
    server.mission_ctrl_url = None
    server.delete_pending_mission = AsyncMock(return_value=False)
    r = Robot("r1", db, client, "prefix", server)
    r._robot_object = api_objects.RobotObjectV1(name="r1", status={})
    r._robot_object.status.online = True
    return r, db


def _make_running_mission(name="m1", needs_canceled=False):
    mission = api_objects.MissionObjectV1(
        name=name, robot="r1",
        mission_tree=[{"name": "0", "route": {"waypoints": [
            {"x": 1.0, "y": 1.0, "theta": 0.0}]}, "parent": "root"}],
        status={}, timeout=1)
    mission.status.state = mission_object.MissionStateV1.RUNNING
    mission.needs_canceled = needs_canceled
    return mission


@pytest.mark.unit
async def test_timeout_sends_cancel_order_before_failing_mission():
    """A RUNNING mission that times out (not user-cancelled) must still tell the
    robot to abandon the order, so it doesn't keep reporting a stale orderId that
    blocks every mission dispatched after it."""
    r, _ = _make_robot()
    mission = _make_running_mission()
    r._current_mission = mission
    r._send_instant_action = AsyncMock()
    r.get_next_mission = AsyncMock()

    await r._wait_mission_timeout(0, mission.name)

    r._send_instant_action.assert_awaited_once()
    sent_action = r._send_instant_action.await_args.args[0]
    assert sent_action.actionType == types.VDA5050InstantActionType.CANCEL_ORDER
    assert mission.status.state == mission_object.MissionStateV1.FAILED
    assert mission.status.failure_reason == "Mission timed out"
    r.get_next_mission.assert_awaited_once()


@pytest.mark.unit
async def test_timeout_cancel_order_sent_after_needs_canceled_too():
    """A mission the operator already asked to cancel, which then also times out
    waiting for the robot's confirmation, must get the same cancelOrder nudge --
    the whole point is the robot may never have actually stopped either way."""
    r, _ = _make_robot()
    mission = _make_running_mission(needs_canceled=True)
    r._current_mission = mission
    r._send_instant_action = AsyncMock()
    r.get_next_mission = AsyncMock()

    await r._wait_mission_timeout(0, mission.name)

    r._send_instant_action.assert_awaited_once()
    assert mission.status.state == mission_object.MissionStateV1.CANCELED


@pytest.mark.unit
async def test_blocked_mission_timeout_does_not_send_cancel_order():
    """A mission blocked on an impassable edge is legitimately waiting for an
    operator reroute -- the timeout must still no-op entirely for it, cancelOrder
    included, exactly as before this fix."""
    r, _ = _make_robot()
    mission = _make_running_mission()
    mission.status.blocked = True
    r._current_mission = mission
    r._send_instant_action = AsyncMock()
    r.get_next_mission = AsyncMock()

    await r._wait_mission_timeout(0, mission.name)

    r._send_instant_action.assert_not_awaited()
    r.get_next_mission.assert_not_awaited()
    assert mission.status.state == mission_object.MissionStateV1.RUNNING


@pytest.mark.unit
async def test_timeout_does_not_duplicate_an_outstanding_cancel_order():
    """The realistic needs_canceled state: the explicit cancel is still in
    _current_instant_actions (handle_instant_action() keeps resending it), so the
    timeout must not mint a second cancelOrder on top of it."""
    r, _ = _make_robot()
    mission = _make_running_mission(needs_canceled=True)
    r._current_mission = mission
    r._send_instant_action = AsyncMock()
    r.get_next_mission = AsyncMock()
    await r._send_cancel_order(f"{mission.name}-instantaction-n0")
    r._send_instant_action.reset_mock()

    await r._wait_mission_timeout(0, mission.name)

    r._send_instant_action.assert_not_awaited()
    assert mission.status.state == mission_object.MissionStateV1.CANCELED
