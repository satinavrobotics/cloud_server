"""WP11 F1: the map delete saga (packages/api/map_delete.py) and the routes around it.

The database is an in-memory fake that understands exactly the statements map_delete.py and
events.emit issue; the advisory locks are shared between fakes to stand in for API workers.
"""
import datetime
import functools
import importlib
import json
import os
import uuid

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

import packages.api.main as main
from cloud_common.objects.map import MapObjectV1, MapStatusV1
from cloud_common.objects.object import ObjectLifecycleV1
from cloud_common.objects.robot import RobotObjectV1, RobotStatusV1
from packages.api import map_delete
from packages.api.map_delete import MapDeleter
from packages.api.server import ApiDelegationService
from packages.events.codes import EventCode
from packages.events.ids import event_id

# The module, not packages.events.emit() the function that the package re-exports.
emit_mod = importlib.import_module("packages.events.emit")

pytestmark = pytest.mark.unit

T0 = datetime.datetime(2026, 9, 25, 12, 0, tzinfo=datetime.timezone.utc)


# --- fake database -----------------------------------------------------------------------------

class FakeCursor:
    def __init__(self, db):
        self.db = db
        self.rowcount = 0
        self._rows = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, params=()):
        db, self._rows, self.rowcount = self.db, [], 0
        db.statements.append(sql)
        if sql == map_delete.MARK_SQL:
            name, _spec, status, patch_json = params
            row = db.rows.get(name)
            if row is None:
                db.rows[name] = {"lifecycle": "DELETING", "status": json.loads(status)}
            elif row["lifecycle"] != "DELETING":
                row["lifecycle"] = "DELETING"
                row["status"].update(json.loads(patch_json))
            else:
                return
            self._rows, self.rowcount = [(name,)], 1
        elif sql == map_delete.NOTIFY_SQL:
            db.notifies.append(params)
        elif sql == map_delete.LOAD_SQL:
            row = db.rows.get(params[0])
            if row is not None:
                self._rows = [(row["lifecycle"], dict(row["status"]))]
        elif sql == map_delete.LIST_SQL:
            self._rows = [(n,) for n, r in sorted(db.rows.items()) if r["lifecycle"] == "DELETING"]
        elif sql == map_delete.RECORD_SQL:
            patch_json, name = params
            row = db.rows.get(name)
            if row is not None and row["lifecycle"] == "DELETING":
                row["status"].update(json.loads(patch_json))
                self.rowcount = 1
        elif sql == map_delete.FINISH_SQL:
            row = db.rows.get(params[0])
            if row is not None and row["lifecycle"] == "DELETING":
                del db.rows[params[0]]
                self.rowcount = 1
        elif sql == emit_mod.INSERT_SQL:
            if db.fail_events:
                raise RuntimeError("fleet_events unavailable")
            db.events.append(dict(zip(emit_mod.COLUMNS, params)))
            self.rowcount = 1
        elif sql.startswith("SELECT pg_try_advisory_lock"):
            key = params[0]
            got = key not in db.locks
            if got:
                db.locks[key] = self
            self._rows = [(got,)]
        else:
            raise AssertionError(f"unexpected SQL: {sql}")

    async def fetchone(self):
        return self._rows[0] if self._rows else None

    async def fetchall(self):
        return list(self._rows)


class FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, db):
        self.db = db
        self.closed = False
        self._lock_cursor = None

    def cursor(self):
        return FakeCursor(self.db)

    def transaction(self):
        return FakeTransaction()

    async def execute(self, sql, params=()):
        cursor = FakeCursor(self.db)
        await cursor.execute(sql, params)
        self._lock_cursor = cursor
        return cursor

    async def close(self):
        self.closed = True
        for key, holder in list(self.db.locks.items()):
            if holder is self._lock_cursor:
                del self.db.locks[key]


class FakeDb:
    def __init__(self, locks=None):
        self.rows = {}
        self.events = []
        self.notifies = []
        self.statements = []
        self.locks = {} if locks is None else locks
        self.fail_events = False

    def connection(self):
        db = self

        class _Ctx:
            async def __aenter__(self):
                return FakeConn(db)

            async def __aexit__(self, *exc):
                return False
        return _Ctx()

    async def dedicated_connection(self):
        return FakeConn(self)

    def seed(self, name, lifecycle="ALIVE", **status):
        self.rows[name] = {"lifecycle": lifecycle,
                           "status": {**json.loads(MapStatusV1().json()), **status}}


