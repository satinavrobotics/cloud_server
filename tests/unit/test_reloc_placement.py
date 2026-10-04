"""Relocalization on local maps, server side (D2; docs/satinav-maps-redesign.md ## 16).

- the orchestrator save call gets cloud_map_id / cloud_session_id (orchestrator proxy);
- OrchestratorMaps.held: cached, never raising ("does the orchestrator hold this map?");
- positionInitialized / localizationScore reach robot status;
- POST .../place with source "reloc": identity transform, no still check, refusals;
- `reloc` in the placement-suggestions answer; degraded warning on a placed reloc session.
"""
import json
import os

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

import packages.api.main as main  # noqa: E402
import packages.controllers.mission.vda5050_types as types  # noqa: E402
from cloud_common.objects.robot import RobotObjectV1, RobotStatusV1  # noqa: E402
from packages.api import maps  # noqa: E402
from packages.api import orchestrator_client as oc  # noqa: E402
from packages.api.orchestrator_maps import OrchestratorMaps  # noqa: E402
from packages.api.orchestrator_proxy import with_cloud_ids  # noqa: E402
from packages.utils import map_sessions as ms  # noqa: E402
from tests.unit import test_maps_m1 as m1  # noqa: E402
from tests.unit.test_placement_suggestion import (  # noqa: E402,F401
    _status, _unplaced, db,
)

pytestmark = pytest.mark.unit

# `can_start` / `can_start_reason` were added to every reloc read (additive; reloc_job.py). The
# FakeHolder below cannot start anything, so it is always this:
NO_START = {"can_start": False, "can_start_reason": "relocalization cannot be started from here"}


def _robot(db, name="r1", online=True, address=True, **status):
    extra = {"ip_address": "10.0.0.5", "entrypoint_port": 8080} if address else {}
    db.robots[name] = RobotObjectV1(name=name, status=RobotStatusV1(
        online=online, state="IDLE", pose={"x": 0.0, "y": 0.0, "theta": 0.0}, **status), **extra)
    return db.robots[name]


class FakeHolder:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    async def held(self, robot, cloud_map_id, fresh=False):
        self.calls.append((robot.name, cloud_map_id, fresh))
        return self.answer


# --- 1. the ids in the orchestrator save call -----------------------------------------------------

SESSION = {"session_id": "6f1c0c2e-0000-4000-8000-000000000001", "map_name": "shed",
           "purpose": "mapping", "ended_at": None}


class TestSaveBody:
    def test_ids_are_added(self):
        out = json.loads(with_cloud_ids("POST", "maps/cloud-shed/save", b'{"stop_after": true}',
                                        SESSION))
        assert out == {"stop_after": True, "cloud_map_id": "shed",
                       "cloud_session_id": SESSION["session_id"]}

    def test_empty_body(self):
        out = json.loads(with_cloud_ids("POST", "maps/cloud-shed/save", b"", SESSION))
        assert out["cloud_map_id"] == "shed"

    def test_ids_the_caller_sent_are_kept(self):
        body = json.dumps({"cloud_map_id": "x", "cloud_session_id": "y"}).encode()
        assert json.loads(with_cloud_ids("POST", "maps/cloud-shed/save", body, SESSION)) == {
            "cloud_map_id": "x", "cloud_session_id": "y"}

    def test_explicit_null_ids_count_as_unset(self):
        body = json.dumps({"cloud_map_id": None, "cloud_session_id": None}).encode()
        assert json.loads(with_cloud_ids("POST", "maps/cloud-shed/save", body, SESSION)) == {
            "cloud_map_id": "shed", "cloud_session_id": SESSION["session_id"]}

    @pytest.mark.parametrize("method,path,body,session", [
        ("GET", "maps/cloud-shed/save", b"", SESSION),
        ("POST", "maps/lab/mapping/start", b"{}", SESSION),
        ("POST", "maps/cloud-shed/save", b"{}", None),
        ("POST", "maps/cloud-shed/save", b"{}", {**SESSION, "purpose": "operate"}),
        ("POST", "maps/cloud-other/save", b"{}", SESSION),      # not the session's own map
        ("POST", "maps/lab/save", b"{}", SESSION),
        ("POST", "maps/cloud-shed/save", b"not json", SESSION),
        ("POST", "maps/cloud-shed/save", b"[1]", SESSION),
    ])
    def test_everything_else_is_unchanged(self, method, path, body, session):
        assert with_cloud_ids(method, path, body, session) == body


