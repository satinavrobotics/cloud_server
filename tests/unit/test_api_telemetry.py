"""Unit tests for packages/api/telemetry.py: advisory-lock writer election, the writer term
(level gating, rehydration, failover) and failure isolation in DiagnosticsService.

A fake lock server stands in for pg_try_advisory_lock; the database behind the writer is the
FakeDatabase/FakePool of tests/unit/telemetry_ingest/conftest.py.
"""

import asyncio
import datetime
import hashlib
import json
import os
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from packages.api import telemetry as telemetry_mod
from packages.api.diagnostics import DiagnosticsService
from packages.api.telemetry import (
    ApiTelemetry, WRITER_LOCK_KEY, WriterElection, adopt_orphan_spills,
)
from packages.events import schemas
from packages.events.codes import EventCode
from packages.events.emit import Event, build_row
from packages.telemetry_ingest import SpillFile, tables
from tests.unit.telemetry_ingest.conftest import FakeConnection, FakeDatabase, FakePool

pytestmark = pytest.mark.unit

UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 9, 24, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _strict_payloads():
    previous = schemas.strict_validation()
    schemas.set_strict_validation(True)
    yield
    schemas.set_strict_validation(previous)


# --- fakes ---------------------------------------------------------------------------------

class LockServer:
    """Session-level advisory locks: released when the owning connection closes or dies."""

    def __init__(self, db=None):
        self.db = db or FakeDatabase()
        self.owner = {}          # key -> connection
        self.down = False        # new connections fail
        self.connections = []

    def kill(self, conn):
        """The backend of `conn` goes away (process killed, network cut)."""
        conn.dead = True
        self._release(conn)

    def _release(self, conn):
        for key, owner in list(self.owner.items()):
            if owner is conn:
                del self.owner[key]


class _Result:
    def __init__(self, row):
        self.row = row

    async def fetchone(self):
        return self.row


class LockConn(FakeConnection):
    def __init__(self, server):
        super().__init__(server.db)
        self.server = server
        self.dead = False

    async def execute(self, sql, params=None):
        if self.dead:
            self.broken = True
            raise ConnectionError("server closed the connection unexpectedly")
        if sql.startswith("SELECT pg_try_advisory_lock"):
            key = params[0]
            owner = self.server.owner.get(key)
            if owner is None:
                self.server.owner[key] = self
            return _Result((self.server.owner[key] is self,))
        if sql == "SELECT 1":
            return _Result((1,))
        raise AssertionError(sql)

    async def close(self):
        self.closed = True
        self.server._release(self)


def connector(server):
    async def connect():
        if server.down:
            raise OSError("connection refused")
        conn = LockConn(server)
        server.connections.append(conn)
        return conn
    return connect


async def _forever(_):
    await asyncio.Event().wait()


def latest_rows(db):
    """robot_latest as load_latest() reads it back from FakeDatabase."""
    def rows(sql, params):
        out = []
        for robot, cols in db.latest.items():
            out.append((robot, cols.get("state_msg"), cols.get("diagnostics"),
                        cols.get("nav_supervisor"), cols.get("active_run_id"),
                        cols.get("site_id"), cols.get("sw_version"), cols.get("last_seen"),
                        None))
        return out
    return rows


def set_level(db, level):
    """Global recording level as RecordingPolicy.refresh() loads it."""
    db.query_results[r"to_regclass"] = lambda sql, params: [(params[0] == "settingsobjectv1",)]
    db.query_results[r"FROM settingsobjectv1"] = [(level,)]
    db.query_results[r"FROM robot_latest"] = latest_rows(db)


_CREATED = []


@pytest.fixture(autouse=True)
async def _stop_created():
    yield
    while _CREATED:
        await _CREATED.pop().stop()


def make_telemetry(server, tmp_path, now=None):
    clock = {"t": 0.0}
    tel = ApiTelemetry(
        "host=unused", str(tmp_path / "spill"), connect=connector(server),
        pool_factory=lambda dsn: _pool(server.db), sleep=_forever,
        now=now or (lambda: T0), monotonic=lambda: clock["t"],
        writer_kwargs={"sleep": _forever})
    tel.clock = clock
    _CREATED.append(tel)
    return tel


async def _pool(db):
    pool = FakePool(db)
    pool.close = AsyncMock()
    return pool


def epoch(seconds):
    return (T0 + datetime.timedelta(seconds=seconds)).timestamp()