class Store:
    """A graph or image store whose delete fails `fail` times (False or an exception)."""

    def __init__(self, fail=0, exc=None):
        self.fail = fail
        self.exc = exc
        self.calls = []

    def __call__(self, map_id):
        self.calls.append(map_id)
        if self.fail:
            self.fail -= 1
            if self.exc is not None:
                raise self.exc
            return False
        return True


def _deleter(db, graph=None, images=None, max_attempts=3):
    sleeps = []

    async def sleep(s):
        sleeps.append(s)
    deleter = MapDeleter(db, graph or Store(), images or Store(), max_attempts=max_attempts,
                         backoff_s=2.0, backoff_max_s=5.0, sleep=sleep, now=lambda: T0)
    return deleter, sleeps


async def _request_and_wait(deleter, map_id):
    body = await deleter.request(map_id)
    await deleter.task_for(map_id)
    return body


# --- the saga ------------------------------------------------------------------------------------

async def test_request_marks_deleting_then_cleans_up_and_removes_the_row():
    db = FakeDb()
    db.seed("site_a")
    graph, images = Store(), Store()
    deleter, sleeps = _deleter(db, graph, images)

    body = await deleter.request("site_a")
    assert body == {"success": True, "map_id": "site_a", "lifecycle": "DELETING",
                    "message": "Map site_a is being deleted"}
    assert db.rows["site_a"]["lifecycle"] == "DELETING"
    assert db.rows["site_a"]["status"]["delete_requested_at"] == T0.isoformat()
    await deleter.task_for("site_a")

    assert "site_a" not in db.rows
    assert graph.calls == ["site_a"] and images.calls == ["site_a"]
    assert [p[1].split(" ", 1)[1] for p in db.notifies] == ["site_a DELETING", "site_a DELETED"]
    assert db.events == [] and sleeps == []
    assert db.locks == {}  # released with the connection


async def test_arango_failure_retries_with_backoff_then_emits_delete_failed():
    db = FakeDb()
    db.seed("site_a")
    graph, images = Store(fail=99), Store()
    deleter, sleeps = _deleter(db, graph, images, max_attempts=3)

    await _request_and_wait(deleter, "site_a")

    assert len(graph.calls) == 3
    assert sleeps == [2.0, 4.0]  # after attempts 1 and 2; none after the last
    row = db.rows["site_a"]
    assert row["lifecycle"] == "DELETING"
    assert row["status"]["delete_attempts"] == 3
    assert row["status"]["delete_error"] == "graph_db: delete failed"
    assert len(db.events) == 1
    event = db.events[0]
    assert event["code"] == EventCode.MAP_DELETE_FAILED.value
    assert event["severity"] == "error" and event["source"] == "api"
    assert event["robot_name"] is None
    assert event["ts"] == T0
    assert event["event_id"] == event_id(EventCode.MAP_DELETE_FAILED, None, T0,
                                         "map:site_a:attempts:3")
    assert json.loads(event["payload"]) == {"map_name": "site_a", "attempts": 3,
                                            "error": "graph_db: delete failed"}


async def test_minio_failure_retries_then_emits_delete_failed():
    db = FakeDb()
    db.seed("site_a")
    images = Store(fail=99, exc=ConnectionError("minio down"))
    deleter, sleeps = _deleter(db, Store(), images, max_attempts=2)

    await _request_and_wait(deleter, "site_a")

    assert len(images.calls) == 2 and sleeps == [2.0]
    assert db.rows["site_a"]["status"]["delete_error"] == "image_db: minio down"
    assert [json.loads(e["payload"])["attempts"] for e in db.events] == [2]


async def test_transient_failure_recovers_without_an_event():
    db = FakeDb()
    db.seed("site_a")
    graph = Store(fail=2)
    deleter, sleeps = _deleter(db, graph, Store(), max_attempts=3)

    await _request_and_wait(deleter, "site_a")

    assert "site_a" not in db.rows
    assert len(graph.calls) == 3 and sleeps == [2.0, 4.0]
    assert db.events == []


async def test_backoff_is_capped():
    deleter, _ = _deleter(FakeDb())
    assert [deleter.backoff(n) for n in (1, 2, 3, 4)] == [2.0, 4.0, 5.0, 5.0]


async def test_event_write_failure_keeps_the_status():
    db = FakeDb()
    db.seed("site_a")
    db.fail_events = True
    deleter, _ = _deleter(db, Store(fail=99), Store(), max_attempts=1)

    await _request_and_wait(deleter, "site_a")

    assert db.rows["site_a"]["status"]["delete_attempts"] == 1
    assert db.events == []


