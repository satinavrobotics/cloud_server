"""WP8 recording policy, API side (docs/satinav-fleet-agent-phase0-v2.md §4, §7 WP8).

- the optional `telemetry_recording` field on the robot spec and the global settings;
- 422 on an invalid value in the robot and settings routes, unchanged calls without it;
- TELEMETRY.RECORDING_CHANGED written by the before-commit hook (packages/api/recording.py)
  in the route's transaction, isolated in a savepoint;
- PostgresDatabase create_object/update_spec running that hook on the same connection;
- the API writer's policy following robot/settings objects from the NOTIFY watchers, and
  what each level lets through (events, diagnostics_ts, robot_latest, RECORDING_CHANGED).
"""
import datetime
import json
import os

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

from unittest.mock import AsyncMock, MagicMock, patch

import pydantic
import pytest
from fastapi import HTTPException

import packages.api.main as main
from cloud_common.objects.common import TELEMETRY_RECORDING_LEVELS
from cloud_common.objects.object import ObjectLifecycleV1
from cloud_common.objects.robot import RobotObjectV1, RobotSpecV1
from cloud_common.objects.settings import GLOBAL_SETTINGS_NAME, SettingsObjectV1
from packages.api import recording
from packages.events.codes import EventCode
from packages.events.schemas import RecordingLevel, RecordingScope
from packages.telemetry_ingest import tables
from tests.unit.telemetry_ingest.conftest import DataError, FakeConnection, FakeDatabase
from tests.unit.test_api_telemetry import (  # noqa: F401 - fixtures used implicitly
    LockServer, _stop_created, diag, epoch, flush, make_telemetry, set_level,
)

T0 = datetime.datetime(2026, 9, 24, 12, 0, tzinfo=datetime.timezone.utc)


# --- models --------------------------------------------------------------------------------

@pytest.mark.unit
class TestModels:
    def test_levels_match_the_events_enum(self):
        assert set(TELEMETRY_RECORDING_LEVELS) == {level.value for level in RecordingLevel}

    @pytest.mark.parametrize("level", [None, "full", "events_only", "off"])
    def test_robot_accepts_each_level(self, level):
        robot = RobotObjectV1(name="r1", status={}, telemetry_recording=level)
        assert robot.telemetry_recording == level
        assert json.loads(robot.spec.json())["telemetry_recording"] == level

    @pytest.mark.parametrize("level", [None, "full", "events_only", "off"])
    def test_settings_accepts_each_level(self, level):
        settings = SettingsObjectV1(name=GLOBAL_SETTINGS_NAME, telemetry_recording=level)
        assert json.loads(settings.spec.json())["telemetry_recording"] == level

    @pytest.mark.parametrize("bad", ["FULL", "everything", 1, ""])
    def test_invalid_values_are_rejected(self, bad):
        with pytest.raises(pydantic.ValidationError):
            RobotObjectV1(name="r1", status={}, telemetry_recording=bad)
        with pytest.raises(pydantic.ValidationError):
            SettingsObjectV1(name=GLOBAL_SETTINGS_NAME, telemetry_recording=bad)

    def test_stored_objects_without_the_field_still_load(self):
        """Rows written before WP8 have no telemetry_recording key: None = inherit."""
        stored_spec = json.loads(RobotSpecV1().json())
        stored_spec.pop("telemetry_recording")
        robot = RobotObjectV1(name="old", lifecycle="ALIVE", status={}, **stored_spec)
        assert robot.telemetry_recording is None
        settings = SettingsObjectV1(name=GLOBAL_SETTINGS_NAME, lifecycle="ALIVE", status={},
                                    fault_error_types=["x"])
        assert settings.telemetry_recording is None

    def test_defaults_are_unset(self):
        assert RobotObjectV1.default_spec()["telemetry_recording"] is None
        assert SettingsObjectV1.default_spec()["telemetry_recording"] is None


# --- check_level ---------------------------------------------------------------------------

@pytest.mark.unit
class TestCheckLevel:
    @pytest.mark.parametrize("data", [{}, {"labels": []}, {"telemetry_recording": None},
                                      {"telemetry_recording": "full"},
                                      {"telemetry_recording": "events_only"},
                                      {"telemetry_recording": "off"}])
    def test_accepts(self, data):
        recording.check_level(data)

    @pytest.mark.parametrize("bad", ["FULL", "all", 3, True, ["full"], ""])
    def test_rejects_with_422(self, bad):
        with pytest.raises(HTTPException) as exc:
            recording.check_level({"telemetry_recording": bad})
        assert exc.value.status_code == 422
        assert exc.value.detail[0]["loc"] == ["body", "telemetry_recording"]

    def test_no_actor_without_authentication(self):
        assert recording.request_actor() is None
        assert recording.request_actor(MagicMock(headers={"X-User": "mallory"})) is None


