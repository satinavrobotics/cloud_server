"""WP9 sites (docs/satinav-fleet-agent-phase0-v2.md §3.6, §4.2, §5.5, §7 WP9), API side.

- SiteObjectV1 (pydantic v1) and its registration in ALL_OBJECTS;
- request validation (422), and the sites CRUD / assignment routes on a fake transactional
  store that mimics the statements of packages/api/sites.py (rollback on error, NOTIFY
  delivered on commit only, EXCLUDE on overlapping ranges);
- RECORDING_CHANGED for the site scope and for assignments that change a robot's level;
- the recording policy: precedence robot > site > global, site and assignment pushes from the
  NOTIFY watchers (no restart), and the API writer's event context taking the site from it;
- PostgresChannelWatcher (the assignment channel's LISTEN loop).

The real SQL (EXCLUDE, range arithmetic, locking) is covered by tests/integration/sites.
"""
import asyncio
import copy
import datetime
import json
import os

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import psycopg  # noqa: E402
import pydantic  # noqa: E402
import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

import packages.api.main as main  # noqa: E402
from cloud_common.objects import ALL_OBJECTS, USER_API_OBJECT_DICT  # noqa: E402
from cloud_common.objects.object import ObjectLifecycleV1  # noqa: E402
from cloud_common.objects.robot import RobotObjectV1  # noqa: E402
from cloud_common.objects.site import SiteObjectV1, SiteSpecV1, valid_site_id  # noqa: E402
from packages.api import recording, sites  # noqa: E402
from packages.api.telemetry import LatestContext  # noqa: E402
from packages.database.postgres import PostgresChannelWatcher  # noqa: E402
from packages.events.codes import EventCode  # noqa: E402
from packages.events.schemas import RecordingLevel, RecordingScope  # noqa: E402
from packages.telemetry_ingest import tables  # noqa: E402
from packages.telemetry_ingest.policy import (  # noqa: E402
    ASSIGNMENTS_CHANNEL, PolicySources, RecordingPolicy, assignment_payload,
    parse_assignment_payload,
)
from packages.telemetry_ingest.rehydrate import LatestRow  # noqa: E402
from tests.unit.test_api_telemetry import (  # noqa: E402,F401 - fixtures used implicitly
    LockServer, _stop_created, diag, epoch, flush, make_telemetry,
)
from tests.unit.test_recording_policy_api import _EmitConnection, _db, _payloads  # noqa: E402

pytestmark = pytest.mark.unit

T0 = datetime.datetime(2026, 9, 24, 12, 0, tzinfo=datetime.timezone.utc)
FULL, EVENTS, OFF = RecordingLevel.FULL, RecordingLevel.EVENTS_ONLY, RecordingLevel.OFF
POLYGON = {"type": "Polygon", "coordinates": [[[19.0, 47.0], [19.1, 47.0], [19.1, 47.1],
                                               [19.0, 47.0]]]}


# --- model -------------------------------------------------------------------------------------

class TestModel:
    def test_registered_like_the_other_objects(self):
        assert SiteObjectV1 in ALL_OBJECTS
        assert SiteObjectV1.table_name() == "siteobjectv1"
        assert USER_API_OBJECT_DICT["site"] is SiteObjectV1

    def test_full_spec(self):
        site = SiteObjectV1(name="farm-1", customer="acme", display_name="Farm 1",
                            sector="agriculture", geofence=POLYGON, gps_datum="ETRS89",
                            rtk_base={"caster": "c", "mountpoint": "M1"},
                            timezone="Europe/Budapest", telemetry_recording="full")
        spec = json.loads(site.spec.json())
        assert spec == {"customer": "acme", "display_name": "Farm 1", "sector": "agriculture",
                        "geofence": POLYGON, "gps_datum": "ETRS89",
                        "rtk_base": {"caster": "c", "mountpoint": "M1"},
                        "timezone": "Europe/Budapest", "telemetry_recording": "full"}

    def test_everything_is_optional(self):
        assert SiteObjectV1.default_spec() == {
            "customer": None, "display_name": None, "sector": None, "geofence": None,
            "gps_datum": None, "rtk_base": None, "timezone": None, "telemetry_recording": None}
        stored = SiteObjectV1(name="s", lifecycle="ALIVE", status={})  # a row with {} spec
        assert stored.telemetry_recording is None and stored.status.dict() == {}

    @pytest.mark.parametrize("geofence", [
        POLYGON,
        {"type": "MultiPolygon", "coordinates": []},
        {"type": "Feature", "geometry": POLYGON, "properties": {}},
        {"type": "FeatureCollection", "features": []},
    ])
    def test_geojson_accepted(self, geofence):
        assert SiteSpecV1(geofence=geofence).geofence == geofence

    @pytest.mark.parametrize("geofence", [
        {"coordinates": []}, {"type": "Circle", "coordinates": []}, {"type": "Polygon"},
        {"type": "Feature"}, {"type": "FeatureCollection"}, "POLYGON((0 0, 1 1))",
    ])
    def test_geojson_rejected(self, geofence):
        with pytest.raises(pydantic.ValidationError):
            SiteSpecV1(geofence=geofence)

    def test_timezone(self):
        assert SiteSpecV1(timezone="UTC").timezone == "UTC"
        with pytest.raises(pydantic.ValidationError):
            SiteSpecV1(timezone="Mars/Olympus_Mons")

    @pytest.mark.parametrize("bad", ["FULL", "all", 1, ""])
    def test_level_rejected(self, bad):
        with pytest.raises(pydantic.ValidationError):
            SiteSpecV1(telemetry_recording=bad)

    @pytest.mark.parametrize("site_id,ok", [
        ("farm-1", True), ("a", True), ("HU.solar:2", True), ("x" * 100, True),
        ("x" * 101, False), ("", False), ("-lead", False), ("has space", False),
        ("slash/no", False), (None, False), (3, False)])
    def test_site_ids(self, site_id, ok):
        assert valid_site_id(site_id) is ok


