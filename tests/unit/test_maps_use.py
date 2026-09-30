"""Maps §14 U1 (docs/satinav-maps-redesign.md §14): operate sessions and placement.

- packages/utils/map_sessions.py: placement math (a property test), the robot-moved and
  robot-drives checks, set_payload (operate / unplaced / services), the robot's `session` view;
- packages/api/maps.py: the start rules as a matrix (purpose x map state x map type x robot
  online/datum x existing session x replace), `replace` atomicity, placement on start, `place`
  and its refusals, pause/resume refused on operate, finish of an operate session, the
  summary's `operating`, the history endpoint (paging), archive/delete naming the robots;
- graph-builder's decide(): not_mapping_session, session_unplaced;
- migration 20260930_01_maps_use (text; the real run is in the U1 rehearsal on Postgres).

The in-memory store is the M1/M3 one (tests/unit/test_maps_m1.py, test_maps_m2.py).
"""
import copy
import importlib.util
import math
import os
import random
import uuid
from pathlib import Path

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

import packages.api.main as main  # noqa: E402
from cloud_common.objects.robot import RobotObjectV1, RobotStatusV1  # noqa: E402
from packages.api import maps  # noqa: E402
from packages.services.graph_builder import ingest  # noqa: E402
from packages.utils import map_geo  # noqa: E402
from packages.utils import map_sessions as ms  # noqa: E402
from tests.unit import test_maps_m1 as m1  # noqa: E402
from tests.unit.test_maps_m2 import ShimDb as M3Db  # noqa: E402

pytestmark = pytest.mark.unit

PUB = m1.PUB
UTM_DATUM = m1.UTM_DATUM
GEO = map_geo.geo_from_datum(UTM_DATUM)


@pytest.fixture
def db():
    d = M3Db()
    with patch.object(maps, "open_store", d.store), patch.object(maps, "_utcnow", m1.Clock()):
        yield d


async def _status(coro):
    try:
        await coro
    except HTTPException as exc:
        return exc.status_code, exc.detail
    raise AssertionError("no HTTPException")


def _robot(db, name="r1", online=True, pose=(0.0, 0.0, 0.0), state="IDLE", **datum):
    db.robots[name] = RobotObjectV1(
        name=name, datum=datum or {},
        status=RobotStatusV1(online=online, state=state,
                             pose={"x": pose[0], "y": pose[1], "theta": pose[2]}))


def _place_body(pose=(5.0, 1.0, 0.5), robot_pose=(0.0, 0.0, 0.0)):
    return {"pose": {"x": pose[0], "y": pose[1], "yaw": pose[2]},
            "robot_pose": {"x": robot_pose[0], "y": robot_pose[1], "theta": robot_pose[2]}}


def _local_with_nodes(db, name="shed", state="ready"):
    db.add_map(name, type="local", status={"state": state})
    db.add_session(name, "r0", "live", node_count=10)


async def _start(db, map_name="shed", **body):
    body.setdefault("robot", "r1")
    return await maps.start_session(None, map_name, body, PUB)


# --- placement math ------------------------------------------------------------------------------

