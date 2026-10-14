"""GET /api/v1/maps/{id}/mapping-stats: session time (wall, paused, active), the node-pair attribution, the track
source, the occupancy area, the regions and the route (packages/api/mapping_stats.py), with fakes."""
import asyncio
import contextlib
import datetime
import json
import os

os.environ.setdefault("POSTGRES_PASSWORD", "test")

import numpy as np  # noqa: E402
import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

pytest.importorskip("psycopg")

from packages.api import drive_stats as ds  # noqa: E402
from packages.api import mapping_stats as mst  # noqa: E402

pytestmark = pytest.mark.unit

UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 10, 11, 15, 0, 0, tzinfo=UTC)
PAUSED, RESUMED = "MAP.SESSION_PAUSED", "MAP.SESSION_RESUMED"
P = ds.Params(min_speed=0.05, max_gap_s=5.0, max_speed=2.0, leg_max_m=10.0)


def at(sec):
    return T0 + datetime.timedelta(seconds=sec)


def session(sid="s1", start=0, end=600, robot="r1", paused_at=None):
    return {"session_id": sid, "map_name": "m", "robot_name": robot, "purpose": "mapping", "kind": "live",
            "started_at": at(start), "ended_at": at(end) if end is not None else None,
            "paused_at": at(paused_at) if paused_at is not None else None, "node_count": 0}


def node(i, x, y, sec, capture=True):
    when = at(sec).isoformat()
    return {"id": str(i), "session_node_id": i, "x": x, "y": y, "yaw": 0.0,
            "timestamp": when, "capture_time": when if capture else None, "camera_stamps_ms": [1, 2]}


def load(sessions, nodes, events=None, track=None, now=700):
    ld = mst.Loaded()
    ld.sessions, ld.nodes, ld.events, ld.track = sessions, nodes, events or {}, track or {}
    ld.now = at(now).timestamp()
    return ld


class TestTimes:
    def test_wall_paused_active(self):
        ev = [(PAUSED, at(100).timestamp()), (RESUMED, at(160).timestamp())]
        t = mst.session_times(session(), ev, at(700).timestamp())
        assert (t["wall_s"], t["paused_s"], t["active_s"], t["open"]) == (600, 60, 540, False)

    def test_open_session_counts_to_now_and_open_pause(self):
        ev = [(PAUSED, at(100).timestamp())]
        t = mst.session_times(session(end=None, paused_at=100), ev, at(400).timestamp())
        assert t["wall_s"] == 400 and t["paused_s"] == 300 and t["active_s"] == 100 and t["open"]

    def test_pause_event_missing_uses_paused_at(self):
        t = mst.session_times(session(end=None, paused_at=250), [], at(400).timestamp())
        assert t["paused_s"] == 150

    def test_finished_while_paused_ends_at_finish_and_repeats_ignored(self):
        ev = [(RESUMED, at(10).timestamp()), (PAUSED, at(500).timestamp()), (PAUSED, at(520).timestamp())]
        t = mst.session_times(session(), ev, at(700).timestamp())
        assert t["paused_s"] == 100                       # stray resume ignored, repeat pause ignored


class TestNodePairs:
    def seg(self, rows, spans=(), max_gap=60.0):
        times = [r[3] for r in rows]
        return mst.node_segments(times, [r[1] for r in rows], [r[2] for r in rows], list(spans), max_gap, 2.0, 10.0)

    def test_rules(self):
        rows = [(0, 0.0, 0.0, 0.0), (1, 1.0, 0.0, 10.0),            # ok: 10 s
                (2, 2.0, 0.0, 500.0),                               # gap 490 s
                (3, 2.5, 0.0, 510.0),                               # ok
                (4, 40.0, 0.0, 511.0),                              # jump 37.5 m / 1 s
                (5, 40.0, 0.0, 511.0),                              # dt 0: reversed
                (6, 40.0, None, 520.0), (7, 40.0, 0.0, None)]       # missing pose / time
        seg, c, eff = self.seg(rows)
        assert len(seg) == 2 and list(eff) == [10.0, 10.0]
        assert (c["pairs_gap"], c["pairs_jump"], c["pairs_reversed"], c["pairs_missing"]) == (1, 1, 1, 2)
        assert c["gap_s"] == 490.0

    def test_paused_seconds_are_taken_out(self):
        rows = [(0, 0.0, 0.0, 0.0), (1, 1.0, 0.0, 100.0)]
        seg, c, eff = self.seg(rows, spans=[(20.0, 90.0)])
        assert list(eff) == [30.0] and c["pairs_attributed"] == 1
        seg, c, eff = self.seg(rows, spans=[(20.0, 95.0)], max_gap=4.0)       # 25 s left > gap 4: gap, not attributed
        assert len(seg) == 0 and c["pairs_gap"] == 1