# --- request validation ------------------------------------------------------------------------

class TestValidation:
    def test_spec_changes(self):
        assert sites.spec_changes({"name": "s", "status": {}, "lifecycle": "ALIVE",
                                   "sector": "x"}) == {"sector": "x"}

    def test_unknown_keys_are_422(self):
        with pytest.raises(HTTPException) as exc:
            sites.spec_changes({"sector": "x", "colour": 1, "zz": 2})
        assert exc.value.status_code == 422
        assert [e["loc"] for e in exc.value.detail] == [["body", "colour"], ["body", "zz"]]

    def test_bad_level_is_422_like_wp8(self):
        with pytest.raises(HTTPException) as exc:
            sites.spec_changes({"telemetry_recording": "loud"})
        assert exc.value.status_code == 422
        assert exc.value.detail[0]["loc"] == ["body", "telemetry_recording"]

    def test_rename_is_422(self):
        with pytest.raises(HTTPException) as exc:
            sites.spec_changes({"name": "other"}, site_id="s1")
        assert exc.value.status_code == 422
        sites.spec_changes({"name": "s1"}, site_id="s1")

    def test_not_an_object(self):
        with pytest.raises(HTTPException) as exc:
            sites.spec_changes(["x"])
        assert exc.value.status_code == 422

    def test_model_errors_are_422(self):
        with pytest.raises(HTTPException) as exc:
            sites.build_spec({"timezone": "Nowhere/Here"})
        assert exc.value.status_code == 422
        assert exc.value.detail[0]["loc"] == ["body", "timezone"]


# --- fake store --------------------------------------------------------------------------------

class SitesStore:
    """State of siteobjectv1, robotobjectv1 (names) and robot_site_assignments, driven by the
    exact statements of packages/api/sites.py."""

    def __init__(self, robots=("r1", "r2")):
        self.robots = set(robots)
        self.sites = {}           # name -> [lifecycle, spec, status]
        self.assignments = []     # dicts: robot, site, lower, upper, by
        self.notifies = []        # committed (channel, payload)
        self.now = T0
        self.statements = []
        self.locks = []           # pg_advisory_xact_lock keys taken
        self.commits = self.rollbacks = 0
        self.fail = None          # fail(sql) -> exception or None

    def connection(self):
        return _Txn(self)

    def open_rows(self, robot):
        return [a for a in self.assignments if a["robot"] == robot and a["upper"] is None]

    def history(self, robot):
        return [(a["site"], a["lower"], a["upper"]) for a in self.assignments
                if a["robot"] == robot]


class _Txn:
    def __init__(self, store):
        self.store = store

    async def __aenter__(self):
        s = self.store
        self.saved = copy.deepcopy((s.sites, s.assignments, s.robots))
        self.conn = _Conn(s)
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        s = self.store
        if exc_type is not None:
            s.sites, s.assignments, s.robots = self.saved
            s.rollbacks += 1
            return False
        s.notifies.extend(self.conn.pending_notifies)
        s.commits += 1
        return False


class _Conn:
    def __init__(self, store):
        self.store = store
        self.pending_notifies = []

    def cursor(self):
        return _Cursor(self)


def _overlaps(a_lower, a_upper, b_lower, b_upper):
    return (a_upper is None or b_lower < a_upper) and (b_upper is None or a_lower < b_upper)