class TestPlacementMath:
    def test_the_robot_pose_lands_on_the_placed_pose(self):
        rng = random.Random(14)
        for _ in range(500):
            pose = {"x": rng.uniform(-500, 500), "y": rng.uniform(-500, 500),
                    "yaw": rng.uniform(-math.pi, math.pi)}
            rpose = {"x": rng.uniform(-500, 500), "y": rng.uniform(-500, 500),
                     "theta": rng.uniform(-math.pi, math.pi)}
            t = ms.placement_transform(pose, rpose)
            x, y, yaw = map_geo.apply_pose(t, rpose["x"], rpose["y"], rpose["theta"])
            assert x == pytest.approx(pose["x"], abs=1e-6)
            assert y == pytest.approx(pose["y"], abs=1e-6)
            assert ms.yaw_difference(yaw, pose["yaw"]) < 1e-9
            # and back: robot_T_map takes the placed pose to the robot's own pose
            inv = map_geo.invert_transform(t)
            bx, by, _ = map_geo.apply_pose(inv, pose["x"], pose["y"], pose["yaw"])
            assert (bx, by) == pytest.approx((rpose["x"], rpose["y"]), abs=1e-6)

    def test_identity_when_the_robot_is_placed_where_it_thinks_it_is(self):
        t = ms.placement_transform({"x": 3, "y": 4, "yaw": 1}, {"x": 3, "y": 4, "theta": 1})
        assert map_geo.is_identity(t, 1e-9)

    @pytest.mark.parametrize("now,moved", [
        ((0.019, 0.0, 0.0), False), ((0.0, 0.021, 0.0), True),
        ((0.0, 0.0, math.radians(0.49)), False), ((0.0, 0.0, math.radians(0.51)), True),
        ((0.0, 0.0, 2 * math.pi), False)])
    def test_robot_moved_tolerance(self, now, moved):
        shown = {"x": 0.0, "y": 0.0, "theta": 0.0}
        got = ms.pose_moved(shown, {"x": now[0], "y": now[1], "theta": now[2]})
        assert (got is not None) == moved

    @pytest.mark.parametrize("state,msg,driving", [
        ("IDLE", None, False),
        ("ON_TASK", None, True),
        ("IDLE", {"driving": True}, True),
        ("IDLE", {"driving": False, "velocity": {"vx": 0.0, "vy": 0.0, "omega": 0.0}}, False),
        ("IDLE", {"velocity": {"vx": 0.005, "vy": 0.0, "omega": 0.0}}, False),
        ("IDLE", {"velocity": {"vx": 0.2, "vy": 0.0, "omega": 0.0}}, True),
        ("IDLE", {"velocity": {"vx": 0.0, "vy": 0.0, "omega": 0.05}}, True),
        ("IDLE", {"nodeStates": [{"nodeId": "n1"}]}, True),
        ("TELEOP", {"driving": False}, False)])
    def test_driving(self, state, msg, driving):
        assert (ms.driving_reason(state, msg) is not None) == driving


# --- the robot view ---------------------------------------------------------------------------------

class TestRobotView:
    SID = uuid.uuid4()

    def _s(self, **kw):
        base = {"session_id": self.SID, "map_name": "shed", "purpose": "mapping",
                "services": ["topo"], "paused_at": None, "ended_at": None, "aligned": True,
                "map_t_session": {"tx": 1.0, "ty": 2.0, "yaw": 0.1}}
        base.update(kw)
        return base

    def test_robot_view(self):
        v = ms.robot_session_view({**self._s(), "robot_name": "r1"})
        assert v == {"session_id": str(self.SID), "map": "shed", "purpose": "mapping",
                     "state": "mapping", "aligned": True,
                     "map_T_session": {"tx": 1.0, "ty": 2.0, "yaw": 0.1},
                     "unplaced_reason": None}
        v = ms.robot_session_view({**self._s(purpose="operate", aligned=False,
                                             placement={"unplaced_reason": "run_changed"})})
        assert v["state"] == "operating" and v["aligned"] is False
        assert v["map_T_session"] is None and v["unplaced_reason"] == "run_changed"
        assert ms.robot_session_view(None) is None

    async def test_robot_routes_carry_session(self, db):
        _local_with_nodes(db)
        _robot(db)
        await _start(db, purpose="operate", placement=_place_body())
        svc = MagicMock()
        svc.database = None
        svc.mapping_switch.snapshots = AsyncMock(return_value={})
        svc.database_get = None

        async def get_object(_cls, name):
            return db.robots[name]

        async def list_objects(_cls, query_params=None):
            return list(db.robots.values())

        svc.database = MagicMock()
        svc.database.get_object = get_object
        svc.database.list_objects = list_objects
        with patch.object(main, "service", svc):
            one = await main.get_robot("r1")
            many = await main.list_robots()
        assert one["session"]["map"] == "shed" and one["session"]["purpose"] == "operate"
        assert one["session"]["aligned"] is True
        assert many[0]["session"] == one["session"]


# --- start rules -------------------------------------------------------------------------------------