# --- 2. does the orchestrator hold the map ----------------------------------------------------------

def _maps_client(handler, calls):
    def factory(_robot):
        def http(timeout):
            return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout)
        return oc.OrchestratorClient(_robot, http_factory=http)
    return factory


def _entry(cloud_id="shed", valid=True):
    return {"name": "n", "valid": valid, "meta": {"name": "n", "cloud_map_id": cloud_id}}


def _holder(rows=None, status=200, exc=None, **kw):
    calls = []

    def handler(request):
        calls.append(request)
        if exc is not None:
            raise exc
        return httpx.Response(status, json=rows if rows is not None else [])
    return OrchestratorMaps(client_factory=_maps_client(handler, calls), **kw), calls


class TestHeld:
    async def test_asks_by_cloud_map_id(self):
        h, calls = _holder([_entry()])
        r = RobotObjectV1(name="r1", status=RobotStatusV1(online=True), ip_address="10.0.0.5",
                          entrypoint_port=8080)
        assert await h.held(r, "shed") is True
        assert calls[0].url.path == "/maps/list"
        assert dict(calls[0].url.params) == {"cloud_map_id": "shed"}

    async def test_only_a_valid_row_counts(self):
        r = _robot(type("D", (), {"robots": {}})())
        for rows, expected in (([], False), ([_entry(valid=False)], False),
                               ([_entry(), _entry(valid=False)], True),
                               ([_entry("other")], False)):
            h, _ = _holder(rows)
            assert await h.held(r, "shed") is expected

    @pytest.mark.parametrize("kw", [
        {"exc": httpx.ConnectError("no route")},
        {"exc": httpx.ReadTimeout("slow")},
        {"status": 404, "rows": []},       # an older orchestrator without the route
        {"status": 500, "rows": []},
        {"exc": ValueError("boom")},
    ])
    async def test_errors_are_unknown_not_raised(self, kw):
        h, _ = _holder(**kw)
        r = _robot(type("D", (), {"robots": {}})())
        assert await h.held(r, "shed") is None

    async def test_malformed_answer_is_unknown_or_not_held(self):
        h, _ = _holder(rows={"not": "a list"})
        r = _robot(type("D", (), {"robots": {}})())
        assert await h.held(r, "shed") is False

    async def test_offline_or_no_address_is_not_asked(self):
        h, calls = _holder([_entry()])
        d = type("D", (), {"robots": {}})()
        assert await h.held(_robot(d, online=False), "shed") is None
        assert await h.held(_robot(d, address=False), "shed") is None
        assert calls == []

    async def test_cache_fresh_and_invalidate(self):
        now = [0.0]
        h, calls = _holder([_entry()], ttl=10, clock=lambda: now[0])
        r = _robot(type("D", (), {"robots": {}})())
        await h.held(r, "shed")
        await h.held(r, "shed")
        assert len(calls) == 1
        await h.held(r, "shed", fresh=True)
        assert len(calls) == 2
        h.invalidate("r1")
        await h.held(r, "shed")
        assert len(calls) == 3
        now[0] = 11.0
        await h.held(r, "shed")
        assert len(calls) == 4