class _Cursor:
    def __init__(self, conn):
        self.conn, self.store = conn, conn.store
        self.rows, self.rowcount = [], -1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def fetchone(self):
        return self.rows[0] if self.rows else None

    async def fetchall(self):
        return list(self.rows)

    async def execute(self, sql, params=None):  # noqa: C901 - a tiny SQL dispatcher
        s = self.store
        q = " ".join(sql.split())
        s.statements.append(q)
        if s.fail is not None:
            exc = s.fail(q)
            if exc is not None:
                raise exc
        self.rows, self.rowcount = [], -1
        p = params or ()
        if q == "SELECT pg_notify(%s, %s)":
            self.conn.pending_notifies.append(tuple(p))
        elif q.startswith("INSERT INTO siteobjectv1"):
            if p[0] in s.sites:
                self.rowcount = 0
            else:
                s.sites[p[0]] = [p[1], json.loads(p[2]), json.loads(p[3])]
                self.rowcount = 1
        elif q.startswith("SELECT lifecycle, spec, status FROM siteobjectv1"):
            if p[0] in s.sites:
                self.rows = [tuple(s.sites[p[0]])]
        elif q.startswith("UPDATE siteobjectv1 SET spec"):
            s.sites[p[1]][1] = json.loads(p[0])
        elif q.startswith("SELECT spec FROM siteobjectv1"):
            if p[0] in s.sites:
                self.rows = [(s.sites[p[0]][1],)]
        elif q.startswith("SELECT a.robot_name FROM robot_site_assignments"):
            self.rows = sorted((a["robot"],) for a in s.assignments
                               if a["site"] == p[0] and a["upper"] is None
                               and a["robot"] in s.robots)
        elif q.startswith("UPDATE robot_site_assignments SET valid = tstzrange(lower(valid), "
                          "greatest"):
            self.rows = []
            for a in s.assignments:
                if a["site"] == p[0] and a["upper"] is None:
                    a["upper"] = max(a["lower"], s.now)
                    self.rows.append((a["robot"],))
        elif q.startswith("DELETE FROM siteobjectv1"):
            s.sites.pop(p[0], None)
        elif q.startswith("SELECT 1 FROM robotobjectv1"):
            self.rows = [(1,)] if p[0] in s.robots else []
        elif q.startswith("SELECT pg_advisory_xact_lock"):
            s.locks.append(p[0])
        elif q.startswith("SELECT 1 FROM siteobjectv1"):
            assert "FOR SHARE" in q
            self.rows = [(1,)] if p[0] in s.sites else []
        elif q == "SELECT clock_timestamp()":
            s.now += datetime.timedelta(seconds=1)
            self.rows = [(s.now,)]
        elif q.startswith("SELECT site_id, lower(valid), assigned_by FROM"):
            assert "FOR UPDATE" in q
            self.rows = [(a["site"], a["lower"], a["by"]) for a in s.open_rows(p[0])]
        elif q.startswith("UPDATE robot_site_assignments SET valid = tstzrange(lower(valid), %s"):
            for a in s.open_rows(p[1]):
                a["upper"] = p[0]
        elif q.startswith("INSERT INTO robot_site_assignments"):
            robot, site, lower, by = p
            for a in s.assignments:
                if a["robot"] == robot and _overlaps(a["lower"], a["upper"], lower, None):
                    raise psycopg.errors.ExclusionViolation("conflicting key value")
            s.assignments.append({"robot": robot, "site": site, "lower": lower,
                                  "upper": None, "by": by})
        elif q.startswith("SELECT site_id, lower(valid), upper(valid), assigned_by"):
            rows = [(a["site"], a["lower"], a["upper"], a["by"]) for a in s.assignments
                    if a["robot"] == p[0]]
            self.rows = sorted(rows, key=lambda r: r[1], reverse=True)
        else:
            raise AssertionError(f"unexpected statement {q}")


@pytest.fixture
def store():
    return SitesStore()


@pytest.fixture
def hooks(monkeypatch):
    """record_change / record_assignment_change replaced by mocks (tested separately below);
    they must be called on the route's own connection, inside its transaction."""
    change, assign = AsyncMock(return_value=True), AsyncMock(return_value=True)
    monkeypatch.setattr(recording, "record_change", change)
    monkeypatch.setattr(recording, "record_assignment_change", assign)
    return change, assign


@pytest.fixture
def svc(store):
    service = MagicMock()
    service.database.connection = store.connection

    async def get_object(cls, name):
        assert cls is SiteObjectV1
        if name not in store.sites:
            raise HTTPException(404, f"Did not find \"site\" with name \"{name}\"")
        lifecycle, spec, status = store.sites[name]
        return SiteObjectV1(name=name, lifecycle=lifecycle, status=status, **spec)

    async def list_objects(cls):
        return [await get_object(cls, name) for name in sorted(store.sites)]
    service.database.get_object = AsyncMock(side_effect=get_object)
    service.database.list_objects = AsyncMock(side_effect=list_objects)
    with patch.object(main, "service", service):
        yield service


def _site_notifies(store):
    return [(p.split(" ")[1], p.split(" ")[2]) for c, p in store.notifies if c == "siteobjectv1"]


def _assign_notifies(store):
    return [parse_assignment_payload(p) for c, p in store.notifies if c == ASSIGNMENTS_CHANNEL]


def put(robot, site_id):
    return main.assign_robot_site(robot, main.AssignRobotSiteRequest(site_id=site_id))


# --- site routes -------------------------------------------------------------------------------