class TestStartMatrix:
    @pytest.mark.parametrize("purpose", ["mapping", "operate"])
    @pytest.mark.parametrize("map_state", ["draft", "ready", "mapping", "archived"])
    @pytest.mark.parametrize("map_type", ["local", "geo"])
    async def test_matrix(self, db, purpose, map_state, map_type):
        spec = {"type": map_type}
        if map_type == "geo":
            spec["geo"] = GEO
        db.add_map("m", status={"state": map_state}, **spec)
        if map_state == "mapping":
            db.add_session("m", "r9", "live", ended=False)
        _robot(db, **UTM_DATUM)
        body = {"robot": "r1", "purpose": purpose}
        if (map_state == "archived" or (purpose == "operate" and map_state == "draft")
                or (purpose == "mapping" and map_state == "mapping")):
            code, _ = await _status(maps.start_session(None, "m", body, PUB))
            assert code == 409
            return
        out = await maps.start_session(None, "m", body, PUB)
        s = out["session"]
        assert s["purpose"] == purpose
        if purpose == "mapping":
            assert out["map_state"] == "mapping" and s["services"] == ["topo"]
        else:
            assert out["map_state"] == map_state and s["services"] is None
            assert s["state"] == "operating"
            assert db.maps["m"]["status"]["state"] == map_state  # the map state is untouched
        # geo: placed by the datum; an empty local map's first mapping: identity, placed; an
        # operate session on a local map without placement: not placed.
        assert s["aligned"] is (map_type == "geo" or purpose == "mapping")

    async def test_offline_and_no_datum(self, db):
        db.add_map("g", type="geo", geo=GEO, status={"state": "ready"})
        db.add_session("g", "r0", "live", node_count=3)
        _robot(db, online=False, **UTM_DATUM)
        assert (await _status(_start(db, "g", purpose="operate")))[0] == 409
        _robot(db)  # online, no datum
        code, detail = await _status(_start(db, "g", purpose="operate"))
        assert code == 409 and "datum" in detail

    @pytest.mark.parametrize("body,loc", [
        ({"purpose": "watch"}, "purpose"),
        ({"purpose": "operate", "services": ["topo"]}, "services"),
        ({"services": ["lidar"]}, "services"),
        ({"services": []}, "services"),
        ({"placement": {"pose": {"x": 1, "y": 2}}}, "placement"),
        ({"replace": "maybe"}, "replace")])
    async def test_422(self, db, body, loc):
        _local_with_nodes(db)
        _robot(db)
        code, detail = await _status(_start(db, **body))
        assert code == 422 and detail[0]["loc"][:2] == ["body", loc]

    async def test_placement_on_a_geo_map_is_422(self, db):
        db.add_map("g", type="geo", geo=GEO, status={"state": "ready"})
        _robot(db, **UTM_DATUM)
        code, detail = await _status(_start(db, "g", purpose="operate",
                                            placement=_place_body()))
        assert code == 422 and detail[0]["loc"] == ["body", "placement"]

    async def test_several_robots_use_a_map_another_one_maps(self, db):
        db.add_map("g", type="geo", geo=GEO, status={"state": "ready"})
        for r in ("r1", "r2", "r3"):
            _robot(db, r, **UTM_DATUM)
        await _start(db, "g", robot="r1")  # mapping
        await _start(db, "g", robot="r2", purpose="operate")
        await _start(db, "g", robot="r3", purpose="operate")
        assert db.maps["g"]["status"]["state"] == "mapping"
        summary = await maps.session_summary(None, "g")
        assert summary["open"]["robot_name"] == "r1"
        assert sorted(o["robot"] for o in summary["operating"]) == ["r2", "r3"]
        assert all(o["aligned"] for o in summary["operating"])

    async def test_services(self, db):
        db.add_map("d", type="local", status={"state": "draft"})
        _robot(db)
        out = await _start(db, "d", services=["topo", "grid", "topo"])
        assert out["session"]["services"] == ["topo", "grid"]
        assert db.sessions[-1]["services"] == ["topo", "grid"]


