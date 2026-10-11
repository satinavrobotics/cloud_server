"""GET /api/v1/maps/{id}/sessions/{sid}/trace: the pure computation (packages/utils/
session_trace.py), the route logic (packages/api/session_trace.py) and the Arango query."""
import datetime
import os

os.environ.setdefault("POSTGRES_PASSWORD", "test")

import uuid  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

import packages.api.main as main  # noqa: E402
from packages.api import session_trace as api  # noqa: E402
from packages.topomap_dbs.graph_db.server import GraphDatabaseService  # noqa: E402
from packages.utils import session_trace as st  # noqa: E402
from tests.unit.test_maps_m1 import FakeDb, _status, fdb  # noqa: E402,F401

pytestmark = pytest.mark.unit

UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)


def _n(x, y, secs=None):
    return {"x": x, "y": y,
            "time": None if secs is None else T0 + datetime.timedelta(seconds=secs)}


class TestParseTime:
    def test_aware_naive_and_garbage(self):
        assert st.parse_time("2026-10-01T12:00:00+00:00") == T0
        assert st.parse_time("2026-10-01T12:00:00Z") == T0
        naive = st.parse_time("2026-10-01T12:00:00")
        assert naive.tzinfo is not None
        assert st.parse_time(None) is None and st.parse_time("") is None
        assert st.parse_time("yesterday") is None and st.parse_time(5) is None


class TestStep:
    def test_distance_dt_speed(self):
        s = st.step(_n(0, 0, 0), _n(3, 4, 2))
        assert s == {"distance_m": 5.0, "dt_s": 2.0, "speed_mps": 2.5}

    def test_missing_time_gives_null_dt_and_speed(self):
        s = st.step(_n(0, 0), _n(3, 4, 2))
        assert s == {"distance_m": 5.0, "dt_s": None, "speed_mps": None}

    @pytest.mark.parametrize("secs", [0, -3])
    def test_zero_or_negative_dt_has_no_speed(self, secs):
        s = st.step(_n(0, 0, 5), _n(1, 0, 5 + secs))
        assert s["distance_m"] == 1.0 and s["dt_s"] == float(secs) and s["speed_mps"] is None

    def test_missing_pose(self):
        s = st.step({"x": None, "y": 0, "time": T0}, _n(1, 0, 1))
        assert s["distance_m"] is None and s["speed_mps"] is None and s["dt_s"] == 1.0

    def test_steps_first_is_none(self):
        out = st.steps([_n(0, 0, 0), _n(1, 0, 1), _n(1, 1, 3)])
        assert out[0] is None and out[1]["speed_mps"] == 1.0 and out[2]["speed_mps"] == 0.5


class TestSummarize:
    def test_summary(self):
        nodes = [_n(0, 0, 0), _n(1, 0, 1), _n(1, 2, 2), _n(1, 2, 12)]
        out = st.summarize(nodes)
        assert out["node_count"] == 4
        assert out["total_distance_m"] == 3.0
        assert out["duration_s"] == 12.0
        assert out["first_time"] == T0.isoformat()
        assert out["last_time"] == (T0 + datetime.timedelta(seconds=12)).isoformat()
        assert out["max_speed_mps"] == 2.0
        assert out["median_speed_mps"] == 1.0   # speeds 1.0, 2.0, 0.0

    def test_empty(self):
        out = st.summarize([])
        assert out == {"node_count": 0, "first_time": None, "last_time": None,
                       "duration_s": None, "total_distance_m": 0.0,
                       "median_speed_mps": None, "max_speed_mps": None}

    def test_single_node(self):
        out = st.summarize([_n(0, 0, 0)])
        assert out["node_count"] == 1 and out["duration_s"] is None
        assert out["first_time"] == out["last_time"] == T0.isoformat()
        assert out["median_speed_mps"] is None

    def test_no_timestamps(self):
        out = st.summarize([_n(0, 0), _n(3, 4), _n(3, 5)])
        assert out["total_distance_m"] == 6.0
        assert out["first_time"] is None and out["duration_s"] is None
        assert out["max_speed_mps"] is None

    def test_duplicate_times_are_skipped_for_speed(self):
        out = st.summarize([_n(0, 0, 0), _n(1, 0, 0), _n(2, 0, 1)])
        assert out["max_speed_mps"] == 1.0 and out["median_speed_mps"] == 1.0


# --- route logic ---------------------------------------------------------------------------------

def _row(i, x, y, secs=None, **extra):
    stamp = None if secs is None else (T0 + datetime.timedelta(seconds=secs)).isoformat()
    row = {"id": f"node-{i}", "session_node_id": i, "x": x, "y": y, "yaw": 0.1 * i,
           "timestamp": stamp, "depth": [], "costmap": []}
    row.update(extra)
    return row


