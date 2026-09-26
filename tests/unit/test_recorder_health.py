"""WP13 recorder health: alert rules, the evaluator (raise / clear / hysteresis / minimum
durations / stale), the monitor that writes the api row + alert events on the writer's lock
connection (failures, retries, restore after restart, spill at term end), and
GET /api/v1/health/recording through the real FastAPI app (ASGI, no lifespan).

The dispatch -> database -> API path on TimescaleDB is tests/integration/recorder_health.
"""
import datetime
import json
import os

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

from contextlib import asynccontextmanager  # noqa: E402
from unittest.mock import patch  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402

import packages.api.main as main  # noqa: E402
from packages.api import recorder_health as rh  # noqa: E402
from packages.api.telemetry import ApiTelemetry  # noqa: E402
from packages.events import ids  # noqa: E402
from packages.events.codes import EventCode  # noqa: E402
from packages.events.emit import INSERT_SQL, COLUMNS as EVENT_COLUMNS  # noqa: E402
from packages.telemetry_ingest import health  # noqa: E402

pytestmark = pytest.mark.unit

UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
TH = rh.Thresholds(stale_s=60, queue_pct=80, queue_clear_pct=60, spill_s=300,
                   sweep_factor=3, sweep_clear_factor=1.5, raise_s=10, clear_s=30)


def at(seconds: float) -> datetime.datetime:
    return T0 + datetime.timedelta(seconds=seconds)


def report(pct=0.0, pending=0, pending_age=None, lag=None, period=1.0, capacity=10000):
    out = {"queue": {"depth": int(capacity * pct / 100), "capacity": capacity, "pct": pct},
           "spill": {"pending": pending, "pending_age_s": pending_age},
           "dropped": {"total": 0, "by_table": {}}, "writer": {}}
    if lag is not None:
        out["heartbeat_sweep"] = {"period_s": period, "lag_s": lag,
                                  "last_completed_age_s": lag, "gap_max_s": 0.0}
    return out


def only(measures, alert):
    found = [m for m in measures if m.alert == alert]
    assert len(found) == 1, measures
    return found[0]


# --- rules --------------------------------------------------------------------------------------

class TestRules:
    def test_queue_thresholds_and_band(self):
        high = only(rh.measures_for("api", report(pct=85), TH), rh.ALERT_QUEUE)
        assert high.bad and not high.good and high.value == 85 and high.threshold == 80
        band = only(rh.measures_for("api", report(pct=70), TH), rh.ALERT_QUEUE)
        assert not band.bad and not band.good          # hysteresis band: hold
        low = only(rh.measures_for("api", report(pct=59.9), TH), rh.ALERT_QUEUE)
        assert low.good and not low.bad
        edge = only(rh.measures_for("api", report(pct=80), TH), rh.ALERT_QUEUE)
        assert not edge.bad                            # "> 80 %", not ">="

    def test_queue_without_capacity_is_not_measured(self):
        r = report()
        r["queue"]["pct"] = None
        assert not [m for m in rh.measures_for("api", r, TH) if m.alert == rh.ALERT_QUEUE]

    def test_spill_rule(self):
        young = only(rh.measures_for("api", report(pending=3, pending_age=299), TH),
                     rh.ALERT_SPILL)
        assert not young.bad and not young.good
        old = only(rh.measures_for("api", report(pending=3, pending_age=301), TH),
                   rh.ALERT_SPILL)
        assert old.bad and old.value == 301 and old.threshold == 300
        assert old.raise_after_s == 0                  # the age already is the duration
        empty = only(rh.measures_for("api", report(pending=0), TH), rh.ALERT_SPILL)
        assert empty.good and empty.value == 0

    def test_sweep_lag_rule_scales_with_period(self):
        m = only(rh.measures_for("dispatch", report(lag=3.5), TH), rh.ALERT_SWEEP)
        assert m.bad and m.threshold == 3.0
        assert only(rh.measures_for("dispatch", report(lag=2.9), TH), rh.ALERT_SWEEP).bad is False
        m = only(rh.measures_for("dispatch", report(lag=2.0), TH), rh.ALERT_SWEEP)
        assert not m.bad and not m.good                # between 1.5x and 3x: hold
        assert only(rh.measures_for("dispatch", report(lag=1.5), TH), rh.ALERT_SWEEP).good
        m = only(rh.measures_for("dispatch", report(lag=5.0, period=2.0), TH), rh.ALERT_SWEEP)
        assert not m.bad and m.threshold == 6.0
        assert not [m for m in rh.measures_for("api", report(), TH) if m.alert == rh.ALERT_SWEEP]

    def test_stale_skips_the_other_rules(self):
        ms = rh.measures_for("dispatch", report(pct=99, lag=10), TH, report_age_s=61,
                             check_stale=True)
        assert [m.alert for m in ms] == [rh.ALERT_STALE]
        assert ms[0].bad and ms[0].value == 61
        fresh = rh.measures_for("dispatch", report(pct=99, lag=10), TH, report_age_s=5,
                                check_stale=True)
        assert only(fresh, rh.ALERT_STALE).good
        assert only(fresh, rh.ALERT_QUEUE).bad and only(fresh, rh.ALERT_SWEEP).bad

    def test_missing_row_gets_grace(self):
        grace = rh.measures_for("dispatch", None, TH, check_stale=True, missing_for_s=30)
        assert [m.alert for m in grace] == [rh.ALERT_STALE] and not grace[0].bad
        late = rh.measures_for("dispatch", None, TH, check_stale=True, missing_for_s=61)
        assert late[0].bad and late[0].value is None


