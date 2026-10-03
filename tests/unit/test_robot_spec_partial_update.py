"""mission-dispatch must not write its cached robot spec back over newer changes.

Robots publish <prefix>/<robot>/datum every few seconds, and the force-cancel flag is cleared
by dispatch. Both used to call update_spec() with dispatch's cached *full* spec, silently
reverting e.g. an operator's PUT of telemetry_recording committed in between. They now use
PostgresDatabase.update_spec_fields(), which sets only the given keys (`spec || patch`).
"""
import copy
import json
from unittest.mock import MagicMock

import pytest

import cloud_common.objects as api_objects
import packages.controllers.mission.vda5050_types as types
from packages.controllers.mission.server import Robot
from packages.database.postgres import PostgresDatabase

pytestmark = pytest.mark.unit


class StoreDB:
    """In-memory robot row with the two spec write semantics: update_spec replaces the
    whole spec (what dispatch used to do), update_spec_fields merges keys (jsonb ||)."""

    def __init__(self, robot):
        self.spec = json.loads(robot.spec.json())
        self.writes = []

    async def update_spec(self, cls, name, spec, publisher_id):
        self.writes.append(("full", name))
        self.spec = json.loads(spec.json())

    async def update_spec_fields(self, cls, name, fields, publisher_id):
        self.writes.append(("fields", name, sorted(fields)))
        json.dumps(fields)  # must be JSON-serialisable as passed
        self.spec = {**self.spec, **copy.deepcopy(fields)}

    async def update_status(self, *args, **kwargs):
        pass

    async def get_object(self, *args, **kwargs):
        raise AssertionError("no map lookup expected")


def _robot(db):
    server = MagicMock()
    server.disable_request_factsheet = True
    server.push_telemetry = False
    server.mission_ctrl_url = None
    r = Robot("r1", db, MagicMock(), "prefix", server)
    return r


def _api_put(db, **fields):
    """Another service commits a spec change after dispatch cached the robot."""
    db.spec.update(fields)


async def test_datum_write_keeps_a_concurrent_level_change():
    cached = api_objects.RobotObjectV1(name="r1", status={})
    db = StoreDB(cached)
    r = _robot(db)
    r._robot_object = cached                              # dispatch's cache: level unset
    _api_put(db, telemetry_recording="full", labels=["ops"])

    await r._process_datum_message(types.RobotDatum(latitude=47.5, longitude=19.0,
                                                    bearing_deg=12.0))

    assert db.writes == [("fields", "r1", ["datum", "datum_changed_at"])]
    assert db.spec["telemetry_recording"] == "full"       # survived the datum write
    assert db.spec["labels"] == ["ops"]
    assert db.spec["datum"] == {"latitude": 47.5, "longitude": 19.0, "bearing_deg": 12.0,
                                "frame": "enu", "utm_zone": None, "utm_north": None,
                                "utm_easting": None, "utm_northing": None}
    assert r._robot_object.datum.latitude == 47.5          # cache kept in sync as before
    # the stored spec still loads as a robot
    api_objects.RobotObjectV1(name="r1", status={}, **db.spec)


async def test_force_cancel_clear_keeps_a_concurrent_level_change():
    cached = api_objects.RobotObjectV1(name="r1", status={}, needs_order_cancel=True)
    db = StoreDB(cached)
    r = _robot(db)
    r._robot_object = cached
    r._send_instant_action = MagicMock()

    async def send(*_a, **_k):
        pass
    r._send_instant_action = send
    _api_put(db, telemetry_recording="off")

    await r._handle_force_cancel(cached)

    assert db.writes == [("fields", "r1", ["needs_order_cancel"])]
    assert db.spec["needs_order_cancel"] is False
    assert db.spec["telemetry_recording"] == "off"


# --- the SQL of update_spec_fields ---------------------------------------------------------

class _Cursor:
    def __init__(self, log):
        self.log, self.rowcount = log, 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, params=None):
        self.log.append((sql, params))

    async def fetchone(self):
        return ("r1", "ALIVE", {}, {})


class _Conn:
    def __init__(self, log):
        self.log = log

    def cursor(self):
        return _Cursor(self.log)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _pg():
    db = PostgresDatabase(dbname="t", user="u", password="p", host="h", port=5432)
    log = []
    db._pool = MagicMock()
    db._pool.connection = MagicMock(return_value=_Conn(log))

    async def notify(cursor, table, name, lifecycle, publisher_id):
        log.append((f"NOTIFY {table}, '{publisher_id} {name} {lifecycle}'", None))
    db._notify = notify
    return db, log


async def test_update_spec_fields_is_one_merge_statement_plus_notify():
    db, log = _pg()
    await db.update_spec_fields(api_objects.RobotObjectV1, "r1",
                                {"datum": {"latitude": 1.0, "longitude": 2.0,
                                           "bearing_deg": 0.0}}, "pub")
    (sql, params), (notify, _) = log
    assert sql == ("UPDATE robotobjectv1 SET spec = spec || %s::jsonb "
                   "WHERE name = %s RETURNING *;")
    assert json.loads(params[0]) == {"datum": {"latitude": 1.0, "longitude": 2.0,
                                               "bearing_deg": 0.0}}
    assert params[1] == "r1"
    assert notify.startswith("NOTIFY robotobjectv1, 'pub r1 ALIVE'")


async def test_update_spec_fields_rejects_unknown_keys_and_skips_empty():
    db, log = _pg()
    with pytest.raises(ValueError):
        await db.update_spec_fields(api_objects.RobotObjectV1, "r1", {"bogus": 1}, "pub")
    await db.update_spec_fields(api_objects.RobotObjectV1, "r1", {}, "pub")
    assert log == []


async def _datum(r, db, **kw):
    db.writes.clear()
    await r._process_datum_message(types.RobotDatum(**{"latitude": 47.5, "longitude": 19.0, **kw}))


async def test_datum_changed_at_moves_only_when_the_datum_changes():
    cached = api_objects.RobotObjectV1(name="r1", status={})
    db = StoreDB(cached)
    r = _robot(db)
    r._robot_object = cached

    await _datum(r, db)                                   # first datum: a change
    first = db.spec["datum_changed_at"]
    assert first is not None and "datum_stamp" not in db.spec or db.spec["datum_stamp"] is None

    await _datum(r, db, longitude=19.0000050)             # ~0.4 m jitter: not a change
    assert "datum_changed_at" not in db.writes[0][2]
    assert db.spec["datum_changed_at"] == first

    await _datum(r, db, longitude=19.0001)                # ~7.6 m: a change
    assert db.spec["datum_changed_at"] != first
    second = db.spec["datum_changed_at"]

    await _datum(r, db, longitude=19.0001, frame="utm")   # same place, new frame: a change
    assert db.spec["datum_changed_at"] != second
    api_objects.RobotObjectV1(name="r1", status={}, **db.spec)   # still loads


async def test_datum_stamp_is_stored_when_sent_and_ignored_when_garbage():
    cached = api_objects.RobotObjectV1(name="r1", status={})
    db = StoreDB(cached)
    r = _robot(db)
    r._robot_object = cached

    await _datum(r, db, stamp="2026-10-03T10:00:00Z")
    assert db.spec["datum_stamp"].startswith("2026-10-03T10:00:00")
    await _datum(r, db, stamp="not a time")               # the datum itself is still accepted
    assert db.spec["datum_stamp"].startswith("2026-10-03T10:00:00")