class FakeGraph:
    def __init__(self, rows=None):
        self.rows = rows or []     # session id -> rows is simulated by one session
        self.calls = []

    def session_trace_nodes(self, map_id, session_id):
        self.calls.append((map_id, session_id))
        return list(self.rows)

    def delete(self, node_id):   # what the node-delete feature does to the collection
        self.rows = [r for r in self.rows if r["id"] != node_id]


class FakeImages:
    def __init__(self, by_node=None):
        self.by_node = by_node or {}

    def list_node_images(self, node_id, map_id):
        return list(self.by_node.get(node_id, []))


def _setup(fdb, purpose="mapping", map_name="yard"):
    fdb.add_map(map_name)
    return fdb.add_session(map_name, "r1", "live", ended=False, purpose=purpose)


async def _trace(sid, graph, images=None, map_id="yard", **kw):
    return await api.session_trace(None, graph, images or FakeImages(), map_id, str(sid), **kw)


class TestTrace:
    async def test_nodes_steps_images_and_summary(self, fdb):
        s = _setup(fdb)
        graph = FakeGraph([
            _row(0, 0.0, 0.0, 0, depth=["front"]),
            _row(1, 3.0, 4.0, 2, costmap=["local", "global"]),
            _row(2, 3.0, 4.0, None),
        ])
        images = FakeImages({"node-1": ["right", "front"]})
        out = await _trace(s["session_id"], graph, images)
        assert graph.calls == [("yard", str(s["session_id"]))]
        assert out["total"] == 3 and out["next_offset"] is None
        assert out["timestamp_source"] == "cloud_ingest" and out["purpose"] == "mapping"
        a, b, c = out["items"]
        assert [i["index"] for i in out["items"]] == [0, 1, 2]
        assert a["from_previous"] is None and a["depth"] == ["front"]
        assert a["timestamp"] == T0.isoformat() and a["images"] == []
        assert b["from_previous"] == {"distance_m": 5.0, "dt_s": 2.0, "speed_mps": 2.5}
        assert b["costmap"] == ["global", "local"] and b["pose"] == {"x": 3.0, "y": 4.0,
                                                                     "yaw": pytest.approx(0.1)}
        assert [i["image_id"] for i in b["images"]] == ["front", "right"]
        assert b["images"][0]["url"] == "/api/v1/images/yard/node-1?image_id=front"
        assert b["images"][0]["thumb_url"].endswith("&size=thumb")
        assert c["timestamp"] is None
        assert c["from_previous"] == {"distance_m": 0.0, "dt_s": None, "speed_mps": None}
        assert out["summary"]["node_count"] == 3 and out["summary"]["max_speed_mps"] == 2.5

    async def test_zero_nodes(self, fdb):
        s = _setup(fdb)
        out = await _trace(s["session_id"], FakeGraph())
        assert out["total"] == 0 and out["items"] == [] and out["next_offset"] is None
        assert out["summary"]["node_count"] == 0 and out["summary"]["first_time"] is None

    async def test_nodes_without_any_timestamp(self, fdb):
        s = _setup(fdb)
        out = await _trace(s["session_id"], FakeGraph([_row(0, 0, 0), _row(1, 1, 0)]))
        assert [i["timestamp"] for i in out["items"]] == [None, None]
        assert out["items"][1]["from_previous"]["speed_mps"] is None
        assert out["summary"]["total_distance_m"] == 1.0

    async def test_duplicate_timestamps(self, fdb):
        s = _setup(fdb)
        out = await _trace(s["session_id"], FakeGraph([_row(0, 0, 0, 5), _row(1, 1, 0, 5)]))
        assert out["items"][1]["from_previous"] == {"distance_m": 1.0, "dt_s": 0.0,
                                                    "speed_mps": None}

    async def test_pagination_keeps_the_step_across_pages_and_summary_is_whole(self, fdb):
        s = _setup(fdb)
        graph = FakeGraph([_row(i, float(i), 0.0, i) for i in range(5)])
        first = await _trace(s["session_id"], graph, limit=2)
        assert [i["index"] for i in first["items"]] == [0, 1] and first["next_offset"] == 2
        second = await _trace(s["session_id"], graph, limit=2, offset=2)
        assert [i["index"] for i in second["items"]] == [2, 3]
        assert second["items"][0]["from_previous"]["distance_m"] == 1.0
        last = await _trace(s["session_id"], graph, limit=2, offset=4)
        assert last["next_offset"] is None and len(last["items"]) == 1
        assert first["summary"] == second["summary"] == last["summary"]
        assert first["summary"]["node_count"] == 5 and first["total"] == 5
        beyond = await _trace(s["session_id"], graph, offset=50)
        assert beyond["items"] == [] and beyond["total"] == 5

    @pytest.mark.parametrize("kw", [{"limit": 0}, {"limit": 201}, {"offset": -1}])
    async def test_bad_paging_is_422(self, fdb, kw):
        s = _setup(fdb)
        code, _ = await _status(_trace(s["session_id"], FakeGraph(), **kw))
        assert code == 422

    async def test_operate_session_is_an_empty_trace_and_never_asks_arango(self, fdb):
        s = _setup(fdb, purpose="operate")
        graph = FakeGraph([_row(0, 0, 0, 0)])
        out = await _trace(s["session_id"], graph)
        assert out["purpose"] == "operate" and out["items"] == [] and out["total"] == 0
        assert graph.calls == []

    async def test_deleted_nodes_do_not_appear(self, fdb):
        s = _setup(fdb)
        graph = FakeGraph([_row(0, 0, 0, 0), _row(1, 1, 0, 1), _row(2, 2, 0, 2)])
        graph.delete("node-1")
        out = await _trace(s["session_id"], graph)
        assert [i["node_id"] for i in out["items"]] == ["node-0", "node-2"]
        assert [i["index"] for i in out["items"]] == [0, 1]
        assert out["items"][1]["from_previous"]["distance_m"] == 2.0

    async def test_unknown_map_unknown_session_and_other_map_are_404(self, fdb):
        s = _setup(fdb)
        fdb.add_map("other")
        assert (await _status(_trace(s["session_id"], FakeGraph(), map_id="nowhere")))[0] == 404
        assert (await _status(_trace(uuid.uuid4(), FakeGraph())))[0] == 404
        assert (await _status(_trace("not-a-uuid", FakeGraph())))[0] == 404
        graph = FakeGraph([_row(0, 0, 0, 0)])
        assert (await _status(_trace(s["session_id"], graph, map_id="other")))[0] == 404
        assert graph.calls == []

    async def test_finished_session_still_traces(self, fdb):
        fdb.add_map("yard")
        s = fdb.add_session("yard", "r1", "live", ended=True, purpose="mapping")
        out = await _trace(s["session_id"], FakeGraph([_row(0, 0, 0, 0)]))
        assert out["total"] == 1 and out["state"] == "finished"


