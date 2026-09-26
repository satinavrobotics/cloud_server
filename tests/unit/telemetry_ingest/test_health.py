"""WP13 health numbers of the ingest library: spill pending count / since when, queue
capacity, flush timestamps, and health.ingest_report()."""

import json
import os

import pytest

from packages.events.emit import build_row
from packages.telemetry_ingest import health
from packages.telemetry_ingest.metrics import Metrics
from packages.telemetry_ingest.queue import IngestQueue, SpillFile
from packages.telemetry_ingest.writer import TelemetryWriter
from tests.unit.telemetry_ingest.helpers import event

pytestmark = pytest.mark.unit


class Wall:
    def __init__(self, t=5000.0):
        self.t = t

    def __call__(self):
        return self.t


class TestSpillPending:
    def test_append_consume_and_since(self, spill_path):
        wall = Wall()
        spill = SpillFile(spill_path, wall=wall)
        assert spill.pending_lines == 0 and spill.pending_since is None
        spill.append([build_row(event(1)), build_row(event(2))])
        assert spill.pending_lines == 2 and spill.pending_since == 5000.0
        wall.t = 5100.0
        spill.append([build_row(event(3))])
        assert spill.pending_lines == 3 and spill.pending_since == 5000.0  # continuous
        spill.consume(2)
        assert spill.pending_lines == 1 and spill.pending_since == 5000.0
        spill.consume(1)
        assert spill.pending_lines == 0 and spill.pending_since is None and not spill.pending
        wall.t = 5200.0
        spill.append([build_row(event(4))])
        assert spill.pending_since == 5200.0                              # a new episode

    def test_existing_file_counts_from_mtime(self, spill_path):
        SpillFile(spill_path).append([build_row(event(i)) for i in range(4)])
        os.utime(spill_path, (1234.0, 1234.0))
        again = SpillFile(spill_path, wall=Wall())
        assert again.pending_lines == 4 and again.pending_since == 1234.0

    def test_vanished_file_resets(self, spill_path):
        spill = SpillFile(spill_path, wall=Wall())
        spill.append([build_row(event(1))])
        os.unlink(spill_path)
        assert spill.read(10) == ([], 0)
        assert spill.pending_lines == 0 and spill.pending_since is None


def test_queue_capacity(spill_path):
    assert IngestQueue("api", SpillFile(spill_path), maxsize=123).capacity == 123


def test_metrics_flush_timestamps():
    m = Metrics()
    m.flush_finished(0.1, failed=False)                  # no timestamp given: unchanged
    assert m.last_flush_at is None and m.last_flush_ok_at is None
    m.flush_finished(0.1, failed=False, at=10.0)
    m.flush_finished(0.1, failed=True, at=20.0)
    assert m.last_flush_at == 20.0 and m.last_flush_ok_at == 10.0
    snap = m.snapshot()
    assert snap["last_flush_ok_at"] == 10.0 and snap["writer_tick_at"] is None


async def test_writer_stamps_flushes(pool, db, clock, spill_path):
    wall = Wall(7000.0)
    queue = IngestQueue("dispatch", SpillFile(spill_path), maxsize=10)
    writer = TelemetryWriter(pool, queue, clock=clock, wall=wall)
    queue.put_event(event(1))
    assert await writer.flush_once()
    assert writer.metrics.last_flush_ok_at == 7000.0
    db.unavailable = True
    wall.t = 7005.0
    queue.put_event(event(2))
    assert not await writer.flush_once()
    assert writer.metrics.last_flush_at == 7005.0 and writer.metrics.last_flush_ok_at == 7000.0


def test_ingest_report(spill_path):
    wall = Wall(100.0)
    m = Metrics()
    spill = SpillFile(spill_path, m, wall=wall)
    spill.append([build_row(event(1))])
    m.dropped("robot_state_ts", "queue_full", 3)
    m.dropped("diagnostics_ts", "write_failed", 2)
    m.flush_finished(0.2, failed=False, at=90.0)
    m.writer_tick_at = 99.5
    rep = health.ingest_report(m, queue_depth=850, queue_capacity=1000, spill=spill,
                               writer_running=True, now=160.0)
    json.dumps(rep)
    assert rep["queue"] == {"depth": 850, "capacity": 1000, "pct": 85.0, "depth_max": 0}
    assert rep["dropped"]["total"] == 5
    assert rep["dropped"]["by_table"]["robot_state_ts"] == {"queue_full": 3}
    assert rep["spill"] == {"pending": 1, "pending_age_s": 60.0}
    assert rep["events_spilled"] == 1
    assert rep["writer"]["running"] is True
    assert rep["writer"]["last_flush_ok_age_s"] == 70.0
    assert rep["writer"]["last_tick_age_s"] == 60.5
    empty = health.ingest_report(Metrics(), queue_depth=0, queue_capacity=0)
    assert empty["queue"]["pct"] is None and empty["spill"] == {"pending": 0,
                                                                 "pending_age_s": None}
    assert empty["writer"]["last_flush_ok_age_s"] is None


def test_row_params():
    params = health.row_params("dispatch", "recorder", None, {"a": 1})
    assert params[0] == "dispatch" and params[1] == os.getpid() and params[3] == "recorder"
    assert json.loads(params[5]) == {"a": 1} and len(params) == 6
    with_alerts = health.row_params("api", "writer", None, {}, alerts=[{"alert": "x"}])
    assert len(with_alerts) == 7 and json.loads(with_alerts[6]) == [{"alert": "x"}]
    assert health.UPSERT_SQL.count("%s") == 6
    assert health.UPSERT_WITH_ALERTS_SQL.count("%s") == 7
    assert "alerts" not in health.UPSERT_SQL   # dispatch never overwrites the API's alerts