# --- evaluator ----------------------------------------------------------------------------------

def queue(pct):
    return rh.measures_for("api", report(pct=pct), TH)


class TestEvaluator:
    def test_raise_needs_min_duration(self):
        ev = rh.AlertEvaluator()
        assert ev.update(queue(90), at(0)) == []
        assert ev.update(queue(90), at(5)) == []
        [t] = ev.update(queue(90), at(10))
        assert t.raised and t.alert == rh.ALERT_QUEUE and t.process == "api"
        assert t.value == 90 and t.threshold == 80 and t.at == at(10)
        assert ev.update(queue(95), at(15)) == []      # exactly one RAISED
        assert [a["alert"] for a in ev.active()] == [rh.ALERT_QUEUE]

    def test_short_spike_never_raises(self):
        ev = rh.AlertEvaluator()
        ev.update(queue(90), at(0))
        ev.update(queue(50), at(5))                    # dipped: the window restarts
        assert ev.update(queue(90), at(9)) == []
        assert ev.update(queue(90), at(14)) == []
        assert len(ev.update(queue(90), at(19))) == 1

    def test_clear_needs_min_duration_and_hysteresis(self):
        ev = rh.AlertEvaluator()
        ev.update(queue(90), at(0))
        ev.update(queue(90), at(10))
        assert ev.update(queue(70), at(20)) == []      # band: holds, no clear window
        assert ev.update(queue(70), at(100)) == []
        assert ev.update(queue(50), at(110)) == []     # clear window starts
        assert ev.update(queue(85), at(120)) == []     # back up: window reset, no new RAISED
        assert ev.update(queue(50), at(130)) == []
        assert ev.update(queue(50), at(150)) == []
        [t] = ev.update(queue(50), at(160))
        assert not t.raised and t.raised_at == at(10) and t.at == at(160)
        assert ev.active() == []
        assert ev.update(queue(50), at(200)) == []     # exactly one CLEARED

    def test_flapping_around_threshold_emits_one_pair(self):
        ev = rh.AlertEvaluator()
        transitions = []
        for i, pct in enumerate([81, 79, 81, 79, 81, 79] * 20):
            transitions += ev.update(queue(pct), at(i * 5))
        # never 10 s continuously above 80 %: nothing is raised
        assert [t.raised for t in transitions] == []
        ev2 = rh.AlertEvaluator()
        transitions = []
        for i, pct in enumerate([90] * 3 + [59, 61] * 30):
            transitions += ev2.update(queue(pct), at(i * 5))
        assert [t.raised for t in transitions] == [True]   # 61 % holds, never 30 s below 60

    def test_windows_run_on_report_time(self):
        ev = rh.AlertEvaluator()

        def lagging(reported):
            return rh.measures_for("dispatch", report(lag=4.0), TH, report_age_s=1,
                                   check_stale=True, observed_at=reported)
        # the same stored report re-read for 30 s: one sample, never 10 s of evidence
        for s in range(0, 35, 5):
            assert ev.update(lagging(at(0)), at(s)) == []
        # two consecutive reports 10 s apart: raised
        [t] = ev.update(lagging(at(10)), at(40))
        assert t.alert == rh.ALERT_SWEEP and t.at == at(40)

    def test_spill_raises_at_once_when_old_enough(self):
        ev = rh.AlertEvaluator()
        young = rh.measures_for("dispatch", report(pending=2, pending_age=200), TH)
        assert ev.update(young, at(0)) == []
        old = rh.measures_for("dispatch", report(pending=2, pending_age=301), TH)
        [t] = [t for t in ev.update(old, at(101)) if t.alert == rh.ALERT_SPILL]
        assert t.raised and t.process == "dispatch"
        empty = rh.measures_for("dispatch", report(pending=0), TH)
        assert ev.update(empty, at(110)) == []
        [t] = ev.update(empty, at(140))
        assert not t.raised and t.alert == rh.ALERT_SPILL

    def test_sweep_lag_raise_and_clear(self):
        ev = rh.AlertEvaluator()
        lagging = rh.measures_for("dispatch", report(lag=4.0), TH)
        ev.update(lagging, at(0))
        assert [t.alert for t in ev.update(lagging, at(10))] == [rh.ALERT_SWEEP]
        ok = rh.measures_for("dispatch", report(lag=1.0), TH)
        ev.update(ok, at(20))
        assert [(t.alert, t.raised) for t in ev.update(ok, at(50))] == [(rh.ALERT_SWEEP, False)]

    def test_stale_raise_hold_and_clear(self):
        ev = rh.AlertEvaluator()
        fresh = lambda age, **kw: rh.measures_for(  # noqa: E731
            "dispatch", report(**kw), TH, report_age_s=age, check_stale=True)
        assert ev.update(fresh(5, pct=90), at(0)) == []
        [t] = ev.update(fresh(61, pct=90), at(5))     # stale: raise at once
        assert t.alert == rh.ALERT_STALE and t.value == 61
        # while stale, the queue rule holds (its 10 s window is not advanced by stale data)
        assert ev.update(fresh(120, pct=90), at(60)) == []
        assert ev.update(fresh(2, pct=0), at(70)) == []
        [t] = ev.update(fresh(2, pct=0), at(100))
        assert not t.raised and t.alert == rh.ALERT_STALE and t.raised_at == at(5)
        assert ev.active() == []

    def test_restore_prevents_second_raise(self):
        ev = rh.AlertEvaluator()
        n = ev.restore([{"alert": rh.ALERT_QUEUE, "process": "api", "value": 91,
                         "threshold": 80, "since": at(-100).isoformat()},
                        {"alert": "bogus", "process": "api"}, "junk"])
        assert n == 1
        assert ev.update(queue(95), at(0)) == [] and ev.update(queue(95), at(20)) == []
        ev.update(queue(10), at(30))
        [t] = ev.update(queue(10), at(60))
        assert not t.raised and t.raised_at == at(-100)

    def test_transition_event_rows(self):
        raised = rh.Transition(True, rh.ALERT_QUEUE, "api", 91.0, 80.0, at(10))
        cleared = rh.Transition(False, rh.ALERT_QUEUE, "api", 12.0, 80.0, at(70),
                                raised_at=at(10))
        from packages.events.emit import build_row
        r1, r2 = build_row(raised.event(), strict=True), build_row(cleared.event(), strict=True)
        assert r1["code"] == "SYSTEM.RECORDER_ALERT_RAISED" and r1["severity"] == "warning"
        assert r2["code"] == "SYSTEM.RECORDER_ALERT_CLEARED" and r2["severity"] == "info"
        assert r1["robot_name"] is None and r1["source"] == "api"
        assert r1["payload"] == {"alert": rh.ALERT_QUEUE, "process": "api", "value": 91.0,
                                 "threshold": 80.0, "raised_at": None, "duration_s": None}
        assert r2["payload"]["duration_s"] == 60.0
        assert r2["payload"]["raised_at"].startswith("2026-09-26T12:00:10")
        # deterministic: a retried write can't duplicate
        assert build_row(raised.event())["event_id"] == r1["event_id"]
        assert r1["event_id"] == ids.event_id(EventCode.SYSTEM_RECORDER_ALERT_RAISED, None,
                                              at(10), f"{rh.ALERT_QUEUE}|api")


