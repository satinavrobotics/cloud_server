"""Unit tests for the edgeBlocked mission-failure routing feature.

Covers:
- VDA5050Error.errorType parsing (the discriminator the robot sends).
- MissionStatusV1 non-terminal block fields.
- The mission controller's _handle_edge_blocked / _clear_block logic: recording a
  block, keeping the mission RUNNING, suspending the timeout, idempotency, and
  clearing the block when the robot resumes.
"""
import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock

import cloud_common.objects as api_objects
import cloud_common.objects.mission as mission_object
import cloud_common.objects.robot as robot_object
import packages.controllers.mission.vda5050_types as types
from packages.controllers.mission.server import Robot
from packages.database.postgres import PostgresDatabase


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _build_state(errors):
    return types.VDA5050State(
        headerId=0, timestamp="", nodeStates=[], edgeStates=[], errors=errors,
        batteryState=None, agvPosition=None, velocity=None)


def _edge_blocked_error(node_ref="m1-n0-s4", edge_ref="m1-e3",
                        desc="Edge blocked: Waypoint 1 unreachable after retries"):
    refs = []
    if node_ref is not None:
        refs.append(types.VDA5050ErrorReference(
            referenceKey="nodeId", referenceValue=node_ref))
    if edge_ref is not None:
        refs.append(types.VDA5050ErrorReference(
            referenceKey="edgeId", referenceValue=edge_ref))
    return types.VDA5050Error(
        errorType="edgeBlocked", errorReferences=refs,
        errorDescription=desc, errorLevel=types.VDA5050ErrorLevel.WARNING)


def _make_mission(name="m1", robot="r1"):
    return api_objects.MissionObjectV1(
        name=name, robot=robot,
        mission_tree=[{"name": "0", "route": {"waypoints": [
            {"x": 1.0, "y": 1.0, "theta": 0.0},
            {"x": 2.0, "y": 2.0, "theta": 0.0}]}, "parent": "root"}],
        status={}, timeout=1000)


def _make_robot():
    db = AsyncMock(spec=PostgresDatabase)
    db.update_status = AsyncMock()
    client = MagicMock()
    server = MagicMock()
    server.push_telemetry = False
    r = Robot("r1", db, client, "prefix", server)
    r._robot_object = api_objects.RobotObjectV1(name="r1", status={})
    r._robot_object.status.state = robot_object.RobotStateV1.ON_TASK
    return r, db


def _mission_status_writes(db):
    return [c for c in db.update_status.call_args_list
            if c.args and c.args[0] is api_objects.MissionObjectV1]


# ---------------------------------------------------------------------------
# Pure model tests
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_vda5050_error_errortype_parses_and_defaults():
    assert types.VDA5050Error(errorDescription="x").errorType is None
    assert types.VDA5050Error(
        errorType="edgeBlocked", errorDescription="x").errorType == "edgeBlocked"
    # Survives parsing from a raw dict on the state errors[] channel.
    st = types.VDA5050State(
        headerId=1, timestamp="t", nodeStates=[], edgeStates=[],
        batteryState=None, agvPosition=None, velocity=None,
        errors=[{"errorType": "edgeBlocked", "errorDescription": "x",
                 "errorLevel": "WARNING", "errorReferences": []}])
    assert st.errors[0].errorType == "edgeBlocked"


@pytest.mark.unit
def test_mission_status_block_field_defaults_round_trip():
    s = mission_object.MissionStatusV1()
    assert s.blocked is False
    assert s.blocked_node is None
    assert s.blocked_edge is None
    assert s.blocked_waypoint_index is None
    assert s.block_reason is None
    d = s.dict()
    for key in ("blocked", "blocked_node", "blocked_edge",
                "blocked_waypoint_index", "block_reason"):
        assert key in d


# ---------------------------------------------------------------------------
# Controller logic tests
# ---------------------------------------------------------------------------
@pytest.mark.unit
async def test_edge_blocked_records_block_and_stays_running():
    r, db = _make_robot()
    mission = _make_mission()
    mission.status.state = mission_object.MissionStateV1.RUNNING
    r._current_mission = mission
    r._arm_mission_timeout()
    timeout_task = r._mission_timeout_task
    assert timeout_task is not None

    blocked = r._handle_edge_blocked(_build_state([_edge_blocked_error()]))

    assert blocked is True
    assert mission.status.blocked is True
    assert mission.status.blocked_node == "0"
    assert mission.status.blocked_edge == "m1-e3"
    assert mission.status.blocked_waypoint_index == 1
    assert "unreachable" in mission.status.block_reason
    assert mission.status.node_status["0"].error_msg is not None
    # Non-terminal: the mission must NOT be failed.
    assert mission.status.state == mission_object.MissionStateV1.RUNNING
    # Robot reflects the IDLE/waiting state.
    assert r._robot_object.status.state == robot_object.RobotStateV1.IDLE
    # Timeout watchdog is suspended so the wait isn't mislabeled as a TIMEOUT.
    assert r._mission_timeout_task is None
    await asyncio.sleep(0)  # let the cancellation propagate
    assert timeout_task.cancelled()


@pytest.mark.unit
async def test_edge_blocked_tolerates_missing_edge_reference():
    r, _ = _make_robot()
    mission = _make_mission()
    mission.status.state = mission_object.MissionStateV1.RUNNING
    r._current_mission = mission

    blocked = r._handle_edge_blocked(
        _build_state([_edge_blocked_error(edge_ref=None)]))

    assert blocked is True
    assert mission.status.blocked is True
    assert mission.status.blocked_node == "0"
    assert mission.status.blocked_edge is None


@pytest.mark.unit
async def test_edge_blocked_is_idempotent_across_repeated_warnings():
    r, db = _make_robot()
    mission = _make_mission()
    mission.status.state = mission_object.MissionStateV1.RUNNING
    r._current_mission = mission
    state = _build_state([_edge_blocked_error()])

    r._handle_edge_blocked(state)
    writes_after_first = len(_mission_status_writes(db))
    assert writes_after_first == 1

    # The idle robot re-emits the identical WARNING every tick; no new DB write.
    r._handle_edge_blocked(state)
    assert len(_mission_status_writes(db)) == writes_after_first


@pytest.mark.unit
async def test_block_clears_when_robot_stops_reporting():
    r, db = _make_robot()
    mission = _make_mission()
    mission.status.state = mission_object.MissionStateV1.RUNNING
    r._current_mission = mission

    r._handle_edge_blocked(_build_state([_edge_blocked_error()]))
    assert mission.status.blocked is True

    # After a reroute the robot ingests the new order and stops reporting the block.
    blocked = r._handle_edge_blocked(_build_state([]))

    assert blocked is False
    assert mission.status.blocked is False
    assert mission.status.blocked_node is None
    assert mission.status.blocked_edge is None
    assert mission.status.blocked_waypoint_index is None
    assert mission.status.block_reason is None
    assert mission.status.node_status["0"].error_msg is None
    # Mission resumes: robot back ON_TASK and the timeout is re-armed.
    assert r._robot_object.status.state == robot_object.RobotStateV1.ON_TASK
    assert r._mission_timeout_task is not None
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_no_block_when_no_current_mission():
    r, _ = _make_robot()
    r._current_mission = None
    assert r._handle_edge_blocked(_build_state([_edge_blocked_error()])) is False