def diag(temp=60.0, stale=()):
    return {
        "jtop": {"level": 0, "values": {"gpu_percent": 10, "cpu_temp_c": temp,
                                        "power_total_mw": 5000}},
        "host_stats": {"level": 0, "values": {"cpu_percent": 20.0, "ram_percent": 50.0}},
        "ros_health": {"level": 0, "values": {f"{s}_stale": s in stale
                                              for s in ("esp32", "gps", "sati_pose")}},
    }


async def flush(tel):
    assert await tel._term.writer.flush_once()


def event_codes(db):
    return sorted((row["code"], row["ts"]) for row in db.events.values())


# --- lock key ------------------------------------------------------------------------------

def test_lock_key_matches_the_migrations_lock_derivation():
    expected = int.from_bytes(hashlib.sha256(b"telemetry_writer").digest()[:8], "big",
                              signed=True)
    assert WRITER_LOCK_KEY == expected
    from packages.api.entrypoint import MIGRATION_LOCK_KEY
    assert WRITER_LOCK_KEY != MIGRATION_LOCK_KEY


# --- election ------------------------------------------------------------------------------

class Recorder:
    def __init__(self, fail=False):
        self.acquired = 0
        self.lost = 0
        self.fail = fail

    async def on_acquired(self, conn):
        self.acquired += 1
        if self.fail:
            raise RuntimeError("boom")

    async def on_lost(self):
        self.lost += 1


def election(server, rec):
    return WriterElection(connector(server), rec.on_acquired, rec.on_lost, sleep=_forever)


class TestElection:
    async def test_only_one_acquires(self):
        server = LockServer()
        ra, rb = Recorder(), Recorder()
        a, b = election(server, ra), election(server, rb)
        await a.step()
        await b.step()
        await b.step()
        assert (a.holding, b.holding) == (True, False)
        assert (ra.acquired, rb.acquired) == (1, 0)
        await a.step()  # still holding: health check only
        assert a.holding and ra.acquired == 1

    async def test_takeover_after_the_holder_dies(self):
        server = LockServer()
        ra, rb = Recorder(), Recorder()
        a, b = election(server, ra), election(server, rb)
        await a.step()
        await b.step()
        server.kill(a._conn)            # the holder's backend is gone: lock released
        await b.step()
        assert b.holding and rb.acquired == 1
        await a.step()                  # the old holder notices on its next check
        assert not a.holding and ra.lost == 1
        await a.step()                  # and from then on is a normal candidate
        assert not a.holding and server.owner[a.key] is b._conn

    async def test_lose_when_health_check_fails_releases_everything(self):
        server = LockServer()
        rec = Recorder()
        a = election(server, rec)
        await a.step()
        a._conn.dead = True             # connection broken but the server has not noticed
        await a.step()
        assert not a.holding and rec.lost == 1
        assert a._conn is None and server.owner == {}

    async def test_postgres_down_is_retried_without_raising(self):
        server = LockServer()
        server.down = True
        rec = Recorder()
        a = election(server, rec)
        await a.step()
        await a.step()
        assert not a.holding and a.errors == 2
        server.down = False
        await a.step()
        assert a.holding and rec.acquired == 1

    async def test_failed_start_releases_the_lock(self):
        server = LockServer()
        a, b = election(server, Recorder(fail=True)), election(server, Recorder())
        await a.step()
        assert not a.holding and server.owner == {}
        await b.step()
        assert b.holding

    async def test_stop_releases_the_lock(self):
        server = LockServer()
        rec = Recorder()
        a = election(server, rec)
        a.start()
        for _ in range(100):
            if a.holding:
                break
            await asyncio.sleep(0)
        assert a.holding
        await a.stop()
        assert not a.holding and rec.lost == 1 and server.owner == {}

    async def test_hung_query_counts_as_lost(self, monkeypatch):
        server = LockServer()
        rec = Recorder()
        a = WriterElection(connector(server), rec.on_acquired, rec.on_lost, sleep=_forever,
                           query_timeout_s=0.01)
        await a.step()

        async def hang(sql, params=None):
            await asyncio.Event().wait()
        a._conn.execute = hang
        await a.step()
        assert not a.holding and rec.lost == 1


# --- the writer term -----------------------------------------------------------------------