# --- routes --------------------------------------------------------------------------------

def _svc(robot=None, settings=None):
    svc = MagicMock()
    stored = {}
    if robot is not None:
        stored[("robot", robot.name)] = robot
    if settings is not None:
        stored[("settings", settings.name)] = settings

    async def get_object(cls, name):
        obj = stored.get((cls.get_alias(), name))
        if obj is None:
            raise HTTPException(404, "missing")
        return cls(**json.loads(obj.json()))
    svc.database.get_object = AsyncMock(side_effect=get_object)

    async def update_spec(cls, name, spec, publisher_id, **kwargs):
        obj = stored[(cls.get_alias(), name)]
        stored[(cls.get_alias(), name)] = cls(name=name, lifecycle=obj.lifecycle,
                                              status=obj.status, **json.loads(spec.json()))
    svc.database.update_spec = AsyncMock(side_effect=update_spec)
    svc.database.create_object = AsyncMock()
    svc.database.update_status = AsyncMock()
    return svc


@pytest.mark.unit
class TestRobotRoutes:
    async def test_put_invalid_level_is_422_and_writes_nothing(self):
        svc = _svc(RobotObjectV1(name="r1", status={}))
        with patch.object(main, "service", svc):
            with pytest.raises(HTTPException) as exc:
                await main.update_robot("r1", {"telemetry_recording": "loud"})
        assert exc.value.status_code == 422
        svc.database.update_spec.assert_not_awaited()

    async def test_put_level_passes_the_hook_and_returns_the_field(self):
        svc = _svc(RobotObjectV1(name="r1", status={}))
        with patch.object(main, "service", svc):
            result = await main.update_robot("r1", {"telemetry_recording": "full"})
        assert result["telemetry_recording"] == "full"
        kwargs = svc.database.update_spec.call_args.kwargs
        assert callable(kwargs["before_commit"])

    async def test_put_back_to_inherit(self):
        svc = _svc(RobotObjectV1(name="r1", status={}, telemetry_recording="off"))
        with patch.object(main, "service", svc):
            result = await main.update_robot("r1", {"telemetry_recording": None})
        assert result["telemetry_recording"] is None
        assert "before_commit" in svc.database.update_spec.call_args.kwargs

    async def test_put_without_the_field_is_unchanged(self):
        """Existing clients: same update_spec call as before WP8, level untouched."""
        svc = _svc(RobotObjectV1(name="r1", status={}, telemetry_recording="full"))
        with patch.object(main, "service", svc):
            result = await main.update_robot("r1", {"labels": ["a"]})
        assert svc.database.update_spec.call_args.kwargs == {}
        assert result["labels"] == ["a"] and result["telemetry_recording"] == "full"
        assert set(result) == set(RobotObjectV1(name="x", status={}).dict())

    async def test_status_only_put_does_not_touch_the_spec(self):
        svc = _svc(RobotObjectV1(name="r1", status={}))
        with patch.object(main, "service", svc):
            await main.update_robot("r1", {"status": {"online": True}})
        svc.database.update_spec.assert_not_awaited()

    async def test_register_new_robot_with_level_passes_the_hook(self):
        svc = _svc()
        with patch.object(main, "service", svc):
            result = await main.create_robot({"name": "r9", "telemetry_recording": "off"})
        assert result["telemetry_recording"] == "off"
        assert "before_commit" in svc.database.create_object.call_args.kwargs

    async def test_register_without_level_is_unchanged(self):
        svc = _svc()
        with patch.object(main, "service", svc):
            result = await main.create_robot({"name": "r9"})
        assert result["telemetry_recording"] is None
        assert svc.database.create_object.call_args.kwargs == {}

    async def test_register_invalid_level_is_422(self):
        svc = _svc()
        with patch.object(main, "service", svc):
            with pytest.raises(HTTPException) as exc:
                await main.create_robot({"name": "r9", "telemetry_recording": "most"})
        assert exc.value.status_code == 422
        svc.database.create_object.assert_not_awaited()