class TestHeldCaching:
    async def test_unknown_is_not_cached_for_the_full_ttl(self):
        now = [0.0]
        h, calls = _holder(exc=httpx.ConnectError("no route"), ttl=15, clock=lambda: now[0])
        r = _robot(type("D", (), {"robots": {}})())
        assert await h.held(r, "shed") is None
        assert await h.held(r, "shed") is None
        assert len(calls) == 1                     # a burst shares the short unknown answer
        now[0] = 2.5                               # past the ~2 s unknown TTL, far inside 15 s
        await h.held(r, "shed")
        assert len(calls) == 2

    async def test_a_real_answer_keeps_the_full_ttl(self):
        now = [0.0]
        h, calls = _holder([_entry()], ttl=15, clock=lambda: now[0])
        r = _robot(type("D", (), {"robots": {}})())
        await h.held(r, "shed")
        now[0] = 14.0
        await h.held(r, "shed")
        assert len(calls) == 1

    async def test_expired_entries_are_pruned(self):
        now = [0.0]
        h, _ = _holder([_entry()], ttl=10, clock=lambda: now[0])
        r = _robot(type("D", (), {"robots": {}})())
        for name in ("a", "b", "c"):
            await h.held(r, name)
        assert len(h._cache) == 3
        now[0] = 11.0
        await h.held(r, "d")
        assert list(k[1] for k in h._cache) == ["d"]

    async def test_concurrent_callers_share_one_call(self):
        import asyncio
        gate = asyncio.Event()
        calls = []

        class Slow(OrchestratorMaps):
            async def _fetch(self, robot, cloud_map_id):
                calls.append(cloud_map_id)
                await gate.wait()
                return True
        h = Slow()
        r = _robot(type("D", (), {"robots": {}})())
        tasks = [asyncio.ensure_future(h.held(r, "shed", fresh=True)) for _ in range(5)]
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        gate.set()
        assert await asyncio.gather(*tasks) == [True] * 5
        assert calls == ["shed"]
        assert not h._inflight

    async def test_invalidate_drops_a_read_in_flight(self):
        import asyncio
        gate = asyncio.Event()
        answers = iter([False, True])

        class Slow(OrchestratorMaps):
            async def _fetch(self, robot, cloud_map_id):
                answer = next(answers)
                await gate.wait()
                return answer
        h = Slow()
        r = _robot(type("D", (), {"robots": {}})())
        first = asyncio.ensure_future(h.held(r, "shed"))
        await asyncio.sleep(0)
        h.invalidate("r1")                       # e.g. a proxied save while the read runs
        second = asyncio.ensure_future(h.held(r, "shed"))
        await asyncio.sleep(0)
        gate.set()
        assert (await first, await second) == (False, True)
        assert await h.held(r, "shed") is True   # the stale answer was not cached over it


class TestProxyInvalidation:
    @pytest.mark.parametrize("method,path,invalidated", [
        ("POST", "maps/cloud-shed/save", True), ("DELETE", "maps/lab", True),
        ("PUT", "maps/lab/rename", True), ("POST", "maps/lab/load", True),
        ("GET", "maps/list", False), ("POST", "processes/x/start", False)])
    async def test_non_get_maps_calls_drop_the_held_cache(self, method, path, invalidated):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock, patch
        import packages.api.orchestrator_proxy as proxy
        holder = MagicMock()
        service = SimpleNamespace(
            database=MagicMock(get_object=AsyncMock(return_value=_robot(
                type("D", (), {"robots": {}})()))),
            orchestrator_maps=holder)
        request = MagicMock(method=method, headers={}, url=MagicMock(query=""))
        request.app.state.service = service
        request.body = AsyncMock(return_value=b"")

        class Client:
            def __init__(self, *a, **k): ...
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def request(self, **k):
                return httpx.Response(200, content=b"{}")
        with patch.object(proxy.httpx, "AsyncClient", Client), \
                patch.object(proxy, "_open_mapping_session", AsyncMock(return_value=None)):
            await proxy.proxy_to_orchestrator("r1", path, request)
        assert holder.invalidate.called is invalidated


# --- 3. position state in the robot status -----------------------------------------------------------

