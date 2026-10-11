"""The session trace on the robot's capture time (packages/utils/capture_time.py)."""
import datetime

import pytest

from packages.topomap_dbs.graph_db.server import GraphDatabaseService
from packages.utils import session_trace as st
from tests.unit.test_session_trace import (  # noqa: F401
    T0, FakeGraph, _n, _row, _setup, _trace, fdb)

pytestmark = pytest.mark.unit


def _cap(secs):
    return (T0 + datetime.timedelta(seconds=secs)).isoformat()


class TestTrace:
    async def test_prefers_capture_time_when_every_node_has_it(self, fdb):
        s = _setup(fdb)
        # receipt times are bursty (10 ms apart); capture times are 2 s apart
        rows = [_row(i, 2.0 * i, 0.0, 0.01 * i, capture_time=_cap(2 * i)) for i in range(3)]
        out = await _trace(s["session_id"], FakeGraph(rows))
        assert out["timestamp_source"] == "robot_capture"
        assert [i["timestamp"] for i in out["items"]] == [_cap(0), _cap(2), _cap(4)]
        assert {i["timestamp_source"] for i in out["items"]} == {"robot_capture"}
        assert out["items"][1]["from_previous"] == {"distance_m": 2.0, "dt_s": 2.0,
                                                    "speed_mps": 1.0}
        assert out["summary"]["first_time"] == _cap(0) and out["summary"]["duration_s"] == 4.0

    async def test_orders_by_capture_time_when_all_have_it(self, fdb):
        s = _setup(fdb)
        rows = [_row(0, 0, 0, 0, capture_time=_cap(10)),     # received first, captured last
                _row(1, 1, 0, 1, capture_time=_cap(0)),
                _row(2, 2, 0, 2, capture_time=_cap(5))]
        out = await _trace(s["session_id"], FakeGraph(rows))
        assert [i["node_id"] for i in out["items"]] == ["node-1", "node-2", "node-0"]
        assert [i["index"] for i in out["items"]] == [0, 1, 2]

    async def test_equal_capture_times_fall_back_to_session_node_id(self, fdb):
        s = _setup(fdb)
        rows = [_row(2, 0, 0, 0, capture_time=_cap(1)), _row(1, 1, 0, 1, capture_time=_cap(1))]
        out = await _trace(s["session_id"], FakeGraph(rows))
        assert [i["node_id"] for i in out["items"]] == ["node-1", "node-2"]

    async def test_mixed_session_keeps_receipt_order_and_per_step_basis(self, fdb):
        s = _setup(fdb)
        rows = [_row(0, 0, 0, 0),                                       # old node
                _row(1, 1, 0, 10, capture_time=_cap(100)),
                _row(2, 3, 0, 11, capture_time=_cap(101))]
        out = await _trace(s["session_id"], FakeGraph(rows))
        assert out["timestamp_source"] == "mixed"
        assert [i["node_id"] for i in out["items"]] == ["node-0", "node-1", "node-2"]
        assert out["items"][0]["timestamp_source"] == "cloud_ingest"
        assert out["items"][1]["timestamp"] == _cap(100)
        # old -> new: receipt times; new -> new: capture times
        assert out["items"][1]["from_previous"]["dt_s"] == 10.0
        assert out["items"][2]["from_previous"]["dt_s"] == 1.0
        # the summary stays on one clock (receipt) in a mixed session
        assert out["summary"]["first_time"] == _cap(0) and out["summary"]["duration_s"] == 11.0

    async def test_old_nodes_without_capture_time_are_unchanged(self, fdb):
        s = _setup(fdb)
        out = await _trace(s["session_id"], FakeGraph([_row(0, 0, 0, 0), _row(1, 3, 4, 2)]))
        assert out["timestamp_source"] == "cloud_ingest"
        assert out["items"][1]["from_previous"]["dt_s"] == 2.0

    async def test_derives_capture_from_stored_camera_stamps(self, fdb):
        s = _setup(fdb)
        ms0 = int((T0 + datetime.timedelta(seconds=30)).timestamp() * 1000)
        rows = [_row(0, 0, 0, 0, camera_stamps_ms=[ms0]),
                _row(1, 1, 0, 0.1, camera_stamps_ms=[ms0 + 4000, ms0 + 4001])]
        out = await _trace(s["session_id"], FakeGraph(rows))
        assert out["timestamp_source"] == "robot_capture"
        assert out["items"][1]["from_previous"]["dt_s"] == 4.0

    async def test_insane_stored_capture_time_is_ignored(self, fdb):
        s = _setup(fdb)
        rows = [_row(0, 0, 0, 0, capture_time="1970-01-01T00:00:05+00:00", camera_stamps_ms=[5]),
                _row(1, 1, 0, 1, capture_time="garbage")]
        out = await _trace(s["session_id"], FakeGraph(rows))
        assert out["timestamp_source"] == "cloud_ingest"
        assert out["items"][1]["from_previous"]["dt_s"] == 1.0


class TestUtils:
    def test_timestamp_source_and_order(self):
        a = {"capture": T0, "session_node_id": 1, "id": "a"}
        b = {"capture": None, "session_node_id": 0, "id": "b"}
        assert st.timestamp_source([]) == "cloud_ingest"
        assert st.timestamp_source([a, b]) == "mixed"
        assert st.timestamp_source([a]) == "robot_capture"
        assert st.order([a, b]) == [a, b]
        late = {"capture": T0 + datetime.timedelta(seconds=1), "session_node_id": 0, "id": "z"}
        assert st.order([late, a]) == [a, late]

    def test_step_uses_capture_only_when_both_have_it(self):
        one = {**_n(0, 0, 0), "capture": T0}
        two = {**_n(1, 0, 0.01), "capture": T0 + datetime.timedelta(seconds=4)}
        assert st.step(one, two)["dt_s"] == 4.0
        assert st.step(_n(0, 0, 0), two)["dt_s"] == 0.01


def test_arango_query_projects_capture_fields():
    assert "capture_time: n.capture_time" in GraphDatabaseService.SESSION_TRACE_AQL
    assert "camera_metadata[*].timestamp" in GraphDatabaseService.SESSION_TRACE_AQL
