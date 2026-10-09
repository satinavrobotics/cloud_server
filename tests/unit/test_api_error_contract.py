"""API error contract: deliberate HTTPExceptions pass through, validation problems are specific
4xx, anything unexpected is a logged 500 without internals, and a missing service is a 503."""
import json
import logging
import os
from types import SimpleNamespace
from unittest.mock import patch

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

import httpx  # noqa: E402
import pytest  # noqa: E402

import packages.api.main as main  # noqa: E402
from cloud_common.objects.mission import (  # noqa: E402
    MissionNodeV1, MissionObjectV1, MissionSpecV1, MissionStatusV1)
from cloud_common.objects.robot import RobotObjectV1, RobotStatusV1  # noqa: E402

pytestmark = pytest.mark.unit

SECRET = "connection to 10.0.0.5:5432 refused (password=hunter2)"


class Db:
    def __init__(self, get_error=None):
        self.get_error = get_error
        self.created = []
        self.robot = RobotObjectV1(name="r1", status=RobotStatusV1())

    async def get_object(self, cls, name):
        if self.get_error is not None:
            raise self.get_error
        if cls is RobotObjectV1 and name == "r1":
            return self.robot
        raise main.HTTPException(404, f"{name} not found")

    async def list_objects(self, cls, **kw):
        if self.get_error is not None:
            raise self.get_error
        return []

    async def create_object(self, obj, publisher_id, **kw):
        self.created.append(obj)


async def _call(db, method, path, **kw):
    with patch.object(main, "service", SimpleNamespace(database=db) if db is not None else None):
        transport = httpx.ASGITransport(app=main.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            return await client.request(method, path, **kw)


async def test_db_error_on_get_robot_is_500_without_internals(caplog):
    with caplog.at_level(logging.ERROR):
        resp = await _call(Db(RuntimeError(SECRET)), "GET", "/api/v1/robots/r1")
    assert resp.status_code == 500
    assert "hunter2" not in resp.text and "10.0.0.5" not in resp.text
    assert isinstance(resp.json()["detail"], str)
    # logged with its traceback
    assert any(r.exc_info and "hunter2" in repr(r.exc_info[1]) for r in caplog.records)


@pytest.mark.parametrize("path", ["/api/v1/robots/r1", "/api/v1/robots/r1/status",
                                  "/api/v1/missions/m1", "/api/v1/missions/m1/status",
                                  "/api/v1/missions", "/api/v1/robots",
                                  "/api/v1/detection_results/d1"])
async def test_reads_do_not_turn_db_errors_into_404(path):
    resp = await _call(Db(RuntimeError(SECRET)), "GET", path)
    assert resp.status_code == 500 and "hunter2" not in resp.text


async def test_postgres_404_passes_through_unchanged():
    resp = await _call(Db(), "GET", "/api/v1/robots/nobody")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "nobody not found"
    resp = await _call(Db(), "GET", "/api/v1/missions/nobody/status")
    assert resp.status_code == 404


async def test_service_not_initialized_is_503():
    for path in ("/api/v1/robots", "/api/v1/missions/m1", "/api/v1/settings"):
        resp = await _call(None, "GET", path)
        assert resp.status_code == 503, path


async def test_register_robot_db_error_on_lookup_is_500_not_a_blind_create():
    db = Db(RuntimeError(SECRET))
    resp = await _call(db, "POST", "/api/v1/robots", json={"name": "r1"})
    assert resp.status_code == 500 and db.created == []


async def test_register_robot_404_creates_it():
    db = Db()
    resp = await _call(db, "POST", "/api/v1/robots", json={"name": "r2"})
    assert resp.status_code == 200 and [o.name for o in db.created] == ["r2"]


async def test_settings_db_error_is_500_and_404_creates_defaults():
    resp = await _call(Db(RuntimeError(SECRET)), "GET", "/api/v1/settings")
    assert resp.status_code == 500 and "hunter2" not in resp.text
    db = Db()
    resp = await _call(db, "GET", "/api/v1/settings")
    assert resp.status_code == 200 and len(db.created) == 1


async def test_invalid_bodies_are_specific_400s():
    resp = await _call(Db(), "POST", "/api/v1/robots", json={})
    assert resp.status_code == 400 and "name" in resp.json()["detail"]
    resp = await _call(Db(), "POST", "/api/v1/missions",
                       json={"name": "m", "robot": "r1", "mission_tree": "nope"})
    assert resp.status_code == 400 and resp.json()["detail"].startswith("Invalid mission")


async def test_mission_put_rejects_status():
    resp = await _call(Db(), "PUT", "/api/v1/missions/m1", json={"status": {"state": "RUNNING"}})
    assert resp.status_code == 400 and "status" in resp.json()["detail"]
