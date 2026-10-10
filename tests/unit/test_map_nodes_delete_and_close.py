"""POST /api/v1/maps/{id}/nodes/delete and DELETE /api/v1/maps/{id}?close_sessions=true
(packages/api/maps.py::delete_nodes / delete_map_closing_sessions, TopomapDatabaseClient.delete_nodes).

Built on the in-memory store and switch fakes of tests/unit/test_mapping_switch.py.
"""
import os
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

import packages.api.main as main  # noqa: E402
from packages.api import maps  # noqa: E402
from packages.topomap_dbs.client import TopomapDatabaseClient  # noqa: E402
from packages.topomap_dbs.graph_db.server import GraphDatabaseService  # noqa: E402
from packages.topomap_dbs.image_db.server import ImageDatabaseService  # noqa: E402
from tests.unit import test_maps_m1 as m1  # noqa: E402
from tests.unit.test_mapping_switch import (  # noqa: E402,F401 - `db` is a fixture
    FakeOrch, add_robot, db, make_switch)

pytestmark = pytest.mark.unit

N1, N2, N9 = (str(uuid.uuid4()) for _ in range(3))


async def _status(coro):
    try:
        await coro
    except HTTPException as exc:
        return exc.status_code, exc.detail
    raise AssertionError("no HTTPException")


def _delete_fn(deleted=(N1,), missing=(), edges=2, failures=()):
    calls = []

    def fn(map_id, ids):
        calls.append((map_id, list(ids)))
        return {"deleted": list(deleted), "missing": list(missing), "edges_deleted": edges,
                "image_failures": list(failures)}
    fn.calls = calls
    return fn


async def run_delete_nodes(db, ids=(N1,), count=5, fn=None, notify=None, name="yard"):
    fn = fn or _delete_fn()
    def counter(_name):
        if count is None:
            raise RuntimeError("ArangoDB down")
        return count
    out = await maps.delete_nodes(None, name, {"node_ids": list(ids)}, m1.PUB, "op", fn,
                                  arango_node_count=counter, notify=notify)
    return out, fn


# --- the request ----------------------------------------------------------------------------

class TestRequest:
    @pytest.mark.parametrize("body", [{}, {"node_ids": []}, {"node_ids": [1]},
                                      {"node_ids": [""]}, {"node_ids": ["a/b"]},
                                      {"node_ids": [N1] + [str(uuid.uuid4()) for _ in range(500)]},
                                      {"node_ids": ["reconstruction"]}, {"node_ids": ["n1"]},
                                      {"node_ids": [N1, "reconstruction"]},
                                      {"node_ids": [N1.upper()]},
                                      {"node_ids": [N1], "x": 1}, {"node_ids": N1}])
    async def test_bad_bodies_are_422(self, db, body):
        db.add_map("yard", type="local")
        code, _ = await _status(maps.delete_nodes(None, "yard", body, m1.PUB, None, _delete_fn()))
        assert code == 422

    async def test_500_ids_and_duplicates(self, db):
        db.add_map("yard", type="local")
        base = [str(uuid.uuid4()) for _ in range(498)]
        out, fn = await run_delete_nodes(db, base + [base[0], base[0]])
        assert fn.calls == [("yard", base)]
        assert out["deleted"] == [N1]


# --- guards ---------------------------------------------------------------------------------

class TestGuards:
    async def test_unknown_map_is_404(self, db):
        code, _ = await _status(run_delete_nodes(db))
        assert code == 404

    async def test_deleting_map_is_409(self, db):
        db.add_map("yard", lifecycle="DELETING", type="local")
        fn = _delete_fn()
        code, detail = await _status(run_delete_nodes(db, fn=fn))
        assert code == 409 and "being deleted" in detail and not fn.calls

    async def test_unpaused_mapping_session_is_409_naming_the_robot(self, db):
        db.add_map("yard", type="local", status={"state": "mapping"})
        db.add_session("yard", "r1", ended=False, purpose="mapping")
        fn = _delete_fn()
        code, detail = await _status(run_delete_nodes(db, fn=fn))
        assert code == 409 and "r1 (mapping)" in detail and not fn.calls

    async def test_paused_and_operate_sessions_do_not_block(self, db):
        db.add_map("yard", type="local", status={"state": "paused"})
        db.add_session("yard", "r1", ended=False, purpose="mapping", paused_at=m1.T0)
        db.add_session("yard", "r2", ended=False, purpose="operate")
        out, fn = await run_delete_nodes(db)
        assert out["deleted"] == [N1] and fn.calls


# --- the effect -----------------------------------------------------------------------------

