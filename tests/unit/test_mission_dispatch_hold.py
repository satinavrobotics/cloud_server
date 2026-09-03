"""Unit tests for withholding mission dispatch until the robot can receive it.

Covers:
- MissionStatusV1 held/held_reason fields.
- Robot._dispatch_hold_reason() for offline / nav-not-ready / ready robots.
- Robot._try_start_mission(): a PENDING mission stays PENDING (not dispatched) with
  held/held_reason set when the robot is offline or nav-not-ready, and dispatches
  normally (clearing held) once the robot becomes ready. Cancel still works while held.
- The _on_client_message() retry: a held mission is redispatched once a state message
  reports the robot back online with no readiness errors.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

import cloud_common.objects as api_objects
import cloud_common.objects.mission as mission_object
import packages.controllers.mission.vda5050_types as types
from packages.controllers.mission.server import Robot
from packages.database.postgres import PostgresDatabase


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_mission(name="m1", robot="r1"):
    return api_objects.MissionObjectV1(
        name=name, robot=robot,
        mission_tree=[{"name": "0", "route": {"waypoints": [
            {"x": 1.0, "y": 1.0, "theta": 0.0},
            {"x": 2.0, "y": 2.0, "theta": 0.0}]}, "parent": "root"}],
        status={}, timeout=1000)


def _make_robot(online=True):
    db = AsyncMock(spec=PostgresDatabase)
    db.update_status = AsyncMock()
    client = MagicMock()
    server = MagicMock()
    server.push_telemetry = False
    # Avoid the map-deployment/charging-mission HTTP side channel in
    # _on_client_message — irrelevant to dispatch-hold behavior.
    server.mission_ctrl_url = None
    r = Robot("r1", db, client, "prefix", server)
    r._robot_object = api_objects.RobotObjectV1(name="r1", status={})
    r._robot_object.status.online = online
    return r, db


def _build_state(errors=None):
    return types.VDA5050State(
        headerId=0, timestamp="", nodeStates=[], edgeStates=[],
        errors=errors or [], batteryState=None, agvPosition=None, velocity=None)


def _mission_status_writes(db):
    return [c for c in db.update_status.call_args_list
            if c.args and c.args[0] is api_objects.MissionObjectV1]


# ---------------------------------------------------------------------------
# Pure model tests
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_mission_status_held_field_defaults_round_trip():
    s = mission_object.MissionStatusV1()
    assert s.held is False
    assert s.held_reason is None
    d = s.dict()
    assert "held" in d
    assert "held_reason" in d


# ---------------------------------------------------------------------------
# _dispatch_hold_reason
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_dispatch_hold_reason_offline():
    r, _ = _make_robot(online=False)
    assert r._dispatch_hold_reason() == "Robot is offline"


@pytest.mark.unit
def test_dispatch_hold_reason_nav_not_ready():
    r, _ = _make_robot(online=True)
    r._robot_object.status.errors = {"navigationNotReadyError": "x"}
    assert r._dispatch_hold_reason() == "Robot navigation is not ready"


@pytest.mark.unit
def test_dispatch_hold_reason_ready():
    r, _ = _make_robot(online=True)
    assert r._dispatch_hold_reason() is None


# ---------------------------------------------------------------------------
# _try_start_mission dispatch gate
# ---------------------------------------------------------------------------
@pytest.mark.unit
async def test_try_start_mission_holds_when_offline():
    r, db = _make_robot(online=False)
    mission = _make_mission()
    r._missions[mission.name] = mission

    await r._try_start_mission()

    assert mission.status.state == mission_object.MissionStateV1.PENDING
    assert mission.status.held is True
    assert mission.status.held_reason == "Robot is offline"
    r._mqtt_client.publish.assert_not_called()
    assert r._mission_timeout_task is None
    assert len(_mission_status_writes(db)) == 1


@pytest.mark.unit
async def test_held_hold_reason_change_writes_again_but_repeat_does_not():
    r, db = _make_robot(online=False)
    mission = _make_mission()
    r._missions[mission.name] = mission

    await r._try_start_mission()
    writes_after_first = len(_mission_status_writes(db))
    assert writes_after_first == 1

    # Still offline, same reason: no redundant write.
    await r._try_start_mission()
    assert len(_mission_status_writes(db)) == writes_after_first

    # Reason changes (now online but nav-not-ready): one more write.
    r._robot_object.status.online = True
    r._robot_object.status.errors = {"poseHealthNotReadyError": "x"}
    await r._try_start_mission()
    assert mission.status.held_reason == "Robot navigation is not ready"
    assert len(_mission_status_writes(db)) == writes_after_first + 1


@pytest.mark.unit
async def test_try_start_mission_dispatches_once_ready():
    r, db = _make_robot(online=False)
    mission = _make_mission()
    r._missions[mission.name] = mission
    await r._try_start_mission()
    assert mission.status.held is True

    r._robot_object.status.online = True
    await r._try_start_mission()

    assert mission.status.held is False
    assert mission.status.held_reason is None
    assert mission.status.state == mission_object.MissionStateV1.RUNNING
    r._mqtt_client.publish.assert_called()
    assert r._mission_timeout_task is not None
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_held_mission_can_still_be_canceled():
    r, _ = _make_robot(online=False)
    mission = _make_mission()
    mission.needs_canceled = True
    r._missions[mission.name] = mission

    await r._try_start_mission()

    assert mission.status.state == mission_object.MissionStateV1.CANCELED
    assert mission.status.held is False
    r._mqtt_client.publish.assert_not_called()


# ---------------------------------------------------------------------------
# _on_client_message retry trigger
# ---------------------------------------------------------------------------
@pytest.mark.unit
async def test_on_client_message_retries_a_held_mission_once_online():
    r, _ = _make_robot(online=False)
    mission = _make_mission()
    r._missions[mission.name] = mission
    await r._try_start_mission()
    assert mission.status.held is True

    await r._on_client_message(_build_state())

    assert mission.status.held is False
    assert mission.status.state == mission_object.MissionStateV1.RUNNING
    r._mqtt_client.publish.assert_called()
    r._cancel_mission_timeout()
    if r._robot_online_task is not None:
        r._robot_online_task.cancel()


@pytest.mark.unit
async def test_on_client_message_does_not_touch_an_already_running_mission():
    r, db = _make_robot(online=True)
    mission = _make_mission()
    mission.status.state = mission_object.MissionStateV1.RUNNING
    r._current_mission = mission

    await r._on_client_message(_build_state())

    # `held` was never set for this mission, so the retry branch must not fire —
    # no extra mission-status writes beyond whatever _on_client_message itself does.
    assert mission.status.held is False
    assert mission.status.state == mission_object.MissionStateV1.RUNNING
    if r._robot_online_task is not None:
        r._robot_online_task.cancel()