class TestReplace:
    async def test_without_replace_is_409_and_with_replace_switches(self, db):
        _local_with_nodes(db, "shed")
        db.add_map("g", type="geo", geo=GEO, status={"state": "ready"})
        _robot(db, **UTM_DATUM)
        first = (await _start(db, "g", purpose="operate"))["session"]
        code, detail = await _status(_start(db, "shed", purpose="operate"))
        assert code == 409 and "replace" in detail
        out = await _start(db, "shed", purpose="operate", replace=True)
        assert out["replaced_session"]["session_id"] == first["session_id"]
        assert out["replaced_session"]["state"] == "finished"
        assert [s["map_name"] for s in db.sessions if s["ended_at"] is None
                and s["robot_name"] == "r1"] == ["shed"]
        assert db.codes()[-2:] == ["MAP.SESSION_FINISHED", "MAP.SESSION_STARTED"]

    async def test_a_refused_start_keeps_the_old_session(self, db):
        db.add_map("g", type="geo", geo=GEO, status={"state": "ready"})
        db.add_map("arch", type="local", status={"state": "archived"})
        _robot(db, **UTM_DATUM)
        await _start(db, "g", purpose="operate")
        before = copy.deepcopy((db.maps, db.sessions))
        events = list(db.events)
        code, _ = await _status(_start(db, "arch", purpose="operate", replace=True))
        assert code == 409
        assert (db.maps, db.sessions) == before and db.events == events

    async def test_replacing_mapping_with_operate_on_the_same_map_carries_the_placement(
            self, db):
        db.add_map("shed", type="local", status={"state": "draft"})
        _robot(db)
        mapping = (await _start(db, "shed"))["session"]  # empty map: identity, placed
        db.sessions[-1]["node_count"] = 20
        out = await _start(db, "shed", purpose="operate", replace=True)
        s = out["session"]
        assert s["aligned"] is True and s["placement"]["source"] == "session"
        assert s["placement"]["from_session_id"] == mapping["session_id"]
        assert out["map_state"] == "ready"  # the mapping session finished

    async def test_a_placed_session_on_another_map_is_not_carried(self, db):
        _local_with_nodes(db, "shed")
        _local_with_nodes(db, "barn")
        _robot(db)
        await _start(db, "barn", purpose="operate", placement=_place_body())
        s = (await _start(db, "shed", purpose="operate", replace=True))["session"]
        assert s["aligned"] is False and s["placement"] is None


class TestPlacement:
    async def test_extending_a_local_map_starts_unplaced_until_placed(self, db):
        _local_with_nodes(db)
        _robot(db, pose=(2.0, 3.0, 0.25))
        out = await maps.start_session(None, "shed", {"robot": "r1"}, PUB)
        s = out["session"]
        assert s["aligned"] is False and out["map_state"] == "mapping"
        body = _place_body(pose=(10.0, -4.0, 1.0), robot_pose=(2.0, 3.0, 0.25))
        out = await maps.place_session(None, "shed", s["session_id"], body, PUB, "ann")
        placed = out["session"]
        assert placed["aligned"] is True and placed["placement"]["source"] == "user"
        assert placed["placement"]["actor"] == "ann"
        x, y, yaw = map_geo.apply_pose(placed["map_T_session"], 2.0, 3.0, 0.25)
        assert (x, y, yaw) == pytest.approx((10.0, -4.0, 1.0))
        assert db.codes()[-1] == "MAP.SESSION_PLACED"
        # a placed mapping session is not re-placed (its nodes would split)
        code, _ = await _status(maps.place_session(None, "shed", s["session_id"], body, PUB))
        assert code == 409

    async def test_placement_on_start(self, db):
        _local_with_nodes(db)
        _robot(db, pose=(1.0, 1.0, 0.0))
        out = await _start(db, purpose="operate",
                           placement=_place_body(pose=(4.0, 5.0, 0.0), robot_pose=(1.0, 1.0, 0.0)))
        s = out["session"]
        assert s["aligned"] is True
        assert s["map_T_session"] == pytest.approx({"tx": 3.0, "ty": 4.0, "yaw": 0.0})
        assert db.codes()[-1] == "MAP.SESSION_STARTED"
        assert db.events[-1]["payload"]["purpose"] == "operate"
        assert db.events[-1]["payload"]["placement"]["source"] == "user"

    async def test_operate_can_be_re_placed(self, db):
        _local_with_nodes(db)
        _robot(db)
        sid = (await _start(db, purpose="operate", placement=_place_body()))["session"][
            "session_id"]
        out = await maps.place_session(None, "shed", sid, _place_body(pose=(7.0, 7.0, 0.0)),
                                       PUB)
        assert out["session"]["map_T_session"]["tx"] == pytest.approx(7.0)
        assert db.events[-1]["payload"]["old_map_T_session"]["tx"] == pytest.approx(5.0)

    @pytest.mark.parametrize("case,code", [
        ("driving_state", 409), ("driving_msg", 409), ("moved", 409), ("turned", 409),
        ("offline", 409), ("finished", 409), ("geo", 409), ("unknown", 404),
        ("other_map", 404), ("bad_body", 422)])
    async def test_place_refusals(self, db, case, code):
        _local_with_nodes(db)
        db.add_map("g", type="geo", geo=GEO, status={"state": "ready"})
        _robot(db, **UTM_DATUM)
        map_name = "g" if case == "geo" else "shed"
        sid = (await _start(db, map_name, purpose="operate"))["session"]["session_id"]
        body = _place_body()
        if case == "driving_state":
            _robot(db, state="ON_TASK", **UTM_DATUM)
        elif case == "driving_msg":
            db.state_msgs["r1"] = {"velocity": {"vx": 0.3, "vy": 0.0, "omega": 0.0}}
        elif case == "moved":
            _robot(db, pose=(0.05, 0.0, 0.0), **UTM_DATUM)
        elif case == "turned":
            _robot(db, pose=(0.0, 0.0, math.radians(1.0)), **UTM_DATUM)
        elif case == "offline":
            _robot(db, online=False, **UTM_DATUM)
        elif case == "finished":
            await maps.session_action(None, "shed", sid, "finish", PUB)
        elif case == "unknown":
            sid = str(uuid.uuid4())
        elif case == "other_map":
            map_name = "g"
        elif case == "bad_body":
            body = {"pose": {"x": 1, "y": 1, "yaw": 0}}
        before = copy.deepcopy(db.sessions)
        got, _ = await _status(maps.place_session(None, map_name, sid, body, PUB))
        assert got == code and db.sessions == before

    async def test_start_with_placement_refused_while_driving(self, db):
        _local_with_nodes(db)
        _robot(db)
        db.state_msgs["r1"] = {"driving": True}
        code, detail = await _status(_start(db, purpose="operate", placement=_place_body()))
        assert code == 409 and "driving" in detail and db.sessions[-1]["robot_name"] == "r0"