class TestEffect:
    async def test_response_event_and_stream(self, db):
        db.add_map("yard", type="local")
        notify = AsyncMock()
        fn = _delete_fn(deleted=(N1, N2), missing=(N9,), edges=3)
        out, _ = await run_delete_nodes(db, [N1, N2, N9], fn=fn, notify=notify)
        assert out == {"deleted": [N1, N2], "missing": [N9], "edges_deleted": 3,
                       "map_state": "ready"}
        [event] = [e for e in db.events if e["code"] == "MAP.NODES_DELETED"]
        assert event["payload"]["deleted"] == 2 and event["payload"]["edges_deleted"] == 3
        assert event["payload"]["missing"] == 1 and event["payload"]["map_name"] == "yard"
        [(map_id, message)] = [c.args for c in notify.await_args_list]
        assert map_id == "yard" and message["type"] == "nodes_deleted"
        assert message["node_ids"] == [N1, N2] and message["edges_deleted"] == 3

    async def test_nothing_deleted_sends_nothing(self, db):
        db.add_map("yard", type="local")
        notify = AsyncMock()
        out, _ = await run_delete_nodes(db, fn=_delete_fn(deleted=(), missing=(N1,), edges=0),
                                        notify=notify)
        assert out["deleted"] == [] and out["missing"] == [N1]
        notify.assert_not_awaited()

    async def test_a_failing_stream_never_fails_the_delete(self, db):
        db.add_map("yard", type="local")
        out, _ = await run_delete_nodes(db, notify=AsyncMock(side_effect=RuntimeError("x")))
        assert out["deleted"] == [N1]

    async def test_image_failures_are_reported(self, db):
        db.add_map("yard", type="local")
        out, _ = await run_delete_nodes(db, fn=_delete_fn(failures=(N1,)))
        assert out["image_failures"] == [N1]

    async def test_failed_ids_are_in_response_and_event(self, db):
        db.add_map("yard", type="local")
        out, _ = await run_delete_nodes(db, [N1, N2], fn=_delete_fn(
            deleted=(N1, N2), failures=(N2,)))
        assert out["image_failures"] == [N2]
        [event] = [e for e in db.events if e["code"] == "MAP.NODES_DELETED"]
        assert event["payload"]["image_failures"] == 1
        assert event["payload"]["image_failed_ids"] == [N2]

    async def test_start_and_resume_are_refused_while_nodes_are_deleted(self, db):
        db.add_map("yard", type="local")
        seen = {}

        def fn(map_id, ids):
            # runs in a thread while the guard is held
            seen["during"] = dict(maps._NODE_DELETES)
            return {"deleted": ids, "missing": [], "edges_deleted": 0, "image_failures": []}
        await run_delete_nodes(db, fn=fn)
        assert seen["during"] == {"yard": 1} and maps._NODE_DELETES == {}
        with maps._deleting_nodes("yard"):
            code, detail = await _status(maps.start_session(
                None, "yard", {"robot": "r1"}, m1.PUB))
            assert code == 409 and "being deleted" in detail
            code, detail = await _status(maps.session_action(
                None, "yard", str(uuid.uuid4()), "resume", m1.PUB))
            assert code == 409 and "being deleted" in detail
            code, _ = await _status(maps.session_action(          # pause is not blocked
                None, "yard", str(uuid.uuid4()), "pause", m1.PUB))
            assert code == 404
        maps.refuse_while_deleting_nodes("yard")

    async def test_guard_is_released_on_error(self, db):
        db.add_map("yard", type="local")
        db.add_session("yard", "r1", ended=False, purpose="mapping")
        await _status(run_delete_nodes(db))
        assert maps._NODE_DELETES == {}

    async def test_empty_ready_map_goes_back_to_draft(self, db):
        db.add_map("yard", type="local")
        out, _ = await run_delete_nodes(db, count=0)
        assert out["map_state"] == "draft" and db.maps["yard"]["status"]["state"] == "draft"
        assert db.notifies                      # the map row change is announced

    async def test_slam_map_or_remaining_nodes_keep_it_ready(self, db):
        db.add_map("yard", type="local", status={"state": "ready",
                                           "slam_saved_at": "2026-10-09T10:00:00+00:00"})
        out, _ = await run_delete_nodes(db, count=0)
        assert out["map_state"] == "ready"
        db.add_map("shed", type="local")
        out, _ = await run_delete_nodes(db, count=3, name="shed")
        assert out["map_state"] == "ready"
        out, _ = await run_delete_nodes(db, count=None, name="shed")     # count unreadable
        assert out["map_state"] == "ready"


# --- the stores -----------------------------------------------------------------------------