class TestCompute:
    def test_attribution_and_cells(self):
        s = session()
        nodes = [node(i, 0.05 + i * 1.0, 0.05, 10 + i * 10) for i in range(4)]       # 3 pairs of 1 m / 10 s
        out = mst.compute(load([s], {"s1": nodes}), P, 60.0)
        t = out["tiles"]
        assert t.sums[1].sum() == pytest.approx(30.0)
        ax, ay = ds.key_cells(t.key)
        assert sorted(set(ax)) == list(range(0, 31)) and set(ay) == {0}
        assert out["totals"]["nodes"] == 4 and out["totals"]["images"] == 8
        assert out["per"][0]["attributed_s"] == 30.0 and out["per"][0]["time_source"] == "nodes"
        assert int(out["node_n"].sum()) == 4

    def test_receipt_time_when_capture_missing_and_outside_counted(self):
        nodes = [node(0, 0.05, 0.05, 10, capture=False), node(1, 1.05, 0.05, 20, capture=True),
                 node(2, 2.05, 0.05, 99999, capture=False)]
        out = mst.compute(load([session()], {"s1": nodes}), P, 60.0)
        e = out["per"][0]
        assert e["timestamp_source"] == "mixed" and e["nodes_outside_session"] == 1
        assert e["pairs"]["pairs_gap"] == 1                       # 99979 s on the receipt clock

    def test_track_source_wins_over_nodes(self):
        track = [(at(10).timestamp() + 1.5 * i, 0.05 + 0.5 * i, 0.05, "e") for i in range(5)]
        nodes = [node(i, 0.05 + i, 0.05, 10 + 10 * i) for i in range(3)]
        out = mst.compute(load([session()], {"s1": nodes}, track={"s1": track}), P, 60.0)
        assert out["per"][0]["time_source"] == "track"
        assert out["tiles"].sums[1].sum() == pytest.approx(6.0)

    def test_two_sessions_and_empty_session(self):
        a = [node(i, i * 1.0, 0.05, 10 + 5 * i) for i in range(3)]
        out = mst.compute(load([session("s1"), session("s2", 700, 800), session("s3", 900, None)],
                               {"s1": a, "s2": [], "s3": []}, now=1000), P, 60.0)
        t = out["totals"]
        assert t["active"] == 600 + 100 + 100 and t["empty"] == 2 and t["open"] == 1
        assert [e["time_source"] for e in out["per"]] == ["nodes", "none", "none"]


class FakeCursor:
    def __init__(self, events, track):
        self.events, self.track, self._rows = events, track, []

    async def execute(self, sql, args=()):
        if "fleet_events" in sql:
            self._rows = [(sid, c, t) for sid, evs in self.events.items() for c, t in evs]
        elif "robot_track_ts" in sql:
            self._rows = [(sid, t, x, y, e) for sid, rows in self.track.items() for t, x, y, e in rows]
        else:
            raise AssertionError(sql)

    async def fetchall(self):
        return list(self._rows)


class FakeStore:
    def __init__(self, sessions, known):
        self.s, self.known = sessions, known

    async def get_map(self, name):
        return object() if name in self.known else None

    async def sessions(self, name):
        return list(self.s) + [dict(self.s[0], session_id="op", purpose="operate")] if self.s else []


class FakeGraph:
    def __init__(self, nodes):
        self.nodes = nodes

    def session_trace_nodes(self, map_id, sid):
        return self.nodes.get(sid, [])


