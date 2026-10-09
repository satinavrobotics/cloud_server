"""Dispatcher: a robot delete ends _on_robot_change (the DELETED lifecycle is not overwritten),
and a failed getObjects result write neither aborts the state message nor is lost."""
import fastapi
import pytest
from unittest.mock import AsyncMock

import cloud_common.objects as api_objects
import packages.controllers.mission.vda5050_types as types
from cloud_common.objects.object import ObjectLifecycleV1

from tests.unit.test_mission_stuck_order import _build_state, _make_robot


@pytest.mark.unit
async def test_pending_delete_ends_robot_change_and_keeps_deleted_lifecycle():
    r, _ = _make_robot()
    r._robot_server.disable_request_factsheet = False
    r._robot_server.delete_robot = AsyncMock()
    r._robot_object.status.factsheet.agv_class = ""
    r._send_instant_action = AsyncMock()
    r._handle_force_cancel = AsyncMock()

    update = api_objects.RobotObjectV1(name=r._robot_object.name, status={})
    update.lifecycle = ObjectLifecycleV1.PENDING_DELETE
    update.switch_teleop = True
    await r._on_robot_change(update)

    assert r._robot_object.lifecycle == ObjectLifecycleV1.DELETED
    assert r._robot_object is not update
    r._send_instant_action.assert_not_awaited()
    r._handle_force_cancel.assert_not_awaited()


def _finished_get_objects(action_id="a1"):
    return types.VDA5050ActionState(
        actionId=action_id, actionType=types.NVActionType.GET_OBJECTS,
        actionStatus=types.VDA5050ActionStatus.FINISHED,
        resultDescription='[{"bbox2d": {"size_x": 1}, "object_id": 1, "class_id": "x"}]')


def _writes(db):
    return [c for c in db.update_status.await_args_list
            if c.args[0].__name__ == "DetectionResultsObjectV1"]


@pytest.mark.unit
async def test_detection_write_failure_is_retried_and_does_not_abort_state():
    r, db = _make_robot()
    db.update_status.side_effect = [RuntimeError("db down")] + [None] * 10
    state = _build_state(order_id="")
    state.actionStates = [_finished_get_objects()]
    handled = AsyncMock()
    r.handle_instant_action = handled

    await r._on_client_message(state)       # write fails: logged, state processing continues
    assert "a1" not in r._detection_actions_done
    handled.assert_awaited()

    await r._on_client_message(state)       # retried and lands
    assert "a1" in r._detection_actions_done
    n = len(_writes(db))
    await r._on_client_message(state)       # processed once
    assert len(_writes(db)) == n


@pytest.mark.unit
async def test_detection_create_failure_retries_create():
    r, db = _make_robot()
    db.create_object.side_effect = [fastapi.HTTPException(500, "boom"), None]
    state = _build_state(order_id="")
    state.actionStates = [_finished_get_objects()]

    await r._on_client_message(state)
    assert r._detection_results_object is None
    assert "a1" not in r._detection_actions_done
    await r._on_client_message(state)
    assert db.create_object.await_count == 2
    assert "a1" in r._detection_actions_done