class TestStores:
    def test_graph_removes_edges_before_nodes(self):
        graph = GraphDatabaseService.__new__(GraphDatabaseService)
        graph.db = MagicMock()
        graph.db.has_collection.return_value = True
        order = []

        def execute(aql, bind_vars):
            order.append(bind_vars["@col"])
            return iter([1, 1, 1]) if bind_vars["@col"] == "edges_m" else iter(["a", "b"])
        graph.db.aql.execute.side_effect = execute
        assert graph.delete_nodes("m", ["a", "b", "a", "z"]) == (["a", "b"], 3)
        assert order == ["edges_m", "nodes_m"]
        refs = graph.db.aql.execute.call_args_list[0].kwargs["bind_vars"]["refs"]
        assert refs == ["nodes_m/a", "nodes_m/b", "nodes_m/z"]
        assert "_from IN @refs OR e._to IN @refs" in graph.DELETE_EDGES_OF_NODES_AQL

    def test_graph_without_collection_or_ids(self):
        graph = GraphDatabaseService.__new__(GraphDatabaseService)
        graph.db = MagicMock()
        graph.db.has_collection.return_value = False
        assert graph.delete_nodes("m", ["a"]) == ([], 0)
        assert graph.delete_nodes("m", []) == ([], 0)

    def test_image_objects_of_every_layer(self):
        image = ImageDatabaseService.__new__(ImageDatabaseService)
        image.logger = MagicMock()
        image.default_map_id = "d"
        image.bucket_prefix = "map-"
        image.client = MagicMock()
        image.client.bucket_exists.return_value = True
        image.client.list_objects.side_effect = lambda bucket, prefix, recursive: [
            SimpleNamespace(object_name=f"{prefix}{sub}/x") for sub in
            ("images", "thumbs", "depth", "costmap")]
        assert image.delete_nodes_objects([N1, N2], "m") == []
        removed = [c.args[1] for c in image.client.remove_object.call_args_list]
        assert len(removed) == 8 and f"{N2}/costmap/x" in removed and f"{N1}/depth/x" in removed
        assert all(c.kwargs["prefix"].endswith("/") for c in
                   image.client.list_objects.call_args_list)

    def test_image_failure_and_missing_bucket(self):
        image = ImageDatabaseService.__new__(ImageDatabaseService)
        image.logger = MagicMock()
        image.default_map_id = "d"
        image.bucket_prefix = "map-"
        image.client = MagicMock()
        image.client.bucket_exists.return_value = False
        assert image.delete_nodes_objects([N1], "m") == []
        image.client.bucket_exists.return_value = True
        image.client.list_objects.side_effect = RuntimeError("down")
        assert image.delete_nodes_objects([N1], "m") == [N1]

    def test_client_combines_both(self):
        client = TopomapDatabaseClient.__new__(TopomapDatabaseClient)
        client.graph = MagicMock()
        client.image = MagicMock()
        client.graph.delete_nodes.return_value = ([N1], 4)
        client.image.delete_nodes_objects.return_value = []
        assert client.delete_nodes("m", [N1, N2, N1]) == {
            "deleted": [N1], "missing": [N2], "edges_deleted": 4, "image_failures": []}
        client.graph.delete_nodes.assert_called_once_with("m", [N1, N2])

    def test_non_node_ids_are_refused_before_anything_is_touched(self):
        client = TopomapDatabaseClient.__new__(TopomapDatabaseClient)
        client.graph = MagicMock()
        client.image = MagicMock()
        with pytest.raises(ValueError):
            client.delete_nodes("m", [N1, "reconstruction"])
        client.graph.delete_nodes.assert_not_called()
        client.image.delete_nodes_objects.assert_not_called()

    @pytest.mark.parametrize("bad", ["reconstruction", "n1", "", "a/b", N1.upper()])
    def test_image_guard_refuses_non_node_prefixes(self, bad):
        image = ImageDatabaseService.__new__(ImageDatabaseService)
        image.logger = MagicMock()
        image.default_map_id = "d"
        image.bucket_prefix = "map-"
        image.client = MagicMock()
        image.client.bucket_exists.return_value = True
        with pytest.raises(ValueError):
            image.delete_nodes_objects([N1, bad], "m")
        image.client.list_objects.assert_not_called()
        image.client.remove_object.assert_not_called()


def test_route_is_registered():
    routes = [r for r in main.app.routes
              if getattr(r, "path", None) == "/api/v1/maps/{map_id}/nodes/delete"]
    assert len(routes) == 1 and routes[0].methods == {"POST"}