class TestSiteRoutes:
    async def test_create_get_list(self, store, svc, hooks):
        created = await main.create_site({"name": "s1", "display_name": "One",
                                          "geofence": POLYGON})
        assert created["name"] == "s1" and created["display_name"] == "One"
        assert created["lifecycle"] == ObjectLifecycleV1.ALIVE
        assert set(created) == set(SiteObjectV1(name="x").dict())
        assert store.sites["s1"][1]["geofence"] == POLYGON
        assert _site_notifies(store) == [("s1", "ALIVE")]
        assert (await main.get_site("s1"))["display_name"] == "One"
        await main.create_site({"name": "s0"})
        assert [s["name"] for s in await main.list_sites()] == ["s0", "s1"]
        hooks[0].assert_not_awaited()          # no level set: no RECORDING_CHANGED

    async def test_create_with_level_records_the_change_in_its_transaction(self, store, svc,
                                                                           hooks):
        await main.create_site({"name": "s1", "telemetry_recording": "off"})
        conn, scope, scope_id, old, new, actor = hooks[0].call_args.args
        assert isinstance(conn, _Conn)
        assert (scope, scope_id, old, new, actor) == (RecordingScope.SITE, "s1", None,
                                                      "off", None)

    async def test_duplicate_is_409_and_writes_nothing(self, store, svc, hooks):
        await main.create_site({"name": "s1", "sector": "a"})
        with pytest.raises(HTTPException) as exc:
            await main.create_site({"name": "s1", "sector": "b"})
        assert exc.value.status_code == 409
        assert store.sites["s1"][1]["sector"] == "a"
        assert _site_notifies(store) == [("s1", "ALIVE")]     # the rolled-back one is gone

    @pytest.mark.parametrize("body", [
        {}, {"name": "bad id"}, {"name": "s1", "timezone": "Nowhere/X"},
        {"name": "s1", "extra": 1}, {"name": "s1", "telemetry_recording": "FULL"},
        {"name": "s1", "geofence": {"type": "Blob"}}])
    async def test_create_invalid_is_422(self, store, svc, hooks, body):
        with pytest.raises(HTTPException) as exc:
            await main.create_site(body)
        assert exc.value.status_code == 422
        assert store.sites == {} and store.notifies == []

    async def test_partial_update(self, store, svc, hooks):
        await main.create_site({"name": "s1", "sector": "solar", "customer": "acme"})
        updated = await main.update_site("s1", {"display_name": "S", "customer": None})
        assert (updated["sector"], updated["display_name"], updated["customer"]) == \
            ("solar", "S", None)
        assert _site_notifies(store) == [("s1", "ALIVE"), ("s1", "ALIVE")]
        hooks[0].assert_not_awaited()          # level not in the body

    async def test_update_level_records_old_and_new(self, store, svc, hooks):
        await main.create_site({"name": "s1", "telemetry_recording": "full"})
        hooks[0].reset_mock()
        await main.update_site("s1", {"telemetry_recording": None})
        _, scope, scope_id, old, new, _ = hooks[0].call_args.args
        assert (scope, scope_id, old, new) == (RecordingScope.SITE, "s1", "full", None)

    async def test_update_accepts_what_get_returned(self, store, svc, hooks):
        await main.create_site({"name": "s1"})
        body = {**(await main.get_site("s1")), "sector": "x"}
        body["lifecycle"] = "ALIVE"
        assert (await main.update_site("s1", body))["sector"] == "x"

    async def test_update_errors(self, store, svc, hooks):
        with pytest.raises(HTTPException) as exc:
            await main.update_site("nope", {"sector": "x"})
        assert exc.value.status_code == 404
        await main.create_site({"name": "s1", "timezone": "UTC"})
        for body in ({"timezone": "Nowhere/X"}, {"telemetry_recording": "loud"},
                     {"name": "s2"}, {"bogus": 1}):
            with pytest.raises(HTTPException) as exc:
                await main.update_site("s1", body)
            assert exc.value.status_code == 422
        assert store.sites["s1"][1]["timezone"] == "UTC"

    async def test_get_unknown_is_404(self, store, svc):
        with pytest.raises(HTTPException) as exc:
            await main.get_site("nope")
        assert exc.value.status_code == 404

    async def test_delete(self, store, svc, hooks):
        await main.create_site({"name": "s1", "telemetry_recording": "off"})
        hooks[0].reset_mock()
        assert await main.delete_site("s1") == {"success": True, "message": "Site s1 deleted"}
        assert "s1" not in store.sites
        assert _site_notifies(store)[-1] == ("s1", "DELETED")
        _, scope, scope_id, old, new, _ = hooks[0].call_args.args
        assert (scope, scope_id, old, new) == (RecordingScope.SITE, "s1", "off", None)
        with pytest.raises(HTTPException) as exc:
            await main.delete_site("s1")
        assert exc.value.status_code == 404

    async def test_delete_refused_while_robots_are_assigned(self, store, svc, hooks):
        await main.create_site({"name": "s1"})
        await put("r2", "s1")
        await put("r1", "s1")
        with pytest.raises(HTTPException) as exc:
            await main.delete_site("s1")
        assert exc.value.status_code == 409
        assert exc.value.detail["robots"] == ["r1", "r2"]
        assert "s1" in store.sites and len(store.open_rows("r1")) == 1
        await put("r1", None)
        await put("r2", None)
        await main.delete_site("s1")
        assert [h[0] for h in store.history("r1")] == ["s1"]     # history is kept

    async def test_delete_closes_assignments_of_robots_that_no_longer_exist(self, store, svc,
                                                                            hooks):
        await main.create_site({"name": "s1"})
        await put("r1", "s1")
        store.robots.discard("r1")                               # robot deleted meanwhile
        store.notifies.clear()
        await main.delete_site("s1")
        assert store.open_rows("r1") == []
        assert _assign_notifies(store) == [("r1", None)]

    async def test_unexpected_errors_are_500(self, store, svc, hooks):
        store.fail = lambda q: RuntimeError("db gone") if q.startswith("INSERT") else None
        with pytest.raises(HTTPException) as exc:
            await main.create_site({"name": "s1"})
        assert exc.value.status_code == 500

    async def test_service_not_initialized(self):
        with patch.object(main, "service", None):
            with pytest.raises(HTTPException) as exc:
                await main.list_sites()
        assert exc.value.status_code == 503