@pytest.mark.unit
class TestSettingsRoutes:
    async def test_put_invalid_level_is_422(self):
        svc = _svc(settings=SettingsObjectV1(name=GLOBAL_SETTINGS_NAME))
        with patch.object(main, "service", svc):
            with pytest.raises(HTTPException) as exc:
                await main.update_settings({"telemetry_recording": "verbose"})
        assert exc.value.status_code == 422
        svc.database.update_spec.assert_not_awaited()

    async def test_put_level_passes_the_hook(self):
        svc = _svc(settings=SettingsObjectV1(name=GLOBAL_SETTINGS_NAME))
        with patch.object(main, "service", svc):
            result = await main.update_settings({"telemetry_recording": "full"})
        assert result["telemetry_recording"] == "full"
        assert "before_commit" in svc.database.update_spec.call_args.kwargs

    async def test_put_without_the_field_is_unchanged(self):
        svc = _svc(settings=SettingsObjectV1(name=GLOBAL_SETTINGS_NAME,
                                             telemetry_recording="off"))
        with patch.object(main, "service", svc):
            result = await main.update_settings({"fault_error_types": ["e"]})
        assert svc.database.update_spec.call_args.kwargs == {}
        assert result["fault_error_types"] == ["e"] and result["telemetry_recording"] == "off"

    async def test_get_shows_the_field(self):
        svc = _svc(settings=SettingsObjectV1(name=GLOBAL_SETTINGS_NAME))
        with patch.object(main, "service", svc):
            result = await main.get_settings()
        assert "telemetry_recording" in result and result["telemetry_recording"] is None


# --- RECORDING_CHANGED ---------------------------------------------------------------------

def _db(global_level=None, robots=None, sites=None, assignments=None):
    db = FakeDatabase()
    db.query_results = {
        r"to_regclass": lambda sql, params: [(params[0] != "siteobjectv1" or sites is not None,)],
        r"FROM robotobjectv1": list((robots or {}).items()),
        r"FROM siteobjectv1": list((sites or {}).items()),
        r"FROM robot_site_assignments": list((assignments or {}).items()),
        r"FROM settingsobjectv1": [(global_level,)],
    }
    return db


class _EmitConnection(FakeConnection):
    """FakeConnection whose cursors report rowcount, which emit() reads."""

    def cursor(self):
        cursor = super().cursor()
        cursor.rowcount = 1
        return cursor


async def _record(db, *args, **kwargs):
    conn = _EmitConnection(db)
    return await recording.record_change(conn, *args, ts=T0, **kwargs)


def _payloads(db):
    return [(row["robot_name"], row["site_id"], row["code"], row["source"], row["severity"],
             json.loads(row["payload"])) for row in db.events.values()]


@pytest.mark.unit
class TestRecordChange:
    @pytest.fixture(autouse=True)
    def _reset_stats(self):
        recording.stats.update(written=0, failed=0)

    async def test_robot_level_set(self):
        db = _db(global_level="events_only")
        assert await _record(db, RecordingScope.ROBOT, "r1", None, "full", None)
        assert _payloads(db) == [("r1", None, EventCode.TELEMETRY_RECORDING_CHANGED.value,
                                  "api", "info",
                                  {"old_level": "events_only", "new_level": "full",
                                   "scope": "robot", "scope_id": "r1", "actor": None})]
        assert recording.stats == {"written": 1, "failed": 0}

    async def test_robot_back_to_inherit_resolves_through_site_and_global(self):
        db = _db(global_level="off", sites={"s1": "full"}, assignments={"r1": "s1"})
        assert await _record(db, RecordingScope.ROBOT, "r1", "events_only", None, "ops")
        ((robot, site, *_, payload),) = _payloads(db)
        assert (robot, site) == ("r1", "s1")
        assert payload == {"old_level": "events_only", "new_level": "full", "scope": "robot",
                           "scope_id": "r1", "actor": "ops"}

    async def test_no_site_resolves_to_global(self):
        """No siteobjectv1 table and no assignment (the state until WP9)."""
        db = _db(global_level="full")
        assert await _record(db, RecordingScope.ROBOT, "r1", "off", None, None)
        ((_, site, *_, payload),) = _payloads(db)
        assert site is None and payload["new_level"] == "full"

    async def test_global_level(self):
        db = _db()
        assert await _record(db, RecordingScope.GLOBAL, None, None, "off", None)
        ((robot, site, *_, payload),) = _payloads(db)
        assert (robot, site) == (None, None)
        assert payload == {"old_level": "events_only", "new_level": "off", "scope": "global",
                           "scope_id": None, "actor": None}

    async def test_written_at_level_off(self):
        """No level gate on this code: both old and new being `off` elsewhere is irrelevant."""
        db = _db(global_level="off")
        assert await _record(db, RecordingScope.ROBOT, "r1", "full", "off", None)
        assert len(db.events) == 1

    @pytest.mark.parametrize("old,new", [(None, None), ("full", "full"), ("bogus", None)])
    async def test_no_change_no_event(self, old, new):
        db = _db()
        assert not await _record(db, RecordingScope.ROBOT, "r1", old, new, None)
        assert db.events == {} and db.statements == []

    async def test_failure_is_contained(self):
        db = _db()
        db.fail = lambda sql, params: DataError("fleet_events missing") \
            if "fleet_events" in sql else None
        assert not await _record(db, RecordingScope.GLOBAL, None, None, "full", None)
        assert db.events == {} and db.rollbacks == 1
        assert recording.stats == {"written": 0, "failed": 1}

    async def test_hook_reads_the_specs(self):
        db = _db()
        hook = recording.change_hook(RecordingScope.ROBOT, "r1", None)
        conn = _EmitConnection(db)
        assert await hook(conn, {"labels": []}, {"telemetry_recording": "full"})
        assert not await hook(conn, {"telemetry_recording": "full"},
                              {"telemetry_recording": "full"})
        assert await hook(conn, None, {"telemetry_recording": "off"})  # create
        assert len(db.events) == 2