# --- monitor on a fake lock connection ------------------------------------------------------------

class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._rows = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, params=None):
        conn = self.conn
        if sql == health.SELECT_SQL:
            if conn.fail_read:
                raise OSError("read failed")
            self._rows = [conn.rows[p] for p in sorted(conn.rows)]
        elif sql == health.UPSERT_WITH_ALERTS_SQL:
            if conn.fail_write:
                raise OSError("connection lost")
            conn.tx.append(("api", params))
        elif sql == INSERT_SQL:
            conn.tx.append(("event", dict(zip(EVENT_COLUMNS, params))))
        else:
            raise AssertionError(sql)

    async def fetchall(self):
        return self._rows


class FakeTx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        self.conn.tx = []

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is None:
            for kind, item in self.conn.tx:
                (self.conn.events if kind == "event" else self.conn.upserts).append(item)
        self.conn.tx = []
        return False


class FakeLockConn:
    def __init__(self):
        self.rows = {}
        self.upserts = []
        self.events = []
        self.tx = []
        self.fail_read = False
        self.fail_write = False

    def cursor(self):
        return FakeCursor(self)

    def transaction(self):
        return FakeTx(self)

    def set_dispatch(self, rep, age):
        self.rows["dispatch"] = ("dispatch", 7, "h", "recorder", at(-3600), at(0), age, rep, [])


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return at(self.t)

    def mono(self):
        return self.t