# --- assignment routes -------------------------------------------------------------------------

class TestAssignmentRoutes:
    @pytest.fixture(autouse=True)
    def _sites(self, store):
        for name in ("s1", "s2"):
            store.sites[name] = ["ALIVE", SiteSpecV1().dict(), {}]

    async def test_first_assignment(self, store, svc, hooks):
        result = await put("r1", "s1")
        ts = store.now
        assert result == {"robot_name": "r1", "site_id": "s1", "changed": True,
                          "previous": None,
                          "assignment": {"robot_name": "r1", "site_id": "s1",
                                         "valid_from": ts.isoformat(), "valid_to": None,
                                         "assigned_by": None, "current": True}}
        assert store.history("r1") == [("s1", ts, None)]
        assert _assign_notifies(store) == [("r1", "s1")]
        assert store.locks == [sites.assignment_lock_key("r1")]
        lock = store.statements.index("SELECT pg_advisory_xact_lock(%s)")
        assert lock < store.statements.index("SELECT clock_timestamp()")  # clock after lock
        conn, robot, old, new, actor, at = hooks[1].call_args.args
        assert isinstance(conn, _Conn) and (robot, old, new, actor, at) == \
            ("r1", None, "s1", None, ts)

    async def test_move_closes_and_opens_in_one_transaction(self, store, svc, hooks):
        await put("r1", "s1")
        t1 = store.now
        commits = store.commits
        result = await put("r1", "s2")
        t2 = store.now
        assert store.commits == commits + 1
        assert store.history("r1") == [("s1", t1, t2), ("s2", t2, None)]
        assert result["previous"]["valid_to"] == result["assignment"]["valid_from"] == \
            t2.isoformat()
        assert _assign_notifies(store) == [("r1", "s1"), ("r1", "s2")]
        assert hooks[1].call_args.args[1:4] == ("r1", "s1", "s2")

    async def test_same_site_is_a_no_op(self, store, svc, hooks):
        await put("r1", "s1")
        hooks[1].reset_mock()
        result = await put("r1", "s1")
        assert result["changed"] is False and result["assignment"]["site_id"] == "s1"
        assert len(store.history("r1")) == 1 and len(_assign_notifies(store)) == 1
        hooks[1].assert_not_awaited()
        result = await put("r2", None)            # unassigned and unassign again
        assert result == {"robot_name": "r2", "site_id": None, "changed": False,
                          "assignment": None, "previous": None}

    async def test_unassign(self, store, svc, hooks):
        await put("r1", "s1")
        result = await put("r1", None)
        assert result["changed"] and result["assignment"] is None
        assert result["previous"]["site_id"] == "s1" and result["previous"]["valid_to"]
        assert store.open_rows("r1") == []
        assert _assign_notifies(store)[-1] == ("r1", None)

    @pytest.mark.parametrize("robot,site", [("ghost", "s1"), ("r1", "nope")])
    async def test_unknown_robot_or_site_is_404(self, store, svc, hooks, robot, site):
        with pytest.raises(HTTPException) as exc:
            await put(robot, site)
        assert exc.value.status_code == 404
        assert store.assignments == [] and store.notifies == []

    async def test_bad_site_id_is_422(self, store, svc, hooks):
        with pytest.raises(HTTPException) as exc:
            await put("r1", "bad id")
        assert exc.value.status_code == 422

    def test_request_model(self):
        with pytest.raises(pydantic.ValidationError):
            main.AssignRobotSiteRequest()                       # site_id is required
        with pytest.raises(pydantic.ValidationError):
            main.AssignRobotSiteRequest(site_id="s1", robot="x")
        assert main.AssignRobotSiteRequest(site_id=None).site_id is None

    async def test_overlap_is_409_and_rolls_back(self, store, svc, hooks):
        # A row written behind the route's back (not through this module) overlaps.
        store.assignments.append({"robot": "r1", "site": "s2", "lower": T0 +
                                  datetime.timedelta(days=1), "upper": None, "by": "x"})
        store.assignments[0]["upper"] = T0 + datetime.timedelta(days=2)
        with pytest.raises(HTTPException) as exc:
            await put("r1", "s1")
        assert exc.value.status_code == 409
        assert len(store.assignments) == 1 and store.notifies == []

    async def test_migrations_missing_is_503(self, store, svc, hooks):
        store.fail = lambda q: psycopg.errors.UndefinedTable("no table") \
            if "robot_site_assignments" in q else None
        with pytest.raises(HTTPException) as exc:
            await put("r1", "s1")
        assert exc.value.status_code == 503
        with pytest.raises(HTTPException) as exc:
            await main.list_robot_site_assignments("r1")
        assert exc.value.status_code == 503

    async def test_recording_failure_never_fails_the_assignment(self, store, svc, monkeypatch):
        # The real record_assignment_change on a connection that cannot run it: logged,
        # counted, the assignment commits.
        recording.stats.update(written=0, failed=0)
        result = await put("r1", "s1")
        assert result["changed"] and store.open_rows("r1")
        assert recording.stats["failed"] == 1

    async def test_history_newest_first(self, store, svc, hooks):
        await put("r1", "s1")
        await put("r1", "s2")
        await put("r1", None)
        await put("r1", "s1")
        history = await main.list_robot_site_assignments("r1")
        assert [(h["site_id"], h["current"]) for h in history] == \
            [("s1", True), ("s2", False), ("s1", False)]
        assert history[1]["valid_to"] < history[0]["valid_from"]   # unassigned in between
        assert history[2]["valid_to"] == history[1]["valid_from"]
        assert await main.list_robot_site_assignments("r2") == []

    async def test_history_of_unknown_robot(self, store, svc, hooks):
        with pytest.raises(HTTPException) as exc:
            await main.list_robot_site_assignments("ghost")
        assert exc.value.status_code == 404
        await put("r1", "s1")
        store.robots.discard("r1")                      # deleted robot: history remains
        assert len(await main.list_robot_site_assignments("r1")) == 1


