"""Maps §14 U3 (docs/satinav-maps-redesign.md §14.2): run-change detection in the dispatcher.

- run_change.RunChangeDetector over header-id sequences: a fresh ONLINE, an ONLINE of a mere
  MQTT reconnect, the last will, a decreasing state headerId, a dispatcher restart, and one
  restart seen on both topics counted once;
- VDA5050Connection reads the client's `state` key;
- map_sessions.plan_geo_replace (the datum path): re-place after a run change, realign on a
  changed datum, the retained-datum trust rule, local maps and other zones;
- Robot._on_run_changed / _replace_geo_session on a fake Postgres: the SQL, MAP.SESSION_UNPLACED
  / MAP.SESSION_REALIGNED, the lost compare-and-set, and the mapping/set publish;
- the connection message reaches the Robot even without the fleet recorder.
"""
import contextlib
import json
import math
import os

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

from unittest.mock import AsyncMock, MagicMock  # noqa: E402

import pytest  # noqa: E402

import cloud_common.objects as api_objects  # noqa: E402
import packages.controllers.mission.vda5050_types as types  # noqa: E402
from packages.controllers.mission import server as dispatch_server  # noqa: E402
from packages.controllers.mission.run_change import RunChangeDetector  # noqa: E402
from packages.controllers.mission.server import Robot  # noqa: E402
from packages.events.emit import INSERT_SQL  # noqa: E402
from packages.utils import map_geo  # noqa: E402
from packages.utils import map_sessions as ms  # noqa: E402

pytestmark = pytest.mark.unit

UTM_DATUM = {"latitude": 47.47946, "longitude": 19.03238, "bearing_deg": 0.0, "frame": "utm",
             "utm_zone": 34, "utm_north": True, "utm_easting": 351756.484938,
             "utm_northing": 5260323.440888}
GEO = map_geo.geo_from_datum(UTM_DATUM)
MOVED = {**UTM_DATUM, "utm_easting": UTM_DATUM["utm_easting"] + 40.0}


# --- the detector ------------------------------------------------------------------------------

class TestDetector:
    def test_fresh_process_online(self):
        d = RunChangeDetector()
        assert d.on_connection("ONLINE", 1) is None       # dispatcher start: baseline only
        assert d.on_connection("ONLINE", 2) is None       # MQTT reconnect, same process
        assert d.on_connection("CONNECTIONBROKEN", 0) is None  # the will: ignored
        ev = d.on_connection("ONLINE", 1)                 # a new process
        assert ev == {"signal": "connection_online", "connection_header_id": 1,
                      "last_connection_header_id": 2}

    def test_same_header_online_is_a_new_process(self):
        d = RunChangeDetector()
        d.on_connection(types.VDA5050ConnectionState.ONLINE, 1)
        assert d.on_connection(types.VDA5050ConnectionState.ONLINE, 1) is not None

    def test_decreasing_state(self):
        d = RunChangeDetector()
        assert d.on_state(812) is None
        assert d.on_state(813) is None and d.on_state(813) is None
        ev = d.on_state(0)
        assert ev["state_header_id"] == 0 and ev["last_state_header_id"] == 813

    def test_one_restart_counted_once(self):
        d = RunChangeDetector()
        d.on_connection("ONLINE", 1)
        d.on_state(500)
        assert d.on_connection("ONLINE", 1) is not None   # the new process says hello
        assert d.on_state(0) is None                      # its first state: baseline
        assert d.on_state(1) is None
        # and the other order: the state shows it first, the ONLINE comes late
        d.on_state(40)
        assert d.on_state(3) is not None
        assert d.on_connection("ONLINE", 1) is None

    def test_garbage_is_ignored(self):
        d = RunChangeDetector()
        assert d.on_state(None) is None and d.on_connection("ONLINE", "x") is None

    def test_connection_state_alias(self):
        m = types.VDA5050Connection(headerId=1, timestamp="t", state="ONLINE")
        assert m.connectionState == types.VDA5050ConnectionState.ONLINE
        m = types.VDA5050Connection(headerId=1, timestamp="t", connectionState="OFFLINE",
                                    state="ONLINE")
        assert m.connectionState == types.VDA5050ConnectionState.OFFLINE  # the spec key wins


# --- the datum path ----------------------------------------------------------------------------

def geo_session(aligned=True, datum=UTM_DATUM, map_type="geo", geo=GEO, **kw):
    s = {"session_id": "6f1c0c2e-0000-4000-8000-000000000001", "map_name": "yard",
         "purpose": "operate", "aligned": aligned, "datum": dict(datum) if datum else None,
         "map_t_session": map_geo.session_transform(GEO, UTM_DATUM), "placement": None,
         "paused_at": None, "map_geo": geo, "map_type": map_type, "services": None}
    s.update(kw)
    return s


