"""MISSION.CANCEL_REQUESTED from POST /api/v1/missions/{name}/cancel
(packages/api/run_admin.py record_cancel_requested): tagged with the mission's open run,
gated by the robot's recording level, and never failing the cancel itself."""
import datetime
import json
import os
import uuid

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

from contextlib import asynccontextmanager  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest.mock import AsyncMock, patch  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402

import packages.api.main as main  # noqa: E402
from packages.api import run_admin  # noqa: E402
from packages.events.emit import COLUMNS as EVENT_COLUMNS, INSERT_SQL  # noqa: E402
from packages.telemetry_ingest.policy import PolicySources  # noqa: E402

pytestmark = pytest.mark.unit

NOW = datetime.datetime(2026, 9, 27, 9, 0, tzinfo=datetime.timezone.utc)
RUN = uuid.UUID(int=7)


class Cursor:
    def __init__(self, db):
        self.db, self._row, self.rowcount = db, None, 0

    async def execute(self, sql, params=()):
        if self.db.fail:
            raise RuntimeError("database down")
        if sql.startswith("SELECT now()"):
            self._row = (NOW,)
        elif "FROM mission_runs" in sql:
            self.db.run_queries.append(params)
            self._row = (self.db.open_run,) if self.db.open_run else None
        elif sql == INSERT_SQL:
            self.db.events.append(dict(zip(EVENT_COLUMNS, params)))
            self.rowcount = 1
        else:
            raise AssertionError(f"unexpected SQL: {sql}")

    async def fetchone(self):
        return self._row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class Db:
    def __init__(self, open_run=RUN, fail=False):
        self.open_run, self.fail = open_run, fail
        self.events, self.run_queries = [], []

    def cursor(self):
        return Cursor(self)

    @asynccontextmanager
    async def connection(self):
        yield self


def sources(level=None, global_level=None):
    return PolicySources(robot_levels={"r1": level}, global_level=global_level)


async def record(db, level="events_only", robot="r1"):
    with patch.object(run_admin, "load_sources", AsyncMock(return_value=sources(level))):
        return await run_admin.record_cancel_requested(db, "m1", robot)


async def test_event_tagged_with_open_run():
    db = Db()
    assert await record(db) is True
    (event,) = db.events
    assert event["code"] == "MISSION.CANCEL_REQUESTED"
    assert event["robot_name"] == "r1"
    assert event["run_id"] == RUN
    assert event["source"] == "api"
    assert db.run_queries == [("m1",)]


async def test_no_open_run_still_records():
    db = Db(open_run=None)
    assert await record(db, level="full") is True
    assert db.events[0]["run_id"] is None


async def test_level_off_records_nothing():
    db = Db()
    assert await record(db, level="off") is False
    assert db.events == []


async def test_database_failure_never_raises():
    assert await record(Db(fail=True)) is False


async def test_cancel_route_emits_after_the_cancel():
    mission = SimpleNamespace(spec=SimpleNamespace(robot="r1"), cancel=AsyncMock())
    database = SimpleNamespace(get_object=AsyncMock(return_value=mission),
                               update_spec=AsyncMock(), update_spec_fields=AsyncMock())
    recorder = AsyncMock(return_value=True)
    with patch.object(main, "service", SimpleNamespace(database=database)), \
            patch.object(run_admin, "record_cancel_requested", recorder):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                     base_url="http://t") as client:
            resp = await client.post("/api/v1/missions/m1/cancel")
    assert resp.status_code == 200
    database.update_spec_fields.assert_awaited_once()
    recorder.assert_awaited_once_with(database, "m1", "r1")


async def test_cancel_route_failure_emits_nothing():
    database = SimpleNamespace(get_object=AsyncMock(side_effect=RuntimeError("nope")),
                               update_spec=AsyncMock(), update_spec_fields=AsyncMock())
    recorder = AsyncMock()
    with patch.object(main, "service", SimpleNamespace(database=database)), \
            patch.object(run_admin, "record_cancel_requested", recorder):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app, raise_app_exceptions=False),
                                     base_url="http://t") as client:
            resp = await client.post("/api/v1/missions/m1/cancel")
    assert resp.status_code == 500
    recorder.assert_not_awaited()


class _Store:
    """A mission row with the real jsonb `||` semantics of update_spec_fields."""

    def __init__(self, spec):
        self.spec = dict(spec)
        self.reads_done = False

    async def get_object(self, cls, name):
        from cloud_common.objects.mission import (
            MissionObjectV1, MissionStatusV1)
        from cloud_common.objects.object import ObjectLifecycleV1
        stale = self.spec  # the API's read
        self.reads_done = True
        # The dispatcher's replan patch lands between the API's read and its write.
        self.spec = {**self.spec, "route_rev": 5, "planned_path": ["n9"]}
        return MissionObjectV1(name=name, lifecycle=ObjectLifecycleV1.ALIVE,
                               status=MissionStatusV1(), **stale)

    async def update_spec(self, cls, name, spec, publisher_id):
        self.spec = json.loads(spec.json())  # whole-spec overwrite

    async def update_spec_fields(self, cls, name, fields, publisher_id):
        self.spec = {**self.spec, **fields}


async def test_cancel_does_not_revert_a_concurrent_replan_patch():
    from cloud_common.objects.mission import MissionNodeV1
    store = _Store(json.loads(
        __import__("cloud_common.objects.mission", fromlist=["x"]).MissionSpecV1(
            robot="r1", mission_tree=[MissionNodeV1(sequence={})]).json()))
    database = SimpleNamespace(get_object=store.get_object, update_spec=store.update_spec,
                               update_spec_fields=store.update_spec_fields)
    with patch.object(main, "service", SimpleNamespace(database=database)), \
            patch.object(run_admin, "record_cancel_requested", AsyncMock(return_value=True)):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                     base_url="http://t") as client:
            resp = await client.post("/api/v1/missions/m1/cancel")
    assert resp.status_code == 200
    assert store.spec["needs_canceled"] is True
    assert store.spec["route_rev"] == 5
    assert store.spec["planned_path"] == ["n9"]


async def test_mission_put_edit_writes_only_the_edited_fields():
    from cloud_common.objects.mission import MissionNodeV1
    store = _Store(json.loads(
        __import__("cloud_common.objects.mission", fromlist=["x"]).MissionSpecV1(
            robot="r1", mission_tree=[MissionNodeV1(sequence={})]).json()))
    database = SimpleNamespace(get_object=store.get_object, update_spec=store.update_spec,
                               update_spec_fields=store.update_spec_fields,
                               update_status=AsyncMock())
    with patch.object(main, "service", SimpleNamespace(database=database)):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                     base_url="http://t") as client:
            resp = await client.put("/api/v1/missions/m1", json={"timeout": 77})
    assert resp.status_code == 200
    assert store.spec["timeout"] == 77
    assert store.spec["route_rev"] == 5 and store.spec["planned_path"] == ["n9"]