class TestWriterTerm:
    async def test_non_writer_writes_nothing(self, tmp_path):
        server = LockServer()
        set_level(server.db, "full")
        server.owner[WRITER_LOCK_KEY] = object()   # someone else holds the lock
        tel = make_telemetry(server, tmp_path)
        await tel.election.step()
        assert not tel.is_writer
        tel.on_diagnostics("r1", epoch(0), diag())
        tel.on_nav_supervisor("r1", {"state": "DRIVE"})
        assert server.db.events == {} and server.db.latest == {}
        assert server.db.timeseries[tables.DIAGNOSTICS_TABLE] == []
        assert tel.snapshot()["is_writer"] is False

    @pytest.mark.parametrize("level, series, events", [
        ("full", 2, 1), ("events_only", 0, 1), ("off", 0, 0)])
    async def test_level_gating(self, tmp_path, level, series, events):
        server = LockServer()
        set_level(server.db, level)
        tel = make_telemetry(server, tmp_path)
        await tel.election.step()
        assert tel.is_writer
        tel.on_diagnostics("r1", epoch(0), diag(temp=60.0))
        tel.on_diagnostics("r1", epoch(1), diag(temp=90.0))
        await flush(tel)
        assert len(server.db.timeseries[tables.DIAGNOSTICS_TABLE]) == series
        assert len(server.db.events) == events
        # robot_latest is written at every level (detectors need it to rehydrate)
        assert "diagnostics" in server.db.latest["r1"]
        if series:
            row = dict(zip(tables.DIAGNOSTICS_COLUMNS,
                           server.db.timeseries[tables.DIAGNOSTICS_TABLE][1]))
            assert row["temp_max"] == 90.0 and row["power_w"] == 5.0
            assert row["gnss_fix"] is None and row["gnss_sats"] is None
        if events:
            (row,) = server.db.events.values()
            assert row["code"] == "SYSTEM.THERMAL_HIGH" and row["source"] == "api"
            assert row["ts"] == T0 + datetime.timedelta(seconds=1)

    async def test_nav_supervisor_writes_events_and_latest_but_no_series(self, tmp_path):
        server = LockServer()
        set_level(server.db, "full")
        tel = make_telemetry(server, tmp_path)
        await tel.election.step()
        stamp = lambda s: {"sec": int(epoch(s)), "nanosec": 0}  # noqa: E731
        tel.on_nav_supervisor("r1", {"state": "DRIVE", "stamp": stamp(0)})
        tel.on_nav_supervisor("r1", {"state": "RECOVER", "last_drive_cause": "FROZEN",
                                     "stamp": stamp(2)})
        tel.on_nav_supervisor("r1", {"state": "DRIVE", "stamp": stamp(7)})
        await flush(tel)
        rows = sorted(server.db.events.values(), key=lambda r: r["ts"])
        assert [r["code"] for r in rows] == ["NAV.RECOVERY_ENTERED", "NAV.RECOVERY_EXITED"]
        assert json.loads(rows[1]["payload"]) == {"cause": "FROZEN", "duration_s": 5.0}
        assert server.db.timeseries == {tables.ROBOT_STATE_TABLE: [], tables.DIAGNOSTICS_TABLE: []}
        assert "nav_supervisor" in server.db.latest["r1"]
        assert set(server.db.latest["r1"]) == {"nav_supervisor"}  # only the API's column

    async def test_event_context_from_dispatch_columns(self, tmp_path):
        server = LockServer()
        set_level(server.db, "events_only")
        # WP9: the site is the robot's current assignment (via the recording policy), not
        # the robot_latest copy, which may lag behind an assignment change.
        server.db.query_results[r"to_regclass"] = lambda sql, params: [
            (params[0] in ("settingsobjectv1", "robot_site_assignments"),)]
        server.db.query_results[r"FROM robot_site_assignments"] = [("r1", "site-a")]
        run_id = uuid.uuid4()
        server.db.latest["r1"] = {"active_run_id": run_id, "site_id": "site-old",
                                  "sw_version": "jetson-2026.09+gabc"}
        tel = make_telemetry(server, tmp_path)
        await tel.election.step()
        tel.on_diagnostics("r1", epoch(0), diag(temp=60.0))
        tel.on_diagnostics("r1", epoch(1), diag(temp=90.0))
        await flush(tel)
        (row,) = server.db.events.values()
        assert (row["run_id"], row["site_id"], row["sw_version"]) == (
            run_id, "site-a", "jetson-2026.09+gabc")
        # periodic refresh picks up a new run
        new_run = uuid.uuid4()
        server.db.latest["r1"]["active_run_id"] = new_run
        tel.clock["t"] = 60.0
        await tel.election.step()
        assert tel._term.ctx.run_for("r1", T0) == new_run
        assert tel._term.policy.stale  # marked for the writer to reload

    async def test_rehydration_after_restart_emits_no_spurious_events(self, tmp_path):
        server = LockServer()
        set_level(server.db, "events_only")
        first = make_telemetry(server, tmp_path)
        await first.election.step()
        first.on_diagnostics("r1", epoch(0), diag(temp=60.0))
        first.on_diagnostics("r1", epoch(1), diag(temp=90.0, stale=("gps",)))
        first.on_nav_supervisor("r1", {"state": "DRIVE"})
        first.on_nav_supervisor("r1", {"state": "RECOVER", "last_drive_cause": "FROZEN"})
        await flush(first)
        assert len(server.db.events) == 3
        await first.stop()
        assert server.owner == {}

        second = make_telemetry(server, tmp_path)
        await second.election.step()
        assert second.is_writer
        second.on_diagnostics("r1", epoch(10), diag(temp=80.0, stale=("gps",)))
        second.on_nav_supervisor("r1", {"state": "RECOVER", "last_drive_cause": "FROZEN"})
        await flush(second)
        assert len(server.db.events) == 3           # nothing new happened
        second.on_diagnostics("r1", epoch(11), diag(temp=70.0))
        await flush(second)
        assert sorted(r["code"] for r in server.db.events.values()) == [
            "NAV.RECOVERY_ENTERED", "SYSTEM.NODE_DOWN", "SYSTEM.NODE_UP",
            "SYSTEM.THERMAL_HIGH", "SYSTEM.THERMAL_OK"]

    async def test_failover_between_two_workers_has_no_duplicates(self, tmp_path):
        """Both workers receive every MQTT message; only the elected one writes."""
        server = LockServer()
        set_level(server.db, "full")
        a, b = make_telemetry(server, tmp_path / "a"), make_telemetry(server, tmp_path / "b")
        await a.election.step()
        await b.election.step()
        assert (a.is_writer, b.is_writer) == (True, False)

        def publish(i, temp):
            for worker in (a, b):
                worker.on_diagnostics("r1", epoch(i), diag(temp=temp))

        publish(0, 60.0)
        publish(1, 90.0)
        await flush(a)
        server.kill(a.election._conn)     # worker A dies (its writes so far are committed)
        await b.election.step()
        assert b.is_writer
        publish(2, 91.0)
        publish(3, 70.0)
        await flush(b)
        assert event_codes(server.db) == [
            ("SYSTEM.THERMAL_HIGH", T0 + datetime.timedelta(seconds=1)),
            ("SYSTEM.THERMAL_OK", T0 + datetime.timedelta(seconds=3))]
        # samples 0 and 1 written by A, 2 and 3 by B: no gap, no duplicate
        assert len(server.db.timeseries[tables.DIAGNOSTICS_TABLE]) == 4
        # A notices, stops writing and becomes a candidate again
        await a.election.step()
        assert not a.is_writer

    async def test_same_event_from_two_writers_is_stored_once(self, tmp_path):
        """Overlap window: the old writer has not noticed yet that it lost the lock."""
        server = LockServer()
        set_level(server.db, "events_only")
        a, b = make_telemetry(server, tmp_path / "a"), make_telemetry(server, tmp_path / "b")
        await a.election.step()
        for worker in (a, b):
            worker.on_diagnostics("r1", epoch(0), diag(temp=60.0))
        server.kill(a.election._conn)
        await b.election.step()
        b._term.diagnostics_detector("r1").update(
            T0, diag(temp=60.0))  # b saw the same baseline as a
        for worker in (a, b):
            worker.on_diagnostics("r1", epoch(1), diag(temp=90.0))
        await flush(a)
        await flush(b)
        assert server.db.event_inserts == 2 and len(server.db.events) == 1