async def test_rerun_is_idempotent_and_starts_a_new_round():
    db = FakeDb()
    db.seed("site_a")
    graph = Store(fail=99)
    deleter, _ = _deleter(db, graph, Store(), max_attempts=2)
    await _request_and_wait(deleter, "site_a")
    first_requested = db.rows["site_a"]["status"]["delete_requested_at"]

    # A second DELETE on a map already DELETING: not re-marked (no NOTIFY, same request time),
    # a new round whose attempts continue the count, so its event has a new id.
    notifies = len(db.notifies)
    await _request_and_wait(deleter, "site_a")
    assert len(db.notifies) == notifies
    assert db.rows["site_a"]["status"]["delete_requested_at"] == first_requested
    assert db.rows["site_a"]["status"]["delete_attempts"] == 4
    assert [json.loads(e["payload"])["attempts"] for e in db.events] == [2, 4]
    assert len({e["event_id"] for e in db.events}) == 2

    # Stores are clean by now ("not found" is success): the third request finishes.
    graph.fail = 0
    await _request_and_wait(deleter, "site_a")
    assert "site_a" not in db.rows


async def test_unknown_map_is_accepted_and_its_leftovers_deleted():
    # No Postgres row (e.g. a graph that graph-builder created lazily): still 202 and cleaned.
    db = FakeDb()
    graph = Store()
    deleter, _ = _deleter(db, graph, Store())
    body = await _request_and_wait(deleter, "ghost")
    assert body["lifecycle"] == "DELETING"
    assert graph.calls == ["ghost"] and db.rows == {}


async def test_one_runner_per_map_across_workers():
    locks = {}
    db_a, db_b = FakeDb(locks), FakeDb(locks)
    db_b.rows = db_a.rows
    db_a.seed("site_a", lifecycle="DELETING")
    other_worker = FakeConn(db_a)
    await other_worker.execute("SELECT pg_try_advisory_lock(%s)", (map_delete.lock_key("site_a"),))

    graph = Store()
    deleter, _ = _deleter(db_b, graph, Store())
    await deleter.start("site_a")
    assert graph.calls == []  # the other worker holds the lock
    assert db_a.rows["site_a"]["lifecycle"] == "DELETING"

    await other_worker.close()  # that worker died: its lock goes with its connection
    await deleter.start("site_a")
    assert graph.calls == ["site_a"] and "site_a" not in db_a.rows


async def test_start_does_not_duplicate_a_running_task():
    db = FakeDb()
    db.seed("site_a", lifecycle="DELETING")
    deleter, _ = _deleter(db)
    first = deleter.start("site_a")
    assert deleter.start("site_a") is first
    await first


async def test_resume_at_startup_finishes_leftover_deletes():
    db = FakeDb()
    db.seed("left_a", lifecycle="DELETING", delete_requested_at=T0.isoformat(), delete_attempts=5)
    db.seed("left_b", lifecycle="DELETING")
    db.seed("alive")
    graph = Store()
    deleter, _ = _deleter(db, graph, Store())

    assert await deleter.resume() == ["left_a", "left_b"]
    for name in ("left_a", "left_b"):
        await deleter.task_for(name)
    assert sorted(graph.calls) == ["left_a", "left_b"]
    assert list(db.rows) == ["alive"]


async def test_resume_round_after_restart_continues_the_attempt_count():
    db = FakeDb()
    db.seed("site_a", lifecycle="DELETING", delete_requested_at=T0.isoformat(), delete_attempts=5)
    deleter, _ = _deleter(db, Store(fail=99), Store(), max_attempts=2)
    await deleter.resume()
    await deleter.task_for("site_a")
    assert db.rows["site_a"]["status"]["delete_attempts"] == 7
    assert db.events[0]["event_id"] == event_id(EventCode.MAP_DELETE_FAILED, None, T0,
                                                "map:site_a:attempts:7")


async def test_start_resume_never_raises_and_stop_cancels():
    db = FakeDb()
    db.seed("site_a", lifecycle="DELETING")
    deleter, _ = _deleter(db)
    deleter.start_resume()
    await deleter._resume_task
    await deleter.stop()
    assert deleter.task_for("site_a") is None

    broken = MapDeleter(MagicMock(connection=MagicMock(side_effect=RuntimeError("down"))),
                        Store(), Store())
    broken.start_resume()
    await broken._resume_task  # logged, not raised