class TestAgvPosition:
    def test_score_is_optional(self):
        p = types.VDA5050AgvPosition(x=1, y=2, theta=0)
        assert p.localizationScore is None and p.positionInitialized is True
        p = types.VDA5050AgvPosition(x=1, y=2, theta=0, positionInitialized=False,
                                     localizationScore=0.4)
        assert (p.positionInitialized, p.localizationScore) == (False, 0.4)

    async def test_they_reach_the_status(self):
        from tests.unit.test_maps_use_run_change import FakeDb, _robot as dispatcher_robot
        r = dispatcher_robot(FakeDb([]))
        msg = types.VDA5050State(
            headerId=1, timestamp="2026-10-03T10:00:00Z", nodeStates=[], edgeStates=[],
            batteryState=None, velocity=None,
            agvPosition=dict(x=1.0, y=2.0, theta=0.5, mapId="map", positionInitialized=False,
                             localizationScore=0.25))
        await r._on_client_message(msg)
        st = r._robot_object.status
        assert (st.position_initialized, st.localization_score) == (False, 0.25)
        assert (st.pose.x, st.pose.y, st.pose.theta, st.pose.map_id) == (1.0, 2.0, 0.5, "map")

    def test_defaults_are_unknown(self):
        st = RobotStatusV1()
        assert st.position_initialized is None and st.localization_score is None


# --- 4. placing with source reloc ----------------------------------------------------------------------

RELOC = {"source": "reloc"}