def make_monitor(clock):
    return rh.RecorderHealthMonitor(TH, eval_s=5, now=clock.now, monotonic=clock.mono)


class TestMonitor:
    async def test_writes_api_row_and_one_event_per_transition(self, caplog):
        clock, conn = Clock(), FakeLockConn()
        mon = make_monitor(clock)
        conn.set_dispatch(report(lag=1.0), 3)
        for step in range(0, 25, 5):
            clock.t = step
            assert await mon.maybe_tick(conn, report(pct=95), role="writer", started_at=at(0))
        assert [e["code"] for e in conn.events] == ["SYSTEM.RECORDER_ALERT_RAISED"]
        assert json.loads(conn.events[0]["payload"])["alert"] == rh.ALERT_QUEUE
        last = conn.upserts[-1]
        assert last[0] == "api" and last[3] == "writer"
        alerts = json.loads(last[6])
        assert [(a["alert"], a["process"]) for a in alerts] == [(rh.ALERT_QUEUE, "api")]
        assert "Recorder alert RAISED: writer_queue_high (process api)" in caplog.text
        # rate limit: nothing happens inside eval_s
        clock.t = 21
        assert not await mon.maybe_tick(conn, report(), role="writer", started_at=at(0))

    async def test_db_failure_keeps_state_and_retries_without_duplicates(self, caplog):
        clock, conn = Clock(), FakeLockConn()
        mon = make_monitor(clock)
        conn.set_dispatch(report(), 3)
        conn.fail_write = True
        for step in (0, 5, 10, 15):
            clock.t = step
            await mon.maybe_tick(conn, report(pct=95), role="writer", started_at=at(0))
        assert conn.events == [] and mon.pending_events == 1 and mon.write_failures == 4
        assert [a["alert"] for a in mon.active_alerts()] == [rh.ALERT_QUEUE]
        assert "Recorder alert RAISED" in caplog.text
        assert "alert state is kept in memory" in caplog.text
        conn.fail_write = False
        clock.t = 20
        await mon.maybe_tick(conn, report(pct=95), role="writer", started_at=at(0))
        assert len(conn.events) == 1 and mon.pending_events == 0
        assert conn.events[0]["ts"] == at(10)          # the transition time, not the retry

    async def test_unreadable_table_holds_dispatch_rules(self):
        clock, conn = Clock(), FakeLockConn()
        mon = make_monitor(clock)
        conn.fail_read = True
        for step in range(0, 200, 5):
            clock.t = step
            await mon.maybe_tick(conn, report(), role="writer", started_at=at(0))
        assert mon.active_alerts() == [] and mon.read_failures == 40

    async def test_missing_then_stale_dispatch(self):
        clock, conn = Clock(), FakeLockConn()
        mon = make_monitor(clock)
        for step in range(0, 65, 5):                    # no dispatch row yet: 60 s grace
            clock.t = step
            await mon.maybe_tick(conn, report(), role="writer", started_at=at(0))
        assert mon.active_alerts() == [] and conn.events == []
        clock.t = 65
        await mon.maybe_tick(conn, report(), role="writer", started_at=at(0))
        assert [(a["alert"], a["process"]) for a in mon.active_alerts()] == \
            [(rh.ALERT_STALE, "dispatch")]
        payload = json.loads(conn.events[0]["payload"])
        assert payload == {"alert": rh.ALERT_STALE, "process": "dispatch", "value": None,
                           "threshold": 60.0, "raised_at": None, "duration_s": None}
        conn.set_dispatch(report(), 2)
        for step in range(70, 110, 5):
            clock.t = step
            await mon.maybe_tick(conn, report(), role="writer", started_at=at(0))
        assert [e["code"] for e in conn.events] == ["SYSTEM.RECORDER_ALERT_RAISED",
                                                    "SYSTEM.RECORDER_ALERT_CLEARED"]
        assert mon.active_alerts() == []

    async def test_restore_from_api_row(self):
        clock, conn = Clock(), FakeLockConn()
        conn.rows["api"] = ("api", 1, "h", "writer", at(-60), at(-3), 3.0, report(),
                            [{"alert": rh.ALERT_SPILL, "process": "dispatch", "value": 400,
                              "threshold": 300, "since": at(-50).isoformat()}])
        mon = make_monitor(clock)
        assert await mon.restore(conn) == 1
        conn.set_dispatch(report(pending=4, pending_age=500), 2)
        await mon.maybe_tick(conn, report(), role="writer", started_at=at(0))
        assert conn.events == []                        # still active: not raised again
        conn.fail_read = True
        assert await rh.RecorderHealthMonitor(TH).restore(conn) == 0   # never raises

    async def test_pending_backlog_is_bounded(self, monkeypatch):
        monkeypatch.setattr(rh, "MAX_PENDING_EVENTS", 3)
        clock = Clock()
        mon = make_monitor(clock)
        for i in range(5):
            mon.evaluate(report(), None, False, at(0))
            mon._pending.append({"dummy": i})
        mon.evaluate(report(), None, False, at(0))
        assert mon.pending_events == 3 and mon.events_dropped == 2


