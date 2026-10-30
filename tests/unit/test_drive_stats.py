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
from packages.utils import run_cells_db as rdb  # noqa: E402

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
    """The queries of drive_stats. `summaries`: stored CellRows (rdb.fetch answers them by the window / robot);
    the raw queries leave out the runs the summaries part takes, as their NOT EXISTS clause does."""

    def __init__(self, track, legs, recoveries, track_runs=(), summaries=()):
        self.track, self.legs, self.recoveries, self.track_runs = track, legs, recoveries, set(track_runs)
        self.summaries = list(summaries)
        self.sql = []
        self._rows = []

    def _taken(self, sql, args):
        """Run ids of the summaries part, from the window arguments of the NOT EXISTS clause (algo, [from], [to],
        [robot])."""
        if "NOT EXISTS (SELECT 1 FROM run_cells c" not in sql:
            return set()
        tail = list(args[-1 - sql.count("c.started_at") - ("c.robot_name" in sql) - 1:-1])
        algo, rest = tail[0], tail[1:]
        lo = rest.pop(0) if "c.started_at >= %s" in sql else None
        hi = rest.pop(0) if "c.started_at < %s" in sql else None
        robot = rest.pop(0) if "c.robot_name = %s" in sql else None
        return {s.run_id for s in self.summaries if s.algo_version == algo and (lo is None or s.started_at >= lo)
                and (hi is None or s.started_at < hi) and (robot is None or s.robot_name == robot)}

    async def execute(self, sql, args=()):
        self.sql.append((sql, args))
        if "FROM robot_track_ts t JOIN" in sql:
            taken = self._taken(sql, args)
            self._rows = sorted((r for r in self.track if r[0] not in taken), key=lambda r: -r[1])[:args[-1]]
        elif "NAV.RECOVERY_EXITED" in sql:
            self._rows = self.recoveries
        elif "FROM run_legs" in sql:
            taken = self._taken(sql, args)
            self._rows = [r for r in self.legs if r[0] not in taken]
        elif "SELECT DISTINCT run_id" in sql:
            self._rows = [(r,) for r in self.track_runs if r in args[0]]
        elif "FROM run_cells WHERE map_id" in sql:
            lo = args[2] if "started_at >= %s" in sql else None
            self._rows = [rdb_stored(s) for s in self.summaries if lo is None or s.started_at >= lo]
        else:
            raise AssertionError(sql)

    async def fetchall(self):
        return list(self._rows)


def rdb_stored(row):
    """A CellRow as the SELECT of run_cells_db.COLUMNS returns it."""
    p = rdb.encode(row)
    return tuple(p[:len(rdb.COLUMNS) - 1]) + (row.created_at,)


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
    def install(track=(), legs=(), recoveries=(), track_runs=(), known=("m",), summaries=()):
        cursor = FakeCursor(list(track), list(legs), list(recoveries), track_runs, summaries)

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
        sql, args = next(c for c in cur.sql if "FROM robot_track_ts" in c[0])
        assert "t.ts >= %s" in sql and "t.ts < %s" in sql and "t.robot_name = %s" in sql
        assert args[0] == "m" and "r1" in args
        sql, args = next(c for c in cur.sql if "FROM run_cells" in c[0])           # the summaries part, same window
        assert "started_at >= %s" in sql and "started_at < %s" in sql and "robot_name = %s" in sql
        assert a in args and "r1" in args
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


# --- stored per-run summaries (docs/region_stats_2026-10-27.md 6.5) --------------------------------

import datetime as _dt  # noqa: E402

from packages.api import run_cells as rcells  # noqa: E402

DAY0 = _dt.datetime(2026, 10, 14, 0, 0, tzinfo=_dt.timezone.utc)


def summarise(run, rows, recoveries=(), started=None, map_id="m"):
    """The stored summary of one fixture run (rows as track_run builds them)."""
    started = started or _dt.datetime.fromtimestamp(rows[0][1], _dt.timezone.utc)
    data = rcells.RunData(run, "r1", map_id, started, started + _dt.timedelta(minutes=5),
                          [(r[1], r[2], r[3], r[4], r[5], map_id) for r in rows],
                          [(t, d) for rid, t, d in recoveries if rid == run], [], [])
    [row] = rcells.summarise_rows(data, lambda m: {}, None)
    return row


def two_runs():
    a = track_run("a", [0.05, 0.4, 0.8, 0.8, 1.2, 1.9], t0=DAY0.timestamp() + 3600)
    b = track_run("b", [0.5, 0.9, 1.3, 1.3, 1.3, 2.0, 2.4], y=0.35, t0=DAY0.timestamp() + 7200)
    rec = [("a", a[3][1] + 2.0, 1.2), ("b", b[4][1] + 0.5, 0.8)]
    return a, b, rec