class TestPlaceReloc:
    async def _place(self, db, holder, body=RELOC, **status):
        _robot(db, **status)
        s = _unplaced(db)
        return await maps.place_session(None, "shed", str(s["session_id"]), body, m1.PUB, "ann",
                                        holder=holder), s

    def test_request_needs_no_poses_only_for_reloc(self):
        assert maps.PlaceRequest(source="reloc").pose is None
        with pytest.raises(Exception):
            maps.PlaceRequest(source="last_position")
        with pytest.raises(Exception):
            maps.PlaceRequest()
        with pytest.raises(Exception):
            maps.PlaceRequest(source="session")
        # maps §17: a geo session placed from the robot's datum needs no poses either
        assert maps.PlaceRequest(source="datum").pose is None

    async def test_identity_placement_without_the_still_check(self, db):
        h = FakeHolder(True)
        # the robot "moves": no robot_pose to compare, and a velocity would refuse a manual one
        out, s = await self._place(db, h, position_initialized=True, localization_score=0.9)
        sess = out["session"]
        assert sess["aligned"] is True
        assert sess["placement"]["source"] == "reloc"
        assert sess["map_T_session"] == {"tx": 0.0, "ty": 0.0, "yaw": 0.0}
        assert h.calls == [("r1", "shed", True)]

    async def test_the_still_check_is_not_called(self, db):
        async def boom(*a, **k):
            raise AssertionError("check_robot_still must not run for reloc")
        from unittest.mock import patch
        with patch.object(maps, "check_robot_still", boom):
            out, _ = await self._place(db, FakeHolder(True))
        assert out["session"]["aligned"] is True

    async def test_manual_placement_still_checks_the_robot(self, db):
        _robot(db)
        s = _unplaced(db)
        body = {"pose": {"x": 1.0, "y": 2.0, "yaw": 0.3},
                "robot_pose": {"x": 5.0, "y": 0.0, "theta": 0.0}}
        assert await _status(maps.place_session(None, "shed", str(s["session_id"]), body,
                                                m1.PUB, holder=FakeHolder(True))) == 409

    @pytest.mark.parametrize("answer", [False, None])
    async def test_not_held_or_unknown_is_refused(self, db, answer):
        _robot(db)
        s = _unplaced(db)
        assert await _status(maps.place_session(None, "shed", str(s["session_id"]), RELOC,
                                                m1.PUB, holder=FakeHolder(answer))) == 409

    async def test_without_a_holder_is_refused(self, db):
        _robot(db)
        s = _unplaced(db)
        assert await _status(maps.place_session(None, "shed", str(s["session_id"]), RELOC,
                                                m1.PUB)) == 409

    async def test_offline_robot_is_refused(self, db):
        _robot(db, online=False)
        s = _unplaced(db)
        assert await _status(maps.place_session(None, "shed", str(s["session_id"]), RELOC,
                                                m1.PUB, holder=FakeHolder(True))) == 409

    async def test_already_placed_is_refused(self, db):
        _robot(db)
        s = _unplaced(db, aligned=True)
        assert await _status(maps.place_session(None, "shed", str(s["session_id"]), RELOC,
                                                m1.PUB, holder=FakeHolder(True))) == 409

    async def test_an_uninitialized_position_is_refused(self, db):
        _robot(db, position_initialized=False)
        s = _unplaced(db)
        assert await _status(maps.place_session(None, "shed", str(s["session_id"]), RELOC,
                                                m1.PUB, holder=FakeHolder(True))) == 409

    async def test_a_low_score_places_and_only_warns(self, db):
        r = _robot(db, position_initialized=True, localization_score=0.1)
        s = _unplaced(db)
        out = await maps.place_session(None, "shed", str(s["session_id"]), RELOC, m1.PUB,
                                       holder=FakeHolder(True))
        assert out["session"]["aligned"] is True
        view = {"placement_source": out["session"]["placement"]["source"]}
        assert ms.localization_warning(view, r.status)

    @pytest.mark.parametrize("kw", [{"online": False}, {"position_initialized": False}])
    async def test_cheap_refusals_come_before_the_orchestrator_is_asked(self, db, kw):
        _robot(db, **kw)
        s = _unplaced(db)
        h = FakeHolder(True)
        assert await _status(maps.place_session(None, "shed", str(s["session_id"]), RELOC,
                                                m1.PUB, holder=h)) == 409
        assert h.calls == []

    async def test_an_already_placed_session_does_not_ask_the_orchestrator(self, db):
        _robot(db)
        s = _unplaced(db, aligned=True)
        h = FakeHolder(True)
        assert await _status(maps.place_session(None, "shed", str(s["session_id"]), RELOC,
                                                m1.PUB, holder=h)) == 409
        assert h.calls == []

    async def test_one_robot_read_and_one_session_read_before_the_orchestrator(self, db):
        _robot(db)
        s = _unplaced(db)
        reads = []
        orig = m1.FakeStore.robot

        async def counting(self, name):
            reads.append(name)
            return await orig(self, name)
        from unittest.mock import patch
        with patch.object(m1.FakeStore, "robot", counting):
            await maps.place_session(None, "shed", str(s["session_id"]), RELOC, m1.PUB,
                                     holder=FakeHolder(True))
        assert reads == ["r1", "r1"]          # the pre-check and the transaction, no more

    async def test_the_stored_placement_follows_the_one_d0_definition(self, db, monkeypatch):
        """Change D0 in reloc_map_t_session() only: pose, robot_pose and the transform stay
        consistent (pose is the robot pose carried through the transform)."""
        monkeypatch.setattr(ms, "reloc_map_t_session",
                            lambda: {"tx": 10.0, "ty": -2.0, "yaw": 0.5})
        _robot(db)
        db.robots["r1"].status.pose.x, db.robots["r1"].status.pose.y = 1.0, 2.0
        s = _unplaced(db)
        out = await maps.place_session(None, "shed", str(s["session_id"]), RELOC, m1.PUB,
                                       holder=FakeHolder(True))
        placement = out["session"]["placement"]
        t = out["session"]["map_T_session"]
        assert t == {"tx": 10.0, "ty": -2.0, "yaw": 0.5}
        assert ms.placement_transform(placement["pose"], placement["robot_pose"]) == \
            pytest.approx(t)

    async def test_start_with_a_reloc_placement_is_422(self, db):
        _robot(db)
        body = {"robot": "r1", "purpose": "operate", "placement": RELOC}
        db.add_map("lab", type="local", status={"state": "ready"})
        db.add_session("lab", "r0", "live", node_count=10)
        assert await _status(maps.start_session(None, "lab", body, m1.PUB)) == 422


# --- 5. the answer the client reads ---------------------------------------------------------------------