# --- PostgresDatabase hooks ----------------------------------------------------------------

class _Cursor:
    def __init__(self, log, old_spec):
        self.log, self.old_spec, self.rowcount = log, old_spec, 1
        self._last = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, params=None):
        self.log.append(("sql", sql.split()[0], sql))
        self._last = sql

    async def fetchone(self):
        if "FOR UPDATE" in self._last:
            return (self.old_spec,)
        return ("r1", "ALIVE", {}, {})


class _Conn:
    def __init__(self, log, old_spec):
        self.log, self.old_spec = log, old_spec

    def cursor(self):
        return _Cursor(self.log, self.old_spec)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.log.append(("commit",))
        return False


def _pg(old_spec=None):
    from packages.database.postgres import PostgresDatabase
    db = PostgresDatabase(dbname="t", user="u", password="p", host="h", port=5432)
    log = []
    conn = _Conn(log, old_spec)
    db._pool = MagicMock()
    db._pool.connection = MagicMock(return_value=conn)

    async def notify(cursor, table, name, lifecycle, publisher_id):
        log.append(("notify", table, name))
    db._notify = notify
    return db, log, conn


@pytest.mark.unit
class TestPostgresHooks:
    async def test_update_spec_runs_the_hook_after_the_write_before_commit(self):
        db, log, conn = _pg(old_spec={"telemetry_recording": "off"})
        seen = []

        async def hook(c, old, new):
            seen.append((c, old, new))
            log.append(("hook",))
        robot = RobotObjectV1(name="r1", status={}, telemetry_recording="full")
        await db.update_spec(RobotObjectV1, "r1", robot.spec, "pub", before_commit=hook)
        kinds = [entry[1] if entry[0] == "sql" else entry[0] for entry in log]
        assert kinds == ["SELECT", "UPDATE", "notify", "hook", "commit"]
        assert "FOR UPDATE" in log[0][2]
        c, old, new = seen[0]
        assert c is conn and old == {"telemetry_recording": "off"}
        assert new["telemetry_recording"] == "full" and new == json.loads(robot.spec.json())

    async def test_update_spec_without_hook_is_unchanged(self):
        db, log, _ = _pg()
        await db.update_spec(RobotObjectV1, "r1", RobotObjectV1(name="r1", status={}).spec,
                             "pub")
        kinds = [entry[1] if entry[0] == "sql" else entry[0] for entry in log]
        assert kinds == ["UPDATE", "notify", "commit"]

    async def test_failing_hook_never_fails_the_write(self):
        db, log, _ = _pg()

        async def hook(c, old, new):
            raise RuntimeError("boom")
        await db.update_spec(RobotObjectV1, "r1", RobotObjectV1(name="r1", status={}).spec,
                             "pub", before_commit=hook)
        assert log[-1] == ("commit",)

    async def test_create_object_runs_the_hook_with_no_old_spec(self):
        db, log, conn = _pg()
        seen = []

        async def hook(c, old, new):
            seen.append((old, new["telemetry_recording"]))
            log.append(("hook",))
        await db.create_object(RobotObjectV1(name="r1", status={}, telemetry_recording="off"),
                               "pub", before_commit=hook)
        kinds = [entry[1] if entry[0] == "sql" else entry[0] for entry in log]
        assert kinds == ["INSERT", "notify", "hook", "commit"]
        assert seen == [(None, "off")]


# --- the API writer's policy follows NOTIFYs; what each level writes -----------------------