def tile_dict(body):
    return {(t[0], t[1]): t[2:] for t in body["tiles"]}


class TestSummaries:
    def both(self, patched, monkeypatch, summarised=("a", "b"), **kw):
        a, b, rec = two_runs()
        track = a + b
        sums = [summarise(r, rows, rec) for r, rows in (("a", a), ("b", b)) if r in summarised]
        patched(track=track, recoveries=rec, summaries=sums)
        mixed, _ = call(FakeReads(), **kw)
        monkeypatch.setattr(ds.config, "DRIVE_STATS_SOURCE", "raw")
        raw, _ = call(FakeReads(), cache=ResultCache(4, 60.0), **kw)
        monkeypatch.undo()
        return mixed, raw

    def test_every_run_summarised_equals_the_raw_answer(self, patched, monkeypatch):
        mixed, raw = self.both(patched, monkeypatch)
        assert mixed["source"]["basis"] == "summaries" and raw["source"]["basis"] == "raw"
        assert mixed["source"]["summary_runs"] == 2 and mixed["source"]["raw_runs"] == 0
        assert raw["source"]["summary_runs"] == 0 and raw["source"]["raw_runs"] == 2
        assert tile_dict(mixed).keys() == tile_dict(raw).keys()
        for cell, vals in tile_dict(raw).items():
            assert tile_dict(mixed)[cell] == pytest.approx(vals, abs=1e-6)
        assert len(mixed["regions"]) == len(raw["regions"]) == 2
        for rm, rr in zip(mixed["regions"], raw["regions"]):
            assert rm.keys() == rr.keys()
            for k, v in rr.items():
                assert rm[k] == pytest.approx(v, abs=0.06), k           # the regions round to 0.01 m / 0.1 s
        for k in ("track_runs", "track_segments", "track_pairs_rejected", "recovery_unattributed_s", "tiles_track"):
            assert mixed["source"][k] == pytest.approx(raw["source"][k])
        assert mixed["source"]["partial"] is False

    def test_a_run_not_summarised_yet_comes_from_the_raw_rows_once(self, patched, monkeypatch):
        mixed, raw = self.both(patched, monkeypatch, summarised=("a",))
        assert mixed["source"]["summary_runs"] == 1 and mixed["source"]["raw_runs"] == 1
        assert mixed["source"]["track_runs"] == 2
        assert tile_dict(mixed).keys() == tile_dict(raw).keys()
        for cell, vals in tile_dict(raw).items():
            assert tile_dict(mixed)[cell] == pytest.approx(vals, abs=1e-6)       # nothing missing, nothing doubled
        for rm, rr in zip(mixed["regions"], raw["regions"]):
            assert rm["n"] == rr["n"] and rm["len_m"] == pytest.approx(rr["len_m"], abs=0.01)

    def test_other_min_speed_reads_raw_and_says_so(self, patched, monkeypatch):
        a, b, rec = two_runs()
        patched(track=a + b, recoveries=rec, summaries=[summarise("a", a, rec), summarise("b", b, rec)])
        body, _ = call(FakeReads(), min_speed=0.2)
        assert body["source"]["basis"] == "raw" and body["source"]["basis_reason"] == "min_speed"
        assert body["source"]["summary_runs"] == 0 and body["source"]["raw_runs"] == 2

    def test_config_raw_never_reads_the_summaries(self, patched, monkeypatch):
        a, b, rec = two_runs()
        cur = patched(track=a + b, recoveries=rec, summaries=[summarise("a", a, rec)])
        monkeypatch.setattr(ds.config, "DRIVE_STATS_SOURCE", "raw")
        body, _ = call(FakeReads())
        assert body["source"]["basis_reason"] == "config" and not any("run_cells" in s for s, _ in cur.sql)

    def test_runs_started_outside_the_window_are_not_summed_from_summaries(self, patched, monkeypatch):
        a, b, rec = two_runs()
        old = summarise("a", a, rec, started=DAY0 - _dt.timedelta(days=3))
        patched(track=a + b, recoveries=rec, summaries=[old, summarise("b", b, rec)])
        body, _ = call(FakeReads(), start=DAY0, end=DAY0 + _dt.timedelta(days=1))
        # b from its summary; a started before the window, so its rows inside the window come raw (the old rule)
        assert body["source"]["summary_runs"] == 1 and body["source"]["raw_runs"] == 1

    def test_partial_is_only_the_raw_part_hitting_the_cap(self, patched, monkeypatch):
        a, b, rec = two_runs()
        patched(track=a + b, recoveries=rec, summaries=[summarise("a", a, rec)])
        monkeypatch.setattr(ds.config, "DRIVE_STATS_MAX_ROWS", 3)
        body, _ = call(FakeReads())
        assert body["source"]["partial"] is True and body["source"]["track_rows"] == 3
        patched(track=a + b, recoveries=rec, summaries=[summarise("a", a, rec), summarise("b", b, rec)])
        body, _ = call(FakeReads(), cache=ResultCache(4, 60.0))
        assert body["source"]["partial"] is False and body["source"]["track_rows"] == 0

    def test_the_client_keys_survive(self, patched):
        a, b, rec = two_runs()
        patched(track=a, summaries=[summarise("a", a, rec)])
        body, _ = call(FakeReads())
        for k in ("track_runs", "track_rows", "track_segments", "track_pairs_rejected", "leg_runs", "leg_rows",
                  "legs_skipped", "legs_dropped_run_has_track", "recovery_unattributed_s", "partial",
                  "tiles_track", "tiles_leg_only"):
            assert k in body["source"]
        assert body["tile_fields"] == ["ix", "iy", "n", "len_m", "time_s", "recovery_s", "stopped_s"]

    def test_legs_summaries_count_as_leg_runs(self, patched):
        leg = (1, "n1", "n2", 10.0, 2.0, 1.0, DAY0.timestamp(), DAY0.timestamp() + 10, "m")
        data = rcells.RunData("L", "r1", "m", DAY0, DAY0 + _dt.timedelta(minutes=1), [], [], [], [leg])
        [row] = rcells.summarise_rows(data, lambda m: {"n1": (0.05, 5.05), "n2": (2.05, 5.05)}, None)
        patched(summaries=[row])
        body, _ = call(FakeReads())
        assert body["source"]["leg_runs"] == 1 and body["source"]["track_runs"] == 0
        assert body["source"]["leg_rows"] == 1 and {t[1] for t in body["tiles"]} == {50}


