"""DELETE /api/v1/robots/{name}: the cleanup helper (packages/api/robot_delete.py), the route's
contract, and the dispatcher-side teardown that makes a re-registered robot a fresh one."""
import asyncio
import os
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

import pytest
from fastapi import HTTPException

import cloud_common.objects as api_objects
import packages.api.main as main
from cloud_common.objects.object import ObjectLifecycleV1
from cloud_common.objects.robot import RobotObjectV1, RobotStatusV1
from packages.api import maps, robot_delete
from packages.api.robot_delete import RobotDeleter
from packages.controllers.mission import fleet_recorder as fr
from packages.controllers.mission.server import Robot, RobotServer

pytestmark = pytest.mark.unit


class FakeCursor:
    def __init__(self, db):
        self.db = db

    async def execute(self, sql, params=()):
        self.db.statements.append((sql, params))
        self.db.pending = (self.db.active_mission,) if sql == robot_delete.ACTIVE_MISSION_SQL \
            and self.db.active_mission else None

    async def fetchone(self):
        return self.db.pending

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeDb:
    def __init__(self, robot_state="IDLE", active_mission=None, sessions=()):
        self.statements, self.pending = [], None
        self.active_mission = active_mission
        self.robot_state = robot_state
        self.sessions = list(sessions)
        self.robot_exists = True
        self.lifecycle_calls = []

    def deleted_tables(self):
        return [sql.split()[2] for sql, _ in self.statements if sql.startswith("DELETE FROM")]

    @asynccontextmanager
    async def connection(self):
        class Conn:
            def cursor(_, *a, **k):
                return FakeCursor(self)
        yield Conn()

    async def set_lifecycle(self, cls, name, lifecycle, publisher):
        self.lifecycle_calls.append((cls, name, lifecycle))
        self.robot_exists = False


def install(db, monkeypatch):
    finish = AsyncMock()
    stop = AsyncMock()

    class Store:
        cursor = FakeCursor(db)

        async def lock_robot(self, name):
            if not db.robot_exists:
                return None
            return RobotObjectV1(name=name, status=RobotStatusV1(state=db.robot_state))

        async def open_sessions_of_robot(self, name):
            return list(db.sessions)

        async def lock_map(self, name):
            return MagicMock()

        async def lock_session(self, sid):
            return next(s for s in db.sessions if s["session_id"] == sid)

    @asynccontextmanager
    async def open_store(_db, _pid):
        yield Store()

    monkeypatch.setattr(maps, "open_store", open_store)
    monkeypatch.setattr(maps, "_finish_in", finish)
    monkeypatch.setattr(maps, "stop_services", stop)
    return finish, stop


async def test_idle_delete_defaults_keep_history(monkeypatch):
    db = FakeDb(sessions=[{"session_id": "s1", "map_name": "m", "ended_at": None}])
    finish, stop = install(db, monkeypatch)
    out = await RobotDeleter(db).delete("r1")
    assert out == {"success": True, "message": "Robot r1 deleted",
                   "deleted": {"telemetry": False, "rosbags": False, "sessions_closed": 1}}
    finish.assert_awaited_once()
    assert db.lifecycle_calls == [(RobotObjectV1, "r1", ObjectLifecycleV1.DELETED)]
    tables = set(db.deleted_tables())
    assert tables == set(robot_delete.STATE_TABLES)
    assert {"robot_site_assignments", "robot_run_epochs"} <= tables
    assert not tables & set(robot_delete.TELEMETRY_TABLES)


async def test_delete_telemetry_flag(monkeypatch):
    db = FakeDb()
    install(db, monkeypatch)
    out = await RobotDeleter(db).delete("r1", delete_telemetry=True)
    assert out["deleted"] == {"telemetry": True, "rosbags": False, "sessions_closed": 0}
    assert set(db.deleted_tables()) == set(robot_delete.STATE_TABLES) | set(
        robot_delete.TELEMETRY_TABLES)
    assert {"robot_state_ts", "diagnostics_ts", "fleet_events", "mission_runs"} <= set(
        db.deleted_tables())


async def test_delete_rosbags_flag(monkeypatch):
    db = FakeDb()
    install(db, monkeypatch)
    bags = AsyncMock(return_value={"success": True, "count_deleted": 2})
    out = await RobotDeleter(db).delete("r1", delete_rosbags=True, rosbag_deleter=bags)
    bags.assert_awaited_once_with("r1")
    assert out["deleted"]["rosbags"] is True
    bags2 = AsyncMock()
    db2 = FakeDb()
    install(db2, monkeypatch)
    await RobotDeleter(db2).delete("r1", rosbag_deleter=bags2)
    bags2.assert_not_awaited()


