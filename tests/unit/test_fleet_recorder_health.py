"""WP13: mission-dispatch's recorder health report (fleet_recorder.health_report /
write_health): heartbeat sweep lag, queue/spill numbers, and the `dispatch` recorder_health
upsert, which must never raise."""

import asyncio

import pytest

from packages.controllers.mission import fleet_recorder as fr
from packages.events.emit import Event, build_row
from packages.events.codes import EventCode
from tests.unit.fleet_recorder_fakes import T0, make_recorder

pytestmark = pytest.mark.unit


def test_sweep_lag_is_age_or_worst_gap(tmp_path):
    recorder, _db, clock = make_recorder(tmp_path)
    recorder._started_at = clock()
    rep = recorder.health_report()
    sweep = rep["heartbeat_sweep"]
    assert sweep["period_s"] == fr.SWEEP_PERIOD_S and sweep["lag_s"] == 0.0  # just started
    for _ in range(3):
        clock.advance(1.0)
        recorder.sweep_completed()
    clock.advance(0.4)
    sweep = recorder.health_report()["heartbeat_sweep"]
    assert sweep["gap_max_s"] == 1.0 and sweep["last_completed_age_s"] == 0.4
    assert sweep["lag_s"] == 1.0
    # a stall between two reports shows even though it recovered
    clock.advance(4.6)
    recorder.sweep_completed()               # gap 5.0 s
    clock.advance(1.0)
    recorder.sweep_completed()
    sweep = recorder.health_report()["heartbeat_sweep"]
    assert sweep["gap_max_s"] == 5.0 and sweep["lag_s"] == 5.0
    # new window: the old gap is gone; a dead sweep grows with its age
    clock.advance(7.0)
    sweep = recorder.health_report()["heartbeat_sweep"]
    assert sweep["gap_max_s"] == 0.0 and sweep["lag_s"] == 7.0


def test_report_has_queue_spill_and_runs(tmp_path):
    recorder, _db, clock = make_recorder(tmp_path)
    recorder._started_at = clock()
    recorder.queue.spill.append([build_row(Event(
        EventCode.ROBOT_ONLINE, T0, robot_name="r1", payload={"connection_state": "ONLINE"}))])
    rep = recorder.health_report()
    assert rep["queue"]["capacity"] == recorder.queue.capacity
    assert rep["queue"]["depth"] == 0 and rep["queue"]["pct"] == 0.0
    assert rep["spill"]["pending"] == 1 and rep["spill"]["pending_age_s"] is not None
    assert rep["runs"] == {"ops_pending": 0, "op_failures": 0, "ops_dropped": 0,
                           "active_runs": 0}
    assert rep["writer"]["running"] is False       # make_recorder starts no writer
    assert rep["report_period_s"] == fr.HEALTH_REPORT_PERIOD_S


async def test_write_health_upserts_the_dispatch_row(tmp_path):
    recorder, db, clock = make_recorder(tmp_path)
    recorder._started_at = clock()
    assert await recorder.write_health()
    row = db.health["dispatch"]
    assert row["role"] == "recorder" and row["started_at"] == T0
    assert "heartbeat_sweep" in row["report"] and "queue" in row["report"]


async def test_write_health_never_raises(tmp_path, caplog):
    recorder, db, _clock = make_recorder(tmp_path)
    db.unavailable = True
    for _ in range(5):
        assert await recorder.write_health() is False
    assert recorder.health_failures == 5
    assert caplog.text.count("Recorder health report failed") == 3   # throttled
    assert recorder.snapshot()["health_failures"] == 5
    recorder._pool = None
    assert await recorder.write_health() is False


async def test_write_health_times_out(tmp_path, monkeypatch):
    recorder, db, _clock = make_recorder(tmp_path)
    monkeypatch.setattr(fr, "HEALTH_WRITE_TIMEOUT_S", 0.01)

    class Hang:
        def connection(self, timeout=None):
            return self

        async def __aenter__(self):
            await asyncio.sleep(10)

        async def __aexit__(self, *exc):
            return False

    recorder._pool = Hang()
    assert await recorder.write_health() is False and recorder.health_failures == 1


async def test_health_loop_reports_periodically(tmp_path):
    recorder, db, clock = make_recorder(tmp_path)
    recorder._started_at = clock()
    task = asyncio.get_running_loop().create_task(recorder._health_loop())
    for _ in range(20):
        await asyncio.sleep(0.002)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert "dispatch" in db.health