class TestSeriesReusesTheLoad:
    def test_open_to_loads_once(self, patched):
        """The client sends `from` only: the series window ends at "now" too, and shares the one load."""
        cur = patched(track=track_run("a", [0.05, 0.8], t0=DAY0.timestamp() + 60))
        call(FakeReads(), start=DAY0, series="day")
        assert sum("FROM robot_track_ts t JOIN" in sql for sql, _ in cur.sql) == 1

    def test_default_window_differs_and_loads_again(self, patched):
        cur = patched(track=track_run("a", [0.05, 0.8], t0=DAY0.timestamp() + 60))
        call(FakeReads(), series="day")
        assert sum("FROM robot_track_ts t JOIN" in sql for sql, _ in cur.sql) == 2


class TestSeriesFromSummaries:
    def test_equals_the_raw_series_when_no_run_crosses_midnight(self, patched, monkeypatch):
        a, b, rec = two_runs()
        sums = [summarise("a", a, rec), summarise("b", b, rec)]
        patched(track=a + b, recoveries=rec, summaries=sums)
        kw = dict(start=DAY0, end=DAY0 + _dt.timedelta(days=2), series="day")
        mixed, _ = call(FakeReads(), **kw)
        monkeypatch.setattr(ds.config, "DRIVE_STATS_SOURCE", "raw")
        raw, _ = call(FakeReads(), cache=ResultCache(4, 60.0), **kw)
        sm, sr = mixed["series"], raw["series"]
        for k in ("len_m", "time_s", "recovery_share", "stopped_share", "new_tiles", "cumulative_tiles"):
            assert sm[k] == pytest.approx(sr[k], abs=1e-3), k
        # the moving speed uses len / (time - stopped): within the stopped segments' own length (<= 0.05 m/s)
        assert sm["moving_speed_mps"][0] == pytest.approx(sr["moving_speed_mps"][0], abs=0.05)
        assert sm["source"]["summary_runs"] == 2 and sr["source"]["summary_runs"] == 0

    def test_a_run_over_midnight_belongs_to_the_day_it_started(self, patched):
        rows = track_run("x", [0.05, 0.5, 0.95, 1.4], t0=DAY0.timestamp() + 86400 - 2.0)      # crosses the midnight
        row = summarise("x", rows)
        patched(summaries=[row])
        body, _ = call(FakeReads(), start=DAY0, end=DAY0 + _dt.timedelta(days=3), series="day")
        s = body["series"]
        assert s["len_m"][0] > 0 and s["len_m"][1] == 0.0
        assert s["new_tiles"][0] == s["cumulative_tiles"][2] > 0

    def test_legs_summaries_are_not_in_the_series(self, patched):
        leg = (1, "n1", "n2", 10.0, 0.0, 0.0, DAY0.timestamp(), DAY0.timestamp() + 10, "m")
        data = rcells.RunData("L", "r1", "m", DAY0, DAY0 + _dt.timedelta(minutes=1), [], [], [], [leg])
        [row] = rcells.summarise_rows(data, lambda m: {"n1": (0.05, 5.05), "n2": (2.05, 5.05)}, None)
        patched(summaries=[row])
        body, _ = call(FakeReads(), start=DAY0, end=DAY0 + _dt.timedelta(days=1), series="day")
        assert body["series"]["len_m"] == [0.0] and body["series"]["source"]["leg_only_runs_not_in_series"] == 1