# --- failure isolation ---------------------------------------------------------------------

class TestIsolation:
    async def test_handler_exceptions_are_contained(self, tmp_path, monkeypatch):
        server = LockServer()
        set_level(server.db, "full")
        tel = make_telemetry(server, tmp_path)
        await tel.election.step()

        def boom(*a, **k):
            raise RuntimeError("detector bug")
        monkeypatch.setattr(tel._term, "diagnostics_detector", boom)
        monkeypatch.setattr(tel._term, "nav_detector", boom)
        tel.on_diagnostics("r1", epoch(0), diag())
        tel.on_nav_supervisor("r1", {"state": "DRIVE"})
        assert tel.handler_errors.count == 2

    async def test_queue_errors_are_contained(self, tmp_path, monkeypatch):
        server = LockServer()
        set_level(server.db, "full")
        tel = make_telemetry(server, tmp_path)
        await tel.election.step()
        monkeypatch.setattr(tel._term.queue, "put_diagnostics",
                            MagicMock(side_effect=ValueError("bad row")))
        tel.on_diagnostics("r1", epoch(0), diag())
        assert tel.handler_errors.count == 1

    async def test_diagnostics_service_caches_and_broadcasts_even_if_telemetry_raises(self):
        ws_manager = MagicMock()
        ws_manager.broadcast = AsyncMock()
        service = DiagnosticsService(mqtt_host="localhost", mqtt_port=1883, mqtt_keepalive=60,
                                     ws_manager=ws_manager)
        service.telemetry = MagicMock()
        service.telemetry.on_diagnostics.side_effect = RuntimeError("boom")
        service.telemetry.on_nav_supervisor.side_effect = RuntimeError("boom")
        await service._handle_diagnostics("r1", 1.0, {"jtop": {"level": 0, "values": {}}})
        await service._handle_nav_supervisor("r1", {"state": "DRIVE"})
        assert service.get_cached("r1")["diagnostics"] == {"jtop": {"level": 0, "values": {}}}
        assert service.get_cached_nav_supervisor("r1")["supervisor"] == {"state": "DRIVE"}
        assert ws_manager.broadcast.await_count == 2
        service.telemetry.on_diagnostics.assert_called_once_with(
            "r1", 1.0, {"jtop": {"level": 0, "values": {}}})

    async def test_diagnostics_service_without_telemetry_is_unchanged(self):
        ws_manager = MagicMock()
        ws_manager.broadcast = AsyncMock()
        service = DiagnosticsService(mqtt_host="localhost", mqtt_port=1883, mqtt_keepalive=60,
                                     ws_manager=ws_manager)
        assert service.telemetry is None
        await service._handle_diagnostics("r1", 1.0, {})
        assert service.get_cached("r1")["diagnostics"] == {}

    async def test_pool_failure_on_acquire_releases_lock_and_retries(self, tmp_path):
        server = LockServer()
        set_level(server.db, "full")
        tel = make_telemetry(server, tmp_path)

        async def broken_pool(dsn):
            raise OSError("cannot open pool")
        tel._pool_factory = broken_pool
        await tel.election.step()
        assert not tel.is_writer and server.owner == {}
        tel._pool_factory = lambda dsn: _pool(server.db)
        await tel.election.step()
        assert tel.is_writer

    async def test_stop_without_start_and_twice(self, tmp_path):
        tel = make_telemetry(LockServer(), tmp_path)
        await tel.stop()
        await tel.stop()

    def test_build_from_config_disabled(self, monkeypatch):
        import packages.config as config
        monkeypatch.setattr(config, "TELEMETRY_INGEST_ENABLED", False)
        assert telemetry_mod.build_from_config() is None

    def test_build_from_config_enabled(self, monkeypatch):
        import packages.config as config
        monkeypatch.setattr(config, "TELEMETRY_INGEST_ENABLED", True)
        tel = telemetry_mod.build_from_config()
        assert isinstance(tel, ApiTelemetry) and not tel.is_writer


# --- spill files ---------------------------------------------------------------------------

class TestOrphanSpills:
    def test_adopts_dead_workers_files_only(self, tmp_path):
        row = build_row(Event(EventCode.SYSTEM_THERMAL_HIGH, T0, robot_name="r1",
                              payload={"temp_c": 90.0, "threshold_c": 85.0}))
        dead = SpillFile(tmp_path / "api-999999.jsonl")
        dead.append([row])
        alive = SpillFile(tmp_path / f"api-{os.getppid()}.jsonl")
        alive.append([row])
        (tmp_path / "api-notapid.jsonl").write_text("x\n")
        own = SpillFile(tmp_path / f"api-{os.getpid()}.jsonl")
        assert adopt_orphan_spills(own, str(tmp_path), os.getpid()) == 1
        assert not (tmp_path / "api-999999.jsonl").exists()
        assert (tmp_path / f"api-{os.getppid()}.jsonl").exists()
        rows, _ = own.read(10)
        assert [r["event_id"] for r in rows] == [row["event_id"]]