# --- routes --------------------------------------------------------------------------------------

def _svc(maps=None, robot=None):
    """A mocked service whose ensure_map_not_deleting/map_lifecycle are the real methods."""
    maps = maps or {}
    svc = MagicMock()
    svc.default_map_id = "default"

    async def get_object(cls, name):
        if cls is MapObjectV1:
            if name not in maps:
                raise HTTPException(404, f"no map {name}")
            return maps[name]
        if cls is RobotObjectV1 and robot is not None:
            return robot
        raise HTTPException(404, "not found")
    svc.database.get_object = AsyncMock(side_effect=get_object)
    svc.database.update_spec = AsyncMock()
    svc.map_lifecycle = functools.partial(ApiDelegationService.map_lifecycle, svc)
    svc.ensure_map_not_deleting = functools.partial(
        ApiDelegationService.ensure_map_not_deleting, svc)
    return svc


def _map(name, lifecycle=ObjectLifecycleV1.ALIVE):
    return MapObjectV1(name=name, lifecycle=lifecycle)


async def test_list_hides_deleting_maps():
    svc = _svc()
    svc.database.list_objects = AsyncMock(return_value=[
        _map("a"), _map("b", ObjectLifecycleV1.DELETING)])
    with patch.object(main, "service", svc):
        result = await main.list_maps()
    assert [m["name"] for m in result["maps"]] == ["a"] and result["count"] == 1


async def test_assigning_a_deleting_map_is_409():
    robot = RobotObjectV1(name="r1", status=RobotStatusV1())
    svc = _svc({"gone": _map("gone", ObjectLifecycleV1.DELETING), "ok": _map("ok")}, robot)
    with patch.object(main, "service", svc):
        with pytest.raises(HTTPException) as exc:
            await main.update_robot_map("r1", main.UpdateRobotMapRequest(map_id="gone"))
        assert exc.value.status_code == 409
        svc.database.update_spec.assert_not_called()

        # An alive map, and an id with no map row (sentinels), still work.
        for map_id in ("ok", "unregistered"):
            result = await main.update_robot_map("r1", main.UpdateRobotMapRequest(map_id=map_id))
            assert result["success"] is True


async def test_put_robot_with_a_deleting_current_map_is_409():
    robot = RobotObjectV1(name="r1", status=RobotStatusV1())
    svc = _svc({"gone": _map("gone", ObjectLifecycleV1.DELETING)}, robot)
    with patch.object(main, "service", svc):
        with pytest.raises(HTTPException) as exc:
            await main.update_robot("r1", {"current_map": "gone"})
    assert exc.value.status_code == 409
    svc.database.update_spec.assert_not_called()


async def test_create_robot_on_a_deleting_map_is_409():
    svc = _svc({"gone": _map("gone", ObjectLifecycleV1.DELETING)})
    svc.database.create_object = AsyncMock()
    with patch.object(main, "service", svc):
        with pytest.raises(HTTPException) as exc:
            await main.create_robot({"name": "r9", "current_map": "gone"})
    assert exc.value.status_code == 409
    svc.database.create_object.assert_not_called()


async def test_load_and_datum_on_a_deleting_map_are_409():
    svc = _svc({"gone": _map("gone", ObjectLifecycleV1.DELETING)})
    svc.load_map = AsyncMock()
    svc.update_map_datum = AsyncMock()
    with patch.object(main, "service", svc):
        with pytest.raises(HTTPException) as exc:
            await main.load_map(main.LoadMapRequest(map_id="gone"))
        assert exc.value.status_code == 409
        with pytest.raises(HTTPException) as exc:
            await main.update_map_datum("gone", main.UpdateDatumRequest(
                datum_latitude=1.0, datum_longitude=2.0))
        assert exc.value.status_code == 409
    svc.load_map.assert_not_called()
    svc.update_map_datum.assert_not_called()


async def test_delete_route_returns_202():
    import httpx
    svc = MagicMock()
    svc.delete_map = AsyncMock(return_value={"success": True, "map_id": "m",
                                             "lifecycle": "DELETING"})
    with patch.object(main, "service", svc):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                     base_url="http://api") as client:
            response = await client.delete("/api/v1/maps/m")
    assert response.status_code == 202
    assert response.json()["lifecycle"] == "DELETING"
    svc.delete_map.assert_awaited_once_with("m")
