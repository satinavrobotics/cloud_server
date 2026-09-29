"""Maps redesign M1 (docs/satinav-maps-redesign.md §2-§4, §7, §12).

- the map model: new optional fields, old rows still load, effective type/state;
- packages/utils/map_geo.py: classification from the legacy datum, UTM origin, map_T_session;
- migration 20260928_01_map_sessions: schema text and the data step on a fake connection;
- packages/api/maps.py and its routes on an in-memory store that mirrors SqlStore (rollback
  on error, events and NOTIFYs only on commit): create, list filters, patch, sessions (start,
  pause, resume, finish), archive/restore, the delete guard, the graph read.

The real SQL (partial unique index, locking, the migration on a copy of production) is covered
by tests/integration/maps (run.sh).
"""
import contextlib
import copy
import datetime
import importlib.util
import json
import math
import os
import uuid
from pathlib import Path

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

from unittest.mock import AsyncMock, MagicMock, Mock, patch  # noqa: E402

import psycopg  # noqa: E402
import pydantic  # noqa: E402
import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

import packages.api.main as main  # noqa: E402
from cloud_common.objects.map import (  # noqa: E402
    MapGeoV1, MapObjectV1, MapSpecV1, MapStatusV1, effective_state, effective_type,
    has_real_datum,
)
from cloud_common.objects.object import ObjectLifecycleV1  # noqa: E402
from cloud_common.objects.robot import RobotObjectV1, RobotStatusV1  # noqa: E402
from packages.api import maps  # noqa: E402
from packages.events.codes import EventCode  # noqa: E402
from packages.events.emit import build_row  # noqa: E402
from packages.utils import geo, map_geo  # noqa: E402

pytestmark = pytest.mark.unit

# The live map `map` on 2026-09-28 (datum frame enu, Budapest).
LIVE_MAP_SPEC = {"datum_frame": "enu", "description": None, "datum_latitude": 47.4979,
                 "datum_utm_zone": None, "datum_longitude": 19.0402, "datum_utm_north": None,
                 "datum_bearing_deg": 0.0, "datum_utm_easting": None, "datum_utm_northing": None}
# What sati_vda5050_client publishes (zone 34N), as in test_datum_frame.py.
UTM_DATUM = {"latitude": 47.47946, "longitude": 19.03238, "bearing_deg": 0.0, "frame": "utm",
             "utm_zone": 34, "utm_north": True, "utm_easting": 351756.484938,
             "utm_northing": 5260323.440888}
ENU_DATUM = {"latitude": 47.4979, "longitude": 19.0402, "bearing_deg": 0.0, "frame": "enu"}


# --- model -------------------------------------------------------------------------------------

class TestModel:
    def test_new_fields_default_to_none(self):
        spec = MapSpecV1()
        assert spec.type is None and spec.geo is None
        status = MapStatusV1()
        assert status.state is None and status.open_session_id is None
        assert status.grid_version is None
        assert MapObjectV1.default_spec()["type"] is None

    def test_pre_m1_row_loads(self):
        m = MapObjectV1(name="map", lifecycle="ALIVE", status={"edge_count": 0, "node_count": 0},
                        **LIVE_MAP_SPEC)
        assert m.type is None and m.status.state is None
        assert effective_type(m) == "geo" and effective_state(m.status) == "ready"

    def test_m1_row_round_trips(self):
        stored = {**LIVE_MAP_SPEC, "type": "geo",
                  "geo": {"utm_zone": 34, "utm_north": True, "origin_e": 1.0, "origin_n": 2.0}}
        m = MapObjectV1(name="map", status={"state": "mapping", "open_session_id": "s"}, **stored)
        assert isinstance(m.geo, MapGeoV1) and m.geo.utm_zone == 34
        spec = json.loads(m.spec.json())
        assert spec["type"] == "geo" and spec["geo"]["origin_n"] == 2.0
        assert MapObjectV1(name="map", status=json.loads(m.status.json()), **spec) == m

    def test_unknown_keys_are_ignored(self):
        # Old images read rows written by newer ones the same way (pydantic v1 default).
        m = MapObjectV1(name="m", status={"state": "ready", "future": 1}, future_spec=2)
        assert not hasattr(m, "future_spec")

    @pytest.mark.parametrize("bad", [{"type": "hybrid"}, {"geo": {"utm_zone": 61,
                                     "utm_north": True, "origin_e": 0, "origin_n": 0}},
                                     {"geo": {"utm_zone": 34}}])
    def test_invalid_spec(self, bad):
        with pytest.raises(pydantic.ValidationError):
            MapSpecV1(**bad)

    def test_invalid_state(self):
        with pytest.raises(pydantic.ValidationError):
            MapStatusV1(state="deleted")

    @pytest.mark.parametrize("lat,lon,real", [(47.0, 19.0, True), (0.0, 0.0, False),
                                              (None, 19.0, False), (0.0, 19.0, True)])
    def test_has_real_datum(self, lat, lon, real):
        assert has_real_datum(lat, lon) is real

    def test_effective_type_prefers_stored_type(self):
        assert effective_type(MapSpecV1(type="local", datum_latitude=47.0,
                                        datum_longitude=19.0)) == "local"
        assert effective_type(MapSpecV1()) == "local"
        assert effective_state(MapStatusV1(state="draft")) == "draft"


# --- map_geo -----------------------------------------------------------------------------------