class TestPlanGeoReplace:
    def test_unplaced_is_re_placed_by_a_new_datum(self):
        t, reason = ms.plan_geo_replace(geo_session(aligned=False), MOVED, False)
        assert reason == "run_changed" and t["tx"] == pytest.approx(40.0)

    def test_same_datum_needs_trust(self):
        assert ms.plan_geo_replace(geo_session(aligned=False), UTM_DATUM, False) is None
        t, reason = ms.plan_geo_replace(geo_session(aligned=False), UTM_DATUM, True)
        assert reason == "run_changed" and t["tx"] == pytest.approx(0.0)

    def test_placed_realigns_only_on_a_change(self):
        assert ms.plan_geo_replace(geo_session(), UTM_DATUM, True) is None
        t, reason = ms.plan_geo_replace(geo_session(), MOVED, False)
        assert reason == "datum" and t["tx"] == pytest.approx(40.0)

    def test_nothing_to_do(self):
        other_zone = {**UTM_DATUM, "utm_zone": 33, "longitude": 14.9}
        assert ms.plan_geo_replace(geo_session(aligned=False), other_zone, True) is None
        assert ms.plan_geo_replace(geo_session(aligned=False, map_type="local", geo=None),
                                   MOVED, True) is None
        assert ms.plan_geo_replace(None, MOVED, True) is None
        assert ms.plan_geo_replace(geo_session(), None, True) is None


# --- the dispatcher on a fake Postgres ------------------------------------------------------------

class FakeCursor:
    def __init__(self, db):
        self.db = db
        self.rowcount = 0
        self._rows = []

    async def execute(self, sql, params=()):
        self.db.sql.append((sql, params))
        for prefix, result in self.db.results:
            if sql.lstrip().startswith(prefix):
                rows, self.rowcount = result(params) if callable(result) else result
                self._rows = list(rows)
                return
        self._rows, self.rowcount = [], 1

    async def fetchone(self):
        return self._rows[0] if self._rows else None

    async def fetchall(self):
        return list(self._rows)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, db):
        self.db = db

    def cursor(self):
        return FakeCursor(self.db)

    @contextlib.asynccontextmanager
    async def transaction(self):
        yield self


class FakeDb:
    """PostgresDatabase.connection() over programmed results: [(sql prefix, (rows, rowcount))]."""

    def __init__(self, results):
        self.results = results
        self.sql = []
        self.update_spec_fields = AsyncMock()
        self.update_status = AsyncMock()

    @contextlib.asynccontextmanager
    async def connection(self):
        yield FakeConn(self)

    def events(self):
        return [p for s, p in self.sql if s.startswith(INSERT_SQL[:20])]

    def codes(self):
        return [p[5] for p in self.events()]


def _robot(db):
    server = MagicMock()
    server.push_telemetry = False
    server.mission_ctrl_url = None
    server.disable_request_factsheet = True
    server.fleet_recorder = None
    server.mqtt_epoch = 1
    r = Robot("r1", db, MagicMock(), "uagv/v2/RobotCompany", server)
    r._robot_object = api_objects.RobotObjectV1(name="r1", status={"online": True})
    return r


def _session_row(s):
    return tuple(s[k] for k in ms.ROBOT_SESSION_KEYS)


def _sets(robot):
    return [json.loads(c.args[1]) for c in robot._mqtt_client.publish.call_args_list
            if c.args[0] == "uagv/v2/RobotCompany/r1/mapping/set"]