def _robot(name="r1", level=None, deleted=False):
    return RobotObjectV1(name=name, status={}, telemetry_recording=level,
                         lifecycle=ObjectLifecycleV1.DELETED if deleted
                         else ObjectLifecycleV1.ALIVE)


def _settings(level=None, name=GLOBAL_SETTINGS_NAME):
    return SettingsObjectV1(name=name, telemetry_recording=level)


async def _burst(tel, t0):
    """One thermal transition (-> SYSTEM.THERMAL_HIGH) with its rows, then flush."""
    tel.on_diagnostics("r1", epoch(t0), diag(temp=60.0))
    tel.on_diagnostics("r1", epoch(t0 + 1), diag(temp=90.0))
    tel.on_diagnostics("r1", epoch(t0 + 2), diag(temp=70.0))
    await flush(tel)


def _counts(db):
    return (sorted(r["code"] for r in db.events.values()),
            len(db.timeseries[tables.DIAGNOSTICS_TABLE]), "r1" in db.latest)


@pytest.mark.unit
class TestApiLevels:
    @pytest.mark.parametrize("level,events,rows", [
        ("full", ["SYSTEM.THERMAL_HIGH", "SYSTEM.THERMAL_OK"], 3),
        ("events_only", ["SYSTEM.THERMAL_HIGH", "SYSTEM.THERMAL_OK"], 0),
        ("off", [], 0),
    ])
    async def test_each_level(self, tmp_path, level, events, rows):
        server = LockServer()
        set_level(server.db, level)
        tel = make_telemetry(server, tmp_path)
        await tel.election.step()
        await _burst(tel, 0)
        assert _counts(server.db) == (events, rows, True)  # robot_latest at every level

    async def test_unset_everywhere_is_events_only(self, tmp_path):
        server = LockServer()
        set_level(server.db, None)
        tel = make_telemetry(server, tmp_path)
        await tel.election.step()
        await _burst(tel, 0)
        assert _counts(server.db) == (["SYSTEM.THERMAL_HIGH", "SYSTEM.THERMAL_OK"], 0, True)

    async def test_recording_changed_passes_the_gate_at_off(self, tmp_path):
        from packages.events.emit import Event
        server = LockServer()
        set_level(server.db, "off")
        tel = make_telemetry(server, tmp_path)
        await tel.election.step()
        tel._term.queue.put_event(Event(
            EventCode.TELEMETRY_RECORDING_CHANGED, T0, robot_name="r1", discriminator="x",
            payload={"old_level": "full", "new_level": "off", "scope": "robot"}))
        await flush(tel)
        assert [r["code"] for r in server.db.events.values()] == [
            "TELEMETRY.RECORDING_CHANGED"]

    async def test_switching_without_restart(self, tmp_path):
        server = LockServer()
        set_level(server.db, "events_only")
        tel = make_telemetry(server, tmp_path)
        await tel.election.step()

        tel.on_robot_object(_robot(level="full"))           # robot override -> full
        await _burst(tel, 0)
        assert _counts(server.db)[1] == 3

        tel.on_settings_object(_settings("off"))            # global off, robot still full
        await _burst(tel, 10)
        assert _counts(server.db)[1] == 6

        tel.on_robot_object(_robot(level=None))             # back to inherit -> off
        before = len(server.db.events)
        await _burst(tel, 20)
        assert _counts(server.db)[1] == 6 and len(server.db.events) == before

        tel.on_settings_object(_settings("events_only"))    # global events_only
        await _burst(tel, 30)
        assert _counts(server.db)[1] == 6 and len(server.db.events) == before + 2

        tel.on_robot_object(_robot(level="off"))
        tel.on_robot_object(_robot(deleted=True))           # deleted: override forgotten
        assert tel._term.policy.level_for("r1") is RecordingLevel.EVENTS_ONLY

    async def test_other_settings_rows_and_non_writers_are_ignored(self, tmp_path):
        server = LockServer()
        set_level(server.db, "events_only")
        tel = make_telemetry(server, tmp_path)
        tel.on_settings_object(_settings("off"))            # not the writer yet: no-op
        await tel.election.step()
        tel.on_settings_object(_settings("off", name="other"))
        assert tel._term.policy.level_for("r1") is RecordingLevel.EVENTS_ONLY
        tel.on_robot_object(object())                        # garbage: logged, not raised
        assert tel.handler_errors.count == 1

    async def test_term_start_reloads_once_more(self, tmp_path):
        server = LockServer()
        set_level(server.db, "full")
        tel = make_telemetry(server, tmp_path)
        await tel.election.step()
        assert tel._term.policy.loaded and tel._term.policy.stale
