"""API write paths: PUT allowlists, server-owned fields on POST, field-level status writes,
POST /robots/{name}/clear-fault and the fixed LiveKit grants of POST /api/createToken."""
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

import httpx  # noqa: E402
import pytest  # noqa: E402

import packages.api.main as main  # noqa: E402
from cloud_common.objects.mission import (  # noqa: E402
    MissionNodeV1, MissionObjectV1, MissionSpecV1, MissionStateV1, MissionStatusV1)
from cloud_common.objects.robot import RobotObjectV1, RobotStateV1, RobotStatusV1  # noqa: E402
from packages.database.postgres import PostgresDatabase  # noqa: E402

pytestmark = pytest.mark.unit


class Store:
    """In-memory rows with the field-level write semantics of PostgresDatabase."""

    def __init__(self):
        self.robot = RobotObjectV1(name="r1", status=RobotStatusV1())
        self.mission = MissionObjectV1(
            name="m1", status=MissionStatusV1(),
            **json.loads(MissionSpecV1(robot="r1", mission_tree=[MissionNodeV1(sequence={})]).json()))
        self.created = []
        self.calls = []
        self.status = {}

    async def get_object(self, cls, name):
        obj = self.robot if cls is RobotObjectV1 else self.mission
        if name != obj.name:
            raise main.HTTPException(404, "nope")
        return obj.copy(deep=True)

    async def update_spec_fields(self, cls, name, fields, publisher_id, **kw):
        self.calls.append(("spec_fields", name, fields))
        obj = self.robot if cls is RobotObjectV1 else self.mission
        for k, v in fields.items():
            setattr(obj, k, getattr(cls.get_spec_class()(**{**json.loads(obj.spec.json()), k: v}), k))

    async def update_status_fields(self, cls, name, fields, publisher_id):
        self.calls.append(("status_fields", name, fields))
        self.robot.status = RobotStatusV1(**{**json.loads(self.robot.status.json()), **fields})

    async def update_status(self, cls, name, status, publisher_id):
        self.calls.append(("status", name))

    async def update_spec(self, *a, **kw):
        self.calls.append(("spec", a[1]))

    async def create_object(self, obj, publisher_id, **kw):
        self.created.append(obj)


async def _call(store, method, path, **kw):
    with patch.object(main, "service", SimpleNamespace(database=store)):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                     base_url="http://t") as client:
            return await client.request(method, path, **kw)


# ---- PUT /robots ----

async def test_robot_put_rejects_unknown_and_server_owned_keys():
    store = Store()
    resp = await _call(store, "PUT", "/api/v1/robots/r1",
                       json={"needs_order_cancel": True, "bogus": 1, "labels": ["a"]})
    assert resp.status_code == 400
    assert "needs_order_cancel" in resp.json()["detail"] and "bogus" in resp.json()["detail"]
    assert store.calls == []


async def test_robot_put_writes_only_the_edited_keys():
    store = Store()
    resp = await _call(store, "PUT", "/api/v1/robots/r1",
                       json={"labels": ["a"], "name": "r1", "lifecycle": "alive", "current_map": "x"})
    assert resp.status_code == 200
    assert store.calls == [("spec_fields", "r1", {"labels": ["a"]})]


async def test_robot_put_invalid_value_is_400():
    store = Store()
    resp = await _call(store, "PUT", "/api/v1/robots/r1", json={"position_mode": "nowhere"})
    assert resp.status_code == 400 and store.calls == []


# ---- PUT /missions ----

async def test_mission_put_rejects_needs_canceled_and_unknown_keys():
    store = Store()
    resp = await _call(store, "PUT", "/api/v1/missions/m1",
                       json={"needs_canceled": True, "bogus": 1})
    assert resp.status_code == 400
    assert "needs_canceled" in resp.json()["detail"] and "bogus" in resp.json()["detail"]
    assert store.calls == []


async def test_mission_put_edit_still_needs_pending():
    store = Store()
    store.mission.status.state = MissionStateV1.RUNNING
    resp = await _call(store, "PUT", "/api/v1/missions/m1", json={"timeout": 5})
    assert resp.status_code == 409


async def test_mission_put_ignores_echoed_server_owned_keys():
    store = Store()
    resp = await _call(store, "PUT", "/api/v1/missions/m1",
                       json={"timeout": 5, "route_rev": 99, "kind": "goto", "name": "m1"})
    assert resp.status_code == 200
    assert [c[0] for c in store.calls] == ["spec_fields"]
    assert list(store.calls[0][2]) == ["timeout"]


