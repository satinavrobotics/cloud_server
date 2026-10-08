"""The mission time limit is optional and off by default."""
import asyncio
import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from cloud_common import objects as api_objects
from cloud_common.objects import mission as mission_object, robot as robot_object
from packages.controllers.mission import fleet_recorder
from packages.controllers.mission.server import Robot
from packages.database.postgres import PostgresDatabase

pytestmark = pytest.mark.unit

_TREE = [{"name": "0", "route": {"waypoints": [{"x": 1.0, "y": 1.0, "theta": 0.0},
                                                {"x": 2.0, "y": 2.0, "theta": 0.0}]},
          "parent": "root"}]


def _mission(**kw):
    return api_objects.MissionObjectV1(name="m1", robot="r1", mission_tree=_TREE, status={}, **kw)


def _robot(mission):
    db = AsyncMock(spec=PostgresDatabase)
    db.update_status = AsyncMock()
    server = MagicMock()
    server.push_telemetry = False
    server.delete_pending_mission = AsyncMock(return_value=False)
    r = Robot("r1", db, MagicMock(), "prefix", server)
    r._robot_object = api_objects.RobotObjectV1(name="r1", status={})
    r._robot_object.status.state = robot_object.RobotStateV1.ON_TASK
    mission.status.state = mission_object.MissionStateV1.RUNNING
    r._current_mission = mission
    r._missions[mission.name] = mission
    return r


def test_default_has_no_limit_and_accepts_old_values():
    assert _mission().timeout is None
    assert _mission(timeout=None).timeout is None
    assert _mission(timeout=300).timeout == datetime.timedelta(seconds=300)
    assert _mission(timeout=datetime.timedelta(seconds=60)).timeout.total_seconds() == 60
    spec = mission_object.MissionSpecV1(robot="r1", mission_tree=_TREE)
    assert spec.timeout is None


def test_default_mission_arms_no_watchdog():
    r = _robot(_mission())
    r._arm_mission_timeout()
    assert r._mission_timeout_task is None


async def test_explicit_timeout_fails_running_mission():
    m = _mission(timeout=1000)
    r = _robot(m)
    r._arm_mission_timeout()
    assert r._mission_timeout_task is not None
    r._cancel_mission_timeout()
    await r._wait_mission_timeout(0, "m1")
    assert m.status.state == mission_object.MissionStateV1.FAILED
    assert m.status.failure_reason == fleet_recorder.MISSION_TIMEOUT_REASON


def test_go_to_request_models_default_to_no_limit():
    from packages.api.main import NavigationRequest, DirectWaypointsRequest
    from packages.services.mission_planner.main import NavigationRequest as PlannerNavigate
    assert PlannerNavigate(robot_name="r", target_x=1, target_y=2).timeout_seconds is None
    assert PlannerNavigate(robot_name="r", target_x=1, target_y=2, timeout_seconds=None).timeout_seconds is None
    assert PlannerNavigate(robot_name="r", target_x=1, target_y=2, timeout_seconds=300).timeout_seconds == 300
    assert NavigationRequest.__fields__["timeout_seconds"].default is None
    assert DirectWaypointsRequest.__fields__["timeout_seconds"].default is None
