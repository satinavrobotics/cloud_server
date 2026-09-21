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