# ---- POST /robots ----

async def test_register_new_robot_status_and_lifecycle_are_server_owned():
    store = Store()
    store.robot.name = "other"
    body = {"name": "r2", "status": {"state": "FAULT", "errors": {"x": 1}}, "lifecycle": "deleted",
            "needs_order_cancel": True, "labels": ["a"]}
    resp = await _call(store, "POST", "/api/v1/robots", json=body)
    assert resp.status_code == 200, resp.text
    created = store.created[0]
    assert created.status.state == RobotStateV1.IDLE and created.status.errors == {}
    assert created.lifecycle.value == "ALIVE"
    assert created.needs_order_cancel is False and created.labels == ["a"]


async def test_reregistration_writes_fields_not_whole_status():
    store = Store()
    resp = await _call(store, "POST", "/api/v1/robots",
                       json={"name": "r1", "ip_address": "10.0.0.5",
                             "factsheet": {"agv_class": "CARRIER", "actions": []}})
    assert resp.status_code == 200, resp.text
    kinds = [c[0] for c in store.calls]
    assert kinds == ["spec_fields", "status_fields"]
    assert store.calls[0][2] == {"ip_address": "10.0.0.5"}
    assert list(store.calls[1][2]) == ["factsheet"]
    assert store.robot.status.factsheet.agv_class == "CARRIER"


# ---- cancel-order / clear-fault ----

async def test_cancel_order_writes_one_spec_field():
    store = Store()
    resp = await _call(store, "POST", "/api/v1/robots/r1/cancel-order")
    assert resp.status_code == 200
    assert store.calls == [("spec_fields", "r1", {"needs_order_cancel": True})]


async def test_clear_fault_sets_state_and_errors_only():
    store = Store()
    store.robot.status = RobotStatusV1(state=RobotStateV1.TELEOP, errors={"e": "boom"},
                                       battery_level=42.0)
    resp = await _call(store, "POST", "/api/v1/robots/r1/clear-fault")
    assert resp.status_code == 200
    assert store.calls == [("status_fields", "r1", {"state": "IDLE", "errors": {}})]
    body = resp.json()
    assert body["status"]["state"] == "IDLE" and body["status"]["errors"] == {}
    assert body["status"]["battery_level"] == 42.0


async def test_clear_fault_unknown_robot_is_404():
    resp = await _call(Store(), "POST", "/api/v1/robots/ghost/clear-fault")
    assert resp.status_code == 404


# ---- PostgresDatabase.update_status_fields ----

class _Cursor:
    def __init__(self):
        self.executed = []
        self.rowcount = 1

    async def execute(self, sql, params=()):
        self.executed.append((sql, list(params)))


class _Ctx:
    def __init__(self, obj):
        self.obj = obj

    async def __aenter__(self):
        return self.obj

    async def __aexit__(self, *a):
        return False


async def test_update_status_fields_is_one_parametrised_merge():
    cursor = _Cursor()
    conn = MagicMock()
    conn.cursor = lambda: _Ctx(cursor)
    db = PostgresDatabase.__new__(PostgresDatabase)
    db._pool = SimpleNamespace(connection=lambda: _Ctx(conn))
    db._logger = MagicMock()
    db._commit_update = AsyncMock()
    await db.update_status_fields(RobotObjectV1, "r1'; DROP", {"state": "IDLE"}, None)
    sql, params = cursor.executed[0]
    assert "status = status || %s::jsonb" in sql and "WHERE name = %s" in sql
    assert params == ['{"state": "IDLE"}', "r1'; DROP"]
    db._commit_update.assert_awaited_once()
    with pytest.raises(ValueError):
        await db.update_status_fields(RobotObjectV1, "r1", {"nope": 1}, None)


# ---- POST /api/createToken ----

async def test_create_token_grants_are_fixed():
    create = AsyncMock(return_value={"token": "t", "ttl": 1, "server_url": "wss://x"})
    svc = SimpleNamespace(create_livekit_token=create)
    with patch.object(main, "service", svc):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                     base_url="http://t") as client:
            resp = await client.post("/api/createToken", json={
                "participantName": "p", "roomName": "r",
                "canPublish": False, "canSubscribe": False, "canPublishData": False})
    assert resp.status_code == 200
    kw = create.await_args.kwargs
    assert (kw["can_publish"], kw["can_subscribe"], kw["can_publish_data"]) == (True, True, True)