# --- RECORDING_CHANGED -------------------------------------------------------------------------

async def _record(db, fn, *args, **kwargs):
    return await fn(_EmitConnection(db), *args, ts=T0, **kwargs)


class TestRecordingChanged:
    @pytest.fixture(autouse=True)
    def _reset_stats(self):
        recording.stats.update(written=0, failed=0)

    async def test_site_scope(self):
        db = _db(global_level="off", sites={"s1": "full"})
        assert await _record(db, recording.record_change, RecordingScope.SITE, "s1", None,
                             "full", None)
        assert _payloads(db) == [(None, "s1", EventCode.TELEMETRY_RECORDING_CHANGED.value,
                                  "api", "info",
                                  {"old_level": "off", "new_level": "full", "scope": "site",
                                   "scope_id": "s1", "actor": None})]

    async def test_site_scope_back_to_inherit(self):
        db = _db(sites={"s1": None})
        assert await _record(db, recording.record_change, RecordingScope.SITE, "s1", "off",
                             None, None)
        ((*_, payload),) = _payloads(db)
        assert (payload["old_level"], payload["new_level"]) == ("off", "events_only")

    async def test_robot_scope_resolves_through_the_site(self):
        db = _db(global_level="events_only", sites={"s1": "off"}, assignments={"r1": "s1"})
        assert await _record(db, recording.record_change, RecordingScope.ROBOT, "r1", "full",
                             None, None)
        ((robot, site, *_, payload),) = _payloads(db)
        assert (robot, site, payload["new_level"]) == ("r1", "s1", "off")

    async def test_assignment_that_changes_the_level(self):
        db = _db(global_level="events_only", sites={"s1": "full", "s2": "off"},
                 assignments={"r1": "s2"})
        assert await _record(db, recording.record_assignment_change, "r1", "s1", "s2", None)
        ((robot, site, code, source, _, payload),) = _payloads(db)
        assert (robot, site, code, source) == ("r1", "s2",
                                               EventCode.TELEMETRY_RECORDING_CHANGED.value,
                                               "api")
        assert payload == {"old_level": "full", "new_level": "off", "scope": "robot",
                           "scope_id": "r1", "actor": None}
        from packages.events import ids
        (row,) = db.events.values()
        assert row["event_id"] == ids.event_id(EventCode.TELEMETRY_RECORDING_CHANGED, "r1", T0,
                                               "robot:r1:site:s1->s2")

    async def test_unassign_to_the_global_level(self):
        db = _db(global_level="off", sites={"s1": "full"})
        assert await _record(db, recording.record_assignment_change, "r1", "s1", None, "ops")
        ((_, site, *_, payload),) = _payloads(db)
        assert site is None and (payload["old_level"], payload["new_level"], payload["actor"]) \
            == ("full", "off", "ops")

    @pytest.mark.parametrize("robots,sites_,old,new", [
        ({"r1": "full"}, {"s1": "off", "s2": "events_only"}, "s1", "s2"),  # robot override
        ({}, {"s1": None, "s2": "events_only"}, "s1", "s2"),   # both resolve events_only
        ({}, {"s1": "off"}, "s1", "s1"),                        # same site
    ])
    async def test_no_effective_change_no_event(self, robots, sites_, old, new):
        db = _db(robots=robots, sites=sites_)
        assert not await _record(db, recording.record_assignment_change, "r1", old, new, None)
        assert db.events == {}
        assert recording.stats == {"written": 0, "failed": 0}

    async def test_failure_is_contained(self):
        db = _db(sites={"s1": "full"})
        db.fail = lambda sql, params: RuntimeError("x") if "fleet_events" in sql else None
        assert not await _record(db, recording.record_assignment_change, "r1", None, "s1",
                                 None)
        assert db.events == {} and recording.stats["failed"] == 1


# --- recording policy --------------------------------------------------------------------------

def _site(name, level=None, deleted=False):
    return SiteObjectV1(name=name, telemetry_recording=level,
                        lifecycle=ObjectLifecycleV1.DELETED if deleted
                        else ObjectLifecycleV1.ALIVE)


