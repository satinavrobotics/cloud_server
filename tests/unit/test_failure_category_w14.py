"""failure_category is set by the dispatcher (TIMEOUT, ROBOT_APP, CANCELED); order edge ids
carry the node index; an empty factsheet action list clears the stored custom actions."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import cloud_common.objects as api_objects
import cloud_common.objects.mission as mission_object
import cloud_common.objects.robot as robot_object
import packages.controllers.mission.server as server_module
import packages.controllers.mission.vda5050_types as types
from packages.controllers.mission.server import Robot

from tests.unit.test_mission_order_rejection import _error, _state
from tests.unit.test_mission_stuck_order import _arm_running_mission, _make_robot
from tests.unit.test_mission_timeout_cancel import (
    _make_robot as _make_timeout_robot, _make_running_mission)
from tests.unit.test_factsheet_footprint import _factsheet, _robot as _factsheet_robot

Cat = mission_object.MissionFailureCategoryV1
pytestmark = pytest.mark.unit


async def test_timeout_sets_category_timeout():
    r, _ = _make_timeout_robot()
    mission = _make_running_mission()
    r._current_mission = mission
    r._send_instant_action = AsyncMock()
    r.get_next_mission = AsyncMock()
    await r._fail_mission_on_timeout(mission.name)
    assert mission.status.state == mission_object.MissionStateV1.FAILED
    assert mission.status.failure_category == Cat.TIMEOUT


async def test_timeout_of_a_cancelled_mission_is_canceled():
    r, _ = _make_timeout_robot()
    mission = _make_running_mission(needs_canceled=True)
    r._current_mission = mission
    r._send_instant_action = AsyncMock()
    r.get_next_mission = AsyncMock()
    await r._fail_mission_on_timeout(mission.name)
    assert mission.status.failure_category == Cat.CANCELED


async def test_robot_rejection_sets_category_robot_app():
    r, _ = _make_robot()
    mission = _arm_running_mission(r)
    r._current_order_id = MagicMock(return_value="m1-n0")
    err = _error(types.VDA5050ErrorLevel.WARNING, "no route", [("orderId", "m1-n0")],
                 etype="noRouteError")
    await r._on_client_message(_state(errors=[err]))
    assert mission.status.failure_category == Cat.ROBOT_APP


async def test_never_adopted_order_sets_category_robot_app(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(server_module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    r, _ = _make_robot()
    mission = _arm_running_mission(r)
    r._order_sent_at = now[0]
    now[0] += Robot.ORDER_GIVE_UP_MIN_S
    for _ in range(Robot.MAX_ORDER_MISMATCHES + 1):
        await r._on_client_message(_state())
    assert mission.status.state == mission_object.MissionStateV1.FAILED
    assert mission.status.failure_category == Cat.ROBOT_APP


def test_fatal_robot_error_sets_category_robot_app():
    r, _ = _make_robot()
    mission = _arm_running_mission(r)
    err = _error(types.VDA5050ErrorLevel.FATAL, "motor fault", [], etype="robotBaseNotReadyError")
    assert r.get_mission_errors(_state(errors=[err])) is True
    assert mission.status.failure_category == Cat.ROBOT_APP


def test_set_mission_state_canceled_sets_category_and_keeps_an_earlier_one():
    r, _ = _make_robot()
    mission = _arm_running_mission(r)
    r._set_mission_state(mission_object.MissionStateV1.CANCELED)
    assert mission.status.failure_category == Cat.CANCELED
    r2, _ = _make_robot()
    m2 = _arm_running_mission(r2)
    m2.status.failure_category = Cat.TIMEOUT
    r2._set_mission_state(mission_object.MissionStateV1.CANCELED)
    assert m2.status.failure_category == Cat.TIMEOUT


def test_edge_ids_are_unique_across_the_orders_of_a_run():
    e0 = types.VDA5050Edge.from_mission_order("m1", 1, 0)
    e1 = types.VDA5050Edge.from_mission_order("m1", 1, 3)
    assert e0.edgeId == "m1-n0-e1" and e1.edgeId == "m1-n3-e1"
    assert e0.edgeId != e1.edgeId


async def test_factsheet_with_an_empty_action_list_clears_custom_actions():
    r, _ = _factsheet_robot()
    r._robot_object.status.factsheet.custom_actions = [
        robot_object.CustomActionV1(action_type="x", action_description="", action_parameters=[],
                                    blocking_type="NONE", icon_hint=None)]
    fs = _factsheet()
    fs.actions = []
    await r._on_client_factsheet(fs)
    assert r._robot_object.status.factsheet.custom_actions == []


async def test_factsheet_without_actions_section_keeps_custom_actions():
    r, _ = _factsheet_robot()
    r._robot_object.status.factsheet.custom_actions = [
        robot_object.CustomActionV1(action_type="x", action_description="", action_parameters=[],
                                    blocking_type="NONE", icon_hint=None)]
    await r._on_client_factsheet(_factsheet())  # actions=None: not reported
    assert len(r._robot_object.status.factsheet.custom_actions) == 1