class FakeReads:
    """Two charts split at x = 1.0 m."""

    async def owner_plane(self, map_id, bbox, level=0):
        rows = cols = 64 * 25 * 2
        idx = np.zeros((rows, cols), np.uint16)
        cx = np.floor((np.arange(cols) * 0.1 + bbox.x0) / 0.1 + 1e-9)
        idx[:, cx < 10] = 1
        idx[:, cx >= 10] = 2
        return {"version": 7, "origin": (bbox.x0, bbox.y0), "step": 0.1, "shape": (rows, cols),
                "keys": ["A", "B"], "index": idx}

    async def view(self, map_id, bbox, level=0, version=None, raster=False, epoch=None, parents=True):
        return {"charts": [{"key": "A", "cells": 100, "area_m2": 1.0}, {"key": "B", "cells": 200, "area_m2": 2.0}]}


class FakeOcc:
    def __init__(self, free=4000, blocked=1000, fail=None):
        self.free, self.blocked, self.fail = free, blocked, fail

    async def known_area(self, map_id, version=None):
        if self.fail:
            raise self.fail
        return {"version": 7, "tiles": 2, "free": self.free, "blocked": self.blocked, "bbox": [0.0, 0.0, 6.4, 6.4]}


@pytest.fixture
def patched(monkeypatch):
    def install(sessions, events=None, track=None, known=("m",)):
        cursor = FakeCursor(events or {}, track or {})

        @contextlib.asynccontextmanager
        async def read_cursor(db):
            yield cursor

        @contextlib.asynccontextmanager
        async def open_store(db, pid):
            yield FakeStore(sessions, set(known))
        monkeypatch.setattr(mst.fleet_reads, "read_cursor", read_cursor)
        monkeypatch.setattr(mst.maps, "open_store", open_store)
    return install


def call(graph, reads, occ, robot=None, now=700, map_id="m"):
    raw, etag = asyncio.run(mst.mapping_stats(None, graph, reads, occ, map_id, robot, cache=ds.ResultCache(4, 60.0),
                                              now=at(now)))
    return json.loads(raw), etag