async def test_proxy_broadcast_reaches_the_maps_clients():
    from packages.api.server import WebSocketProxyManager
    proxy = WebSocketProxyManager()
    good, bad = AsyncMock(), AsyncMock()
    bad.send_json.side_effect = RuntimeError("gone")
    proxy.proxy_connections["map_updates:yard"] = {"clients": {good, bad}}
    assert await proxy.broadcast_map_update("yard", {"type": "nodes_deleted"}) == 1
    good.send_json.assert_awaited_once_with({"type": "nodes_deleted"})
    assert await proxy.broadcast_map_update("other", {}) == 0


# --- DELETE ?close_sessions=true ---------------------------------------------------------------

def setup_two(db, r1_reachable=True):
    db.add_map("yard", type="local", status={"state": "mapping"})
    add_robot(db, "r1")
    add_robot(db, "r2")
    o1 = FakeOrch(running=["topomap"], reachable=r1_reachable)
    o2 = FakeOrch()
    o1.db = o2.db = db
    switch = make_switch({"r1": o1, "r2": o2})
    switch.on_session = AsyncMock()
    switch.on_state = AsyncMock()
    db.add_session("yard", "r1", ended=False, purpose="mapping", services=["topo"])
    db.add_session("yard", "r2", ended=False, purpose="operate")
    return o1, o2, switch


async def close_and_delete(db, switch, delete=None):
    delete = delete or AsyncMock(return_value={"success": True, "map_id": "yard",
                                               "lifecycle": "DELETING"})
    out = await maps.delete_map_closing_sessions(None, "yard", m1.PUB, "op", delete,
                                                 switch=switch, arango_node_count=lambda _m: 3)
    return out, delete


class TestCloseSessions:
    async def test_closes_mapping_and_operate_sessions_then_deletes(self, db):
        o1, o2, switch = setup_two(db)
        out, delete = await close_and_delete(db, switch)
        assert out["success"] is True and out["lifecycle"] == "DELETING"
        assert out["closed_sessions"] == [
            {"robot": "r1", "session_id": str(db.sessions[0]["session_id"]),
             "purpose": "mapping"},
            {"robot": "r2", "session_id": str(db.sessions[1]["session_id"]),
             "purpose": "operate"}]
        assert all(s["ended_at"] is not None for s in db.sessions)
        assert o1.services["topomap"] is False                     # stopped like a finish
        assert [(a["service"], a["action"], a["ok"]) for a in out["robot_actions"]] == [
            ("topomap", "stop", True)]
        delete.assert_awaited_once_with("yard")
        assert "mapping_warning" not in out and "slam_warning" not in out
        assert db.codes().count("MAP.SESSION_FINISHED") == 2

    async def test_offline_robot_is_reported_never_blocking(self, db):
        o1, _o2, switch = setup_two(db, r1_reachable=False)
        out, delete = await close_and_delete(db, switch)
        assert len(out["closed_sessions"]) == 2
        [action] = out["robot_actions"]
        assert action["ok"] is False and "not reachable" in action["detail"]
        assert out["mapping_warning"].startswith("Could not stop topomap")
        delete.assert_awaited_once()
        assert all(s["ended_at"] is not None for s in db.sessions)

    async def test_no_open_sessions(self, db):
        db.add_map("yard", type="local")
        out, delete = await close_and_delete(db, make_switch({}))
        assert out["closed_sessions"] == [] and out["robot_actions"] == []
        delete.assert_awaited_once()

    async def test_unknown_map_goes_to_the_normal_delete(self, db):
        delete = AsyncMock(return_value={"success": True})
        out = await maps.delete_map_closing_sessions(None, "ghost", m1.PUB, None, delete)
        assert out["closed_sessions"] == [] and out["success"] is True

    async def test_pending_slam_save_is_reported_not_awaited(self, db):
        _o1, _o2, switch = setup_two(db)
        switch.slam_save_view = lambda robot: (
            {"map": "yard", "state": "saving", "detail": None, "at": "t"}
            if robot == "r1" else None)
        out, _ = await close_and_delete(db, switch)
        [saved] = out["slam_saves"]
        assert saved["robot"] == "r1" and saved["state"] == "saving"
        assert "r1" in out["slam_warning"] and "saving" in out["slam_warning"]

    async def test_a_failed_save_of_another_map_is_not_reported(self, db):
        _o1, _o2, switch = setup_two(db)
        switch.slam_save_view = lambda robot: {"map": "other", "state": "failed",
                                               "detail": "x", "at": "t"}
        out, _ = await close_and_delete(db, switch)
        assert "slam_saves" not in out and "slam_warning" not in out

    async def test_race_a_robot_opens_a_session_meanwhile(self, db):
        """The delete's guard still applies after the closing: here it finds a new session."""
        _o1, _o2, switch = setup_two(db)

        async def guard_refuses(map_id):
            db.add_session("yard", "r3", ended=False, purpose="operate")
            raise HTTPException(409, "Map 'yard' is in use by r3 (using); finish those "
                                     "sessions before deleting the map")
        code, detail = await _status(close_and_delete(db, switch,
                                                      delete=AsyncMock(side_effect=guard_refuses)))
        assert code == 409 and "r3 (using)" in detail
        assert "sessions already closed:" in detail and "r1:" in detail and "r2:" in detail
        open_robots = [s["robot_name"] for s in db.sessions if s["ended_at"] is None]
        assert open_robots == ["r3"]            # the first two stay closed, the new one is not touched

    async def test_later_close_failure_lists_the_closed_sessions(self, db):
        _o1, _o2, switch = setup_two(db)
        real = maps.session_action

        async def action(*a, **kw):
            if a[2] == str(db.sessions[1]["session_id"]):
                raise HTTPException(409, "robot busy")
            return await real(*a, **kw)
        maps.session_action = action
        try:
            code, detail = await _status(close_and_delete(db, switch))
        finally:
            maps.session_action = real
        assert code == 409 and detail.startswith("robot busy")
        assert f"r1:{db.sessions[0]['session_id']}" in detail

    async def test_session_finished_meanwhile_is_skipped(self, db):
        _o1, _o2, switch = setup_two(db)
        db.sessions[1]["ended_at"] = m1.T0      # r2 finished between the read and the finish
        real = maps.session_action
        calls = []

        async def action(*a, **kw):
            calls.append(a[2])
            return await real(*a, **kw)
        maps.session_action = action
        try:
            out, _ = await close_and_delete(db, switch)
        finally:
            maps.session_action = real
        assert [c["robot"] for c in out["closed_sessions"]] == ["r1"]


