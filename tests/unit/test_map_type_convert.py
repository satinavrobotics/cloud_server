"""Map type conversion geo <-> local (docs/satinav-maps-redesign.md §17).

- packages/utils/map_geo.py: the rotated geo frame (geo.bearing_deg): geo_from_anchor,
  latlon_to_map / map_to_latlon, session_transform with a rotation, the round trip
  geo -> local -> geo with the former datum giving identical lng/lat for every node;
- the consumers of the rotation: the client's `transform` (datum_* through geo.local_to_gps),
  the planner's GPS goal, the reconstruction manifest's crs;
- packages/api/maps.py convert_map_type: the body, the guards (open mapping session, same type,
  deleting), what is stored, operate sessions kept (datum stamped), MAP.TYPE_CHANGED, the
  warnings; the datum placement suggestion and POST .../place {"source": "datum"} on geo maps.
"""
import math
import os
import random

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

import packages.api.main as main  # noqa: E402
from cloud_common.objects.map import (  # noqa: E402
    MapObjectV1, effective_type, has_real_datum)
from cloud_common.objects.robot import RobotObjectV1, RobotStatusV1  # noqa: E402
from packages.api import maps  # noqa: E402
from packages.api import reconstruction as rc  # noqa: E402
from packages.api.server import map_datum_transform  # noqa: E402
from packages.events.codes import EventCode  # noqa: E402
from packages.utils import geo, map_geo  # noqa: E402
from packages.utils import map_sessions as ms  # noqa: E402
from tests.unit import test_maps_m1 as m1  # noqa: E402
from tests.unit.test_maps_m2 import ShimDb  # noqa: E402

pytestmark = pytest.mark.unit

PUB = m1.PUB
UTM_DATUM = m1.UTM_DATUM            # sati_vda5050_client, zone 34N (Budapest)
GEO = map_geo.geo_from_datum(UTM_DATUM)
RNG = random.Random(17)
NODES = [(RNG.uniform(-300, 300), RNG.uniform(-300, 300)) for _ in range(200)]


@pytest.fixture
def db():
    d = ShimDb()
    with patch.object(maps, "open_store", d.store), patch.object(maps, "_utcnow", m1.Clock()):
        yield d


async def _status(coro):
    try:
        await coro
    except HTTPException as exc:
        return exc.status_code, exc.detail
    raise AssertionError("no HTTPException")


def _robot(db, name="r1", online=True, pose=(0.0, 0.0, 0.0), **datum):
    db.robots[name] = RobotObjectV1(
        name=name, datum=datum or {},
        status=RobotStatusV1(online=online, state="IDLE",
                             pose={"x": pose[0], "y": pose[1], "theta": pose[2]}))


def _geo_map(db, name="yard", geo_block=None, state="ready"):
    block = dict(geo_block or GEO)
    db.add_map(name, type="geo", geo=block, **maps.origin_as_legacy_datum(block),
               status={"state": state})


def _spec(db, name):
    return db.maps[name]["spec"]


def _obj(db, name):
    row = db.maps[name]
    return MapObjectV1(name=name, status=row["status"], **row["spec"])


async def _convert(db, name="yard", **body):
    return await maps.convert_map_type(None, name, body, PUB, actor="op")


def _to_geo_body(former, anchor=(0.0, 0.0)):
    return {"type": "geo", "latitude": former["latitude"], "longitude": former["longitude"],
            "bearing_deg": former["bearing_deg"], "utm_zone": former["utm_zone"],
            "utm_north": former["utm_north"], "anchor": {"x": anchor[0], "y": anchor[1]}}


# --- the rotated geo frame ---------------------------------------------------------------------