class TestPolicy:
    def test_payload_round_trip(self):
        assert parse_assignment_payload(assignment_payload("r 1", "s1")) == ("r 1", "s1")
        assert parse_assignment_payload(assignment_payload("r1", None)) == ("r1", None)
        for bad in ("", "r1 s1", "{}", '{"robot_name": "", "site_id": null}',
                    '{"robot_name": "r", "site_id": 3}', "[1]", "null"):
            with pytest.raises(ValueError):
                parse_assignment_payload(bad)

    def test_precedence_robot_site_global(self):
        policy = RecordingPolicy(sources=PolicySources(global_level="off"))
        assert policy.level_for("r1") is OFF
        policy.apply_site_object(_site("s1", "full"))
        assert policy.level_for("r1") is OFF                     # not assigned yet
        policy.apply_assignment_payload(assignment_payload("r1", "s1"))
        assert policy.level_for("r1") is FULL and policy.site_for("r1") == "s1"
        policy.set_robot_level("r1", "events_only")
        assert policy.level_for("r1") is EVENTS                  # robot beats site
        policy.set_robot_level("r1", None)
        assert policy.level_for("r1") is FULL
        policy.apply_site_object(_site("s1", None))
        assert policy.level_for("r1") is OFF                     # site unset -> global
        policy.set_global_level(None)
        assert policy.level_for("r1") is EVENTS                  # nothing -> default

    def test_site_change_moves_every_robot_of_the_site(self):
        policy = RecordingPolicy(sources=PolicySources(
            robot_sites={"r1": "s1", "r2": "s1", "r3": "s2"}, site_levels={"s1": "full"}))
        assert [policy.level_for(r) for r in ("r1", "r2", "r3")] == [FULL, FULL, EVENTS]
        assert policy.apply_site_object(_site("s1", "off"))
        assert [policy.level_for(r) for r in ("r1", "r2", "r3")] == [OFF, OFF, EVENTS]
        assert not policy.apply_site_object(_site("s1", "off"))  # unchanged: no churn

    def test_deleted_site_is_forgotten(self):
        policy = RecordingPolicy(sources=PolicySources(robot_sites={"r1": "s1"},
                                                       site_levels={"s1": "off"}))
        assert policy.apply_site_object(_site("s1", "off", deleted=True))
        assert policy.level_for("r1") is EVENTS
        assert not policy.forget_site("s1")

    def test_unassign(self):
        policy = RecordingPolicy(sources=PolicySources(robot_sites={"r1": "s1"},
                                                       site_levels={"s1": "full"}))
        assert policy.apply_assignment_payload(assignment_payload("r1", None))
        assert policy.site_for("r1") is None and policy.level_for("r1") is EVENTS

    def test_unreadable_payload_reloads(self):
        policy = RecordingPolicy(sources=PolicySources())
        assert not policy.stale
        assert not policy.apply_assignment_payload("garbage")
        assert policy.stale

    def test_push_during_refresh_keeps_it_stale(self):
        policy = RecordingPolicy(sources=PolicySources())
        generation = policy._generation
        policy.apply_assignment_payload(assignment_payload("r1", "s1"))
        policy.apply_site_object(_site("s1", "off"))
        assert policy._generation == generation + 2


# --- API writer --------------------------------------------------------------------------------

class TestLatestContext:
    def test_site_from_the_policy_once_loaded(self):
        row = LatestRow(robot_name="r1", site_id="stale")
        policy = RecordingPolicy()                               # not loaded yet
        ctx = LatestContext({"r1": row}, policy)
        assert ctx.site_for("r1", T0) == "stale"                 # fallback until loaded
        policy.replace_sources(PolicySources(robot_sites={"r1": "s1"}))
        assert ctx.site_for("r1", T0) == "s1"
        policy.set_robot_site("r1", None)
        assert ctx.site_for("r1", T0) is None                    # unassigned wins over latest
        assert LatestContext({"r1": row}).site_for("r1", T0) == "stale"


async def _burst(tel, t0):
    """One thermal transition (-> SYSTEM.THERMAL_HIGH, THERMAL_OK) with its rows, flushed."""
    tel.on_diagnostics("r1", epoch(t0), diag(temp=60.0))
    tel.on_diagnostics("r1", epoch(t0 + 1), diag(temp=90.0))
    tel.on_diagnostics("r1", epoch(t0 + 2), diag(temp=70.0))
    await flush(tel)


def _diag_rows(db):
    return len(db.timeseries[tables.DIAGNOSTICS_TABLE])


def _event_sites(db, since=0):
    rows = sorted(db.events.values(), key=lambda r: r["ts"])
    return [(r["code"], r["site_id"]) for r in rows[since:]]