class TestRelocStatus:
    async def test_available(self, db):
        _robot(db)
        s = _unplaced(db)
        h = FakeHolder(True)
        out = await maps.placement_suggestions(None, "shed", str(s["session_id"]), holder=h)
        assert out["reloc"] == {"available": True, "known": True, "source": "orchestrator",
                                **NO_START}
        assert h.calls == [("r1", "shed", False)]      # a GET: the cached read

    @pytest.mark.parametrize("answer,known", [(False, True), (None, False)])
    async def test_not_held_or_unknown_means_manual(self, db, answer, known):
        _robot(db)
        s = _unplaced(db)
        out = await maps.placement_suggestions(None, "shed", str(s["session_id"]),
                                               holder=FakeHolder(answer))
        assert out["reloc"] == {"available": False, "known": known, "source": "orchestrator",
                                **NO_START}

    async def test_no_holder_is_unknown(self, db):
        _robot(db)
        s = _unplaced(db)
        out = await maps.placement_suggestions(None, "shed", str(s["session_id"]))
        assert out["reloc"]["available"] is False and out["reloc"]["known"] is False

    async def test_placed_or_geo_has_none(self, db):
        _robot(db)
        s = _unplaced(db, aligned=True)
        out = await maps.placement_suggestions(None, "shed", str(s["session_id"]),
                                               holder=FakeHolder(True))
        assert out["reloc"] is None and out["suggestions"] == []


class TestRelocReadsDoNotLock:
    async def test_status_and_map_reloc_take_no_row_lock(self, db):
        _robot(db)
        db.add_map("shed", type="local", status={"state": "ready"})
        s = _unplaced(db)
        db.locks.clear()
        assert (await maps.reloc_status(None, FakeHolder(True), "shed",
                                        str(s["session_id"])))["available"] is True
        await maps.map_reloc(None, FakeHolder(True), "shed", "r1")
        assert db.locks == []

    async def test_place_still_locks(self, db):
        _robot(db)
        s = _unplaced(db)
        db.locks.clear()
        await maps.place_session(None, "shed", str(s["session_id"]), RELOC, m1.PUB,
                                 holder=FakeHolder(True))
        assert ("map", "shed") in db.locks and ("session", str(s["session_id"])) in db.locks


class TestMapReloc:
    async def test_available_and_cached_read(self, db):
        _robot(db)
        db.add_map("shed", type="local", status={"state": "ready"})
        h = FakeHolder(True)
        out = await maps.map_reloc(None, h, "shed", "r1")
        assert out == {"available": True, "known": True, "source": "orchestrator", **NO_START}
        assert h.calls == [("r1", "shed", False)]

    @pytest.mark.parametrize("answer,known", [(False, True), (None, False)])
    async def test_not_held_or_unknown(self, db, answer, known):
        _robot(db)
        db.add_map("shed", type="local", status={"state": "ready"})
        out = await maps.map_reloc(None, FakeHolder(answer), "shed", "r1")
        assert out == {"available": False, "known": known, "source": "orchestrator", **NO_START}

    async def test_unknown_robot_or_no_holder_is_unknown(self, db):
        _robot(db)
        db.add_map("shed", type="local", status={"state": "ready"})
        h = FakeHolder(True)
        assert (await maps.map_reloc(None, h, "shed", "ghost"))["known"] is False
        assert h.calls == []
        assert (await maps.map_reloc(None, None, "shed", "r1"))["known"] is False

    async def test_holder_that_raises_is_unknown(self, db):
        _robot(db)
        db.add_map("shed", type="local", status={"state": "ready"})

        class Boom:
            async def held(self, *a, **k):
                raise RuntimeError("x")
        out = await maps.map_reloc(None, Boom(), "shed", "r1")
        assert out == {"available": False, "known": False, "source": "orchestrator",
                       **NO_START}

    async def test_geo_map_and_unknown_map(self, db):
        from packages.utils import map_geo
        _robot(db)
        db.add_map("geo1", type="geo", status={"state": "ready"},
                   geo=map_geo.geo_from_datum(m1.UTM_DATUM))
        h = FakeHolder(True)
        assert await maps.map_reloc(None, h, "geo1", "r1") == {
            "available": False, "known": True, "source": "orchestrator", "can_start": False,
            "can_start_reason": "a geo map is placed by its datum, not relocalized"}
        assert h.calls == []
        assert await _status(maps.map_reloc(None, h, "nomap", "r1")) == 404