class TestDispatcherRunChange:
    async def test_unplace_on_a_new_run(self):
        sid = "6f1c0c2e-0000-4000-8000-000000000002"
        state = {"aligned": True}

        def unplace(params):
            patch = json.loads(params[0])
            assert patch["unplaced_reason"] == "run_changed" and params[1] == "r1"
            state["aligned"] = False
            return [(sid, "shed", "mapping", {"tx": 1.0, "ty": 0.0, "yaw": 0.0})], 1

        def read(_params):
            s = geo_session(aligned=state["aligned"], datum=None, map_type="local", geo=None,
                            purpose="mapping", services=["topo"], session_id=sid,
                            map_name="shed")
            return [_session_row(s)], 1

        db = FakeDb([("UPDATE map_sessions SET aligned = false", unplace),
                     ("SELECT s.session_id", read)])
        r = _robot(db)
        await r._on_connection_message(types.VDA5050Connection(headerId=1, timestamp="t",
                                                               state="ONLINE"))
        assert db.sql == []  # baseline
        await r._on_connection_message(types.VDA5050Connection(headerId=1, timestamp="t",
                                                               state="ONLINE"))
        assert db.codes() == ["MAP.SESSION_UNPLACED"]
        payload = json.loads(db.events()[0][8])
        assert payload["reason"] == "run_changed" and payload["purpose"] == "mapping"
        assert payload["evidence"]["connection_header_id"] == 1
        assert _sets(r)[-1]["enabled"] is False and _sets(r)[-1]["session_id"] == sid
        call = r._mqtt_client.publish.call_args
        assert call.kwargs == {"qos": 1, "retain": True}

    async def test_state_message_triggers_it_too(self):
        db = FakeDb([("UPDATE map_sessions SET aligned = false", ([], 0))])
        r = _robot(db)
        r._on_client_message = AsyncMock()
        for hid in (10, 11, 0):
            await r._on_state_message(types.VDA5050State(
                headerId=hid, timestamp="", nodeStates=[], edgeStates=[], errors=[]))
        unplaces = [s for s, _ in db.sql if s.startswith("UPDATE map_sessions SET aligned")]
        assert len(unplaces) == 1
        assert _sets(r) == []  # nothing was unplaced: no publish

    async def test_datum_re_places_an_unplaced_geo_session(self):
        s = geo_session(aligned=False,
                        placement={"unplaced_reason": "run_changed", "unplaced_at": "t"})
        placed = dict(s)

        def replace(params):
            datum, transform, placement, sid, was_aligned, old = params
            assert was_aligned is False and json.loads(old) == UTM_DATUM
            assert json.loads(placement)["source"] == "datum"
            placed.update(aligned=True, datum=json.loads(datum),
                          map_t_session=json.loads(transform))
            return [], 1

        reads = iter([s, placed])
        db = FakeDb([("UPDATE map_sessions SET datum", replace),
                     ("SELECT s.session_id", lambda _p: ([_session_row(next(reads))], 1))])
        r = _robot(db)
        r._datum_epoch = 1  # not the first datum of this MQTT epoch: trusted
        await r._process_datum_message(types.RobotDatum(**MOVED))
        assert db.codes() == ["MAP.SESSION_REALIGNED"]
        payload = json.loads(db.events()[0][8])
        assert payload["reason"] == "run_changed" and payload["aligned"] is True
        assert payload["map_T_session"]["tx"] == pytest.approx(40.0)
        assert db.events()[0][9] == "dispatch"
        assert placed["aligned"] is True
        assert _sets(r)[-1]["enabled"] is False  # operate: never captures

    async def test_first_datum_of_an_epoch_with_the_same_datum_is_not_trusted(self):
        s = geo_session(aligned=False)
        db = FakeDb([("SELECT s.session_id", ([_session_row(s)], 1))])
        r = _robot(db)
        await r._process_datum_message(types.RobotDatum(**UTM_DATUM))
        assert not [q for q, _ in db.sql if q.startswith("UPDATE map_sessions SET datum")]
        assert r._datum_epoch == 1
        # the next one (a live message in this epoch) re-places
        db.results.insert(0, ("UPDATE map_sessions SET datum", ([], 1)))
        await r._process_datum_message(types.RobotDatum(**UTM_DATUM))
        assert db.codes() == ["MAP.SESSION_REALIGNED"]

    async def test_lost_compare_and_set_writes_nothing(self):
        s = geo_session()
        db = FakeDb([("UPDATE map_sessions SET datum", ([], 0)),
                     ("SELECT s.session_id", ([_session_row(s)], 1))])
        r = _robot(db)
        await r._process_datum_message(types.RobotDatum(**MOVED))
        assert db.codes() == [] and _sets(r) == []

    async def test_database_down_never_raises(self):
        class Down(FakeDb):
            @contextlib.asynccontextmanager
            async def connection(self):
                raise RuntimeError("down")
                yield  # pragma: no cover

        r = _robot(Down([]))
        await r._on_run_changed({"signal": "x"})
        await r._replace_geo_session(dict(MOVED), True)
        assert _sets(r) == []


class TestServerForwarding:
    async def test_connection_reaches_the_robot_without_the_recorder(self):
        srv = dispatch_server.RobotServer.__new__(dispatch_server.RobotServer)
        srv.fleet_recorder = None
        srv._mqtt_prefix = "uagv/v2/RobotCompany"
        queued = []
        srv._enqueue = lambda _q, obj: queued.append(obj)
        srv._mqtt_messages = None
        msg = MagicMock(topic="uagv/v2/RobotCompany/r1/connection",
                        payload=json.dumps({"headerId": 1, "timestamp": "t",
                                            "state": "ONLINE"}).encode())
        srv._mqtt_on_message(None, None, msg)
        assert len(queued) == 1
        assert queued[0].payload.connectionState == types.VDA5050ConnectionState.ONLINE

    def test_epoch(self):
        srv = dispatch_server.RobotServer.__new__(dispatch_server.RobotServer)
        srv.mqtt_epoch = 0
        srv._mqtt_connected()
        assert srv.mqtt_epoch == 1


def test_math_sanity():
    # the re-placed geo transform is the datum's own session transform
    assert map_geo.session_transform(GEO, MOVED)["tx"] == pytest.approx(40.0)
    assert math.isfinite(map_geo.session_transform(GEO, MOVED)["yaw"])