class TestRoute:
    async def test_route_passes_through(self, fdb, monkeypatch):
        s = _setup(fdb)
        graph = FakeGraph([_row(0, 0, 0, 0)])
        monkeypatch.setattr(main, "service", SimpleNamespace(
            database=None, graph_db=graph, image_db=FakeImages()))
        out = await main.map_session_trace("yard", str(s["session_id"]), limit=10, offset=0)
        assert out["limit"] == 10 and out["total"] == 1

    async def test_route_404(self, fdb, monkeypatch):
        monkeypatch.setattr(main, "service", SimpleNamespace(
            database=None, graph_db=FakeGraph(), image_db=FakeImages()))
        with pytest.raises(HTTPException) as exc:
            await main.map_session_trace("yard", str(uuid.uuid4()))
        assert exc.value.status_code == 404


class TestArangoQuery:
    def _service(self, rows, has=True):
        svc = GraphDatabaseService.__new__(GraphDatabaseService)
        svc.logger = MagicMock()
        svc._session_indexed = set()
        svc.db = MagicMock()
        svc.db.has_collection.return_value = has
        svc.db.aql.execute.return_value = iter(rows)
        return svc

    def test_filters_by_session_server_side_and_ensures_the_index_once(self):
        svc = self._service([{"id": "a"}])
        assert svc.session_trace_nodes("yard", "sid-1") == [{"id": "a"}]
        args, kwargs = svc.db.aql.execute.call_args
        assert "FILTER n.session_id == @sid" in args[0] and "SORT n.timestamp" in args[0]
        assert kwargs["bind_vars"] == {"@col": "nodes_yard", "sid": "sid-1"}
        svc.db.collection.return_value.add_persistent_index.assert_called_once_with(
            fields=["session_id"])
        svc.db.aql.execute.return_value = iter([])
        svc.session_trace_nodes("yard", "sid-1")
        assert svc.db.collection.return_value.add_persistent_index.call_count == 1

    def test_missing_collection_is_empty(self):
        svc = self._service([], has=False)
        assert svc.session_trace_nodes("yard", "s") == []
        svc.db.aql.execute.assert_not_called()

    def test_index_failure_does_not_break_the_read(self):
        svc = self._service([{"id": "a"}])
        svc.db.collection.return_value.add_persistent_index.side_effect = RuntimeError("x")
        assert svc.session_trace_nodes("yard", "s") == [{"id": "a"}]