async def test_rosbag_failure_is_500_and_keeps_robot(monkeypatch):
    db = FakeDb()
    install(db, monkeypatch)
    with pytest.raises(HTTPException) as exc:
        await RobotDeleter(db).delete("r1", delete_rosbags=True,
                                      rosbag_deleter=AsyncMock(return_value={"success": False}))
    assert exc.value.status_code == 500
    assert db.lifecycle_calls == []


@pytest.mark.parametrize("state,mission", [("ON_TASK", None), ("IDLE", "m-1"),
                                           ("ON_TASK", "m-2")])
async def test_active_mission_is_409_and_mutates_nothing(monkeypatch, state, mission):
    db = FakeDb(robot_state=state, active_mission=mission,
                sessions=[{"session_id": "s1", "map_name": "m", "ended_at": None}])
    finish, _ = install(db, monkeypatch)
    with pytest.raises(HTTPException) as exc:
        await RobotDeleter(db).delete("r1", delete_telemetry=True, delete_rosbags=True,
                                      rosbag_deleter=AsyncMock())
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "ROBOT_HAS_ACTIVE_MISSION"
    assert exc.value.detail["robot"] == "r1" and exc.value.detail["mission"] == mission
    assert isinstance(exc.value.detail["message"], str)
    assert db.deleted_tables() == [] and db.lifecycle_calls == []
    finish.assert_not_awaited()


async def test_unknown_robot_is_404(monkeypatch):
    db = FakeDb()
    db.robot_exists = False
    install(db, monkeypatch)
    with pytest.raises(HTTPException) as exc:
        await RobotDeleter(db).delete("ghost")
    assert exc.value.status_code == 404
    assert db.deleted_tables() == []


async def test_route_passes_flags_and_maps_other_errors_to_500(monkeypatch):
    svc = MagicMock()
    monkeypatch.setattr(main, "service", svc)
    with patch.object(main, "RobotDeleter") as deleter:
        deleter.return_value.delete = AsyncMock(return_value={"success": True})
        assert await main.delete_robot("r1", delete_telemetry=True, delete_rosbags=False) == {
            "success": True}
        kw = deleter.return_value.delete.await_args.kwargs
        assert kw["delete_telemetry"] is True and kw["delete_rosbags"] is False
        deleter.return_value.delete = AsyncMock(side_effect=RuntimeError("boom"))
        with pytest.raises(HTTPException) as exc:
            await main.delete_robot("r1")
        assert exc.value.status_code == 500
        deleter.return_value.delete = AsyncMock(side_effect=HTTPException(404, "x"))
        with pytest.raises(HTTPException) as exc:
            await main.delete_robot("r1")
        assert exc.value.status_code == 404


async def test_delete_then_reregister_gets_a_fresh_robot_controller():
    """A DELETED notification (what the API's hard delete produces) drops the live Robot
    controller; the next message for the name builds a brand-new one with no leftovers."""
    server = RobotServer.__new__(RobotServer)
    server._robots = {}
    server._robot_changes = asyncio.Queue()
    server._database = AsyncMock()
    server._mqtt_client, server._mqtt_prefix = MagicMock(), "p"
    server.fleet_recorder = MagicMock()
    server.debug = MagicMock()
    server.disable_request_factsheet = True
    server.push_telemetry = False
    server.mission_ctrl_url = None

    old = Robot("r1", server._database, server._mqtt_client, "p", server)
    old._finished_missions["old-mission"] = None
    old._robot_online_task = asyncio.get_event_loop().create_task(asyncio.sleep(60))
    server._robots["r1"] = old
    task = asyncio.get_event_loop().create_task(server._handle_robot_changes())
    try:
        gone = RobotObjectV1(name="r1", lifecycle=ObjectLifecycleV1.DELETED, status={})
        await server._robot_changes.put(gone)
        for _ in range(5):
            await asyncio.sleep(0)
        assert "r1" not in server._robots
        assert old._alive is False and old._robot_online_task is None
        server.fleet_recorder.on_robot_deleted.assert_called_once_with(gone)

        fresh = RobotObjectV1(name="r1", status={})
        await server._robot_changes.put(fresh)
        for _ in range(5):
            await asyncio.sleep(0)
        new = server._robots["r1"]
        assert new is not old and new._alive
        assert not new._finished_missions and new._current_mission is None
        new.shutdown()
    finally:
        task.cancel()


