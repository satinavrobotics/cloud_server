"""list_objects must return rows in a deterministic (name) order."""
from contextlib import asynccontextmanager
from unittest.mock import MagicMock

import pytest

import cloud_common.objects as api_objects
from cloud_common.objects.mission import MissionQueryParamsV1
from packages.database.postgres import PostgresDatabase

pytestmark = pytest.mark.unit


class _Cursor:
    def __init__(self, sink):
        self.sink = sink

    async def execute(self, query, *a):
        self.sink.append(query)

    async def fetchall(self):
        return []


def _db(sink):
    db = PostgresDatabase.__new__(PostgresDatabase)
    db._logger = MagicMock()

    class Conn:
        @asynccontextmanager
        async def cursor(self):
            yield _Cursor(sink)

    @asynccontextmanager
    async def connection():
        yield Conn()

    db._pool = MagicMock()
    db._pool.connection = connection
    return db


async def test_list_robots_is_ordered_by_name():
    queries = []
    await _db(queries).list_objects(api_objects.RobotObjectV1)
    assert queries[0].endswith(" ORDER BY name;")
    assert "lifecycle != 'DELETED'" in queries[0]


async def test_most_recent_keeps_its_own_ordering():
    queries = []
    params = MissionQueryParamsV1(most_recent=3)
    await _db(queries).list_objects(api_objects.MissionObjectV1, params)
    assert "ORDER BY name" not in queries[0]
    assert "start_timestamp" in queries[0]