# --- ApiTelemetry integration ---------------------------------------------------------------------

class TestApiTelemetryHealth:
    def make(self, tmp_path):
        clock = Clock()
        mon = make_monitor(clock)
        tel = ApiTelemetry("dbname=x", str(tmp_path), now=clock.now, monotonic=clock.mono,
                           health_monitor=mon)
        return tel, mon

    def test_standby_report(self, tmp_path):
        tel, _ = self.make(tmp_path)
        rep = tel.health_report()
        assert rep["election"]["role"] == "standby" and rep["queue"]["depth"] == 0
        assert rep["queue"]["capacity"] > 0 and rep["writer"]["running"] is False
        row = tel.health_row()
        assert row["process"] == "api" and row["role"] == "standby" and row["alerts"] == []

    async def test_term_end_spills_pending_alert_events(self, tmp_path):
        tel, mon = self.make(tmp_path)
        from packages.telemetry_ingest import IngestQueue

        class Term:
            def __init__(self, spill):
                self.queue = IngestQueue("api", spill)
                self.stopped = False

            async def stop(self):
                self.stopped = True

        term = Term(tel._spill_file())
        tel._term = term
        mon.evaluate(report(pct=99), None, False, at(0))
        mon.evaluate(report(pct=99), None, False, at(10))
        assert mon.pending_events == 1
        await tel._stop_term()
        assert term.stopped and mon.pending_events == 0
        assert tel._spill.pending_lines == 1
        rows, _ = tel._spill.read(10)
        assert rows[0]["code"] == "SYSTEM.RECORDER_ALERT_RAISED"


# --- endpoint --------------------------------------------------------------------------------------

class EpCursor:
    def __init__(self, db):
        self.db = db
        self._rows = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, params=None):
        self.db.executed.append(sql)
        if sql == health.SELECT_SQL:
            if self.db.fail is not None:
                raise self.db.fail
            self._rows = list(self.db.rows)

    async def fetchall(self):
        return self._rows


class EpConn:
    def __init__(self, db):
        self.db = db

    def cursor(self):
        return EpCursor(self.db)


class EpDb:
    def __init__(self, rows=(), fail=None):
        self.rows = list(rows)
        self.fail = fail
        self.executed = []

    @asynccontextmanager
    async def connection(self):
        yield EpConn(self)


