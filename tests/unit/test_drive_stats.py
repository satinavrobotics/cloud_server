"""GET /api/v1/maps/{id}/drive-stats: the movement rules, the cell attribution, the leg fallback, the regions
and the cache (packages/api/drive_stats.py), with fakes and a small synthetic track."""
import asyncio
import contextlib
import json
import os

os.environ.setdefault("POSTGRES_PASSWORD", "test")

import numpy as np  # noqa: E402
import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

pytest.importorskip("psycopg")

from packages.api import drive_stats as ds  # noqa: E402
from packages.api.result_cache import ResultCache  # noqa: E402

pytestmark = pytest.mark.unit

P = ds.Params(min_speed=0.05, max_gap_s=5.0, max_speed=2.0, leg_max_m=10.0)
SESSION, EPOCH = "s1", "e1"


def track_run(run, xs, y=0.05, t0=1000.0, dt=1.5, session=SESSION, epoch=EPOCH):
    return [(run, t0 + i * dt, x, y, session, epoch) for i, x in enumerate(xs)]


def tiles_of(rows, recoveries=(), legs=(), nodes=None, params=P):
    loaded = ds.Loaded()
    loaded.track_rows, loaded.recoveries, loaded.legs = list(rows), list(recoveries), list(legs)
    tiles, src, tt, tl = ds.compute(loaded, lambda: nodes or {}, params)
    return tiles, src, tt, tl


def by_cell(tiles):
    ax, ay = ds.key_cells(tiles.key)
    return {(int(a), int(b)): (int(n), *[float(v) for v in s])
            for a, b, n, s in zip(ax, ay, tiles.n, tiles.sums.T)}


class TestSegments:
    def seg(self, rows):
        run = np.array([hash(r[0]) % 1000 for r in rows])
        grp = np.array([hash((r[4], r[5])) % 1000 for r in rows])
        return ds.track_segments(run, grp, *(np.array([r[i] for r in rows], float) for i in (1, 2, 3)), P)

    def test_rules(self):
        rows = track_run("a", [0.0, 0.5, 1.0])                    # 2 good pairs
        rows += [("a", 1003.0 + 100, 1.0, 0.05, SESSION, EPOCH)]  # 100 s gap
        rows += [("a", 1103.0 + 0.1, 30.0, 0.05, SESSION, EPOCH)]  # jump: 29 m in 0.1 s
        rows += [("a", 1103.1 + 1.5, 30.2, 0.05, "s2", EPOCH)]     # new session: not a movement
        seg, rejected = self.seg(rows)
        assert len(seg) == 2 and rejected == 2

    def test_non_increasing_time(self):
        seg, rejected = self.seg([("a", 10.0, 0, 0, "s", "e"), ("a", 10.0, 1, 0, "s", "e")])
        assert len(seg) == 0 and rejected == 1