class TestRotatedFrame:
    def test_anchor_lands_where_asked(self):
        for bearing in (0.0, 30.0, -135.0, 179.0):
            g = map_geo.geo_from_anchor(47.48, 19.03, 12.5, -40.0, bearing)
            lat, lon = map_geo.map_to_latlon(g, 12.5, -40.0)
            assert (lat, lon) == pytest.approx((47.48, 19.03), abs=1e-10)
            assert g["bearing_deg"] == pytest.approx(bearing)
            assert g["utm_zone"] == 34 and g["utm_north"] is True

    def test_bearing_rotates_the_axes(self):
        g = map_geo.geo_from_anchor(47.48, 19.03, 0.0, 0.0, 90.0)
        # +X points grid north: a point 10 m along +X is 10 m grid north of the origin
        e0, n0 = geo.latlon_to_utm(*map_geo.map_to_latlon(g, 0.0, 0.0), 34, True)
        e1, n1 = geo.latlon_to_utm(*map_geo.map_to_latlon(g, 10.0, 0.0), 34, True)
        assert (e1 - e0, n1 - n0) == pytest.approx((0.0, 10.0), abs=1e-6)

    def test_latlon_and_map_are_inverse(self):
        g = map_geo.geo_from_anchor(47.48, 19.03, 5.0, 5.0, 37.0)
        for x, y in NODES:
            assert map_geo.latlon_to_map(g, *map_geo.map_to_latlon(g, x, y)) == pytest.approx(
                (x, y), abs=1e-6)

    def test_enu_bearing_adds_the_convergence(self):
        utm = map_geo.geo_from_anchor(47.48, 19.03, bearing_deg=10.0, frame="utm")
        enu = map_geo.geo_from_anchor(47.48, 19.03, bearing_deg=10.0, frame="enu")
        conv = math.degrees(map_geo.grid_convergence_rad(47.48, 19.03, 34, True))
        assert conv == pytest.approx(-1.45, abs=0.05)   # Budapest, zone 34 (doc §5)
        assert enu["bearing_deg"] == pytest.approx(utm["bearing_deg"] + conv)
        # +X of an 'enu' bearing 0 points true east: a probe 10 m along +X stays on the parallel
        g0 = map_geo.geo_from_anchor(47.48, 19.03, frame="enu")
        lat, _ = map_geo.map_to_latlon(g0, 10.0, 0.0)
        assert lat == pytest.approx(47.48, abs=1e-8)

    @pytest.mark.parametrize("args,match", [
        ((85.0, 19.0), "outside UTM"), ((-81.0, 19.0), "outside UTM"),
        ((47.0, 181.0), "outside -180"), ((47.0, 19.0, 0.0, 0.0, 0.0, None, 40), "too far"),
        ((47.0, 19.0, 0.0, 0.0, float("nan")), "finite"), ((47.0, 19.0, 0.0, 0.0, 0.0, None, 0),
                                                           "outside 1 .. 60")])
    def test_invalid_anchor(self, args, match):
        with pytest.raises(ValueError, match=match):
            map_geo.geo_from_anchor(*args)

    def test_neighbour_zone_accepted(self):
        g = map_geo.geo_from_anchor(47.48, 19.03, utm_zone=33)
        assert g["utm_zone"] == 33
        assert map_geo.map_to_latlon(g, 0.0, 0.0) == pytest.approx((47.48, 19.03), abs=1e-10)

    def test_zero_bearing_is_bit_for_bit_the_old_session_transform(self):
        datum = dict(UTM_DATUM, utm_easting=UTM_DATUM["utm_easting"] + 7.0)
        old = map_geo._session_grid_transform(GEO, datum)
        assert map_geo.session_transform(dict(GEO, bearing_deg=0.0), datum) == old
        assert map_geo.session_transform(GEO, datum) == old

    @pytest.mark.parametrize("datum", [UTM_DATUM, m1.ENU_DATUM,
                                       dict(UTM_DATUM, bearing_deg=12.0)])
    def test_session_transform_with_a_rotated_map_is_the_truth(self, datum):
        """A robot point placed through map_T_session lands on the same lng/lat as the robot's
        own datum puts it (graph-builder, dispatcher and planner all use this transform)."""
        g = map_geo.geo_from_anchor(47.4795, 19.0325, 20.0, -15.0, 33.0)
        t = map_geo.session_transform(g, map_geo.robot_datum(datum))
        kw = {k: datum.get(k) for k in ("frame", "utm_zone", "utm_north", "utm_easting",
                                        "utm_northing")}
        # exact for a UTM datum; an ENU datum's session transform is rigid (no UTM scale
        # factor: about 1.3 cm per 100 m from the datum, doc §13.2), rotated map or not
        tol = 2e-9 if datum["frame"] == "utm" else 1e-6
        for x, y in NODES[:50]:
            mx, my = map_geo.apply_transform(t, x, y)
            truth = geo.local_to_gps(x, y, datum["latitude"], datum["longitude"],
                                     datum.get("bearing_deg") or 0.0, **kw)
            assert map_geo.map_to_latlon(g, mx, my) == pytest.approx(truth, abs=tol)
            # the same error as on the unrotated map: the rotation adds none
            t0 = map_geo.session_transform(GEO, map_geo.robot_datum(datum))
            ref = map_geo.map_to_latlon(GEO, *map_geo.apply_transform(t0, x, y))
            got = map_geo.map_to_latlon(g, mx, my)
            assert got == pytest.approx(ref, abs=2e-9)