class TestDeleteRoute:
    def _service(self, db_):
        service = SimpleNamespace(database=None, mapping_switch=None, reloc_jobs=None,
                                  delete_map=AsyncMock(return_value={"success": True,
                                                                     "map_id": "yard"}))
        return service

    async def test_default_keeps_the_plain_delete(self, db):
        service = self._service(db)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(main, "service", service)
            out = await main.delete_map("yard")
        assert out == {"success": True, "map_id": "yard"}
        service.delete_map.assert_awaited_once_with("yard")

    async def test_default_still_409_through_the_guard(self, db):
        """close_sessions=false: the guard of MapDeleter.request refuses, nothing is closed."""
        db.add_map("yard", type="local")
        db.add_session("yard", "r1", ended=False, purpose="mapping")

        class Cursor:
            async def execute(self, sql, params=None):
                self.rows = [("r1", "mapping")] if sql == maps.REFUSE_OPEN_SESSION_SQL else []

            async def fetchall(self):
                return self.rows
        code, detail = await _status(maps.refuse_open_session(Cursor(), "yard"))
        assert code == 409 and "r1 (mapping)" in detail
        assert db.sessions[0]["ended_at"] is None

    async def test_close_sessions_true_goes_through_the_closing(self, db):
        _o1, _o2, switch = setup_two(db)
        service = self._service(db)
        service.mapping_switch = switch
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(main, "service", service)
            mp.setattr(main, "_arango_node_count", lambda _m: 3)
            out = await main.delete_map("yard", close_sessions=True)
        assert out["success"] is True and len(out["closed_sessions"]) == 2
        service.delete_map.assert_awaited_once_with("yard")


# --- the ArangoDB node count -------------------------------------------------------------------

class TestArangoNodeCount:
    def _count(self, stats):
        graph = SimpleNamespace(get_map_stats=lambda name: stats)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(main, "service", SimpleNamespace(graph_db=graph))
            return main._arango_node_count("yard")

    def test_count_and_unknown_map(self):
        assert self._count({"node_count": 4}) == 4
        assert self._count({"error": "Map yard not found"}) == 0

    def test_other_errors_raise_so_callers_use_the_stored_counts(self):
        with pytest.raises(RuntimeError):
            self._count({"error": "connection reset"})

    async def test_transient_error_never_flips_ready_to_draft(self, db):
        db.add_map("yard", type="local")
        graph = SimpleNamespace(get_map_stats=lambda name: {"error": "timeout"})
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(main, "service", SimpleNamespace(graph_db=graph))
            out = await maps.delete_nodes(None, "yard", {"node_ids": [N1]}, m1.PUB, "op",
                                          _delete_fn(), arango_node_count=main._arango_node_count)
        assert out["map_state"] == "ready"
