"""The robot's footprint (VDA5050 physicalParameters.length/width) is kept on its factsheet."""
from unittest.mock import AsyncMock, MagicMock

import pytest

import cloud_common.objects as api_objects
import packages.controllers.mission.vda5050_types as types
from packages.controllers.mission.server import Robot
from packages.database.postgres import PostgresDatabase

pytestmark = pytest.mark.unit


def _robot():
    db = AsyncMock(spec=PostgresDatabase)
    db.update_status = AsyncMock()
    server = MagicMock()
    server.push_telemetry = False
    r = Robot("r1", db, MagicMock(), "prefix", server)
    r._robot_object = api_objects.RobotObjectV1(name="r1", status={})
    return r, db


def _factsheet(**physical):
    # The optional sections are spelled out so the test also builds under Pydantic 2,
    # where an Optional field without a default is required (AUDIT_BACKLOG C1).
    return types.VDA5050Factsheet(
        protocolLimits=None, protocolFeatures=None, agvGeometry=None,
        loadSpecification=None, localizationParameters=None,
        typeSpecification=types.VDA5050TypeSpecification(agvClass="FORKLIFT"),
        physicalParameters=types.VDA5050PhysicalParameters(**physical))


def test_size_is_unknown_until_the_robot_reports_one():
    assert api_objects.RobotObjectV1(name="r1", status={}).status.factsheet.length == -1
    assert api_objects.RobotObjectV1(name="r1", status={}).status.factsheet.width == -1
    assert api_objects.RobotObjectV1(name="r1", status={}).status.factsheet.height == -1


async def test_factsheet_stores_length_and_width_in_metres():
    r, db = _robot()
    await r._on_client_factsheet(_factsheet(length=0.8, width=0.6))
    fs = r._robot_object.status.factsheet
    assert (fs.length, fs.width) == (0.8, 0.6)
    db.update_status.assert_awaited_once()


async def test_factsheet_without_a_size_keeps_it_unknown():
    r, _ = _robot()
    await r._on_client_factsheet(_factsheet())
    fs = r._robot_object.status.factsheet
    assert (fs.length, fs.width) == (-1, -1)


async def test_factsheet_ignores_a_nonsense_size_and_keeps_the_previous_one():
    r, _ = _robot()
    await r._on_client_factsheet(_factsheet(length=0.8, width=0.6))
    await r._on_client_factsheet(_factsheet(length=0.0, width=-3.0))
    fs = r._robot_object.status.factsheet
    assert (fs.length, fs.width) == (0.8, 0.6)


async def test_factsheet_stores_the_height_and_ignores_a_nonsense_one():
    r, _ = _robot()
    await r._on_client_factsheet(_factsheet(length=0.8, width=0.6, heightMax=0.4))
    assert r._robot_object.status.factsheet.height == 0.4
    await r._on_client_factsheet(_factsheet(heightMax=-1.0))
    assert r._robot_object.status.factsheet.height == 0.4


async def test_factsheet_keeps_speed_and_acceleration_limits():
    r, _ = _robot()
    await r._on_client_factsheet(_factsheet(
        speedMin=0.0, speedMax=1.2, accelerationMax=0.8, decelerationMax=1.5,
        angularSpeedMin=0.0, angularSpeedMax=1.0, heightMin=0.1))
    fs = r._robot_object.status.factsheet
    assert (fs.speed_min, fs.speed_max) == (0.0, 1.2)
    assert (fs.acceleration_max, fs.deceleration_max) == (0.8, 1.5)
    assert (fs.angular_speed_min, fs.angular_speed_max) == (0.0, 1.0)
    assert fs.height_min == 0.1


async def test_factsheet_limits_stay_unknown_or_previous_when_missing_or_nonsense():
    r, _ = _robot()
    await r._on_client_factsheet(_factsheet())
    fs = r._robot_object.status.factsheet
    assert (fs.acceleration_max, fs.deceleration_max, fs.angular_speed_max) == (-1, -1, -1)
    await r._on_client_factsheet(_factsheet(accelerationMax=0.8, angularSpeedMax=1.0))
    await r._on_client_factsheet(_factsheet(accelerationMax=0.0, angularSpeedMax=-2.0))
    fs = r._robot_object.status.factsheet
    assert (fs.acceleration_max, fs.angular_speed_max) == (0.8, 1.0)


def test_registration_factsheet_copies_valid_limits_only():
    from packages.api.main import _apply_factsheet_limits
    fs = api_objects.RobotObjectV1(name="r1", status={}).status.factsheet
    _apply_factsheet_limits(fs, {"speed_max": 1.5, "acceleration_max": 0.7,
                                 "angular_speed_max": "fast", "width": -1, "length": True})
    assert (fs.speed_max, fs.acceleration_max) == (1.5, 0.7)
    assert (fs.angular_speed_max, fs.width, fs.length) == (-1, -1, -1)


async def test_factsheet_with_agv_geometry_is_accepted():
    # The orchestrator's factsheet carries agvGeometry.envelopes2d; it used to fail validation
    # (placeholder section with a required field) and the whole factsheet was dropped.
    raw = {"headerId": 0, "timestamp": "2026-10-08T12:19:51.144Z", "version": "2.0.0",
           "manufacturer": "Satinav Robotics", "serialNumber": "r1",
           "typeSpecification": {"agvClass": "CARRIER"},
           "physicalParameters": {"speedMax": 1.0, "length": 0.52, "width": 0.52},
           "actions": [],
           "agvGeometry": {"envelopes2d": [{"set": "footprint", "polygonPoints": [
               {"x": -0.26, "y": -0.26}, {"x": 0.26, "y": 0.26}]}]}}
    from packages.controllers.mission.server import ClientFactsheetMessage
    message = ClientFactsheetMessage(name="r1", payload=raw).payload
    r, _ = _robot()
    await r._on_client_factsheet(message)
    fs = r._robot_object.status.factsheet
    assert (fs.length, fs.width, fs.speed_max) == (0.52, 0.52, 1.0)
