"""A mission that is paused for teleoperation must still be the same mission when the operator hands the robot back.

Field incident (2026-10-19, robot formidable-peacock): the mission ran to ~70%, the operator took over for ~12 s and gave the robot
back. 1.3 s later the dispatcher failed the mission with "Order rejected: An order is running" (orderUpdateError, a WARNING) and
marked the robot idle, while the robot -- which had simply resumed its paused order -- drove the rest of the route by itself.

What went wrong: starting teleop makes the robot report missionStatus "canceled" for its (paused) order. The dispatcher read that as
"the robot dropped the order", queued a NEW revision, and sent it as soon as teleop ended. The robot, resuming the original order,
refused the second one; the state still carried the older revision of this run, which the rejection rule counted as "not our order".

Fixes under test: (1) no verdict about the order while a teleop pause is in force or just ended, (2) a rejection that comes while the
robot is carrying on an earlier revision of this very run is realigned (cancel, then send again), never a failed mission.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

import cloud_common.objects.robot as robot_object
import cloud_common.objects.mission as mission_object
import packages.controllers.mission.server as server_module
import packages.controllers.mission.vda5050_types as types
from packages.controllers.mission.server import CancelPurpose, NEW_REVISION, Robot

from tests.unit.test_mission_order_rejection import _error
from tests.unit.test_mission_stuck_order import _arm_running_mission, _build_state, _make_robot

pytestmark = pytest.mark.unit

TELEOP = robot_object.RobotStateV1.TELEOP
ON_TASK = robot_object.RobotStateV1.ON_TASK


def _canceled_info(state):
    state.information = [types.VDA5050Info(infoType="missionStatus", infoDescription="canceled", infoLevel="INFO")]
    return state


def _executing(state):
    state.nodeStates = [types.VDA5050NodeState(nodeId="n", sequenceId=0, released=True)]
    return state


def _armed(r):
    mission = _arm_running_mission(r)
    r._current_order_id = MagicMock(return_value="m1-n0")
    r._robot_object.status.state = ON_TASK
    return mission


def test_a_canceled_report_while_the_robot_is_in_teleop_is_not_a_dropped_order():
    r, _ = _make_robot()
    mission = _armed(r)
    r._robot_object.status.state = TELEOP
    r.update_mission_state(_canceled_info(_build_state(order_id="m1-n0")), [])
    assert r._pending_send is None, "queued a new revision for an order that is only paused"
    assert "m1-n0" not in r._dead_order_ids
    assert mission.status.state == mission_object.MissionStateV1.RUNNING


def test_nor_when_teleop_was_just_requested_and_not_yet_acknowledged():
    r, _ = _make_robot()
    _armed(r)
    r._current_instant_actions["t1"] = types.VDA5050Action(actionType=types.NVInstantActionType.START_TELEOP, actionId="t1")
    r.update_mission_state(_canceled_info(_build_state(order_id="m1-n0")), [])
    assert r._pending_send is None
    assert "m1-n0" not in r._dead_order_ids


def test_nor_in_the_moments_after_teleop_ended_but_a_dropped_order_is_still_noticed_later(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(server_module, "time", type("T", (), {"monotonic": staticmethod(lambda: now[0])}))
    r, _ = _make_robot()
    _armed(r)
    r._robot_object.status.state = TELEOP
    r.update_robot_state([types.VDA5050Action(actionType=types.NVInstantActionType.STOP_TELEOP, actionId="s1")])
    assert r._robot_object.status.state == ON_TASK                  # the stopTeleop ack
    r.update_mission_state(_canceled_info(_build_state(order_id="m1-n0")), [])
    assert r._pending_send is None                                   # within the grace: the robot is resuming

    now[0] += Robot.TELEOP_RESUME_GRACE_S + 1
    r.update_mission_state(_canceled_info(_build_state(order_id="m1-n0")), [])
    assert r._pending_send == NEW_REVISION                           # a genuinely dropped order is still handled


def test_a_pending_send_waits_while_teleop_holds_the_robot():
    r, _ = _make_robot()
    _armed(r)
    r._robot_object.status.state = TELEOP
    assert r._teleop_hold() is True
    r._robot_object.status.state = ON_TASK
    assert r._teleop_hold() is False


async def test_a_pending_send_is_not_flushed_during_the_hold():
    r, _ = _make_robot()
    _armed(r)
    r._pending_send = NEW_REVISION
    r._flush_pending_send = AsyncMock()
    r._robot_object.status.state = TELEOP
    await r._on_client_message(_build_state(order_id="m1-n0"))
    r._flush_pending_send.assert_not_awaited()
    r._robot_object.status.state = ON_TASK
    await r._on_client_message(_build_state(order_id="m1-n0"))
    r._flush_pending_send.assert_awaited_once()


async def test_a_cancel_the_operator_asked_for_still_ends_the_mission_in_teleop():
    r, _ = _make_robot()
    mission = _armed(r)
    r._robot_object.status.state = TELEOP
    mission.needs_canceled = True
    r.update_mission_state(_canceled_info(_build_state(order_id="m1-n0")), [])
    assert mission.status.state == mission_object.MissionStateV1.CANCELED


async def test_a_rejection_while_the_robot_carries_on_an_earlier_revision_of_this_run_is_realigned_not_failed():
    r, _ = _make_robot()
    mission = _armed(r)
    mission.status.run_id = "abcd1234"
    mission.status.order_rev = 1
    prefix_old = server_module.order_ids.run_prefix("m1", "abcd1234", 0)
    prefix_new = server_module.order_ids.run_prefix("m1", "abcd1234", 1)
    r._current_order_id = MagicMock(return_value=f"{prefix_new}-n0")
    r._cancel_order = AsyncMock(return_value=True)
    state = _executing(_build_state(order_id=f"{prefix_old}-n0"))
    state.errors = [_error(types.VDA5050ErrorLevel.WARNING, "An order is running", [("orderId", f"{prefix_new}-n0")], etype="orderUpdateError")]

    await r._on_client_message(state)

    assert mission.status.state == mission_object.MissionStateV1.RUNNING, mission.status.failure_reason
    r._cancel_order.assert_awaited_once()
    assert r._cancel_order.await_args.args[0] == CancelPurpose.REPLACE


async def test_a_rejection_from_a_robot_that_runs_someone_elses_order_still_fails_the_mission():
    r, _ = _make_robot()
    mission = _armed(r)
    r._cancel_order = AsyncMock(return_value=True)
    state = _executing(_build_state(order_id="some-other-mission-n3"))
    state.errors = [_error(types.VDA5050ErrorLevel.WARNING, "An order is running", [("orderId", "m1-n0")], etype="orderUpdateError")]

    await r._on_client_message(state)

    assert mission.status.state == mission_object.MissionStateV1.FAILED
    r._cancel_order.assert_not_awaited()