class TestRoundTrip:
    @pytest.mark.parametrize("geo_block", [
        GEO,                                                     # a session-made geo map
        map_geo.geo_from_anchor(47.4795, 19.0325, 33.0, -8.0, 41.0),   # converted, rotated
        map_geo.geo_from_anchor(-33.9, 151.2, 0.0, 0.0, -100.0),       # southern hemisphere
    ])
    async def test_geo_local_geo_gives_identical_lnglat(self, db, geo_block):
        _geo_map(db, geo_block=geo_block)
        before = [map_geo.map_to_latlon(_spec(db, "yard")["geo"], x, y) for x, y in NODES]
        out = await _convert(db, type="local")
        assert out["type"] == "local" and _spec(db, "yard")["geo"] is None
        former = _spec(db, "yard")["former_datum"]
        await _convert(db, **_to_geo_body(former))
        after = [map_geo.map_to_latlon(_spec(db, "yard")["geo"], x, y) for x, y in NODES]
        for a, b in zip(before, after):
            assert a == pytest.approx(b, abs=1e-9)    # degrees: ~0.1 mm
        assert _spec(db, "yard")["geo"]["utm_zone"] == geo_block["utm_zone"]
        assert _spec(db, "yard")["geo"]["utm_north"] == geo_block["utm_north"]

    async def test_round_trip_through_another_anchor_point(self, db):
        """The client anchors on the content centre: the same frame from a different point."""
        _geo_map(db)
        await _convert(db, type="local")
        former = _spec(db, "yard")["former_datum"]
        centre = (120.0, -45.0)
        lat, lon = map_geo.map_to_latlon(dict(former, bearing_deg=former["bearing_deg"]),
                                         *centre)
        await _convert(db, **dict(_to_geo_body(former, centre), latitude=lat, longitude=lon))
        for x, y in NODES:
            assert map_geo.map_to_latlon(_spec(db, "yard")["geo"], x, y) == pytest.approx(
                map_geo.map_to_latlon(GEO, x, y), abs=1e-9)

    async def test_client_transform_matches_the_geo_frame(self, db):
        """The display transform (datum_* -> MapTransform, geo.local_to_gps as the client's
        utils/mapTransform.ts) and the stored geo frame agree, rotation included."""
        db.add_map("yard", type="local")
        await _convert(db, type="geo", latitude=47.4795, longitude=19.0325, bearing_deg=-62.0,
                       anchor={"x": 10.0, "y": 4.0})
        obj = _obj(db, "yard")
        t = map_datum_transform(obj)
        assert t["rotation_rad"] == pytest.approx(math.radians(-62.0))
        assert t["frame"] == "utm"
        for x, y in NODES[:50]:
            shown = geo.local_to_gps(x, y, t["origin_lat"], t["origin_lon"],
                                     math.degrees(t["rotation_rad"]), frame=t["frame"],
                                     utm_zone=t["utm_zone"], utm_north=t["utm_north"],
                                     utm_easting=t["utm_easting"],
                                     utm_northing=t["utm_northing"])
            assert shown == pytest.approx(map_geo.map_to_latlon(obj.geo.dict(), x, y), abs=1e-10)


# --- the consumers ------------------------------------------------------------------------------