class TestEndpoint:
    def nodes(self):
        return {"s1": [node(i, 0.05 + i * 0.5, 0.05, 10 + i * 10) for i in range(5)]}      # 2 m / 40 s

    def test_headline_shape_and_numbers(self, patched):
        patched([session()], events={"s1": [(PAUSED, at(100).timestamp()), (RESUMED, at(160).timestamp())]})
        body, etag = call(FakeGraph(self.nodes()), FakeReads(), FakeOcc())
        h = body["headline"]
        assert body["tile_fields"] == ["ix", "iy", "seconds", "nodes"] and body["cell_m"] == 0.1
        assert h["sessions"] == 1 and h["wall_s"] == 600 and h["paused_s"] == 60 and h["active_s"] == 540
        assert h["mapped_area_m2"] == 12.5 and h["free_area_m2"] == 10.0 and h["blocked_area_m2"] == 2.5
        assert h["seconds_per_m2"] == 43.2 and h["minutes_per_100m2"] == 72.0
        assert h["scene_graph_area_m2"] == 3.0 and h["seconds_per_m2_scene_graph"] == 180.0
        assert h["nodes"] == 5 and h["images"] == 10
        assert len(body["sessions"]) == 1                                   # the operate session is not counted
        sess = body["sessions"][0]
        assert sess["active_s"] == 540 and sess["attributed_s"] == 40 and sess["area_added_m2"] is None
        assert sum(t[2] for t in body["tiles"]) == pytest.approx(40.0, abs=0.1)
        assert sum(t[3] for t in body["tiles"]) == 5 and all(len(t) == 4 for t in body["tiles"])
        assert body["origin"] == [0.0, 0.0] and body["graph_version"] == 7
        regions = {r["key"]: r for r in body["regions"]}
        assert regions["A"]["seconds"] == pytest.approx(19.0, abs=0.2) and regions["A"]["nodes"] == 2
        assert regions["A"]["area_m2"] == 1.0 and regions["A"]["seconds_per_m2"] == pytest.approx(19.0, abs=0.3)
        assert regions["B"]["nodes"] == 3 and body["source"]["occupancy"] == "ok" and etag.startswith('"')

    def test_open_session_and_no_data(self, patched):
        patched([session(end=None)])
        body, _ = call(FakeGraph({}), None, None, now=1000)
        h = body["headline"]
        assert h["sessions_open"] == 1 and h["active_s"] == 1000 and h["mapped_area_m2"] is None
        assert h["seconds_per_m2"] is None and body["tiles"] == [] and body["regions"] == []
        assert body["source"]["occupancy"] == "unavailable" and body["window"]["now"] is not None

    def test_no_sessions(self, patched):
        patched([])
        body, _ = call(FakeGraph({}), FakeReads(), FakeOcc())
        assert body["headline"]["sessions"] == 0 and body["headline"]["active_s"] == 0 and body["sessions"] == []

    def test_occupancy_failure_degrades(self, patched):
        patched([session()])
        for exc in (HTTPException(503, "x"), RuntimeError("boom")):
            body, _ = call(FakeGraph(self.nodes()), FakeReads(), FakeOcc(fail=exc))
            assert body["headline"]["mapped_area_m2"] is None and body["source"]["occupancy"] == "unavailable"
            assert body["headline"]["active_s"] == 600 and body["tiles"]

    def test_robot_filter(self, patched):
        patched([session(robot="r1")])
        body, _ = call(FakeGraph(self.nodes()), FakeReads(), FakeOcc(), robot="other")
        assert body["headline"]["sessions"] == 0

    def test_unknown_map_404(self, patched):
        patched([session()])
        with pytest.raises(HTTPException) as e:
            call(FakeGraph({}), None, None, map_id="zzz")
        assert e.value.status_code == 404

    def test_negative_origin(self, patched):
        patched([session()])
        ns = {"s1": [node(i, -6.0 + i * 0.5, 0.05, 10 + i * 10) for i in range(3)]}
        body, _ = call(FakeGraph(ns), FakeReads(), FakeOcc())
        assert body["origin"] == [-6.4, 0.0] and min(t[0] for t in body["tiles"]) == 4

    def test_cache(self, patched):
        patched([session()])
        cache = ds.ResultCache(4, 60.0)

        class Counting(FakeGraph):
            n = 0

            def session_trace_nodes(self, map_id, sid):
                Counting.n += 1
                return super().session_trace_nodes(map_id, sid)
        g = Counting(self.nodes())
        for _ in range(2):
            asyncio.run(mst.mapping_stats(None, g, None, None, "m", cache=cache, now=at(700)))
        assert Counting.n == 1


class TestRoute:
    def test_etag_304(self, patched, monkeypatch):
        from types import SimpleNamespace
        import packages.api.main as main
        patched([session()])
        monkeypatch.setattr(main, "service", SimpleNamespace(database=None, graph_db=FakeGraph({})))
        monkeypatch.setattr(main, "_scene_graph_reads", lambda: FakeReads())
        monkeypatch.setattr(main, "_occupancy_reads", lambda: FakeOcc())
        monkeypatch.setattr(mst, "CACHE", ds.ResultCache(4, 60.0))

        def get(inm=None):
            req = SimpleNamespace(headers={"if-none-match": inm} if inm else {})
            return asyncio.run(main.map_mapping_stats("m", req, None))
        r = get()
        assert r.status_code == 200 and r.headers["cache-control"] == "private, max-age=60"
        assert json.loads(r.body)["map_id"] == "m"
        assert get(r.headers["etag"]).status_code == 304

    def test_503_reads_degrade(self, patched, monkeypatch):
        from types import SimpleNamespace
        import packages.api.main as main
        patched([session()])

        def boom():
            raise HTTPException(503, "down")
        monkeypatch.setattr(main, "service", SimpleNamespace(database=None, graph_db=FakeGraph({})))
        monkeypatch.setattr(main, "_scene_graph_reads", boom)
        monkeypatch.setattr(main, "_occupancy_reads", boom)
        monkeypatch.setattr(mst, "CACHE", ds.ResultCache(4, 60.0))
        r = asyncio.run(main.map_mapping_stats("m", SimpleNamespace(headers={}), None))
        assert r.status_code == 200