def db_row(process, age, rep, alerts=()):
    return (process, 11, "host", "recorder" if process == "dispatch" else "writer",
            at(-3600), at(-age), float(age), rep, list(alerts))


async def get_health(db, telemetry=None):
    svc = type("Svc", (), {"database": db, "telemetry": telemetry})()
    with patch.object(main, "service", svc):
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
            return await http.get("/api/v1/health/recording")


class TestEndpoint:
    async def test_healthy_from_database(self):
        disp = report(pct=1.5, lag=1.01)
        db = EpDb([db_row("api", 2, report()), db_row("dispatch", 4, disp)])
        resp = await get_health(db)
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok" and body["alerts"] == []
        assert body["database"] == {"ok": True, "error": None}
        assert body["served_by"]["is_writer"] is False
        d = body["processes"]["dispatch"]
        assert d["present"] and not d["stale"] and d["report_age_s"] == 4.0
        assert d["queue"] == disp["queue"] and d["role"] == "recorder"
        assert d["heartbeat_sweep"]["lag_s"] == 1.01
        assert d["heartbeat_sweep"]["lag_threshold_s"] == 3.0
        assert body["thresholds"]["queue_pct"] == 80.0
        assert db.executed[:1] == ["SET TRANSACTION READ ONLY"]

    async def test_stored_alerts_and_read_time_staleness(self):
        stored = [{"alert": rh.ALERT_QUEUE, "process": "api", "value": 88.0,
                   "threshold": 80.0, "since": at(-30).isoformat(), "source": "evaluator"}]
        db = EpDb([db_row("api", 3, report(pct=88), stored),
                   db_row("dispatch", 75, report(pending=2, pending_age=10))])
        body = (await get_health(db)).json()
        assert body["status"] == "alerting"
        assert [(a["alert"], a["process"], a["source"]) for a in body["alerts"]] == [
            (rh.ALERT_QUEUE, "api", "stored"), (rh.ALERT_STALE, "dispatch", "read")]
        assert body["processes"]["dispatch"]["stale"] is True
        # the spill age is brought forward by the report's age
        assert body["processes"]["dispatch"]["spill"]["pending_age_s"] == 85.0

    async def test_missing_rows_are_stale(self):
        body = (await get_health(EpDb([]))).json()
        assert {(a["alert"], a["process"]) for a in body["alerts"]} == {
            (rh.ALERT_STALE, "api"), (rh.ALERT_STALE, "dispatch")}
        assert body["processes"]["dispatch"] == {"present": False, "source": None,
                                                 "stale": True}

    async def test_writer_answers_from_memory_even_when_db_is_down(self, tmp_path):
        clock = Clock()
        mon = make_monitor(clock)
        tel = ApiTelemetry("dbname=x", str(tmp_path), now=clock.now, monotonic=clock.mono,
                           health_monitor=mon)
        tel._term = object()   # is_writer
        tel.health_report = lambda: report(pct=97)
        mon.evaluate(report(pct=97), None, False, at(0))
        mon.evaluate(report(pct=97), None, False, at(10))
        body = (await get_health(EpDb(fail=OSError("down")), tel)).json()
        assert body["database"]["ok"] is False and "down" in body["database"]["error"]
        assert body["served_by"]["is_writer"] is True
        assert body["processes"]["api"]["source"] == "memory"
        assert body["processes"]["api"]["queue"]["pct"] == 97
        assert [(a["alert"], a["source"]) for a in body["alerts"]] == [
            (rh.ALERT_QUEUE, "evaluator"), (rh.ALERT_DB, "read")]

    async def test_missing_table_is_reported_not_500(self):
        import psycopg
        body = (await get_health(EpDb(fail=psycopg.errors.UndefinedTable("nope")))).json()
        assert body["database"]["ok"] is False
        assert "migrations" in body["database"]["error"]
        assert body["alerts"][0]["alert"] == rh.ALERT_DB


def test_thresholds_from_config():
    th = rh.Thresholds.from_config()
    assert (th.stale_s, th.queue_pct, th.queue_clear_pct, th.spill_s) == (60, 80, 60, 300)
    assert (th.sweep_factor, th.sweep_clear_factor, th.raise_s, th.clear_s) == (3, 1.5, 10, 30)