class TestConsumers:
    async def test_planner_gps_goal_in_a_rotated_map(self):
        from packages.services.mission_planner.server import MissionPlannerService
        g = map_geo.geo_from_anchor(47.4795, 19.0325, 0.0, 0.0, 25.0)
        spec = {"type": "geo", "geo": g, **maps.origin_as_legacy_datum(g)}
        svc = MagicMock()
        svc.database.get_object = AsyncMock(return_value=MapObjectV1(name="yard", **spec))
        lat, lon = map_geo.map_to_latlon(g, 30.0, -12.0)
        x, y, how = await MissionPlannerService._gps_to_map(svc, "yard", lat, lon)
        assert (x, y) == pytest.approx((30.0, -12.0), abs=1e-6) and "utm" in how

    def test_reconstruction_crs(self):
        g = map_geo.geo_from_anchor(47.4795, 19.0325, 0.0, 0.0, 25.0)
        assert rc.map_crs({"type": "geo", "geo": g})["bearing_deg"] == pytest.approx(25.0)
        # unchanged contract for every unrotated map
        assert "bearing_deg" not in rc.map_crs({"type": "geo", "geo": GEO})
        assert "bearing_deg" not in rc.map_crs({"type": "geo", "geo": dict(GEO, bearing_deg=0)})

    def test_geo_transform_for_follows_the_rotation(self):
        g = map_geo.geo_from_anchor(47.4795, 19.0325, 0.0, 0.0, 25.0)
        t = ms.geo_transform_for(g, "geo", map_geo.robot_datum(UTM_DATUM))
        assert t == map_geo.session_transform(g, map_geo.robot_datum(UTM_DATUM))
        assert ms.geo_transform_for(g, "local", map_geo.robot_datum(UTM_DATUM)) is None


# --- POST /maps/{id}/type ---------------------------------------------------------------------------