# --- 6. degraded ------------------------------------------------------------------------------------------

class TestDegraded:
    def test_reloc_degraded(self):
        f = ms.reloc_degraded
        assert f("reloc", True, 0.9) is None
        assert f("reloc", None, None) is None
        assert f("reloc", False, 0.9) is not None
        assert f("reloc", True, ms.RELOC_DEGRADED_SCORE - 0.01) is not None
        assert f("reloc", True, ms.RELOC_DEGRADED_SCORE) is None
        assert f("user", False, 0.0) is None            # only reloc sessions warn
        assert f(None, False, 0.0) is None

    def test_threshold_is_the_config_value(self):
        from packages import config
        assert ms.RELOC_DEGRADED_SCORE == config.RELOC_DEGRADED_SCORE

    def test_localization_warning_helper(self):
        st = RobotStatusV1(position_initialized=True, localization_score=0.1)
        assert ms.localization_warning({"placement_source": "reloc"}, st)
        assert ms.localization_warning({"placement_source": "user"}, st) is None
        assert ms.localization_warning(None, st) is None

    def test_session_view_carries_the_source_when_placed(self):
        row = {"session_id": "s", "map_name": "m", "purpose": "operate", "aligned": True,
               "map_t_session": ms.reloc_map_t_session(), "placement": {"source": "reloc"}}
        assert ms.robot_session_view(row)["placement_source"] == "reloc"
        assert ms.robot_session_view({**row, "aligned": False})["placement_source"] is None

    async def test_robot_view_warns_on_a_placed_reloc_session(self):
        r = RobotObjectV1(name="r1", status=RobotStatusV1(
            online=True, position_initialized=False, localization_score=0.1))
        session = {"placement_source": "reloc"}
        out = await main._robot_views([r], {"r1": session})
        assert out[0]["localization_warning"]
        assert out[0]["status"]["position_initialized"] is False
        assert out[0]["status"]["localization_score"] == 0.1
        out = await main._robot_views([r], {"r1": {"placement_source": "user"}})
        assert out[0]["localization_warning"] is None
        assert (await main._robot_views([r]))[0]["localization_warning"] is None


class TestWebSocketLocalizationWarning:
    async def _broadcast(self, status, session_view):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock
        from packages.api.server import ApiDelegationService
        svc = object.__new__(ApiDelegationService)
        svc._running = True
        svc._robot_changes = asyncio.Queue()
        svc.telemetry = None
        svc.logger = MagicMock()
        svc._robot_session = AsyncMock(return_value=session_view)
        svc.ws_manager = MagicMock()

        async def stop(*a, **k):
            svc._running = False
        svc.ws_manager.broadcast = AsyncMock(side_effect=stop)
        await svc._robot_changes.put(RobotObjectV1(name="r1", status=status))
        await asyncio.wait_for(svc._handle_robot_updates(), timeout=2)
        (_, name, message), _ = svc.ws_manager.broadcast.await_args
        assert name == "r1"
        return message

    async def test_degraded_reloc_session_warns(self):
        msg = await self._broadcast(
            RobotStatusV1(online=True, position_initialized=False, localization_score=0.1),
            {"placement_source": "reloc"})
        assert msg["localization_warning"]
        assert msg["status"]["position_initialized"] is False

    async def test_healthy_or_non_reloc_has_none(self):
        healthy = RobotStatusV1(online=True, position_initialized=True, localization_score=0.9)
        assert (await self._broadcast(healthy, {"placement_source": "reloc"})
                )["localization_warning"] is None
        low = RobotStatusV1(online=True, position_initialized=False)
        assert (await self._broadcast(low, {"placement_source": "user"})
                )["localization_warning"] is None
        assert (await self._broadcast(low, None))["localization_warning"] is None