class TestApiTelemetry:
    async def test_site_pushes_switch_level_and_event_site_without_restart(self, tmp_path):
        from tests.unit.test_api_telemetry import set_level
        server = LockServer()
        set_level(server.db, "events_only")
        tel = make_telemetry(server, tmp_path)
        await tel.election.step()

        tel.on_site_object(_site("s1", "full"))
        await _burst(tel, 0)                                   # r1 not at s1 yet
        assert _diag_rows(server.db) == 0
        assert {s for _, s in _event_sites(server.db)} == {None}

        tel.on_site_assignment(assignment_payload("r1", "s1"))  # -> full via the site
        n = len(server.db.events)
        await _burst(tel, 10)
        assert _diag_rows(server.db) == 3
        assert {s for _, s in _event_sites(server.db, n)} == {"s1"}

        tel.on_site_object(_site("s1", "off"))                  # site level change
        n = len(server.db.events)
        await _burst(tel, 20)
        assert _diag_rows(server.db) == 3 and len(server.db.events) == n

        tel.on_robot_object(RobotObjectV1(name="r1", status={},
                                          telemetry_recording="events_only"))
        await _burst(tel, 30)                                   # robot beats site
        assert len(server.db.events) == n + 2
        tel.on_robot_object(RobotObjectV1(name="r1", status={}))

        tel.on_site_assignment(assignment_payload("r1", None))  # unassigned -> global
        n = len(server.db.events)
        await _burst(tel, 40)
        assert _diag_rows(server.db) == 3
        assert {s for _, s in _event_sites(server.db, n)} == {None}

    async def test_resync_and_bad_input(self, tmp_path):
        from tests.unit.test_api_telemetry import set_level
        server = LockServer()
        set_level(server.db, None)
        tel = make_telemetry(server, tmp_path)
        tel.on_site_object(_site("s1", "off"))                  # not the writer: no-op
        tel.on_site_assignment("garbage")
        tel.on_site_assignments_resync()
        await tel.election.step()
        policy = tel._term.policy
        assert policy.snapshot()["site_levels"] == {}
        policy.replace_sources(PolicySources())                 # not stale
        assert not policy.stale
        tel.on_site_assignments_resync()
        assert policy.stale
        policy.replace_sources(PolicySources())
        tel.on_site_assignment("garbage")                       # unreadable -> reload
        assert policy.stale
        tel.on_site_object(object())                            # logged, never raised
        assert tel.handler_errors.count == 1


# --- API watchers ------------------------------------------------------------------------------

class TestApiWatchers:
    async def test_assignment_watcher_feeds_the_policy(self):
        from packages.api.server import ApiDelegationService
        payloads = [None, assignment_payload("r1", "s1"), None]
        fed = []

        class Watcher:
            async def watch(self):
                for p in payloads:
                    yield p
                svc._running = False

        svc = MagicMock()
        svc._running = True
        svc.database.get_channel_watcher = MagicMock(return_value=Watcher())
        svc._feed_policy = lambda method, obj: fed.append((method, obj))
        await ApiDelegationService._watch_site_assignments(svc)
        svc.database.get_channel_watcher.assert_called_with(ASSIGNMENTS_CHANNEL)
        assert fed == [("on_site_assignments_resync", None),
                       ("on_site_assignment", assignment_payload("r1", "s1")),
                       ("on_site_assignments_resync", None)]

    async def test_site_watcher_feeds_the_policy(self):
        from packages.api.server import ApiDelegationService
        fed = []

        class Watcher:
            async def watch(self):
                yield _site("s1", "full")
                svc._running = False

        svc = MagicMock()
        svc._running = True
        svc.database.get_watcher = AsyncMock(return_value=Watcher())
        svc._feed_policy = lambda method, obj: fed.append((method, obj.name))
        await ApiDelegationService._watch_site_changes(svc)
        assert svc.database.get_watcher.call_args.args[0] is SiteObjectV1
        assert fed == [("on_site_object", "s1")]


# --- PostgresChannelWatcher --------------------------------------------------------------------

class _Notification:
    def __init__(self, payload):
        self.payload = payload


class _ListenConn:
    def __init__(self, payloads, fail_listen=False):
        self.payloads, self.fail_listen = list(payloads), fail_listen
        self.executed, self.closed = [], False

    async def execute(self, query):
        self.executed.append(query)
        if self.fail_listen:
            raise psycopg.OperationalError("down")

    async def notifies(self):
        for p in self.payloads:
            yield _Notification(p)
        await asyncio.sleep(3600)

    async def close(self):
        self.closed = True


class TestChannelWatcher:
    async def test_resync_marker_then_payloads_and_reconnect_on_silence(self):
        conns = [_ListenConn(["a", "b"]), _ListenConn(["c"])]
        it = iter(conns)

        async def connect():
            return next(it)
        watcher = PostgresChannelWatcher("dsn", ASSIGNMENTS_CHANNEL, notify_timeout_s=0.05,
                                         retry_s=0, connect=connect)
        gen = watcher.watch()
        got = [await gen.__anext__() for _ in range(5)]
        await gen.aclose()
        assert got == [None, "a", "b", None, "c"]
        assert conns[0].closed and conns[1].closed
        listen = repr(conns[0].executed[0])
        assert "LISTEN" in listen and f"Identifier('{ASSIGNMENTS_CHANNEL}')" in listen

    async def test_errors_retry_instead_of_raising(self):
        conns = [_ListenConn([], fail_listen=True), _ListenConn(["x"])]
        it = iter(conns)
        attempts = []

        async def connect():
            attempts.append(1)
            if len(attempts) == 1:
                raise psycopg.OperationalError("refused")
            return next(it)
        watcher = PostgresChannelWatcher("dsn", "chan", notify_timeout_s=5, retry_s=0,
                                         connect=connect)
        gen = watcher.watch()
        assert [await gen.__anext__() for _ in range(2)] == [None, "x"]
        await gen.aclose()
        assert len(attempts) == 3 and conns[0].closed