class TestOperateLifecycle:
    async def test_pause_resume_refused_finish_is_stop_using(self, db):
        _local_with_nodes(db)
        _robot(db)
        sid = (await _start(db, purpose="operate", placement=_place_body()))["session"][
            "session_id"]
        for action in ("pause", "resume"):
            code, detail = await _status(maps.session_action(None, "shed", sid, action, PUB))
            assert code == 409 and "operate" in detail
        out = await maps.session_action(None, "shed", sid, "finish", PUB)
        assert out["map_state"] == "ready" and out["session"]["state"] == "finished"
        assert db.events[-1]["payload"]["purpose"] == "operate"

    async def test_finishing_operate_keeps_a_mapping_map_mapping(self, db):
        db.add_map("g", type="geo", geo=GEO, status={"state": "ready"})
        _robot(db, "r1", **UTM_DATUM)
        _robot(db, "r2", **UTM_DATUM)
        await _start(db, "g", robot="r1")
        sid = (await _start(db, "g", robot="r2", purpose="operate"))["session"]["session_id"]
        out = await maps.session_action(None, "g", sid, "finish", PUB)
        assert out["map_state"] == "mapping" and db.maps["g"]["status"]["state"] == "mapping"

    async def test_archive_names_the_robots(self, db):
        _local_with_nodes(db)
        _robot(db)
        await _start(db, purpose="operate")
        code, detail = await _status(maps.archive_map(None, "shed", PUB))
        assert code == 409 and "r1 (using)" in detail


