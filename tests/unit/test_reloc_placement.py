"""Relocalization on local maps, server side (D2; docs/satinav-maps-redesign.md section 12.D).

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
        out = json.loads(with_cloud_ids("POST", "maps/lab/save", b'{"stop_after": true}',
                                        SESSION))
        assert out == {"stop_after": True, "cloud_map_id": "shed",
                       "cloud_session_id": SESSION["session_id"]}

    def test_empty_body(self):
        out = json.loads(with_cloud_ids("POST", "maps/lab/save", b"", SESSION))
        assert out["cloud_map_id"] == "shed"

    def test_ids_the_caller_sent_are_kept(self):
        body = json.dumps({"cloud_map_id": "x", "cloud_session_id": "y"}).encode()
        assert json.loads(with_cloud_ids("POST", "maps/lab/save", body, SESSION)) == {
            "cloud_map_id": "x", "cloud_session_id": "y"}

    @pytest.mark.parametrize("method,path,body,session", [
        ("GET", "maps/lab/save", b"", SESSION),
        ("POST", "maps/lab/mapping/start", b"{}", SESSION),
        ("POST", "maps/lab/save", b"{}", None),
        ("POST", "maps/lab/save", b"{}", {**SESSION, "purpose": "operate"}),
        ("POST", "maps/lab/save", b"not json", SESSION),
        ("POST", "maps/lab/save", b"[1]", SESSION),
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
            maps.PlaceRequest(source="datum")

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

    @pytest.mark.parametrize("status", [{"position_initialized": False},
                                        {"localization_score": 0.1}])
    async def test_a_degraded_robot_is_refused(self, db, status):
        _robot(db, **status)
        s = _unplaced(db)
        assert await _status(maps.place_session(None, "shed", str(s["session_id"]), RELOC,
                                                m1.PUB, holder=FakeHolder(True))) == 409

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
        assert out["reloc"] == {"available": True, "known": True, "source": "orchestrator"}
        assert h.calls == [("r1", "shed", True)]

    @pytest.mark.parametrize("answer,known", [(False, True), (None, False)])
    async def test_not_held_or_unknown_means_manual(self, db, answer, known):
        _robot(db)
        s = _unplaced(db)
        out = await maps.placement_suggestions(None, "shed", str(s["session_id"]),
                                               holder=FakeHolder(answer))
        assert out["reloc"] == {"available": False, "known": known, "source": "orchestrator"}

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


class TestMapReloc:
    async def test_available_and_cached_read(self, db):
        _robot(db)
        db.add_map("shed", type="local", status={"state": "ready"})
        h = FakeHolder(True)
        out = await maps.map_reloc(None, h, "shed", "r1")
        assert out == {"available": True, "known": True, "source": "orchestrator"}
        assert h.calls == [("r1", "shed", False)]

    @pytest.mark.parametrize("answer,known", [(False, True), (None, False)])
    async def test_not_held_or_unknown(self, db, answer, known):
        _robot(db)
        db.add_map("shed", type="local", status={"state": "ready"})
        out = await maps.map_reloc(None, FakeHolder(answer), "shed", "r1")
        assert out == {"available": False, "known": known, "source": "orchestrator"}

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
        assert out == {"available": False, "known": False, "source": "orchestrator"}

    async def test_geo_map_and_unknown_map(self, db):
        from packages.utils import map_geo
        _robot(db)
        db.add_map("geo1", type="geo", status={"state": "ready"},
                   geo=map_geo.geo_from_datum(m1.UTM_DATUM))
        h = FakeHolder(True)
        assert await maps.map_reloc(None, h, "geo1", "r1") == {
            "available": False, "known": True, "source": "orchestrator"}
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

    def test_identity_is_one_named_function(self):
        assert ms.reloc_map_t_session() == {"tx": 0.0, "ty": 0.0, "yaw": 0.0}

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