class TestRegionsOfPinsOneVersion:
    """`regions_of` reads a map over 160 m in several chunks; every chunk must come from the same graph version."""

    # three cells in three different 160 m chunks (which start at cell 0, 1600, 3200)
    AX, AY = np.array([2, 1602, 3202], np.int64), np.zeros(3, np.int64)

    class Reads:
        """The owner plane has key `v<version>` (so a chart of another version is recognisable); `versions` is the
        version each successive owner_plane call sees, the last one repeated."""

        def __init__(self, versions, view_versions=None):
            self.versions, self.calls, self.views = list(versions), 0, []
            self.view_versions = view_versions

        async def owner_plane(self, map_id, bbox, level=0):
            v = self.versions[min(self.calls, len(self.versions) - 1)]
            self.calls += 1
            idx = np.ones((4, 4), np.uint16)
            return {"version": v, "origin": (bbox.x0, bbox.y0), "step": 0.1, "shape": (4, 4), "keys": [f"v{v}"],
                    "index": idx, "bbox": [bbox.x0, bbox.y0, bbox.x1, bbox.y1]}

        async def view(self, map_id, bbox, level=0, version=None, raster=False, epoch=None, parents=True):
            self.views.append(version)
            if self.view_versions is not None:
                v = self.view_versions(version)
                if v is None:
                    raise HTTPException(404, "version gone")
                version = v
            return {"version": version, "charts": [{"key": f"v{version}", "cells": 7}]}

    def run(self, reads):
        return asyncio.run(ds.regions_of(reads, "m", self.AX, self.AY))

    def test_one_version_across_all_chunks(self):
        reads = self.Reads([4])
        ids, names, cells, version, status = self.run(reads)
        assert status == "ok" and version == 4 and names == ["v4"] and cells == {"v4": 7}
        assert ids.tolist() == [1, 1, 1] and reads.calls == 3 and set(reads.views) == {4}

    def test_a_version_change_after_the_first_chunk_reads_again_on_the_new_version(self):
        reads = self.Reads([4, 5, 5, 5, 5])             # chunk 1 sees v4, chunk 2 v5: start over, all v5
        ids, names, cells, version, status = self.run(reads)
        assert status == "ok" and version == 5 and names == ["v5"] and ids.tolist() == [1, 1, 1]
        assert cells == {"v5": 7} and reads.calls == 5

    def test_a_version_that_keeps_changing_is_an_error_not_a_mixture(self):
        reads = self.Reads([1, 2, 3, 4, 5, 6, 7, 8, 9])
        ids, names, cells, version, status = self.run(reads)
        assert status == "error" and ids is None and names == [] and cells == {} and version is None

    def test_the_chart_cell_views_are_asked_for_the_pinned_version(self):
        reads = self.Reads([6])
        self.run(reads)
        assert reads.views == [6, 6, 6]

    def test_a_pinned_version_that_is_gone_makes_the_read_start_over(self):
        gone = {"once": True}

        def view_versions(version):
            if gone["once"]:
                gone["once"] = False
                return None                              # 404 on the first view
            return version
        reads = self.Reads([4], view_versions)
        ids, names, cells, version, status = self.run(reads)
        assert status == "ok" and version == 4 and ids.tolist() == [1, 1, 1]

    def test_a_view_of_another_version_makes_the_read_start_over(self):
        reads = self.Reads([4, 4, 4, 4, 4, 4], lambda v: 5 if reads.calls < 2 else v)
        ids, names, cells, version, status = self.run(reads)
        assert status == "ok" and ids.tolist() == [1, 1, 1] and cells == {"v4": 7}

    def test_a_single_chunk_needs_no_retry(self):
        reads = self.Reads([9])
        out = asyncio.run(ds.regions_of(reads, "m", np.array([1, 2], np.int64), np.array([0, 0], np.int64)))
        assert out[3] == 9 and reads.calls == 1