class TestClassify:
    def test_live_map_becomes_geo_zone_34n(self):
        map_type, g = map_geo.classify(LIVE_MAP_SPEC)
        assert map_type == "geo"
        assert (g["utm_zone"], g["utm_north"]) == (34, True)
        e, n = geo.latlon_to_utm(47.4979, 19.0402, 34, True)
        assert (g["origin_e"], g["origin_n"]) == (e, n)
        assert 350000 < e < 360000 and 5260000 < n < 5263000

    @pytest.mark.parametrize("spec", [{}, {"datum_latitude": 0.0, "datum_longitude": 0.0},
                                      {"datum_latitude": 47.0}])
    def test_without_real_datum_local(self, spec):
        assert map_geo.classify(spec) == ("local", None)

    def test_utm_datum_uses_the_reported_point(self):
        spec = {"datum_latitude": UTM_DATUM["latitude"], "datum_longitude": UTM_DATUM["longitude"],
                "datum_frame": "utm", "datum_utm_zone": 34, "datum_utm_north": True,
                "datum_utm_easting": 351756.484938, "datum_utm_northing": 5260323.440888}
        assert map_geo.classify(spec) == ("geo", {"utm_zone": 34, "utm_north": True,
                                                  "origin_e": 351756.484938,
                                                  "origin_n": 5260323.440888})

    def test_utm_datum_without_easting_projects_in_the_reported_zone(self):
        spec = {"datum_latitude": 47.5, "datum_longitude": 17.9, "datum_frame": "utm",
                "datum_utm_zone": 34}  # 17.9 E is zone 33; the robot said 34
        _t, g = map_geo.classify(spec)
        assert g["utm_zone"] == 34
        assert (g["origin_e"], g["origin_n"]) == geo.latlon_to_utm(47.5, 17.9, 34, True)

    def test_southern_hemisphere(self):
        _t, g = map_geo.classify({"datum_latitude": -33.9, "datum_longitude": 18.4})
        assert g["utm_zone"] == 34 and g["utm_north"] is False and g["origin_n"] > 6e6


class TestSessionTransform:
    def _exact(self, datum, map_g, x, y):
        """A robot-frame point placed exactly: local -> lat/lon -> map-zone UTM - origin."""
        lat, lon = geo.local_to_gps(
            x, y, datum["latitude"], datum["longitude"], datum.get("bearing_deg", 0.0),
            frame=datum.get("frame"), utm_zone=datum.get("utm_zone"),
            utm_north=datum.get("utm_north"), utm_easting=datum.get("utm_easting"),
            utm_northing=datum.get("utm_northing"))
        e, n = geo.latlon_to_utm(lat, lon, map_g["utm_zone"], map_g["utm_north"])
        return e - map_g["origin_e"], n - map_g["origin_n"]

    def test_first_utm_session_is_identity(self):
        g = map_geo.geo_from_datum(UTM_DATUM)
        assert map_geo.session_transform(g, UTM_DATUM) == {"tx": 0.0, "ty": 0.0, "yaw": 0.0}

    def test_second_utm_session_is_the_datum_difference(self):
        g = map_geo.geo_from_datum(UTM_DATUM)
        later = {**UTM_DATUM, "utm_easting": UTM_DATUM["utm_easting"] + 12.5,
                 "utm_northing": UTM_DATUM["utm_northing"] - 3.25}
        t = map_geo.session_transform(g, later)
        assert t["tx"] == pytest.approx(12.5, abs=1e-9)
        assert t["ty"] == pytest.approx(-3.25, abs=1e-9)
        assert t["yaw"] == 0.0

    def test_utm_bearing_is_the_yaw(self):
        g = map_geo.geo_from_datum(UTM_DATUM)
        t = map_geo.session_transform(g, {**UTM_DATUM, "bearing_deg": 90.0})
        assert t["yaw"] == pytest.approx(math.pi / 2)

    def test_enu_session_has_the_grid_convergence(self):
        g = map_geo.geo_from_datum(ENU_DATUM)
        t = map_geo.session_transform(g, ENU_DATUM)
        assert t["tx"] == pytest.approx(0.0, abs=1e-6) and t["ty"] == pytest.approx(0.0, abs=1e-6)
        # Budapest, zone 34 (central meridian 21 E): the doc's -1.45 deg (§5). West of the
        # central meridian true east points below grid east (clockwise, negative yaw).
        assert math.degrees(t["yaw"]) == pytest.approx(-1.4451, abs=1e-3)
        # It places points correctly (to within the UTM scale factor, ~4 cm per 100 m):
        for x, y in ((100.0, 0.0), (0.0, 100.0), (-70.0, 70.0)):
            px, py = map_geo.apply_transform(t, x, y)
            ex, ey = self._exact(ENU_DATUM, g, x, y)
            assert math.hypot(px - ex, py - ey) < 0.1

    def test_enu_session_on_a_utm_origin_map(self):
        g = map_geo.geo_from_datum(UTM_DATUM)
        enu = {"latitude": 47.4801, "longitude": 19.0341, "bearing_deg": 10.0, "frame": "enu"}
        t = map_geo.session_transform(g, enu)
        for x, y in ((0.0, 0.0), (50.0, 0.0), (0.0, -50.0)):
            px, py = map_geo.apply_transform(t, x, y)
            ex, ey = self._exact(enu, g, x, y)
            assert math.hypot(px - ex, py - ey) < 0.05

    def test_utm_datum_of_another_zone(self):
        g = {"utm_zone": 34, "utm_north": True, "origin_e": 351756.0, "origin_n": 5260323.0}
        other = {"latitude": 47.47946, "longitude": 19.03238, "frame": "utm", "utm_zone": 33,
                 "utm_north": True, "bearing_deg": 0.0}
        t = map_geo.session_transform(g, other)
        assert abs(t["yaw"]) > math.radians(3)  # zone 33 grid vs zone 34 grid
        px, py = map_geo.apply_transform(t, 40.0, 30.0)
        ex, ey = self._exact(other, g, 40.0, 30.0)
        assert math.hypot(px - ex, py - ey) < 0.05

    def test_robot_datum(self):
        assert map_geo.robot_datum({"latitude": None, "longitude": None}) is None
        assert map_geo.robot_datum({"latitude": 0.0, "longitude": 0.0}) is None
        d = map_geo.robot_datum({"latitude": 47.0, "longitude": 19.0, "frame": None})
        assert d["frame"] == "enu" and d["bearing_deg"] == 0.0

    def test_normalize_yaw(self):
        assert map_geo.normalize_yaw(3 * math.pi) == pytest.approx(math.pi)
        assert map_geo.normalize_yaw(-math.pi) == math.pi