class TestHistory:
    async def test_paging_newest_first(self, db):
        db.add_map("shed", type="local", status={"state": "ready"})
        base = m1.T0
        import datetime
        for i in range(7):
            db.add_session("shed", f"r{i}", "live",
                           started_at=base + datetime.timedelta(minutes=i))
        page = await maps.session_history(None, "shed", limit=3)
        assert [s["robot_name"] for s in page["items"]] == ["r6", "r5", "r4"]
        assert page["count"] == 7 and page["next_before"] == page["items"][-1]["session_id"]
        page = await maps.session_history(None, "shed", limit=3, before=page["next_before"])
        assert [s["robot_name"] for s in page["items"]] == ["r3", "r2", "r1"]
        page = await maps.session_history(None, "shed", limit=3, before=page["next_before"])
        assert [s["robot_name"] for s in page["items"]] == ["r0"]
        assert page["next_before"] is None

    @pytest.mark.parametrize("kw,code", [({"limit": 0}, 422), ({"limit": 201}, 422),
                                         ({"before": "x"}, 422)])
    async def test_bad_query(self, db, kw, code):
        db.add_map("shed", type="local")
        assert (await _status(maps.session_history(None, "shed", **kw)))[0] == code

    async def test_unknown_map(self, db):
        assert (await _status(maps.session_history(None, "nope")))[0] == 404

    async def test_routes_are_registered(self):
        routes = {(m, r.path) for r in main.app.routes for m in getattr(r, "methods", ()) or ()}
        assert ("GET", "/api/v1/maps/{map_id}/sessions") in routes
        assert ("POST", "/api/v1/maps/{map_id}/sessions/{session_id}/place") in routes
        # `place` must win over the generic /{action} route (registered before it)
        paths = [r.path for r in main.app.routes]
        assert (paths.index("/api/v1/maps/{map_id}/sessions/{session_id}/place")
                < paths.index("/api/v1/maps/{map_id}/sessions/{session_id}/{action}"))


# --- graph-builder ---------------------------------------------------------------------------------

class TestIngestRules:
    def _session(self, **kw):
        base = dict(session_id="s1", map_name="shed", paused=False,
                    map_t_session=dict(map_geo.IDENTITY), map_lifecycle="ALIVE",
                    map_state="mapping")
        base.update(kw)
        return ingest.OpenSession(**base)

    def test_operate_and_unplaced_are_rejected(self):
        assert ingest.decide("r1", self._session(purpose="operate", map_state="ready")
                             ).reason == ingest.NOT_MAPPING_SESSION
        assert ingest.decide("r1", self._session(aligned=False)
                             ).reason == ingest.SESSION_UNPLACED
        assert ingest.decide("r1", self._session()).accepted

    def test_row_with_purpose_and_aligned(self):
        row = ("s1", "shed", False, {}, "ALIVE", "mapping", None, None, None, "local",
               "operate", False)
        s = ingest.OpenSession.from_row(row)
        assert s.purpose == "operate" and s.aligned is False
        assert "s.purpose, s.aligned" in ingest.OPEN_SESSION_SQL

    def test_unplaced_geo_session_is_not_realigned_here(self):
        s = self._session(aligned=False, session_datum=dict(m1.ENU_DATUM),
                          robot_datum={**m1.ENU_DATUM, "latitude": 47.5}, map_geo=GEO,
                          map_type="geo")
        assert ingest.plan_realign(s) is None
        assert "AND aligned AND" in ingest.REALIGN_SQL


# --- migration -----------------------------------------------------------------------------------

def _migration():
    path = (Path(__file__).resolve().parents[2] / "packages/api/migrations/versions/"
            "20260930_01_maps_use.py")
    spec = importlib.util.spec_from_file_location("maps_use", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestMigration:
    def test_chain(self):
        mod = _migration()
        assert mod.revision == "20260930_01_maps_use"
        assert mod.down_revision == "20260929_01_maps_m2"

    def test_upgrade_is_idempotent_text(self):
        sql = _migration()._upgrade_sql()
        assert "ADD COLUMN IF NOT EXISTS purpose text NOT NULL DEFAULT 'mapping'" in sql
        assert "ADD COLUMN IF NOT EXISTS services text[]" in sql
        assert "ADD COLUMN IF NOT EXISTS placement jsonb" in sql
        assert "SET services = '{topo}'" in sql
        for c in ("map_sessions_purpose_check", "map_sessions_legacy_mapping_check",
                  "map_sessions_services_check"):
            assert f"DROP CONSTRAINT IF EXISTS {c}" in sql
            assert sql.index(f"DROP CONSTRAINT IF EXISTS {c}") < sql.index(
                f"ADD CONSTRAINT {c}")
        assert "CHECK (kind <> 'legacy' OR purpose = 'mapping')" in sql

    def test_downgrade_drops_operate_sessions_first(self):
        sql = _migration()._downgrade_sql()
        assert sql.index("DELETE FROM map_sessions WHERE purpose = 'operate'") < sql.index(
            "DROP COLUMN IF EXISTS purpose")
