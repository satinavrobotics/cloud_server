"""Unit tests for packages/telemetry_ingest/queue.py (IngestQueue, SpillFile)."""

import datetime
import uuid

import pytest

from packages.events.codes import EventCode
from packages.events.emit import build_row
from packages.telemetry_ingest import queue as q
from packages.telemetry_ingest import tables
from packages.telemetry_ingest.metrics import Metrics
from packages.telemetry_ingest.policy import PolicySources, RecordingPolicy
from packages.telemetry_ingest.queue import IngestQueue, SpillFile, decode_event, encode_event
from tests.unit.telemetry_ingest.helpers import T0, diag_row, event, state_row

pytestmark = pytest.mark.unit


def make_queue(spill_path, source="dispatch", maxsize=100, policy=None):
    return IngestQueue(source, SpillFile(spill_path), maxsize=maxsize, policy=policy)


class TestSpillFile:
    def test_encode_decode_round_trip(self):
        row = build_row(event(1))
        row["run_id"] = uuid.uuid4()
        assert decode_event(encode_event(row)) == row

    def test_append_read_consume(self, spill_path):
        spill = SpillFile(spill_path)
        assert not spill.pending
        rows = [build_row(event(i)) for i in range(5)]
        assert spill.append(rows) == 5
        assert spill.pending
        got, lines = spill.read(3)
        assert got == rows[:3] and lines == 3
        spill.append([build_row(event(9))])  # appended while the prefix is "in flight"
        spill.consume(3)
        got, lines = spill.read(100)
        assert [r["event_id"] for r in got] == [r["event_id"] for r in rows[3:]] + \
            [build_row(event(9))["event_id"]]
        spill.consume(lines)
        assert not spill.pending
        assert not spill_path.exists()
        assert spill.metrics.events_spilled == 6

    def test_pending_survives_restart(self, spill_path):
        SpillFile(spill_path).append([build_row(event(1))])
        again = SpillFile(spill_path)
        assert again.pending
        assert again.read(10)[0][0]["event_id"] == build_row(event(1))["event_id"]

    def test_partial_line_from_crash_is_skipped_and_counted(self, spill_path):
        spill = SpillFile(spill_path)
        spill.append([build_row(event(1))])
        with open(spill_path, "a") as f:
            f.write('{"ts": "2026-09')  # torn write, no newline
        spill = SpillFile(spill_path)  # restart repairs the tail
        spill.append([build_row(event(2))])
        rows, lines = spill.read(10)
        assert [r["event_id"] for r in rows] == [build_row(event(i))["event_id"] for i in (1, 2)]
        assert lines == 3
        assert spill.metrics.spill_corrupt_lines == 1
        spill.consume(lines)
        assert not spill.pending

    def test_reject_goes_to_side_file(self, spill_path):
        spill = SpillFile(spill_path)
        spill.reject([build_row(event(1))])
        assert spill.metrics.events_rejected == 1
        assert not spill.pending
        assert open(spill.rejected_path).read().count("\n") == 1