# --- migration ---------------------------------------------------------------------------------

REV = "20260928_01_map_sessions"


def _migration():
    path = (Path(__file__).resolve().parents[2] / "packages/api/migrations/versions" /
            f"{REV}.py")
    spec = importlib.util.spec_from_file_location(REV, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Result:
    def __init__(self, rows=None, scalar=None):
        self._rows, self._scalar = rows or [], scalar

    def fetchall(self):
        return self._rows

    def scalar(self):
        return self._scalar


class FakeBind:
    """exec_driver_sql on an in-memory mapobjectv1 + map_sessions."""

    def __init__(self, rows=None, table=True):
        self.rows = rows or {}  # name -> [lifecycle, spec, status]
        self.table = table
        self.sessions = {}
        self.sql = []

    def exec_driver_sql(self, sql, params=()):
        self.sql.append(sql)
        if sql.startswith("SELECT to_regclass"):
            return _Result(scalar=self.table)
        if sql.startswith("SELECT name, spec, status FROM mapobjectv1"):
            return _Result([(n, copy.deepcopy(r[1]), copy.deepcopy(r[2]))
                            for n, r in sorted(self.rows.items()) if r[0] != "DELETED"])
        if sql.startswith("UPDATE mapobjectv1 SET spec"):
            self.rows[params[1]][1].update(json.loads(params[0]))
        elif sql.startswith("UPDATE mapobjectv1 SET status"):
            self.rows[params[1]][2].update(json.loads(params[0]))
        elif sql.startswith("INSERT INTO map_sessions"):
            name, robot, datum, transform, nodes = params
            assert "ON CONFLICT (map_name) WHERE kind = 'legacy' DO NOTHING" in sql
            self.sessions.setdefault(name, {"robot": robot, "datum": json.loads(datum)
                                            if datum else None,
                                            "map_t_session": json.loads(transform),
                                            "node_count": nodes})
        else:
            raise AssertionError(sql)
        return _Result()


class TestMigration:
    def test_revision_chain(self):
        m = _migration()
        assert m.revision == REV and m.down_revision == "20260926_02_recorder_health"

    def test_schema(self):
        sql = _migration()._schema()
        assert "CREATE TABLE map_sessions" in sql
        assert ("CREATE UNIQUE INDEX map_sessions_one_open_per_robot ON map_sessions "
                "(robot_name)\n  WHERE ended_at IS NULL") in sql
        assert "map_sessions_one_legacy_per_map" in sql and "kind IN ('live', 'legacy')" in sql
        for col in ("session_id     uuid PRIMARY KEY", "map_t_session  jsonb NOT NULL",
                    "aligned        boolean NOT NULL", "node_count     int NOT NULL DEFAULT 0"):
            assert col in sql

    def test_classifies_live_data(self):
        m = _migration()
        bind = FakeBind({
            "map": ["ALIVE", dict(LIVE_MAP_SPEC), {"edge_count": 0, "node_count": 0}],
            "zero": ["ALIVE", {"datum_latitude": 0.0, "datum_longitude": 0.0}, {"node_count": 7}],
            "nodatum": ["DELETING", {}, {}],
            "gone": ["DELETED", {}, {}],
        })
        m._classify_maps(bind)
        spec, status = bind.rows["map"][1], bind.rows["map"][2]
        assert spec["type"] == "geo" and spec["geo"] == map_geo.classify(LIVE_MAP_SPEC)[1]
        assert spec["datum_latitude"] == 47.4979  # legacy datum kept
        assert status == {"edge_count": 0, "node_count": 0, "state": "ready"}
        assert bind.rows["zero"][1]["type"] == "local" and bind.rows["zero"][1]["geo"] is None
        assert bind.rows["nodatum"][1]["type"] == "local"
        assert "type" not in bind.rows["gone"][1]
        assert set(bind.sessions) == {"map", "zero", "nodatum"}
        legacy = bind.sessions["map"]
        assert legacy["robot"] == "legacy" and legacy["map_t_session"] == map_geo.IDENTITY
        assert legacy["datum"]["latitude"] == 47.4979 and legacy["datum"]["frame"] == "enu"
        assert bind.sessions["zero"] == {"robot": "legacy", "datum": None,
                                         "map_t_session": map_geo.IDENTITY, "node_count": 7}

    def test_idempotent(self):
        m = _migration()
        bind = FakeBind({"map": ["ALIVE", dict(LIVE_MAP_SPEC), {}]})
        m._classify_maps(bind)
        before = copy.deepcopy(bind.rows)
        # A map typed since (e.g. retyped by hand) is never reclassified.
        bind.rows["map"][1]["type"] = "local"
        bind.sql.clear()
        m._classify_maps(bind)
        assert bind.rows["map"][1]["type"] == "local"
        assert not any(s.startswith("UPDATE") for s in bind.sql)
        assert bind.rows["map"][2] == before["map"][2]
        assert len(bind.sessions) == 1

    def test_fresh_database_has_no_map_table(self):
        bind = FakeBind(table=False)
        _migration()._classify_maps(bind)
        assert len(bind.sql) == 1

    def test_downgrade_removes_the_new_keys(self):
        m = _migration()
        executed = []
        with patch.object(m, "op", MagicMock(execute=executed.append)):
            m.downgrade()
        sql = executed[0]
        assert "DROP TABLE IF EXISTS map_sessions" in sql
        assert "spec - 'type' - 'geo'" in sql
        assert "status - 'state' - 'open_session_id' - 'grid_version'" in sql

    def test_raw_sql_only(self):
        src = (Path(__file__).resolve().parents[2] / "packages/api/migrations/versions" /
               f"{REV}.py").read_text()
        assert "sqlalchemy" not in src


# --- the in-memory store -------------------------------------------------------------------------

T0 = datetime.datetime(2026, 9, 28, 12, 0, tzinfo=datetime.timezone.utc)


class FakeStore:
    """The SqlStore interface over dicts. Shared state lives in FakeDb; a store sees it
    directly (writes are undone by FakeDb on an exception)."""

    def __init__(self, db):
        self.db = db
        self.pending_events = []
        self.pending_notifies = []

    async def lock_map(self, name):
        row = self.db.maps.get(name)
        return maps.MapRow(name, row["lifecycle"], row["spec"], row["status"]) if row else None

    async def map_names(self):
        return list(self.db.maps)

    async def insert_map(self, name, spec, status):
        if name in self.db.maps:
            return False
        self.db.maps[name] = {"lifecycle": "ALIVE", "spec": dict(spec), "status": dict(status)}
        self.pending_notifies.append((name, "ALIVE"))
        return True

    async def update_map(self, row, spec=None, status=None):
        stored = self.db.maps[row.name]
        stored["spec"].update(spec or {})
        stored["status"].update(status or {})
        self.pending_notifies.append((row.name, row.lifecycle))

    async def robot(self, name):
        return self.db.robots.get(name)

    async def lock_robot(self, name):
        return self.db.robots.get(name)

    async def robot_state_msg(self, name):
        return self.db.state_msgs.get(name)

    async def sessions(self, map_name):
        return [dict(s) for s in self.db.sessions if s["map_name"] == map_name]

    async def sessions_page(self, map_name, limit, before):
        rows = sorted((s for s in self.db.sessions if s["map_name"] == map_name),
                      key=lambda s: (s["started_at"], str(s["session_id"])), reverse=True)
        if before is not None:
            anchor = next(s for s in self.db.sessions if str(s["session_id"]) == str(before))
            key = (anchor["started_at"], str(anchor["session_id"]))
            rows = [s for s in rows if (s["started_at"], str(s["session_id"])) < key]
        return [dict(s) for s in rows[:limit]]

    async def session_count(self, map_name):
        return sum(1 for s in self.db.sessions if s["map_name"] == map_name)

    async def open_sessions(self):
        return [dict(s) for s in self.db.sessions if s["ended_at"] is None]

    async def open_sessions_of_robot(self, robot_name):
        return [dict(s) for s in self.db.sessions
                if s["robot_name"] == robot_name and s["ended_at"] is None]

    async def lock_session(self, session_id):
        for s in self.db.sessions:
            if str(s["session_id"]) == str(session_id):
                return dict(s)
        return None

    async def insert_session(self, session):
        if any(s["robot_name"] == session["robot_name"] and s["ended_at"] is None
               for s in self.db.sessions):
            raise HTTPException(409, "unique index")
        self.db.sessions.append(dict(session))

    async def update_session(self, session_id, **fields):
        for s in self.db.sessions:
            if str(s["session_id"]) == str(session_id):
                s.update(fields)

    async def emit(self, event):
        self.pending_events.append(build_row(event, strict=True))


class FakeDb:
    def __init__(self):
        self.maps = {}
        self.robots = {}
        self.sessions = []
        self.events = []
        self.notifies = []
        self.state_msgs = {}

    def add_map(self, name, lifecycle="ALIVE", **spec_and_status):
        status = spec_and_status.pop("status", {"state": "ready"})
        self.maps[name] = {"lifecycle": lifecycle, "spec": spec_and_status, "status": status}

    def add_robot(self, name, online=True, **datum):
        self.robots[name] = RobotObjectV1(name=name, status=RobotStatusV1(online=online),
                                          datum=datum or {})

    def add_session(self, map_name, robot_name="legacy", kind="legacy", ended=True, **extra):
        row = {"session_id": uuid.uuid4(), "map_name": map_name, "robot_name": robot_name,
               "kind": kind, "started_at": T0, "paused_at": None,
               "ended_at": T0 if ended else None, "datum": None,
               "map_t_session": dict(map_geo.IDENTITY), "aligned": True, "node_count": 0,
               **extra}
        self.sessions.append(row)
        return row

    @contextlib.asynccontextmanager
    async def store(self, _db, _publisher_id):
        snapshot = copy.deepcopy((self.maps, self.sessions))
        store = FakeStore(self)
        try:
            yield store
        except BaseException:
            self.maps, self.sessions = snapshot
            raise
        self.events.extend(store.pending_events)
        self.notifies.extend(store.pending_notifies)

    def codes(self):
        return [e["code"] for e in self.events]


class Clock:
    """T0, then one second later on every call (distinct event timestamps, as in real life)."""

    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return T0 + datetime.timedelta(seconds=self.calls - 1)


@pytest.fixture
def fdb():
    db = FakeDb()
    with patch.object(maps, "open_store", db.store), patch.object(maps, "_utcnow", Clock()):
        yield db


async def _status(coro):
    try:
        await coro
    except HTTPException as exc:
        return exc.status_code, exc.detail
    raise AssertionError("no HTTPException")


PUB = uuid.uuid4()


# --- create / list / patch -----------------------------------------------------------------------

class TestCreate:
    async def test_create_draft(self, fdb):
        out = await maps.create_map(None, {"name": "yard-1", "type": "geo",
                                           "description": "the yard"}, PUB)
        assert out["name"] == "yard-1" and out["type"] == "geo" and out["geo"] is None
        assert out["status"]["state"] == "draft" and out["description"] == "the yard"
        assert fdb.maps["yard-1"]["spec"]["type"] == "geo"
        assert fdb.maps["yard-1"]["status"]["state"] == "draft"
        assert fdb.codes() == ["MAP.CREATED"] and fdb.notifies == [("yard-1", "ALIVE")]
        payload = json.loads(json.dumps(fdb.events[0]["payload"]))
        assert payload["map_type"] == "geo" and payload["state"] == "draft"

    async def test_duplicate_is_409(self, fdb):
        fdb.add_map("yard")
        code, _ = await _status(maps.create_map(None, {"name": "yard", "type": "local"}, PUB))
        assert code == 409 and fdb.events == []

    async def test_bucket_collision_is_409(self, fdb):
        fdb.add_map("Yard_1")
        code, detail = await _status(maps.create_map(None, {"name": "yard-1", "type": "local"},
                                                     PUB))
        assert code == 409 and "Yard_1" in detail

    async def test_arango_leftover_is_409(self, fdb):
        code, detail = await _status(maps.create_map(
            None, {"name": "default", "type": "local"}, PUB,
            arango_node_count=lambda n: 624 if n == "default" else 0))
        assert code == 409 and "624" in detail and "default" not in fdb.maps
        await maps.create_map(None, {"name": "fresh", "type": "local"}, PUB,
                              arango_node_count=lambda n: 0)
        assert "fresh" in fdb.maps

    @pytest.mark.parametrize("body,loc", [
        ({"name": "yard", "type": "hybrid"}, "type"),
        ({"name": "has space", "type": "local"}, "name"),
        ({"name": "-lead", "type": "local"}, "name"),
        ({"name": "x" * 60, "type": "local"}, "name"),
        ({"name": "GEO", "type": "geo"}, "name"),
        ({"type": "local"}, "name"),
        ({"name": "yard", "type": "local", "colour": 1}, "colour"),
    ])
    async def test_invalid_body_is_422(self, fdb, body, loc):
        code, detail = await _status(maps.create_map(None, body, PUB))
        assert code == 422 and detail[0]["loc"] == ["body", loc]

    async def test_non_object_body_is_422(self, fdb):
        code, _ = await _status(maps.create_map(None, ["yard"], PUB))
        assert code == 422


def _obj(name, lifecycle="ALIVE", **kw):
    status = kw.pop("status", {})
    return MapObjectV1(name=name, lifecycle=lifecycle, status=status, **kw)


class TestList:
    MAPS = [
        _obj("old_geo", datum_latitude=47.0, datum_longitude=19.0),  # pre-M1 row
        _obj("old_local"),
        _obj("draft", type="local", status={"state": "draft"}),
        _obj("arch", type="geo", status={"state": "archived"}),
        _obj("dying", "DELETING", type="local", status={"state": "ready"}),
    ]

    def _names(self, **kw):
        return [m["name"] for m in maps.filter_maps(self.MAPS, **kw)]

    def test_default_hides_archived_and_deleting(self):
        assert self._names() == ["old_geo", "old_local", "draft"]

    def test_filters(self):
        assert self._names(type_="geo") == ["old_geo"]
        assert self._names(state="ready") == ["old_geo", "old_local"]
        assert self._names(state="archived") == ["arch"]
        assert self._names(include_archived=True) == ["old_geo", "old_local", "draft", "arch"]
        assert self._names(type_="local", state="draft") == ["draft"]

    def test_effective_values_shown(self):
        views = {m["name"]: m for m in maps.filter_maps(self.MAPS)}
        assert views["old_geo"]["type"] == "geo" and views["old_geo"]["status"]["state"] == "ready"
        # Backward compatible: the old keys are all still there.
        assert views["old_geo"]["datum_latitude"] == 47.0 and "node_count" in views["old_geo"][
            "status"] and views["old_geo"]["lifecycle"] == ObjectLifecycleV1.ALIVE

    @pytest.mark.parametrize("kw", [{"type_": "hybrid"}, {"state": "gone"}])
    def test_bad_filter_is_422(self, kw):
        with pytest.raises(HTTPException) as exc:
            maps.filter_maps(self.MAPS, **kw)
        assert exc.value.status_code == 422

    async def test_route(self):
        svc = MagicMock()
        svc.database.list_objects = AsyncMock(return_value=self.MAPS)
        with patch.object(main, "service", svc):
            body = await main.list_maps()
            assert body["count"] == 3
            body = await main.list_maps(type="geo", state=None, include_archived=True)
            assert [m["name"] for m in body["maps"]] == ["old_geo", "arch"]


class TestPatch:
    async def test_description(self, fdb):
        fdb.add_map("yard", type="local", description="a")
        out = await maps.patch_map(None, "yard", {"description": "b"}, PUB)
        assert out["description"] == "b" and fdb.maps["yard"]["spec"]["description"] == "b"
        assert out["type"] == "local"

    async def test_rename_and_unknown_are_422(self, fdb):
        fdb.add_map("yard")
        for body, loc in (({"name": "other"}, "name"), ({"type": "geo"}, "type")):
            code, detail = await _status(maps.patch_map(None, "yard", body, PUB))
            assert code == 422 and detail[0]["loc"] == ["body", loc]

    async def test_missing_and_deleting(self, fdb):
        fdb.add_map("dying", lifecycle="DELETING")
        assert (await _status(maps.patch_map(None, "nope", {}, PUB)))[0] == 404
        assert (await _status(maps.patch_map(None, "dying", {}, PUB)))[0] == 409


# --- sessions ------------------------------------------------------------------------------------

class TestSessions:
    async def _start(self, fdb, map_name="yard", robot="r1"):
        return await maps.start_session(None, map_name, {"robot": robot}, PUB)

    async def test_first_geo_session_sets_the_origin(self, fdb):
        fdb.add_map("yard", type="geo", status={"state": "draft"})
        fdb.add_robot("r1", **UTM_DATUM)
        out = await self._start(fdb)
        s = out["session"]
        assert out["map_state"] == "mapping" and s["state"] == "mapping" and s["aligned"] is True
        assert s["map_T_session"] == {"tx": 0.0, "ty": 0.0, "yaw": 0.0}
        assert s["datum"]["frame"] == "utm" and s["kind"] == "live"
        row = fdb.maps["yard"]
        assert row["spec"]["geo"] == {"utm_zone": 34, "utm_north": True,
                                      "origin_e": 351756.484938, "origin_n": 5260323.440888}
        # The legacy datum describes the map frame exactly, for the old client/planner.
        assert row["spec"]["datum_frame"] == "utm" and row["spec"]["datum_utm_zone"] == 34
        assert row["spec"]["datum_utm_easting"] == 351756.484938
        assert row["spec"]["datum_latitude"] == pytest.approx(47.47946, abs=1e-7)
        assert row["status"] == {"state": "mapping", "open_session_id": s["session_id"]}
        assert fdb.codes() == ["MAP.SESSION_STARTED"]
        assert fdb.events[0]["robot_name"] == "r1"

    async def test_later_geo_session_is_translated(self, fdb):
        g = map_geo.geo_from_datum(UTM_DATUM)
        fdb.add_map("yard", type="geo", geo=g, status={"state": "ready"})
        fdb.add_session("yard", "r0", "live")
        fdb.add_robot("r1", **{**UTM_DATUM, "utm_easting": UTM_DATUM["utm_easting"] + 20.0})
        s = (await self._start(fdb))["session"]
        assert s["map_T_session"]["tx"] == pytest.approx(20.0)
        assert s["map_T_session"]["ty"] == pytest.approx(0.0) and s["aligned"] is True
        assert fdb.maps["yard"]["spec"]["geo"] == g  # never moved
        assert "datum_latitude" not in fdb.maps["yard"]["spec"]

    async def test_enu_robot_gets_the_convergence(self, fdb):
        fdb.add_map("yard", type="geo", status={"state": "draft"})
        fdb.add_robot("r1", **ENU_DATUM)
        s = (await self._start(fdb))["session"]
        assert math.degrees(s["map_T_session"]["yaw"]) == pytest.approx(-1.4451, abs=1e-3)

    async def test_geo_needs_a_datum(self, fdb):
        fdb.add_map("yard", type="geo", status={"state": "draft"})
        fdb.add_robot("r1")
        code, detail = await _status(self._start(fdb))
        assert code == 409 and "datum" in detail
        assert fdb.sessions == [] and fdb.maps["yard"]["status"]["state"] == "draft"
        assert fdb.events == []

    async def test_local_first_aligned_then_unaligned(self, fdb):
        fdb.add_map("shed", type="local", status={"state": "draft"})
        fdb.add_robot("r1", **UTM_DATUM)  # a datum is ignored on a local map
        s1 = (await self._start(fdb, "shed"))["session"]
        assert s1["aligned"] is True and s1["datum"] is None
        assert s1["map_T_session"] == map_geo.IDENTITY
        await maps.session_action(None, "shed", s1["session_id"], "finish", PUB)
        # Still empty (no node arrived): the next mapping session still defines the frame.
        s2 = (await self._start(fdb, "shed"))["session"]
        assert s2["aligned"] is True
        await maps.session_action(None, "shed", s2["session_id"], "finish", PUB)
        fdb.sessions[-1]["node_count"] = 4
        # Maps §14 (Q-U4): extending a local map with nodes starts NOT placed.
        s3 = (await self._start(fdb, "shed"))["session"]
        assert s3["aligned"] is False and s3["map_T_session"] == map_geo.IDENTITY

    async def test_migrated_local_map_extends_unaligned(self, fdb):
        fdb.add_map("old", type="local")
        fdb.add_session("old", node_count=12)  # the legacy session
        fdb.add_robot("r1")
        assert (await self._start(fdb, "old"))["session"]["aligned"] is False

    async def test_pre_m1_row_gets_its_type_written(self, fdb):
        fdb.add_map("old", status={})  # no type, no state
        fdb.add_robot("r1")
        await self._start(fdb, "old")
        assert fdb.maps["old"]["spec"]["type"] == "local"

    @pytest.mark.parametrize("setup,code", [
        ("missing_map", 404), ("deleting", 409), ("archived", 409), ("missing_robot", 404),
        ("offline", 409), ("robot_busy", 409), ("map_busy", 409)])
    async def test_refusals(self, fdb, setup, code):
        fdb.add_map("yard", type="local", status={"state": "ready"})
        fdb.add_map("other", type="local", status={"state": "mapping"})
        fdb.add_robot("r1")
        fdb.add_robot("r2")
        target, robot = "yard", "r1"
        if setup == "missing_map":
            target = "nope"
        elif setup == "deleting":
            fdb.maps["yard"]["lifecycle"] = "DELETING"
        elif setup == "archived":
            fdb.maps["yard"]["status"]["state"] = "archived"
        elif setup == "missing_robot":
            robot = "ghost"
        elif setup == "offline":
            fdb.add_robot("r1", online=False)
        elif setup == "robot_busy":
            fdb.add_session("other", "r1", "live", ended=False)
        elif setup == "map_busy":
            fdb.add_session("yard", "r2", "live", ended=False)
        before = copy.deepcopy((fdb.maps, fdb.sessions))
        got, _ = await _status(self._start(fdb, target, robot))
        assert got == code
        assert (fdb.maps, fdb.sessions) == before and fdb.events == []

    async def test_bad_body(self, fdb):
        fdb.add_map("yard", type="local")
        assert (await _status(maps.start_session(None, "yard", {}, PUB)))[0] == 422
        assert (await _status(maps.start_session(None, "yard", {"robot": "r1", "x": 1},
                                                 PUB)))[0] == 422

    async def test_pause_resume_finish(self, fdb):
        fdb.add_map("yard", type="local", status={"state": "draft"})
        fdb.add_robot("r1")
        sid = (await self._start(fdb))["session"]["session_id"]

        out = await maps.session_action(None, "yard", sid, "pause", PUB)
        assert out["changed"] and out["map_state"] == "paused"
        assert out["session"]["state"] == "paused" and out["session"]["paused_at"] is not None
        assert fdb.maps["yard"]["status"]["state"] == "paused"
        again = await maps.session_action(None, "yard", sid, "pause", PUB)
        assert again["changed"] is False

        out = await maps.session_action(None, "yard", sid, "resume", PUB)
        assert out["map_state"] == "mapping" and out["session"]["paused_at"] is None
        assert (await maps.session_action(None, "yard", sid, "resume", PUB))["changed"] is False

        await maps.session_action(None, "yard", sid, "pause", PUB)
        out = await maps.session_action(None, "yard", sid, "finish", PUB)
        assert out["map_state"] == "ready" and out["session"]["state"] == "finished"
        assert out["session"]["paused_at"] is None
        assert fdb.maps["yard"]["status"] == {"state": "ready", "open_session_id": None}
        assert (await maps.session_action(None, "yard", sid, "finish", PUB))["changed"] is False
        for action in ("pause", "resume"):
            assert (await _status(maps.session_action(None, "yard", sid, action, PUB)))[0] == 409

        assert fdb.codes() == ["MAP.SESSION_STARTED", "MAP.SESSION_PAUSED",
                               "MAP.SESSION_RESUMED", "MAP.SESSION_PAUSED",
                               "MAP.SESSION_FINISHED"]
        assert len({e["event_id"] for e in fdb.events}) == 5

    async def test_session_lookup(self, fdb):
        fdb.add_map("yard", type="local")
        fdb.add_map("other", type="local")
        s = fdb.add_session("other", "r1", "live", ended=False)
        for sid, action in ((str(s["session_id"]), "pause"), ("not-a-uuid", "pause"),
                            (str(uuid.uuid4()), "finish"), (str(s["session_id"]), "explode")):
            assert (await _status(maps.session_action(None, "yard", sid, action, PUB)))[0] == 404


class TestArchive:
    async def test_archive_restore(self, fdb):
        fdb.add_map("yard", type="local")
        fdb.add_session("yard")
        out = await maps.archive_map(None, "yard", PUB)
        assert out == {"map_id": "yard", "state": "archived", "changed": True}
        assert (await maps.archive_map(None, "yard", PUB))["changed"] is False
        out = await maps.restore_map(None, "yard", PUB)
        assert out["state"] == "ready"
        assert (await maps.restore_map(None, "yard", PUB))["changed"] is False
        assert fdb.codes() == ["MAP.ARCHIVED", "MAP.RESTORED"]

    async def test_restore_without_sessions_is_draft(self, fdb):
        fdb.add_map("new", type="geo", status={"state": "draft"})
        await maps.archive_map(None, "new", PUB)
        assert (await maps.restore_map(None, "new", PUB))["state"] == "draft"

    async def test_archive_refused_while_open(self, fdb):
        fdb.add_map("yard", type="local", status={"state": "paused"})
        fdb.add_session("yard", "r1", "live", ended=False)
        code, _ = await _status(maps.archive_map(None, "yard", PUB))
        assert code == 409 and fdb.maps["yard"]["status"]["state"] == "paused"


# --- delete guard, SqlStore details ----------------------------------------------------------------

class _Cursor:
    def __init__(self, rows=None, raise_on=None):
        self.rows = rows or []
        self.raise_on = raise_on
        self.sql = []
        self.rowcount = 0

    async def execute(self, sql, params=()):
        self.sql.append((sql, params))
        if self.raise_on and self.raise_on in sql:
            raise psycopg.errors.UniqueViolation("duplicate key")

    async def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    async def fetchall(self):
        rows, self.rows = self.rows, []
        return rows


class TestGuardsAndStore:
    async def test_delete_guard(self):
        cur = _Cursor(rows=[("r1", "mapping"), ("r2", "operate")])
        with pytest.raises(HTTPException) as exc:
            await maps.refuse_open_session(cur, "yard")
        assert exc.value.status_code == 409
        # Maps §14 (Q-U2): the message names the robots using the map.
        assert "r1 (mapping)" in exc.value.detail and "r2 (using)" in exc.value.detail
        assert cur.sql[0][0] == maps.LOCK_MAP_SQL  # locks the map row first
        await maps.refuse_open_session(_Cursor(), "yard")  # no open session: passes

    async def test_unique_violation_is_409(self):
        store = maps.SqlStore(None, _Cursor(raise_on="INSERT INTO map_sessions"), PUB)
        with pytest.raises(HTTPException) as exc:
            await store.insert_session({"session_id": str(uuid.uuid4()), "map_name": "m",
                                        "robot_name": "r1", "started_at": T0, "datum": None,
                                        "map_t_session": map_geo.IDENTITY, "aligned": True})
        assert exc.value.status_code == 409

    async def test_session_ids_are_sent_as_uuids(self):
        cur = _Cursor()
        store = maps.SqlStore(None, cur, PUB)
        sid = str(uuid.uuid4())
        await store.lock_session(sid)
        await store.update_session(sid, paused_at=None)
        assert all(isinstance(p[-1], uuid.UUID) for _s, p in cur.sql)

    async def test_failed_event_never_fails_the_change(self):
        class Conn:
            def transaction(self):
                return contextlib.nullcontext()

        class Tx:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        conn = MagicMock()
        conn.transaction = Mock(return_value=Tx())
        store = maps.SqlStore(conn, _Cursor(), PUB)
        failed = maps.stats["failed"]
        with patch.object(maps, "emit", AsyncMock(side_effect=RuntimeError("no table"))):
            await store.emit(maps._map_event(EventCode.MAP_CREATED, "m", "local", "draft",
                                             None, T0))
        assert maps.stats["failed"] == failed + 1

    def test_session_dict(self):
        sid = uuid.uuid4()
        d = maps.session_dict({"session_id": sid, "map_name": "m", "robot_name": "r",
                               "started_at": T0, "paused_at": T0, "ended_at": None,
                               "map_t_session": {"tx": 1}, "aligned": False})
        assert d["session_id"] == str(sid) and d["state"] == "paused"
        assert d["kind"] == "live" and d["node_count"] == 0
        assert d["started_at"] == T0.isoformat() and d["map_T_session"] == {"tx": 1}


# --- routes ------------------------------------------------------------------------------------

class TestRoutes:
    async def test_get_map_adds_sessions(self, fdb):
        fdb.add_map("yard", type="local")
        fdb.add_session("yard")
        open_s = fdb.add_session("yard", "r1", "live", ended=False, aligned=False)
        svc = MagicMock()
        svc.get_map = AsyncMock(return_value={"success": True, "map_id": "yard"})
        with patch.object(main, "service", svc):
            body = await main.get_map("yard")
        s = body["sessions"]
        assert s["count"] == 2 and s["unaligned"] == 1
        assert s["open"]["session_id"] == str(open_s["session_id"])
        assert s["items"][0]["session_id"] == str(open_s["session_id"])  # newest first

    async def test_service_get_map_has_type_and_state(self):
        from packages.api.server import ApiDelegationService
        svc = MagicMock()
        svc.database.get_object = AsyncMock(return_value=_obj(
            "old", datum_latitude=47.0, datum_longitude=19.0, status={"node_count": 1}))
        svc.graph_db.get_map_stats.return_value = {"node_count": 3, "edge_count": 2}
        out = await ApiDelegationService.get_map(svc, "old")
        assert out["type"] == "geo" and out["state"] == "ready" and out["geo"] is None
        assert out["open_session_id"] is None and out["node_count"] == 3

    async def test_graph(self):
        from packages.api.server import ApiDelegationService
        svc = MagicMock()
        svc.database.get_object = AsyncMock(return_value=_obj(
            "yard", type="geo", geo={"utm_zone": 34, "utm_north": True, "origin_e": 1.0,
                                     "origin_n": 2.0},
            datum_latitude=47.0, datum_longitude=19.0, datum_frame="utm",
            status={"state": "ready"}))
        svc.read_graph = Mock(return_value=([{"id": "n1"}], []))
        out = await ApiDelegationService.get_map_graph(svc, "yard")
        assert out["nodes"] == [{"id": "n1"}] and out["node_count"] == 1
        assert out["type"] == "geo" and out["geo"]["utm_zone"] == 34
        assert out["transform"]["frame"] == "utm"

        svc.database.get_object = AsyncMock(return_value=_obj("dying", "DELETING"))
        with pytest.raises(HTTPException) as exc:
            await ApiDelegationService.get_map_graph(svc, "dying")
        assert exc.value.status_code == 409

    def test_read_graph_shape(self):
        from packages.api.server import ApiDelegationService
        svc = MagicMock()
        svc.graph_db.get_all_nodes.return_value = [
            {"_key": "k1", "node_id": 7, "pose": {"x": 1.0, "y": 2.0, "yaw": 0.5},
             "created_at": "t"}]
        svc.graph_db.get_edges.return_value = [{"from": 7, "to": 8, "weight": 1.5}]
        nodes, edges = ApiDelegationService.read_graph(svc, "yard")
        assert nodes == [{"id": "7", "x": 1.0, "y": 2.0, "theta": 0.5, "timestamp": "t",
                          "metadata": {}, "session_id": None}]
        assert edges == [{"from": "7", "to": "8", "weight": 1.5, "metadata": {}}]

    async def test_new_routes_are_registered(self):
        routes = {(m, r.path) for r in main.app.routes for m in getattr(r, "methods", ()) or ()}
        for route in (("POST", "/api/v1/maps"), ("PATCH", "/api/v1/maps/{map_id}"),
                      ("GET", "/api/v1/maps/{map_id}/graph"),
                      ("POST", "/api/v1/maps/{map_id}/sessions"),
                      ("POST", "/api/v1/maps/{map_id}/sessions/{session_id}/{action}"),
                      ("POST", "/api/v1/maps/{map_id}/archive"),
                      ("POST", "/api/v1/maps/{map_id}/restore")):
            assert route in routes, route

    async def test_create_route_checks_arango(self, fdb):
        svc = MagicMock()
        svc.graph_db.get_map_stats.side_effect = lambda n: (
            {"node_count": 441} if n == "taken" else {"error": "Map not found"})
        with patch.object(main, "service", svc):
            with pytest.raises(HTTPException) as exc:
                await main.create_map({"name": "taken", "type": "local"})
            assert exc.value.status_code == 409
            out = await main.create_map({"name": "free", "type": "local"})
        assert out["status"]["state"] == "draft"

    async def test_load_map_types_new_maps_and_keeps_stored_type(self):
        from packages.api.server import ApiDelegationService
        svc = MagicMock()
        svc.graph_db.create_map.return_value = True
        svc.graph_db.get_map_stats.return_value = {"node_count": 0, "edge_count": 0}
        svc.read_graph = Mock(return_value=([], []))
        svc.database.create_object = AsyncMock()
        svc.database.get_object = AsyncMock(side_effect=Exception("x"))
        await ApiDelegationService.load_map(svc, map_id="n", datum_latitude=47.0,
                                            datum_longitude=19.0)
        created = svc.database.create_object.call_args[0][0]
        assert created.type == "geo" and created.geo.utm_zone == 34
        assert created.status.state == "ready"

        # Existing local map + a datum in the request: datum updated, still local.
        svc.database.create_object = AsyncMock(side_effect=Exception("duplicate"))
        svc.database.get_object = AsyncMock(return_value=_obj("n", type="local"))
        svc.database.update_spec = AsyncMock()
        await ApiDelegationService.load_map(svc, map_id="n", datum_latitude=47.0,
                                            datum_longitude=19.0)
        spec = svc.database.update_spec.call_args[0][2]
        assert spec.type == "local" and spec.geo is None and spec.datum_latitude == 47.0