class TestConvert:
    async def test_to_local_drops_the_georeference(self, db):
        _geo_map(db)
        out = await _convert(db, type="local")
        spec = _spec(db, "yard")
        assert out["old_type"] == "geo" and out["changed"] is True
        assert spec["type"] == "local" and spec["geo"] is None
        assert spec["datum_latitude"] is None and spec["datum_frame"] == "enu"
        former = spec["former_datum"]
        assert former["utm_zone"] == 34 and former["origin_e"] == GEO["origin_e"]
        assert (former["latitude"], former["longitude"]) == pytest.approx(
            map_geo.map_to_latlon(GEO, 0.0, 0.0))
        assert spec["approx_location"]["latitude"] == pytest.approx(former["latitude"])
        obj = _obj(db, "yard")
        assert effective_type(obj) == "local"
        assert not has_real_datum(obj.datum_latitude, obj.datum_longitude)
        assert map_datum_transform(obj) is None
        assert out["map"]["type"] == "local" and out["map"]["former_datum"]["utm_zone"] == 34
        assert maps.filter_maps([obj], type_="local")[0]["name"] == "yard"
        assert db.codes()[-1] == EventCode.MAP_TYPE_CHANGED.value
        ev = db.events[-1]
        assert ev["payload"]["old_type"] == "geo" and ev["payload"]["new_type"] == "local"
        assert ev["payload"]["old_geo"]["origin_e"] == GEO["origin_e"]
        assert ev["payload"]["actor"] == "op"

    async def test_to_geo_sets_geo_and_the_display_datum(self, db):
        db.add_map("shed", type="local", approx_location={
            "latitude": 47.0, "longitude": 19.0, "source": "manual",
            "set_at": "2026-10-01T00:00:00+00:00"})
        out = await _convert(db, "shed", type="geo", latitude=47.4795, longitude=19.0325,
                             bearing_deg=15.0, anchor={"x": 3.0, "y": 4.0})
        spec = _spec(db, "shed")
        assert spec["type"] == "geo" and spec["geo"]["bearing_deg"] == pytest.approx(15.0)
        assert spec["datum_bearing_deg"] == pytest.approx(15.0)
        assert spec["datum_frame"] == "utm" and spec["datum_utm_easting"] == spec["geo"]["origin_e"]
        assert spec["approx_location"] is None and spec["former_datum"] is None
        assert effective_type(_obj(db, "shed")) == "geo"
        assert map_geo.map_to_latlon(spec["geo"], 3.0, 4.0) == pytest.approx((47.4795, 19.0325))
        assert out["map"]["geo"]["bearing_deg"] == pytest.approx(15.0)
        assert out["warnings"][-1].startswith("Robots without a GNSS datum")

    @pytest.mark.parametrize("body,status", [
        ({"type": "plane"}, 422),
        ({"type": "local", "latitude": 47.0}, 422),
        ({"type": "geo"}, 422),
        ({"type": "geo", "latitude": 47.0}, 422),
        ({"type": "geo", "latitude": 0.0, "longitude": 0.0}, 422),
        ({"type": "geo", "latitude": 91.0, "longitude": 19.0}, 422),
        ({"type": "geo", "latitude": 85.0, "longitude": 19.0}, 422),     # outside UTM
        ({"type": "geo", "latitude": 47.0, "longitude": 19.0, "utm_zone": 40}, 422),
        ({"type": "geo", "latitude": 47.0, "longitude": 19.0, "utm_zone": 61}, 422),
        ({"type": "geo", "latitude": 47.0, "longitude": 19.0, "frame": "wgs"}, 422),
        ({"type": "geo", "latitude": 47.0, "longitude": 19.0, "anchor": {"x": "a"}}, 422),
        ({"type": "geo", "latitude": 47.0, "longitude": 19.0, "extra": 1}, 422),
    ])
    async def test_bad_body(self, db, body, status):
        db.add_map("shed", type="local")
        assert (await _status(maps.convert_map_type(None, "shed", body, PUB)))[0] == status
        assert _spec(db, "shed")["type"] == "local" and db.events == []

    async def test_guards(self, db):
        assert (await _status(_convert(db, "nope", type="local")))[0] == 404
        _geo_map(db)
        code, detail = await _status(_convert(db, type="geo", latitude=47.0, longitude=19.0))
        assert code == 409 and "already a geo map" in detail
        db.add_map("dying", "DELETING", type="geo", geo=GEO)
        assert (await _status(_convert(db, "dying", type="local")))[0] == 409

    @pytest.mark.parametrize("paused", [False, True])
    async def test_refused_while_a_mapping_session_is_open(self, db, paused):
        _geo_map(db, state="paused" if paused else "mapping")
        db.add_session("yard", "r1", "live", ended=False, purpose="mapping",
                       paused_at=m1.T0 if paused else None)
        code, detail = await _status(_convert(db, type="local"))
        assert code == 409 and "r1 (mapping)" in detail and "finish it" in detail
        assert _spec(db, "yard")["type"] == "geo" and db.events == []

    async def test_operate_sessions_keep_their_placement_to_local(self, db):
        _geo_map(db)
        t = map_geo.session_transform(GEO, map_geo.robot_datum(UTM_DATUM))
        _robot(db, "r1", **UTM_DATUM)
        _robot(db, "r2")
        placed = db.add_session("yard", "r1", "live", ended=False, purpose="operate",
                                map_t_session=dict(t), datum=map_geo.robot_datum(UTM_DATUM))
        db.add_session("yard", "r2", "live", ended=False, purpose="operate", aligned=False)
        out = await _convert(db, type="local")
        assert {o["robot"] for o in out["operating"]} == {"r1", "r2"}
        s = next(x for x in db.sessions if x["session_id"] == placed["session_id"])
        assert s["aligned"] is True and s["map_t_session"] == t and s["ended_at"] is None
        assert any(w.startswith("r1 keeps its placement") for w in out["warnings"])
        assert any(w.startswith("r2 is not placed: place it by hand") for w in out["warnings"])
        # nothing re-derives a local session from a datum any more (the dispatcher's rule)
        session = {"map_geo": _spec(db, "yard")["geo"], "map_type": "local", "aligned": True,
                   "datum": s["datum"]}
        assert ms.plan_geo_replace(session, dict(map_geo.robot_datum(UTM_DATUM),
                                                 utm_easting=1.0), True) is None

    async def test_operate_sessions_keep_their_placement_to_geo(self, db):
        db.add_map("shed", type="local")
        _robot(db, "r1", **UTM_DATUM)       # GNSS robot, placed by hand
        _robot(db, "r2")                    # no datum, placed by hand
        _robot(db, "r3", **UTM_DATUM)       # GNSS robot, not placed (restarted)
        t = {"tx": 4.0, "ty": -2.0, "yaw": 0.3}
        s1 = db.add_session("shed", "r1", "live", ended=False, purpose="operate",
                            map_t_session=dict(t))
        db.add_session("shed", "r2", "live", ended=False, purpose="operate",
                       map_t_session=dict(t))
        db.add_session("shed", "r3", "live", ended=False, purpose="operate", aligned=False)
        out = await _convert(db, "shed", type="geo", latitude=47.4795, longitude=19.0325,
                             bearing_deg=20.0)
        s = next(x for x in db.sessions if x["session_id"] == s1["session_id"])
        assert s["aligned"] is True and s["map_t_session"] == t
        # the robot's datum is stamped: the dispatcher keeps the hand placement for this datum
        assert s["datum"] == map_geo.robot_datum(UTM_DATUM)
        view = {"map_geo": _spec(db, "shed")["geo"], "map_type": "geo", "aligned": True,
                "datum": s["datum"]}
        assert ms.plan_geo_replace(view, map_geo.robot_datum(UTM_DATUM), True) is None
        # ... and re-derives from the next, different datum, as on any geo map
        moved = dict(map_geo.robot_datum(UTM_DATUM), utm_easting=UTM_DATUM["utm_easting"] + 5)
        assert ms.plan_geo_replace(view, moved, False)[1] == "datum"
        r2 = next(x for x in db.sessions if x["robot_name"] == "r2")
        assert r2["datum"] is None and r2["aligned"] is True
        w = out["warnings"]
        assert any(x.startswith("r1 keeps its placement") and "GNSS datum" in x for x in w)
        assert any(x.startswith("r2 keeps its placement") and "no GNSS datum" in x for x in w)
        assert any(x.startswith("r3 is not placed: place it from its GNSS datum") for x in w)
        assert db.events[-1]["payload"]["operating"] == ["r1", "r2", "r3"]

    async def test_archived_and_draft_maps_convert(self, db):
        db.add_map("old", type="local", status={"state": "archived"})
        assert (await _convert(db, "old", type="geo", latitude=47.0, longitude=19.0))["type"] == "geo"
        db.add_map("new", type="geo", status={"state": "draft"})     # no origin yet
        out = await _convert(db, "new", type="local")
        assert out["type"] == "local" and _spec(db, "new")["former_datum"] is None

    async def test_geo_without_origin_keeps_its_legacy_datum_as_former(self, db):
        db.add_map("g", type="geo", status={"state": "draft"}, datum_latitude=47.48,
                   datum_longitude=19.03, datum_frame="utm", datum_bearing_deg=5.0)
        await _convert(db, "g", type="local")
        former = _spec(db, "g")["former_datum"]
        assert (former["latitude"], former["longitude"]) == pytest.approx((47.48, 19.03))
        assert former["bearing_deg"] == pytest.approx(5.0)

    async def test_route(self, db):
        db.add_map("shed", type="local")
        svc = MagicMock()
        with patch.object(main, "service", svc):
            out = await main.convert_map_type("shed", {"type": "geo", "latitude": 47.0,
                                                       "longitude": 19.0})
        assert out["type"] == "geo"
        routes = {(m, r.path) for r in main.app.routes for m in getattr(r, "methods", ()) or ()}
        assert ("POST", "/api/v1/maps/{map_id}/type") in routes