class TestAttribution:
    def test_straight_drive_cells_and_totals(self):
        # 0.05 -> 3.05 m along y=0.05: x cell 0..30; 4 pairs of 0.75 m / 1.5 s (0.5 m/s)
        tiles, src, _, _ = tiles_of(track_run("a", [0.05 + 0.75 * i for i in range(5)]))
        cells = by_cell(tiles)
        assert src["track_runs"] == 1 and src["track_segments"] == 4 and src["tiles_track"] == len(tiles)
        assert all(iy == 0 for _, iy in cells)
        assert sorted(ix for ix, _ in cells) == list(range(0, 31))
        assert tiles.sums[0].sum() == pytest.approx(3.0, abs=1e-6)          # length
        assert tiles.sums[1].sum() == pytest.approx(6.0, abs=1e-6)          # time
        assert tiles.sums[3].sum() == 0.0                                   # moving: not stopped
        assert all(v[0] == 1 for v in cells.values())
        mid = cells[(10, 0)]
        assert mid[1] == pytest.approx(0.1, abs=0.03) and mid[2] == pytest.approx(0.2, abs=0.06)

    def test_second_pass_counts_coverage(self):
        rows = track_run("a", [0.05, 0.8, 1.55]) + track_run("b", [0.05, 0.8, 1.55], t0=5000.0)
        tiles, src, _, _ = tiles_of(rows)
        assert src["track_runs"] == 2
        assert set(v[0] for v in by_cell(tiles).values()) == {2}
        assert tiles.sums[0].sum() == pytest.approx(3.0, abs=1e-6)

    def test_same_run_twice_through_a_tile_is_one(self):
        rows = track_run("a", [0.05, 0.8, 0.05, 0.8])
        tiles, _, _, _ = tiles_of(rows)
        assert set(v[0] for v in by_cell(tiles).values()) == {1}
        assert tiles.sums[0].sum() == pytest.approx(2.25, abs=1e-6)

    def test_stopped_time(self):
        # one pair moves 0.75 m, one pair moves 0.01 m (0.0067 m/s < 0.05)
        tiles, _, _, _ = tiles_of(track_run("a", [0.05, 0.8, 0.81]))
        assert tiles.sums[3].sum() == pytest.approx(1.5)
        assert tiles.sums[1].sum() == pytest.approx(3.0)
        # a threshold above the speed of the moving pair (0.5 m/s) makes all of it stopped
        t2, _, _, _ = tiles_of(track_run("a", [0.05, 0.8, 0.81]), params=ds.Params(min_speed=0.6))
        assert t2.sums[3].sum() == pytest.approx(3.0)

    def test_zero_length_goes_to_its_cell(self):
        tiles, _, _, _ = tiles_of(track_run("a", [0.25, 0.25]))
        assert by_cell(tiles) == {(2, 0): (1, 0.0, 1.5, 0.0, 1.5)}

    def test_recovery_overlap(self):
        rows = track_run("a", [0.05, 0.8, 1.55])                  # segments 1000-1001.5, 1001.5-1003
        rec = [("a", 1002.25, 1.5)]                               # 1000.75 .. 1002.25
        tiles, src, _, _ = tiles_of(rows, recoveries=rec)
        assert tiles.sums[2].sum() == pytest.approx(1.5)          # 0.75 s in seg 1 + 0.75 s in seg 2
        assert src["recovery_unattributed_s"] == 0.0
        cells = by_cell(tiles)
        assert cells[(2, 0)][3] == 0.0 or cells[(2, 0)][3] > 0    # smoke: cell exists

    def test_recovery_in_a_gap_is_reported(self):
        rows = track_run("a", [0.05, 0.8])
        rec = [("a", 1200.0, 2.0)]
        tiles, src, _, _ = tiles_of(rows, recoveries=rec)
        assert tiles.sums[2].sum() == 0.0 and src["recovery_unattributed_s"] == 2.0


class TestLegs:
    NODES = {"n1": (0.05, 0.05), "n2": (2.05, 0.05), "n3": (50.0, 50.0)}

    def test_leg_fallback_and_never_mixed(self):
        legs = [("legrun", "n1", "n2", 10.0, 4.0, 2.0)]
        tiles, src, tt, tl = tiles_of([], legs=legs, nodes=self.NODES)
        assert src["leg_runs"] == 1 and src["leg_rows"] == 1 and src["tiles_track"] == 0
        assert src["tiles_leg_only"] == len(tiles) == len(tl)
        assert tiles.sums[0].sum() == pytest.approx(2.0, abs=1e-6)
        assert tiles.sums[1].sum() == pytest.approx(10.0)
        assert tiles.sums[2].sum() == pytest.approx(2.0) and tiles.sums[3].sum() == pytest.approx(4.0)

    def test_leg_rules(self):
        legs = [("r", "n1", "n3", 10.0, 0, 0), ("r", "n1", "nx", 10.0, 0, 0), ("r", "n1", "n2", 0.0, 0, 0)]
        _, src, _, _ = tiles_of([], legs=legs, nodes=self.NODES)
        assert src["legs_skipped"] == 3 and src["leg_rows"] == 0

    def test_track_and_leg_runs_add_up(self):
        legs = [("legrun", "n1", "n2", 10.0, 0.0, 0.0)]
        tiles, src, tt, tl = tiles_of(track_run("a", [0.05, 0.8, 1.55]), legs=legs, nodes=self.NODES)
        assert set(v[0] for v in by_cell(tiles).values()) == {1, 2}
        assert src["tiles_leg_only"] == len(tl) - len(set(tl.key) & set(tt.key))


class FakeCursor:
    def __init__(self, track, legs, recoveries, track_runs=()):
        self.track, self.legs, self.recoveries, self.track_runs = track, legs, recoveries, set(track_runs)
        self.sql = []
        self._rows = []

    async def execute(self, sql, args=()):
        self.sql.append((sql, args))
        if "FROM robot_track_ts t JOIN" in sql:
            self._rows = sorted(self.track, key=lambda r: -r[1])[:args[-1]]
        elif "NAV.RECOVERY_EXITED" in sql:
            self._rows = self.recoveries
        elif "FROM run_legs" in sql:
            self._rows = self.legs
        elif "SELECT DISTINCT run_id" in sql:
            self._rows = [(r,) for r in self.track_runs if r in args[0]]
        else:
            raise AssertionError(sql)

    async def fetchall(self):
        return list(self._rows)


class FakeStore:
    def __init__(self, maps_known):
        self.known = maps_known

    async def get_map(self, name):
        return object() if name in self.known else None