def test_fleet_recorder_forgets_deleted_robot():
    rec = fr.FleetRecorder.__new__(fr.FleetRecorder)
    rec._tracks, rec._latest_rows, rec._runs = {"r1": 1, "r2": 2}, {"r1": 1}, {"r1": 1}
    rec.policy = MagicMock()
    gone = MagicMock()
    gone.name = "r1"
    rec.on_robot_deleted(gone)
    assert rec._tracks == {"r2": 2} and rec._latest_rows == {} and rec._runs == {}
    assert not rec.knows("r1")
    rec.policy.apply_robot_object.assert_called_once_with(gone)


# ---- LiveKit participant removal (best effort) ----

import httpx

from packages.api.livekit_admin import LiveKitAdmin


def _lk_client(calls, handler=None):
    def default(request):
        method = request.url.path.rsplit("/", 1)[-1]
        body = request.read().decode()
        calls.append((method, body))
        if method == "ListRooms":
            return httpx.Response(200, json={"rooms": [{"name": "a@x.io"}, {"name": "b@x.io"}]})
        if method == "ListParticipants":
            room = "a@x.io" if "a@x.io" in body else "b@x.io"
            parts = [{"identity": "dev_r1", "name": "r1"}, {"identity": "other"}] \
                if room == "a@x.io" else [{"identity": "dashboard_1"}]
            return httpx.Response(200, json={"participants": parts})
        return httpx.Response(200, json={})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler or default))


async def test_livekit_removal_success_removes_only_the_robot():
    calls = []
    admin = LiveKitAdmin(url="http://sfu", api_key="k", api_secret="s", client=_lk_client(calls))
    assert await admin.remove_robot_participants("r1") == 1
    removes = [b for m, b in calls if m == "RemoveParticipant"]
    assert len(removes) == 1 and '"dev_r1"' in removes[0] and "a@x.io" in removes[0]


async def test_livekit_removal_api_error_is_swallowed():
    def boom(request):
        return httpx.Response(500, text="nope")
    admin = LiveKitAdmin(url="http://sfu", api_key="k", api_secret="s",
                         client=_lk_client([], boom))
    assert await admin.remove_robot_participants("r1") == 0


async def test_livekit_removal_not_configured_is_noop():
    calls = []
    admin = LiveKitAdmin(url="http://sfu", api_key="", api_secret="", client=_lk_client(calls))
    assert await admin.remove_robot_participants("r1") == 0
    assert calls == []


async def test_livekit_removal_404_participant_not_found_is_fine():
    calls = []

    def handler(request):
        method = request.url.path.rsplit("/", 1)[-1]
        calls.append(method)
        if method == "ListRooms":
            return httpx.Response(200, json={"rooms": [{"name": "a"}]})
        if method == "ListParticipants":
            return httpx.Response(200, json={"participants": [{"identity": "r1"}]})
        return httpx.Response(404, json={"code": "not_found"})
    admin = LiveKitAdmin(url="http://sfu", api_key="k", api_secret="s",
                         client=_lk_client([], handler))
    assert await admin.remove_robot_participants("r1") == 0
    assert calls[-1] == "RemoveParticipant"


async def test_delete_calls_livekit_after_db_delete_and_survives_its_failure(monkeypatch):
    db = FakeDb()
    install(db, monkeypatch)
    order = []
    remover = AsyncMock(side_effect=lambda n: order.append(("lk", list(db.lifecycle_calls))))
    await RobotDeleter(db, livekit_remover=remover).delete("r1")
    remover.assert_awaited_once_with("r1")
    assert order[0][1], "LiveKit removal must run after the DB delete"
    failing = AsyncMock(side_effect=RuntimeError("down"))
    db2 = FakeDb()
    install(db2, monkeypatch)
    out = await RobotDeleter(db2, livekit_remover=failing).delete("r1")
    assert out["success"] is True and db2.lifecycle_calls


def test_livekit_admin_token_is_hs256_with_grant():
    import base64, json
    from packages.api.livekit_admin import admin_token
    t = admin_token("k", "s", {"roomList": True})
    pad = lambda x: x + "=" * (-len(x) % 4)
    claims = json.loads(base64.urlsafe_b64decode(pad(t.split(".")[1])))
    assert claims["iss"] == "k" and claims["video"] == {"roomList": True}
