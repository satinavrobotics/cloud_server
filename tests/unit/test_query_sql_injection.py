"""list_objects binds every query value as a parameter; POST /missions owns status/lifecycle."""
import os

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import cloud_common.objects as api_objects
import packages.api.main as main
from cloud_common.objects.mission import MissionStateV1
from packages.database.postgres import PostgresDatabase

pytestmark = pytest.mark.unit

EVIL = "x' OR '1'='1"


def _db(sink):
    class Cursor:
        async def execute(self, query, params=None):
            sink.append((query, params))

        async def fetchall(self):
            return []

    class Conn:
        @asynccontextmanager
        async def cursor(self):
            yield Cursor()

    @asynccontextmanager
    async def connection():
        yield Conn()

    db = PostgresDatabase.__new__(PostgresDatabase)
    db._logger = MagicMock()
    db._pool = MagicMock()
    db._pool.connection = connection
    return db


async def test_robot_state_with_quote_is_a_bound_parameter():
    sink = []
    await _db(sink).list_objects(api_objects.RobotObjectV1, [("state", EVIL)])
    query, params = sink[0]
    assert "status->>'state' = %s" in query
    assert "1'='1" not in query
    assert params == [EVIL]


async def test_mission_robot_filter_with_quote_is_a_bound_parameter():
    sink = []
    await _db(sink).list_objects(api_objects.MissionObjectV1, [("robot", EVIL)])
    query, params = sink[0]
    assert "spec->>'robot' = %s" in query
    assert "1'='1" not in query
    assert params == [EVIL]


async def test_names_list_is_bound_as_one_array():
    sink = []
    await _db(sink).list_objects(api_objects.RobotObjectV1, [("names", ["a", EVIL])])
    query, params = sink[0]
    assert "name = ANY(%s)" in query
    assert "1'='1" not in query
    assert params == [["a", EVIL]]


@pytest.mark.parametrize("value,expected", [("agv", ["agv"]), (["a", "b"], ["a", "b"])])
async def test_robot_type_single_value_and_list(value, expected):
    sink = []
    await _db(sink).list_objects(api_objects.RobotObjectV1, [("robot_type", value)])
    query, params = sink[0]
    assert "(status->'factsheet'->>'agv_class')::text = ANY(%s)" in query
    assert " IN " not in query
    assert params == [expected]


async def test_most_recent_and_filters_keep_param_order():
    sink = []
    await _db(sink).list_objects(
        api_objects.MissionObjectV1,
        [("state", MissionStateV1.RUNNING), ("robot", "r1"), ("most_recent", 3)])
    query, params = sink[0]
    assert query.endswith("DESC LIMIT %s;")
    assert params == [MissionStateV1.RUNNING.value, "r1", 3]


async def test_create_mission_ignores_caller_status_and_lifecycle():
    svc = MagicMock()
    svc.database.create_object = AsyncMock()
    body = {"name": "m1", "robot": "r1", "mission_tree": [{"sequence": {}}],
            "status": {"state": "COMPLETED"}, "lifecycle": "DELETED"}
    with patch.object(main, "service", svc):
        result = await main.create_mission(body)
    created = svc.database.create_object.call_args.args[0]
    assert created.status.state == MissionStateV1.PENDING
    assert created.lifecycle == api_objects.ObjectLifecycleV1.ALIVE
    assert result["status"]["state"] == "PENDING"