class FakeGraph:
    def __init__(self, nodes):
        self.nodes = nodes

    def get_all_nodes(self, map_id):
        return [{"_key": k, "pose": {"x": x, "y": y}} for k, (x, y) in self.nodes.items()]


class FakeReads:
    """An owner plane with two charts split at x = 1.0 m, origin (-6.4, -6.4), 0.1 m cells."""

    def __init__(self, version=7, step=0.1):
        self.version, self.step, self.calls = version, step, 0

    async def owner_plane(self, map_id, bbox, level=0):
        self.calls += 1
        rows = cols = 64 * 25 * 2
        idx = np.zeros((rows, cols), np.uint16)
        ox, oy = bbox.x0, bbox.y0
        cx = np.floor((np.arange(cols) * 0.1 + ox) / 0.1 + 1e-9)
        idx[:, cx < 10] = 1
        idx[:, cx >= 10] = 2
        return {"version": self.version, "origin": (ox, oy), "step": self.step, "shape": (rows, cols),
                "keys": ["A", "B"], "index": idx, "bbox": [ox, oy, bbox.x1, bbox.y1]}

    async def view(self, map_id, bbox, level=0, version=None, raster=False, epoch=None, parents=True):
        return {"charts": [{"key": "A", "cells": 100}, {"key": "B", "cells": 200}]}


@pytest.fixture
def patched(monkeypatch):
    def install(track=(), legs=(), recoveries=(), track_runs=(), known=("m",)):
        cursor = FakeCursor(list(track), list(legs), list(recoveries), track_runs)

        @contextlib.asynccontextmanager
        async def read_cursor(db):
            yield cursor

        @contextlib.asynccontextmanager
        async def open_store(db, pid):
            yield FakeStore(set(known))
        monkeypatch.setattr(ds.fleet_reads, "read_cursor", read_cursor)
        monkeypatch.setattr(ds.maps, "open_store", open_store)
        return cursor
    return install


def call(reads, graph=None, cache=None, **kw):
    cache = cache or ResultCache(4, 60.0)
    raw, etag = asyncio.run(ds.drive_stats(None, graph or FakeGraph({}), reads, kw.pop("map_id", "m"),
                                           cache=cache, **kw))
    return json.loads(raw), etag


