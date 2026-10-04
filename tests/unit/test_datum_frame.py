"""The frame of a GPS datum ('utm' | 'enu') from the robot's MQTT datum to the map.

MQTT <prefix>/<robot>/datum -> types.RobotDatum -> robot spec `datum` + auto-seeded map datum
-> map/load `transform`. See packages/utils/geo.py for what the frames mean.
"""
import json
from unittest.mock import MagicMock

import pydantic
import pytest

import cloud_common.objects as api_objects
from cloud_common.objects.map import MapObjectV1, MapSpecV1
from cloud_common.objects.robot import RobotDatumV1
import packages.controllers.mission.vda5050_types as types
from packages.api.server import map_datum_transform
from packages.controllers.mission.server import Robot

pytestmark = pytest.mark.unit

# What sati_vda5050_client publishes for the Budapest test datum (zone 34N).
UTM_WIRE = {"latitude": 47.47946, "longitude": 19.03238, "bearing_deg": 0.0, "frame": "utm",
            "utm_zone": 34, "utm_north": True, "utm_easting": 351756.484938,
            "utm_northing": 5260323.440888}


class TestRobotDatumParsing:
    def test_legacy_payload_is_enu(self):
        d = types.RobotDatum(**{"latitude": 47.5, "longitude": 19.1, "bearing_deg": 3.0})
        assert d.frame == "enu"
        assert d.utm_zone is None and d.utm_easting is None

    def test_orchestrator_payload(self):
        d = types.RobotDatum(**{"latitude": 47.5, "longitude": 19.1, "bearing_deg": 0.0,
                                "frame": "enu"})
        assert d.frame == "enu"

    def test_vda5050_client_payload(self):
        d = types.RobotDatum(**UTM_WIRE)
        assert d.frame == "utm"
        assert (d.utm_zone, d.utm_north) == (34, True)
        assert (d.utm_easting, d.utm_northing) == (351756.484938, 5260323.440888)

    def test_frame_is_case_insensitive_and_null_is_enu(self):
        assert types.RobotDatum(latitude=1.0, longitude=2.0, frame="UTM").frame == "utm"
        assert types.RobotDatum(latitude=1.0, longitude=2.0, frame=None).frame == "enu"

    @pytest.mark.parametrize("bad", [{"frame": "wgs84"}, {"utm_zone": 0}, {"utm_zone": 61}])
    def test_invalid_rejected(self, bad):
        with pytest.raises(pydantic.ValidationError):
            types.RobotDatum(latitude=1.0, longitude=2.0, **bad)


class TestStoredObjectsStillLoad:
    def test_map_spec_without_frame_fields(self):
        stored = {"description": "old", "datum_latitude": 47.0, "datum_longitude": 8.0,
                  "datum_bearing_deg": 5.0}
        m = MapObjectV1(name="m", status={}, **stored)
        assert m.datum_frame == "enu"
        assert m.datum_utm_zone is None
        assert m.spec.datum_frame == "enu"

    def test_robot_datum_without_frame_fields(self):
        stored = {"datum": {"latitude": 47.0, "longitude": 8.0, "bearing_deg": 0.0}}
        r = api_objects.RobotObjectV1(name="r", status={}, **stored)
        assert r.datum.frame == "enu"

    def test_robot_datum_round_trips_through_json(self):
        d = RobotDatumV1(**UTM_WIRE)
        assert RobotDatumV1(**json.loads(d.json())) == d


class _Db:
    def __init__(self, map_obj):
        self.map_obj = map_obj
        self.robot_fields = None
        self.map_specs = []

    async def update_spec_fields(self, cls, name, fields, publisher_id):
        json.dumps(fields)
        self.robot_fields = fields

    async def get_object(self, cls, name):
        assert cls is api_objects.MapObjectV1
        return self.map_obj

    async def update_spec(self, cls, name, spec, publisher_id):
        self.map_specs.append(json.loads(spec.json()))


def _robot(db):
    server = MagicMock()
    server.disable_request_factsheet = True
    server.push_telemetry = False
    server.mission_ctrl_url = None
    r = Robot("r1", db, MagicMock(), "prefix", server)
    r._robot_object = api_objects.RobotObjectV1(name="r1", status={})
    return r


class TestDispatchDatumMessage:
    async def test_utm_datum_stored_on_robot(self):
        db = _Db(MapObjectV1(name="site_a", datum_latitude=1.0, datum_longitude=2.0))
        r = _robot(db)
        await r._process_datum_message(types.RobotDatum(**UTM_WIRE))
        # A changed datum also stamps its change time (map-location plan A freshness).
        assert db.robot_fields.pop("datum_changed_at") == r._robot_object.datum_changed_at.isoformat()
        assert db.robot_fields == {"datum": UTM_WIRE}
        assert r._robot_object.datum.frame == "utm"
        assert db.map_specs == []  # the map already had a datum: not re-seeded

    async def test_no_map_datum_auto_seed(self):
        """Maps M2: a map's datum comes from its first mapping session (doc Q1), never from
        a robot's datum message."""
        db = _Db(MapObjectV1(name="site_a", description="yard"))
        r = _robot(db)
        await r._process_datum_message(types.RobotDatum(**UTM_WIRE))
        assert db.robot_fields.pop("datum_changed_at")  # change-time stamp, see above
        assert db.robot_fields == {"datum": UTM_WIRE}
        assert db.map_specs == []
        legacy = _Db(MapObjectV1(name="site_a"))
        r = _robot(legacy)
        await r._process_datum_message(
            types.RobotDatum(latitude=47.5, longitude=19.1, bearing_deg=4.0))
        assert r._robot_object.datum.frame == "enu" and legacy.map_specs == []


class TestMapLoadTransform:
    def test_none_without_datum(self):
        assert map_datum_transform(MapSpecV1()) is None

    def test_legacy_map_is_enu(self):
        t = map_datum_transform(MapSpecV1(datum_latitude=47.0, datum_longitude=8.0,
                                          datum_bearing_deg=90.0))
        assert t["frame"] == "enu"
        assert t["origin_lat"] == 47.0 and t["origin_lon"] == 8.0
        assert t["rotation_rad"] == pytest.approx(1.5707963267948966)
        assert t["utm_zone"] is None and t["utm_north"] is None

    def test_utm_map(self):
        t = map_datum_transform(MapSpecV1(
            datum_latitude=47.47946, datum_longitude=19.03238, datum_frame="utm",
            datum_utm_zone=34, datum_utm_north=True, datum_utm_easting=351756.484938,
            datum_utm_northing=5260323.440888))
        assert t == {"origin_lat": 47.47946, "origin_lon": 19.03238, "rotation_rad": 0.0,
                     "scale": 1.0, "frame": "utm", "utm_zone": 34, "utm_north": True,
                     "utm_easting": 351756.484938, "utm_northing": 5260323.440888}
