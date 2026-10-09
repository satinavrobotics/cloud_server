"""Order rejection by the robot, foreign FATAL errors, malformed robot data.

- A robot that rejects our order keeps reporting its old orderId: an error naming our
  pending order fails the mission at once, with the robot's description.
- The give-up on a never-adopted order also waits for the resend window.
- A FATAL error naming an earlier order's node is not this mission's failure.
- Malformed user_info / getObjects results do not stall mission handling; a finished
  getObjects action is processed once.
"""
import fastapi
import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import cloud_common.objects.mission as mission_object
import packages.controllers.mission.server as server_module
from packages.controllers.mission import order_ids
from packages.controllers.mission.server import Robot
import packages.controllers.mission.vda5050_types as types

from tests.unit.test_mission_stuck_order import (
    _arm_running_mission, _build_state, _make_robot)


def _error(level, desc, refs, etype="orderError"):
    return types.VDA5050Error(
        errorType=etype, errorDescription=desc, errorLevel=level,
        errorReferences=[types.VDA5050ErrorReference(referenceKey=k, referenceValue=v)
                         for k, v in refs])


def _state(order_id="previous-n1", errors=(), **kwargs):
    state = _build_state(order_id=order_id, **kwargs)
    state.errors = list(errors)
    return state


@pytest.mark.unit
async def test_rejection_naming_our_order_fails_fast_with_description():
    r, _ = _make_robot()
    mission = _arm_running_mission(r)
    r._current_order_id = MagicMock(return_value="m1-n0")
    err = _error(types.VDA5050ErrorLevel.WARNING, "no route to node 3",
                 [("orderId", "m1-n0")], etype="noRouteError")

    await r._on_client_message(_state(errors=[err]))

    assert mission.status.state == mission_object.MissionStateV1.FAILED
    assert "no route to node 3" in mission.status.failure_reason
    assert r._order_mismatch_count == 0


@pytest.mark.unit
async def test_fatal_on_our_node_while_mismatching_fails_fast():
    r, _ = _make_robot()
    mission = _arm_running_mission(r)
    r._current_order_id = MagicMock(return_value="m1-n0")
    err = _error(types.VDA5050ErrorLevel.FATAL, "bad node", [("nodeId", "m1-n0-s1")],
                 etype="whatever")

    await r._on_client_message(_state(errors=[err]))

    assert mission.status.state == mission_object.MissionStateV1.FAILED
    assert "bad node" in mission.status.failure_reason


@pytest.mark.unit
async def test_error_of_another_order_while_mismatching_is_not_a_rejection():
    r, _ = _make_robot()
    mission = _arm_running_mission(r)
    r._current_order_id = MagicMock(return_value="m1-n0")
    err = _error(types.VDA5050ErrorLevel.FATAL, "old", [("orderId", "m1-r1-n0")])

    await r._on_client_message(_state(errors=[err]))

    assert mission.status.state == mission_object.MissionStateV1.RUNNING
    assert r._order_mismatch_count == 1


@pytest.mark.unit
async def test_fast_state_rate_does_not_give_up_before_resend_window(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(server_module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    r, _ = _make_robot()
    mission = _arm_running_mission(r)
    r._order_sent_at = now[0]

    for _ in range(Robot.MAX_ORDER_MISMATCHES + 20):  # 40+ states within a second
        await r._on_client_message(_state())
    assert mission.status.state == mission_object.MissionStateV1.RUNNING

    now[0] += Robot.ORDER_GIVE_UP_MIN_S
    await r._on_client_message(_state())
    assert mission.status.state == mission_object.MissionStateV1.FAILED


@pytest.mark.unit
def test_fatal_with_foreign_node_does_not_fail_current_mission():
    r, _ = _make_robot()
    mission = _arm_running_mission(r)
    mission.status.run_id = "abcd1234"
    prefix = r._order_prefix()
    foreign = _error(types.VDA5050ErrorLevel.FATAL, "old failure",
                     [("nodeId", "m1-rdead0000-n0-s1")])

    assert r.get_mission_errors(_state(order_id=f"{prefix}-n0", errors=[foreign])) is False
    assert not any(n.error_msg for n in mission.status.node_status.values())

    own = _error(types.VDA5050ErrorLevel.FATAL, "new failure",
                 [("actionId", f"{prefix}-n0-s1-n0")])
    assert r.get_mission_errors(_state(order_id=f"{prefix}-n0", errors=[own])) is True
    assert mission.status.node_status["0"].error_msg == "new failure"
    assert mission.status.failure_reason == "new failure"


@pytest.mark.unit
def test_reference_helpers():
    assert order_ids.is_reference_of("m", "m-n2-s3") is True
    assert order_ids.is_reference_of("m", "m-n2-s3-n2") is True
    assert order_ids.is_reference_of("m", "m-n2-s3-policy") is True
    assert order_ids.is_reference_of("m", "m-n2") is True
    assert order_ids.is_reference_of("m", "m-r1-n2-s3") is False
    assert order_ids.is_reference_of("m", "robot-own") is None


@pytest.mark.unit
async def test_malformed_user_info_does_not_stop_state_handling():
    r, _ = _make_robot()
    _arm_running_mission(r)
    r.update_mission_state = MagicMock()
    state = _state(order_id="m1-n0")
    state.information = [types.VDA5050Info(infoType="user_info", infoDescription="{nope",
                                           infoLevel="INFO")]

    await r._on_client_message(state)
    await r._on_client_message(state)

    assert r.update_mission_state.call_count == 2


@pytest.mark.unit
async def test_get_objects_processed_once_and_tolerates_existing_row():
    r, db = _make_robot()
    db.create_object.side_effect = fastapi.HTTPException(400, "already exists")
    done = types.VDA5050ActionState(
        actionId="a1", actionType=types.NVActionType.GET_OBJECTS,
        actionStatus=types.VDA5050ActionStatus.FINISHED,
        resultDescription='[{"bbox2d": {"size_x": 1}, "object_id": 1, "class_id": "x"}]')
    bad = types.VDA5050ActionState(
        actionId="a2", actionType=types.NVActionType.GET_OBJECTS,
        actionStatus=types.VDA5050ActionStatus.FINISHED, resultDescription="not json")
    detection_writes = lambda: [c for c in db.update_status.await_args_list
                                if c.args[0].__name__ == "DetectionResultsObjectV1"]

    for _ in range(3):
        await r._on_client_message(_state(order_id="", action_states=[done, bad]))

    assert len(detection_writes()) == 1
    assert db.create_object.await_count == 1