class TestIngestQueue:
    def test_put_event_builds_row(self, spill_path):
        queue = make_queue(spill_path)
        assert queue.put_event(event(1)) == q.QUEUED
        [(table, row)] = queue.drain(10)
        assert table == tables.EVENTS_TABLE
        assert row == build_row(event(1))

    def test_put_event_accepts_prebuilt_row(self, spill_path):
        queue = make_queue(spill_path)
        queue.put_event(build_row(event(1)))
        assert queue.drain(10)[0][1]["code"] == "ROBOT.STATE_CHANGED"

    def test_timeseries_rows_become_ordered_tuples(self, spill_path):
        queue = make_queue(spill_path)
        run = uuid.uuid4()
        queue.put_state(state_row(1, run_id=str(run)))
        queue.put_diagnostics(diag_row(2))
        (t1, r1), (t2, r2) = queue.drain(10)
        assert t1 == tables.ROBOT_STATE_TABLE and len(r1) == len(tables.ROBOT_STATE_COLUMNS)
        by_col = dict(zip(tables.ROBOT_STATE_COLUMNS, r1))
        assert by_col["run_id"] == run and by_col["order_id"] is None
        assert by_col["ts"].tzinfo is not None
        assert t2 == tables.DIAGNOSTICS_TABLE
        assert dict(zip(tables.DIAGNOSTICS_COLUMNS, r2))["gnss_sats"] == 18

    def test_timeseries_rejects_unknown_columns(self, spill_path):
        queue = make_queue(spill_path)
        with pytest.raises(ValueError):
            queue.put_state(state_row(1, speed=3.0))
        with pytest.raises(ValueError):
            queue.put_diagnostics({"robot_name": "r1"})

    def test_queue_full_drops_and_counts_telemetry(self, spill_path):
        queue = make_queue(spill_path, maxsize=2)
        assert queue.put_state(state_row(1)) == q.QUEUED
        assert queue.put_diagnostics(diag_row(2)) == q.QUEUED
        assert queue.put_state(state_row(3)) == q.DROPPED
        assert queue.put_diagnostics(diag_row(4)) == q.DROPPED
        assert queue.put_diagnostics(diag_row(5)) == q.DROPPED
        snap = queue.metrics.snapshot()
        assert snap["rows_dropped"] == {tables.ROBOT_STATE_TABLE: {"queue_full": 1},
                                        tables.DIAGNOSTICS_TABLE: {"queue_full": 2}}
        assert snap["queue_depth"] == 2 and snap["queue_depth_max"] == 2
        assert not queue.spill.pending

    def test_queue_full_spills_events(self, spill_path):
        queue = make_queue(spill_path, maxsize=1)
        assert queue.put_event(event(1)) == q.QUEUED
        assert queue.put_event(event(2)) == q.SPILLED
        assert queue.put_event(event(3)) == q.SPILLED
        rows, _ = queue.spill.read(10)
        assert [r["event_id"] for r in rows] == [build_row(event(i))["event_id"] for i in (2, 3)]
        assert queue.metrics.events_spilled == 2

    def test_spill_failure_counts_lost(self, tmp_path):
        spill = SpillFile(tmp_path / "events.jsonl")
        spill.path = str(tmp_path / "missing-dir" / "events.jsonl")  # unwritable now
        queue = IngestQueue("dispatch", spill, maxsize=1)
        queue.put_event(event(1))
        assert queue.put_event(event(2)) == q.LOST
        assert queue.metrics.events_lost == 1

    def test_latest_merges_per_robot(self, spill_path):
        queue = make_queue(spill_path, maxsize=1)
        queue.put_state(state_row(1))  # queue now full: latest must not care
        run = uuid.uuid4()
        assert queue.put_latest("r1", state_msg={"a": 1}, last_seen=T0) == q.MERGED
        queue.put_latest("r1", state_msg={"a": 2}, active_run_id=str(run))
        queue.put_latest("r2", sw_version="v2")
        latest = queue.take_latest()
        assert latest == {"r1": {"state_msg": {"a": 2}, "last_seen": T0, "active_run_id": run},
                          "r2": {"sw_version": "v2"}}
        assert not queue.latest_pending

    def test_latest_column_ownership(self, spill_path):
        dispatch = make_queue(spill_path, source="dispatch")
        api = make_queue(spill_path, source="api")
        with pytest.raises(ValueError, match="may not write"):
            dispatch.put_latest("r1", diagnostics={})
        with pytest.raises(ValueError, match="may not write"):
            api.put_latest("r1", state_msg={})
        with pytest.raises(ValueError):
            api.put_latest("r1")
        api.put_latest("r1", diagnostics={"cpu": 1}, nav_supervisor={"mode": "DRIVE"})
        assert set(api.take_latest()["r1"]) == {"diagnostics", "nav_supervisor"}

    def test_restore_latest_keeps_newer_values(self, spill_path):
        queue = make_queue(spill_path)
        queue.put_latest("r1", state_msg={"v": 1}, sw_version="old")
        failed = queue.take_latest()
        queue.put_latest("r1", state_msg={"v": 2})
        queue.restore_latest(failed)
        assert queue.take_latest() == {"r1": {"state_msg": {"v": 2}, "sw_version": "old"}}

    def test_drain_respects_limit(self, spill_path):
        queue = make_queue(spill_path)
        for i in range(5):
            queue.put_state(state_row(i))
        assert len(queue.drain(3)) == 3
        assert queue.qsize() == 2
        assert queue.metrics.queue_depth == 2

    def test_spill_queued_at_shutdown(self, spill_path):
        queue = make_queue(spill_path)
        queue.put_event(event(1))
        queue.put_state(state_row(2))
        assert queue.spill_queued() == 1
        assert queue.qsize() == 0
        assert queue.metrics.rows_dropped[tables.ROBOT_STATE_TABLE]["shutdown"] == 1
        assert queue.spill.read(10)[0][0]["event_id"] == build_row(event(1))["event_id"]

    def test_maxsize_must_be_bounded(self, spill_path):
        with pytest.raises(ValueError):
            make_queue(spill_path, maxsize=0)


class TestLevelGating:
    """§4.1: full = everything; events_only = events + robot_latest;
    off = robot_latest + TELEMETRY.RECORDING_CHANGED only."""

    @pytest.fixture
    def queue(self, spill_path):
        policy = RecordingPolicy(sources=PolicySources(
            robot_levels={"full": "full", "evonly": "events_only", "off": "off"}))
        return make_queue(spill_path, policy=policy)

    @pytest.mark.parametrize("robot,event_ok,ts_ok", [
        ("full", True, True),
        ("evonly", True, False),
        ("off", False, False),
        ("unknown", True, False),  # default events_only
    ])
    def test_matrix(self, queue, robot, event_ok, ts_ok):
        assert queue.put_event(event(1, robot)) == (q.QUEUED if event_ok else q.SKIPPED)
        expected_ts = q.QUEUED if ts_ok else q.SKIPPED
        assert queue.put_state(state_row(1, robot)) == expected_ts
        assert queue.put_diagnostics(diag_row(1, robot)) == expected_ts
        assert queue.put_event(event(2, robot, EventCode.TELEMETRY_RECORDING_CHANGED)) == q.QUEUED
        assert queue.put_latest(robot, state_msg={}) == q.MERGED

    def test_skipped_rows_are_counted(self, queue):
        queue.put_event(event(1, "off"))
        queue.put_state(state_row(1, "off"))
        queue.put_state(state_row(2, "evonly"))
        assert dict(queue.metrics.rows_skipped) == {tables.EVENTS_TABLE: 1,
                                                    tables.ROBOT_STATE_TABLE: 2}
        assert queue.qsize() == 0

    def test_level_change_takes_effect_immediately(self, queue):
        assert queue.put_state(state_row(1, "evonly")) == q.SKIPPED
        queue.policy.set_robot_level("evonly", "full")
        assert queue.put_state(state_row(2, "evonly")) == q.QUEUED


def test_naive_timestamps_are_utc(spill_path):
    queue = make_queue(spill_path)
    queue.put_state(state_row(0, ts=datetime.datetime(2026, 9, 24, 12, 0)))
    row = dict(zip(tables.ROBOT_STATE_COLUMNS, queue.drain(1)[0][1]))
    assert row["ts"] == T0