class TestEndpoint:
    def test_shape_origin_and_regions(self, patched):
        patched(track=track_run("a", [0.05, 0.75, 1.45, 2.15]))
        body, etag = call(FakeReads())
        assert body["map_id"] == "m" and body["graph_version"] == 7
        assert body["cell_m"] == 0.1 and body["step"] == 0.1
        assert body["tile_fields"] == ["ix", "iy", "n", "len_m", "time_s", "recovery_s", "stopped_s"]
        # data cells x 0..21, y 0 -> origin snapped to the 6.4 m grid below them
        assert body["origin"] == [0.0, 0.0]
        assert sorted(t[0] for t in body["tiles"]) == list(range(0, 22))
        assert {t[1] for t in body["tiles"]} == {0}
        assert all(len(t) == 7 for t in body["tiles"])
        regions = {r["key"]: r for r in body["regions"]}
        assert set(regions) == {"A", "B"}
        assert regions["A"]["visited"] == 10 and regions["B"]["visited"] == 12
        assert regions["A"]["n"] == regions["B"]["n"] == 1 and regions["A"]["cells"] == 100
        assert regions["A"]["len_m"] + regions["B"]["len_m"] == pytest.approx(2.1, abs=0.01)
        assert body["source"]["scene_graph"] == "ok" and body["source"]["track_runs"] == 1
        assert etag.startswith('"')

    def test_origin_is_negative_snapped_and_index_relative(self, patched):
        patched(track=track_run("a", [-6.0, -5.25]))
        body, _ = call(FakeReads())
        assert body["origin"] == [-6.4, 0.0]
        assert sorted(t[0] for t in body["tiles"])[0] == 4              # -6.0 m = cell -60, origin cell -64

    def test_no_scene_graph_is_absolute(self, patched):
        patched(track=track_run("a", [1.05, 1.8]))
        body, _ = call(None)
        assert body["origin"] == [0.0, 0.0] and body["regions"] == [] and body["graph_version"] is None
        assert body["source"]["scene_graph"] == "none"
        assert min(t[0] for t in body["tiles"]) == 10

    def test_reads_failing_degrades(self, patched):
        patched(track=track_run("a", [1.05, 1.8]))

        class Broken(FakeReads):
            async def owner_plane(self, *a, **k):
                raise HTTPException(503, "down")
        body, _ = call(Broken())
        assert body["regions"] == [] and body["source"]["scene_graph"] == "error" and body["tiles"]

    def test_wrong_step_degrades(self, patched):
        patched(track=track_run("a", [1.05, 1.8]))
        body, _ = call(FakeReads(step=0.05))
        assert body["regions"] == [] and body["source"]["scene_graph"] == "error"

    def test_graph_off_reads_without_plane(self, patched):
        patched(track=track_run("a", [1.05, 1.8]))

        class Off(FakeReads):
            async def owner_plane(self, *a, **k):
                return None
        body, _ = call(Off())
        assert body["regions"] == [] and body["source"]["scene_graph"] == "none" and body["origin"] == [0.0, 0.0]

    def test_leg_fallback_excludes_runs_with_track(self, patched):
        legs = [("legrun", "n1", "n2", 10.0, 0.0, 0.0), ("a", "n1", "n2", 10.0, 0.0, 0.0),
                ("outside", "n1", "n2", 10.0, 0.0, 0.0)]
        patched(track=track_run("a", [0.05, 0.8]), legs=legs, track_runs=["outside"])
        body, _ = call(FakeReads(), graph=FakeGraph({"n1": (0.05, 5.05), "n2": (2.05, 5.05)}))
        src = body["source"]
        assert src["track_runs"] == 1 and src["leg_runs"] == 1 and src["legs_dropped_run_has_track"] == 2
        assert {t[1] for t in body["tiles"]} == {0, 50}

    def test_cap_marks_partial(self, patched, monkeypatch):
        monkeypatch.setattr(ds.config, "DRIVE_STATS_MAX_ROWS", 3)
        patched(track=track_run("a", [0.05, 0.8, 1.55, 2.3, 3.05]))
        body, _ = call(FakeReads())
        assert body["source"]["partial"] is True and body["source"]["track_rows"] == 3

    def test_empty(self, patched):
        patched()
        body, _ = call(FakeReads())
        assert body["tiles"] == [] and body["regions"] == [] and body["source"]["partial"] is False

    def test_unknown_map_and_bad_query(self, patched):
        patched()
        with pytest.raises(HTTPException) as e:
            call(FakeReads(), map_id="nope")
        assert e.value.status_code == 404
        with pytest.raises(HTTPException) as e:
            call(FakeReads(), min_speed=-1.0)
        assert e.value.status_code == 422

    def test_filters_reach_the_sql(self, patched):
        import datetime
        cur = patched()
        a = datetime.datetime(2026, 10, 14, tzinfo=datetime.timezone.utc)
        call(FakeReads(), start=a, end=a + datetime.timedelta(days=1), robot="r1")
        sql, args = cur.sql[0]
        assert "t.ts >= %s" in sql and "t.ts < %s" in sql and "t.robot_name = %s" in sql
        assert args[0] == "m" and args[-2] == a + datetime.timedelta(days=1) or "r1" in args
        with pytest.raises(HTTPException):
            call(FakeReads(), start=a, end=a)


class TestCache:
    def test_ttl_and_single_flight(self):
        now = [0.0]
        cache = ResultCache(2, 10.0, clock=lambda: now[0])
        runs = []

        async def compute():
            runs.append(1)
            await asyncio.sleep(0.01)
            return "v"

        async def go():
            r = await asyncio.gather(*[cache.get(("m", 1), compute) for _ in range(5)])
            assert r == ["v"] * 5 and len(runs) == 1
            await cache.get(("m", 1), compute)
            assert len(runs) == 1
            now[0] = 11.0
            await cache.get(("m", 1), compute)
            assert len(runs) == 2
            for i in range(3):
                await cache.get(("x", i), compute)
            assert len(cache._done) == 2
        asyncio.run(go())

    def test_error_is_not_cached(self):
        cache = ResultCache(2, 10.0)
        n = []

        async def bad():
            n.append(1)
            raise HTTPException(404, "x")

        async def go():
            for _ in range(2):
                with pytest.raises(HTTPException):
                    await cache.get("k", bad)
        asyncio.run(go())
        assert len(n) == 2


class TestRoute:
    def test_etag_304_and_headers(self, patched, monkeypatch):
        from types import SimpleNamespace
        import packages.api.main as main
        patched(track=track_run("a", [0.05, 0.8]))
        monkeypatch.setattr(main, "service", SimpleNamespace(database=None, graph_db=FakeGraph({})))
        monkeypatch.setattr(main, "_scene_graph_reads", lambda: FakeReads())
        monkeypatch.setattr(ds, "CACHE", ResultCache(4, 60.0))

        def get(inm=None):
            req = SimpleNamespace(headers={"if-none-match": inm} if inm else {})
            return asyncio.run(main.map_drive_stats("m", req, None, None, None, None))
        r = get()
        assert r.status_code == 200 and r.headers["cache-control"] == "private, max-age=60"
        assert json.loads(r.body)["map_id"] == "m"
        assert get(r.headers["etag"]).status_code == 304
        assert get('"other"').status_code == 200
