"""Unit tests for packages/telemetry_ingest/metrics.py."""

import json

import pytest

from packages.telemetry_ingest.metrics import Metrics

pytestmark = pytest.mark.unit


def test_starts_at_zero():
    snap = Metrics().snapshot()
    assert snap["rows_written"] == {} and snap["rows_dropped"] == {}
    assert snap["events_spilled"] == 0 and snap["flushes"] == 0 and snap["queue_depth"] == 0


def test_counters_and_snapshot_is_json_safe_copy():
    m = Metrics()
    m.written("fleet_events", 3)
    m.written("fleet_events", 0)
    m.dropped("robot_state_ts", "queue_full", 2)
    m.dropped("robot_state_ts", "write_failed")
    m.skipped("diagnostics_ts")
    m.set_queue_depth(7)
    m.set_queue_depth(2)
    m.flush_finished(0.5, failed=False)
    m.flush_finished(0.25, failed=True)
    snap = m.snapshot()
    json.dumps(snap)
    assert snap["rows_written"] == {"fleet_events": 3}
    assert snap["rows_dropped"] == {"robot_state_ts": {"queue_full": 2, "write_failed": 1}}
    assert snap["rows_skipped"] == {"diagnostics_ts": 1}
    assert snap["queue_depth"] == 2 and snap["queue_depth_max"] == 7
    assert snap["flushes"] == 2 and snap["flush_failures"] == 1
    assert snap["flush_duration_last_s"] == 0.25
    assert snap["flush_duration_max_s"] == 0.5
    assert snap["flush_duration_total_s"] == 0.75
    snap["rows_written"]["fleet_events"] = 99
    assert m.rows_written["fleet_events"] == 3