# --- placing a geo session from the robot's datum ------------------------------------------------------

class TestDatumPlacement:
    ROTATED = map_geo.geo_from_anchor(47.4795, 19.0325, 0.0, 0.0, 20.0)

    def _unplaced(self, db, unplaced_at="2026-10-01T10:00:00+00:00"):
        _geo_map(db, geo_block=self.ROTATED)
        return db.add_session("yard", "r1", "live", ended=False, purpose="operate",
                              aligned=False, datum=None,
                              placement={"unplaced_reason": "run_changed",
                                         "unplaced_at": unplaced_at})

    async def test_suggestion_and_place(self, db):
        s = self._unplaced(db)
        _robot(db, "r1", pose=(2.0, 1.0, 0.4), **UTM_DATUM)
        out = await maps.placement_suggestions(None, "yard", str(s["session_id"]))
        (sug,) = out["suggestions"]
        t = map_geo.session_transform(self.ROTATED, map_geo.robot_datum(UTM_DATUM))
        assert sug["source"] == "datum" and sug["basis"] == "robot_datum"
        assert sug["map_T_session"] == pytest.approx(t)
        assert (sug["pose"]["x"], sug["pose"]["y"]) == pytest.approx(
            map_geo.apply_transform(t, 2.0, 1.0))
        assert sug["robot_pose"] == {"x": 2.0, "y": 1.0, "theta": 0.4}
        assert sug["datum_after_unplace"] is None        # datum_changed_at unknown
        assert out["reloc"] is None

        res = await maps.place_session(None, "yard", str(s["session_id"]), {"source": "datum"},
                                       PUB, actor="op")
        stored = next(x for x in db.sessions if x["session_id"] == s["session_id"])
        assert stored["aligned"] is True and stored["map_t_session"] == pytest.approx(t)
        assert stored["datum"] == map_geo.robot_datum(UTM_DATUM)
        assert stored["placement"]["source"] == "datum"
        assert res["session"]["aligned"] is True
        assert db.codes()[-1] == EventCode.MAP_SESSION_PLACED.value

    async def test_datum_freshness(self, db):
        s = self._unplaced(db)
        _robot(db, "r1", **UTM_DATUM)
        db.robots["r1"].datum_changed_at = "2026-10-01T10:00:05+00:00"
        (sug,) = (await maps.placement_suggestions(None, "yard", str(s["session_id"])))["suggestions"]
        assert sug["datum_after_unplace"] is True and sug["at"].startswith("2026-10-01T10:00:05")
        db.robots["r1"].datum_changed_at = "2026-10-01T09:00:00+00:00"
        (sug,) = (await maps.placement_suggestions(None, "yard", str(s["session_id"])))["suggestions"]
        assert sug["datum_after_unplace"] is False

    async def test_no_suggestion_without_a_usable_datum(self, db):
        s = self._unplaced(db)
        _robot(db, "r1")
        assert (await maps.placement_suggestions(None, "yard", str(s["session_id"])))[
            "suggestions"] == []
        code, detail = await _status(maps.place_session(
            None, "yard", str(s["session_id"]), {"source": "datum"}, PUB))
        assert code == 409 and "no GNSS datum" in detail
        _robot(db, "r1", **dict(UTM_DATUM, utm_zone=33, longitude=14.9))   # another zone
        assert (await _status(maps.place_session(
            None, "yard", str(s["session_id"]), {"source": "datum"}, PUB)))[0] == 409

    async def test_refusals(self, db):
        s = self._unplaced(db)
        _robot(db, "r1", **UTM_DATUM)
        # manual placement on a geo map: still refused, and says how instead
        code, detail = await _status(maps.place_session(
            None, "yard", str(s["session_id"]), {"pose": {"x": 0, "y": 0, "yaw": 0},
                                                 "robot_pose": {"x": 0, "y": 0, "theta": 0}},
            PUB))
        assert code == 409 and '"datum"' in detail
        await maps.place_session(None, "yard", str(s["session_id"]), {"source": "datum"}, PUB)
        code, detail = await _status(maps.place_session(
            None, "yard", str(s["session_id"]), {"source": "datum"}, PUB))
        assert code == 409 and "already placed" in detail
        # a local map has no georeference to place by
        db.add_map("shed", type="local")
        s2 = db.add_session("shed", "r2", "live", ended=False, purpose="operate", aligned=False)
        _robot(db, "r2", **UTM_DATUM)
        code, detail = await _status(maps.place_session(
            None, "shed", str(s2["session_id"]), {"source": "datum"}, PUB))
        assert code == 409 and "local map" in detail
        # offline
        db.add_session("yard", "r3", "live", ended=False, purpose="operate", aligned=False)
        _robot(db, "r3", online=False, **UTM_DATUM)
        s3 = next(x for x in db.sessions if x["robot_name"] == "r3")
        assert (await _status(maps.place_session(
            None, "yard", str(s3["session_id"]), {"source": "datum"}, PUB)))[0] == 409

    async def test_datum_source_not_on_start(self, db):
        db.add_map("shed", type="local", status={"state": "ready"})
        _robot(db, "r1", **UTM_DATUM)
        code, _ = await _status(maps.start_session(
            None, "shed", {"robot": "r1", "purpose": "operate",
                           "placement": {"source": "datum"}}, PUB))
        assert code == 422

    async def test_local_map_suggestions_unchanged(self, db):
        db.add_map("shed", type="local")
        s = db.add_session("shed", "r1", "live", ended=False, purpose="operate", aligned=False,
                           placement={"unplaced_at": "2026-10-01T10:00:00+00:00"})
        _robot(db, "r1", **UTM_DATUM)
        from tests.unit.test_maps_m2 import ShimStore
        with patch.object(maps, "_run_start_pose", AsyncMock(return_value=None)), \
                patch.object(ShimStore, "robot_run_start", AsyncMock(return_value=None),
                             create=True), \
                patch.object(ShimStore, "robot_state_pose", AsyncMock(return_value=None),
                             create=True):
            out = await maps.placement_suggestions(None, "shed", str(s["session_id"]))
        assert all(x["source"] != "datum" for x in out["suggestions"])
