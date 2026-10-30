"""packages/api/region_stats.py (build from run_cells, the read route, maps_needing_build) and the planner's loader
packages/utils/region_stats_load.py, with fakes (docs/region_stats_2026-10-27.md 6.3, 6.4, 8)."""
import asyncio
import contextlib
import datetime
import json
import os
import uuid

os.environ.setdefault("POSTGRES_PASSWORD", "test")

import numpy as np  # noqa: E402
import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

pytest.importorskip("psycopg")

from packages import config  # noqa: E402
from packages.api import region_stats as rs  # noqa: E402
from packages.api.result_cache import ResultCache  # noqa: E402
from packages.utils import cell_raster as cr  # noqa: E402
from packages.utils import region_stats_load as loader  # noqa: E402
from packages.utils import run_cells_db as rdb  # noqa: E402

pytestmark = pytest.mark.unit

NOW = datetime.datetime.now(datetime.timezone.utc)
BUILT = json.dumps({"builder": rs.BUILDER_VERSION})
CLASS_A, CLASS_B = "AMR|0.80x0.50", "AMR|1.00x0.60"


def make_run(i, cells_x, cls=CLASS_A, len_per_cell=0.12, pace=2.0, stopped_per_cell=0.0, age_h=0, blocks_at=(),
             speed_max=1.2, dwell_per_cell=0.0, speed_basis="factsheet"):
    """A run driving along y = 0 over the given cell columns."""
    n = len(cells_x)
    key = cr.cell_key(np.array(cells_x), np.zeros(n, np.int64))
    order = np.argsort(key)
    blocks = np.zeros(n, np.int64)
    for j, x in enumerate(cells_x):
        blocks[j] = 1 if x in blocks_at else 0
    t = NOW - datetime.timedelta(hours=age_h + i)
    return rdb.CellRow(
        run_id=uuid.UUID(int=i), map_id="m", robot_name=f"r{i}", robot_class=cls, speed_max=speed_max,
        started_at=t, ended_at=t, source="track", algo_version=config.RUN_CELLS_ALGO_VERSION, cell_m=0.1, params={},
        frames=[], key=key[order], len_m=np.full(n, len_per_cell)[order], time_s=np.full(n, len_per_cell * pace)[order],
        stopped_s=np.full(n, stopped_per_cell)[order], recovery_s=np.zeros(n)[order], blocks=blocks[order],
        totals={}, created_at=t, dwell_s=np.full(n, dwell_per_cell)[order], speed_basis=speed_basis)


def stored(row):
    p = rdb.encode(row)
    return tuple(p[:len(rdb.COLUMNS) - 1]) + (row.created_at,)


class Reads:
    """Owner plane over x cells [0, 64) with a chart boundary list: [(first cell, key)...]; 0.1 m cells."""

    def __init__(self, version, boundaries, cells=None, graph=True):
        self.version, self.boundaries, self.cells, self.graph = version, boundaries, cells or {}, graph
        self.keys = [k for _, k in boundaries]

    async def topology(self, map_id):
        return {"version": self.version, "keys": set(self.keys)} if self.graph else None

    async def owner_plane(self, map_id, bbox, level=0):
        if not self.graph:
            return None
        rows = cols = 64 * 25 * 2
        ox = round(bbox.x0 / 0.1)
        cx = np.arange(cols) + ox
        idx = np.zeros((rows, cols), np.uint16)
        for n, (first, _key) in enumerate(self.boundaries, 1):
            idx[:, cx >= first] = n
        return {"version": self.version, "origin": (bbox.x0, bbox.y0), "step": 0.1, "shape": (rows, cols),
                "keys": self.keys, "index": idx, "bbox": [bbox.x0, bbox.y0, bbox.x1, bbox.y1]}

    async def view(self, map_id, bbox, level=0, version=None, raster=False, epoch=None, parents=True):
        return {"charts": [{"key": k, "cells": self.cells.get(k, 100)} for k in self.keys]}


class Cur:
    def __init__(self, db):
        self.db = db

    async def execute(self, sql, args=()):
        self.db.executed.append((sql, args))
        s = " ".join(sql.split())
        if s.startswith("SELECT run_id, map_id") or "FROM run_cells WHERE map_id" in s and "SELECT run_id," in s and "map_id, robot_name" in s:
            rows = list(self.db.runs)
            self.rows = [stored(r) for r in rows if (not self.db.robot or r.robot_name == self.db.robot)]
        elif s.startswith("SELECT gen FROM region_stats_inputs"):
            self.rows = [(self.db.gen_locked if "FOR UPDATE" in s and self.db.gen_locked is not None else self.db.gen,)]
        elif s.startswith("SELECT 1 FROM") and "FOR SHARE" in s:
            self.rows = [(1,)] if self.db.map_live else []
        elif s.startswith("SELECT run_id, created_at FROM run_cells"):
            self.rows = [(r.run_id, r.created_at) for r in self.db.runs]
        elif s.startswith("SELECT DISTINCT map_id FROM run_cells"):
            self.rows = [(m,) for m in sorted({r.map_id for r in self.db.runs})]
        elif s.startswith("SELECT map_id, graph_version, status, detail, built_at, runs FROM region_stats_builds"):
            self.rows = list(self.db.builds)
        elif s.startswith("SELECT graph_version, stamp, built_at, status, runs FROM region_stats_builds"):
            rows = [b for b in self.db.build_rows if args[0] == b[0] and (len(args) < 2 or args[1] == b[1])]
            self.rows = [r[1:] for r in sorted(rows, key=lambda r: -r[1])[:1]]
        elif s.startswith("SELECT map_id, graph_version, chart_key"):
            self.rows = [tuple(r[c] for c in rs.COLUMNS) for r in self.db.stat_rows
                         if r["graph_version"] == args[1] and r["robot_class"] == args[2]]
        else:
            self.rows = []

    async def executemany(self, sql, seq):
        self.db.executed.append((sql, "many"))
        self.db.inserted.extend(seq)

    async def fetchall(self):
        return list(self.rows)

    async def fetchone(self):
        return self.rows[0] if self.rows else None


class Db:
    def __init__(self, runs=(), robot=None):
        self.runs, self.robot, self.executed, self.inserted = list(runs), robot, [], []
        self.builds, self.build_rows, self.stat_rows = [], [], []
        self.map_live = True
        self.gen, self.gen_locked = 0, None       # region_stats_inputs.gen; gen_locked = what a writer made it meanwhile

    @contextlib.asynccontextmanager
    async def connection(self):
        class Conn:
            def cursor(conn):
                @contextlib.asynccontextmanager
                async def c():
                    yield Cur(self)
                return c()
        yield Conn()


class Store:
    def __init__(self, known):
        self.known = known

    async def get_map(self, name):
        return object() if name in self.known else None


@pytest.fixture
def patched(monkeypatch):
    def install(db, known=("m",)):
        @contextlib.asynccontextmanager
        async def read_cursor(_db):
            yield Cur(db)

        @contextlib.asynccontextmanager
        async def open_store(_db, _pid):
            yield Store(set(known))
        monkeypatch.setattr(rs.fleet_reads, "read_cursor", read_cursor)
        monkeypatch.setattr(rs.maps, "open_store", open_store)
        monkeypatch.setattr(rs, "CACHE", ResultCache(8, 60.0))
        return db
    return install


def by_key(stats):
    return {(s["chart_key"], s["robot_class"]): s for s in stats}


def collect(db, reads, robot=None):
    return asyncio.run(rs.collect(db, reads, "m", robot=robot))


class TestAggregate:
    def test_one_run_across_two_charts(self, patched):
        db = patched(Db([make_run(1, range(0, 30), stopped_per_cell=0.02, blocks_at=(3, 15))]))
        got = collect(db, Reads(5, [(-10_000, "A"), (10, "B")], cells={"A": 500, "B": 700}))
        assert got.status == "ok" and got.version == 5
        s = by_key(got.stats)
        a, b = s[("A", CLASS_A)], s[("B", CLASS_A)]
        assert a["runs"] == 1 and a["len_m"] == pytest.approx(1.2) and b["len_m"] == pytest.approx(2.4)
        assert a["time_s"] == pytest.approx(2.4) and a["pace_median_s_per_m"] == pytest.approx(2.0)
        assert a["pace_p90_s_per_m"] == pytest.approx(2.0)
        assert a["stopped_s"] == pytest.approx(0.2) and a["stopped_share"] == pytest.approx(0.2 / 2.4)
        assert a["moving_speed_mps"] == pytest.approx(1.2 / 2.2)
        assert a["moving_pace_median_s_per_m"] == pytest.approx(2.2 / 1.2)
        assert (a["blocks"], b["blocks"]) == (1, 1) and a["blocks_per_run"] == 1.0
        assert (a["cells"], b["cells"]) == (500, 700) and (a["visited"], b["visited"]) == (10, 20)
        assert a["speed_max"] == pytest.approx(1.2) and a["recovery_s_per_100m"] == 0.0
        assert a["first_at"] == a["last_at"] == db.runs[0].started_at and a["stamp"] == got.stamp
        assert s[("A", "*")]["runs"] == 1 and s[("A", "*")]["speed_max"] is None
        assert s[("A", "*")]["len_m"] == a["len_m"]

    def test_the_sample_gate_keeps_obstructed_short_passes(self, patched):
        reads = Reads(1, [(-10_000, "A")])
        # 0.6 m driven (below REGION_STATS_MIN_LEN_M) in each case
        plain = make_run(1, range(0, 5))
        blocked = make_run(2, range(0, 5), blocks_at=(2,))
        waited = make_run(3, range(0, 5), stopped_per_cell=2.5)              # 12.5 s stopped
        brief = make_run(4, range(0, 5), stopped_per_cell=1.0)               # 5 s: not enough
        got = collect(patched(Db([plain, blocked, waited, brief])), reads)
        a = by_key(got.stats)[("A", CLASS_A)]
        assert a["runs"] == 2 and a["blocks"] == 1 and a["stopped_s"] == pytest.approx(12.5)
        # no sample reached the length gate: no pace, but the event figures are there
        assert a["pace_median_s_per_m"] is None and a["pace_p90_s_per_m"] is None
        assert a["moving_pace_median_s_per_m"] is None and a["stopped_share"] is not None

    def test_pace_percentiles_use_only_the_long_samples(self, patched):
        long_run = make_run(1, range(0, 30), pace=2.0)
        short_blocked = make_run(2, range(0, 5), pace=50.0, blocks_at=(1,), age_h=1)
        got = collect(patched(Db([long_run, short_blocked])), Reads(1, [(-10_000, "A")]))
        a = by_key(got.stats)[("A", CLASS_A)]
        assert a["runs"] == 2 and a["blocks"] == 1
        assert a["pace_median_s_per_m"] == pytest.approx(2.0) and a["pace_p90_s_per_m"] == pytest.approx(2.0)

    def test_dwell_is_out_of_the_stopped_share_and_the_moving_pace(self, patched):
        run = make_run(1, range(0, 30), stopped_per_cell=0.02, dwell_per_cell=0.1)
        a = by_key(collect(patched(Db([run])), Reads(1, [(-10_000, "A")])).stats)[("A", CLASS_A)]
        time_s, stopped, dwell = 30 * 0.24, 30 * 0.02, 30 * 0.1
        assert a["pace_median_s_per_m"] == pytest.approx(2.0)                    # gross: dwell and stops included
        assert a["stopped_share"] == pytest.approx(stopped / (time_s - dwell))
        assert a["moving_pace_median_s_per_m"] == pytest.approx((time_s - stopped - dwell) / 3.6)

    def test_totals_are_conserved(self, patched):
        run = make_run(1, range(0, 30))
        got = collect(patched(Db([run])), Reads(1, [(-10_000, "A"), (10, "B")]))
        pooled = [s for s in got.stats if s["robot_class"] == "*"]
        assert sum(s["len_m"] for s in pooled) == pytest.approx(float(run.len_m.sum()))
        assert sum(s["time_s"] for s in pooled) == pytest.approx(float(run.time_s.sum()))

    def test_a_chart_split_between_versions_regroups_the_same_cells(self, patched):
        db = patched(Db([make_run(1, range(0, 40)), make_run(2, range(5, 35), age_h=1)]))
        v1 = collect(db, Reads(1, [(-10_000, "A")]))
        v2 = collect(db, Reads(2, [(-10_000, "A1"), (20, "A2")]))
        assert {s["chart_key"] for s in v1.stats} == {"A"}
        assert {s["chart_key"] for s in v2.stats} == {"A1", "A2"}
        a = by_key(v1.stats)[("A", "*")]
        parts = [by_key(v2.stats)[(k, "*")] for k in ("A1", "A2")]
        for f in ("len_m", "time_s", "stopped_s", "recovery_s", "visited"):
            assert sum(p[f] for p in parts) == pytest.approx(a[f])
        assert a["runs"] == 2 and all(p["runs"] == 2 for p in parts)
        assert v1.stamp == v2.stamp                      # same inputs

    def test_samples_below_the_minimum_length_are_dropped(self, patched):
        db = patched(Db([make_run(1, range(0, 30)), make_run(2, range(8, 12))]))      # run 2 clips A with 2 cells = 0.24 m
        s = by_key(collect(db, Reads(1, [(-10_000, "A"), (10, "B")])).stats)
        assert s[("A", "*")]["runs"] == 1 and s[("B", "*")]["runs"] == 1             # run 2: 0.24 m in A, 0.24 m in B
        assert s[("A", "*")]["len_m"] == pytest.approx(1.2)

    def test_newest_runs_only(self, patched, monkeypatch):
        monkeypatch.setattr(config, "REGION_STATS_MAX_RUNS", 2)
        db = patched(Db([make_run(i, range(0, 20), age_h=i * 10, pace=float(i + 1)) for i in (1, 2, 3)]))
        a = by_key(collect(db, Reads(1, [(-10_000, "A")])).stats)[("A", CLASS_A)]
        assert a["runs"] == 2 and a["pace_median_s_per_m"] == pytest.approx(2.5)     # paces 2 and 3 (runs 1, 2)
        assert a["len_m"] == pytest.approx(2 * 2.4)

    def test_percentiles_over_samples(self, patched):
        db = patched(Db([make_run(i, range(0, 20), pace=p, age_h=i) for i, p in ((1, 2.0), (2, 3.0), (3, 5.0))]))
        a = by_key(collect(db, Reads(1, [(-10_000, "A")])).stats)[("A", CLASS_A)]
        assert a["pace_median_s_per_m"] == pytest.approx(3.0) and a["pace_p90_s_per_m"] == pytest.approx(4.6)

    def test_pooled_row_spans_classes_and_class_rows_stay_apart(self, patched):
        db = patched(Db([make_run(1, range(0, 20), cls=CLASS_A), make_run(2, range(0, 20), cls=CLASS_B, speed_max=1.5),
                         make_run(3, range(0, 20), cls="")]))
        s = by_key(collect(db, Reads(1, [(-10_000, "A")])).stats)
        assert {k[1] for k in s} == {CLASS_A, CLASS_B, "", "*"}
        assert s[("A", "*")]["runs"] == 3 and s[("A", CLASS_B)]["runs"] == 1
        assert s[("A", CLASS_B)]["speed_max"] == 1.5 and s[("A", "")]["speed_max"] == 1.2

    def test_sums_are_normalised_by_each_runs_own_speed_max(self, patched):
        """One class, two speed_max values (a speed change): the history pools, the pace is relative to each run's speed."""
        # run 1: speed_max 1.0, pace 2 s/m -> 2.0 x ideal; run 2: speed_max 2.0, pace 1 s/m -> 2.0 x ideal as well
        db = patched(Db([make_run(1, range(0, 20), pace=2.0, speed_max=1.0),
                         make_run(2, range(0, 20), pace=1.0, speed_max=2.0, age_h=1)]))
        a = by_key(collect(db, Reads(1, [(-10_000, "A")])).stats)[("A", CLASS_A)]
        assert a["runs"] == 2 and a["norm_runs"] == 2 and a["speed_max"] is None          # mixed speeds: no single one
        assert a["norm_len_m"] == pytest.approx(4.8)
        assert a["norm_moving_m"] / a["norm_len_m"] == pytest.approx(2.0)
        assert a["norm_stopped_m"] == 0.0

    def test_normalised_stopped_time_scales_with_speed(self, patched):
        run = make_run(1, range(0, 20), pace=2.0, speed_max=0.5, stopped_per_cell=0.12)     # 0.12 s stopped per 0.12 m
        a = by_key(collect(patched(Db([run])), Reads(1, [(-10_000, "A")])).stats)[("A", CLASS_A)]
        assert a["norm_stopped_m"] == pytest.approx(20 * 0.12 * 0.5)
        assert a["norm_moving_m"] == pytest.approx(20 * (0.24 - 0.12) * 0.5)

    def test_placeholder_and_unknown_speed_runs_stay_out_of_the_normalised_sums(self, patched):
        db = patched(Db([make_run(1, range(0, 20), speed_max=0.24),
                         make_run(2, range(0, 20), speed_max=1.0, speed_basis="placeholder", age_h=1),
                         make_run(3, range(0, 20), speed_max=None, speed_basis="unknown", age_h=2)]))
        a = by_key(collect(db, Reads(1, [(-10_000, "A")])).stats)[("A", CLASS_A)]
        assert a["runs"] == 3 and a["len_m"] == pytest.approx(3 * 2.4)                      # raw sums keep every run
        assert a["norm_runs"] == 1 and a["norm_len_m"] == pytest.approx(2.4)

    def test_a_placeholder_class_has_no_normalised_sums(self, patched):
        run = make_run(1, range(0, 20), cls="CARRIER|?x?", speed_max=1.0, speed_basis="placeholder")
        got = by_key(collect(patched(Db([run])), Reads(1, [(-10_000, "A")])).stats)
        assert got[("A", "CARRIER|?x?")]["norm_runs"] == 0 and got[("A", "*")]["norm_len_m"] == 0.0

    def test_dwell_is_kept_out_of_stopped_and_out_of_moving(self, patched):
        run = make_run(1, range(0, 20), stopped_per_cell=0.02, dwell_per_cell=0.04)
        a = by_key(collect(patched(Db([run])), Reads(1, [(-10_000, "A")])).stats)[("A", CLASS_A)]
        assert a["stopped_s"] == pytest.approx(0.4) and a["dwell_s"] == pytest.approx(0.8)
        assert a["stopped_share"] == pytest.approx(0.4 / (4.8 - 0.8))             # builder 2: the dwell is not in the denominator
        assert a["moving_speed_mps"] == pytest.approx(2.4 / (4.8 - 0.4 - 0.8))
        assert a["norm_moving_m"] == pytest.approx((4.8 - 0.4 - 0.8) * 1.2)

    def test_zero_denominators_are_null(self, patched):
        run = make_run(1, range(0, 20), stopped_per_cell=0.24)               # all time stopped
        a = by_key(collect(patched(Db([run])), Reads(1, [(-10_000, "A")])).stats)[("A", CLASS_A)]
        assert a["moving_speed_mps"] is None and a["moving_pace_median_s_per_m"] is None and a["stopped_share"] == 1.0

    def test_empty_map(self, patched):
        got = collect(patched(Db([])), Reads(4, [(-10_000, "A")]))
        assert got.status == "ok" and got.version == 4 and got.stats == []

    def test_no_scene_graph(self, patched):
        db = patched(Db([make_run(1, range(0, 20))]))
        assert collect(db, Reads(1, [(0, "A")], graph=False)).status == "none"
        assert collect(db, None).status == "none"

    def test_robot_filter_goes_to_the_query(self, patched):
        db = patched(Db([make_run(1, range(0, 20)), make_run(2, range(0, 20))], robot="r2"))
        got = collect(db, Reads(1, [(-10_000, "A")]), robot="r2")
        assert by_key(got.stats)[("A", "*")]["runs"] == 1

    def test_stamp_follows_the_inputs(self, patched):
        r1, r2 = make_run(1, range(0, 20)), make_run(2, range(0, 20))
        assert rs.stamp_of([r1]) != rs.stamp_of([r1, r2]) and rs.stamp_of([r2, r1]) == rs.stamp_of([r1, r2])
        assert len(rs.stamp_of([])) == 16


class TestBuildMap:
    def test_writes_one_transaction_and_prunes(self, patched):
        db = patched(Db([make_run(1, range(0, 30))]))
        out = asyncio.run(rs.build_map(db, Reads(3, [(-10_000, "A"), (10, "B")]), "m"))
        assert out["graph_version"] == 3 and out["status"] == "ok" and out["runs"] == 1 and out["charts"] == 4
        sqls = [" ".join(s.split())[:50] for s, _ in db.executed if "region_stats" in s and "region_stats_inputs" not in s]
        assert sqls[0].startswith("DELETE FROM region_stats WHERE map_id")
        assert any("many" == a for _, a in db.executed) and len(db.inserted) == 4
        assert any(s.startswith("INSERT INTO region_stats_builds") for s in sqls)
        assert sqls[-2].startswith("DELETE FROM region_stats WHERE map_id")      # the old versions go last
        assert sqls[-1].startswith("DELETE FROM region_stats_builds WHERE map_id")

    def test_no_scene_graph_stores_a_build_row_only(self, patched):
        db = patched(Db([make_run(1, range(0, 30))]))
        out = asyncio.run(rs.build_map(db, Reads(3, [(0, "A")], graph=False), "m"))
        assert out["status"] == "no_scene_graph" and out["graph_version"] == rs.NO_GRAPH_VERSION and db.inserted == []

    def test_inputs_changed_during_the_build_stores_nothing(self, patched):
        db = patched(Db([make_run(1, range(0, 30))]))
        db.gen, db.gen_locked = 4, 5
        out = asyncio.run(rs.build_map(db, Reads(3, [(-10_000, "A")]), "m"))
        assert out["status"] == "inputs_changed" and db.inserted == []
        assert not any(s.startswith("DELETE") or "INSERT INTO region_stats_builds" in s for s, _ in db.executed)

    def test_the_generation_built_from_is_stored(self, patched):
        db = patched(Db([make_run(1, range(0, 30))]))
        db.gen = 4
        asyncio.run(rs.build_map(db, Reads(3, [(-10_000, "A")]), "m"))
        sql, args = [e for e in db.executed if e[0].startswith("INSERT INTO region_stats_builds")][0]
        assert "inputs_gen" in sql and args[-1] == 4
        assert any(s.startswith("INSERT INTO region_stats_inputs") for s, _ in db.executed)

    def test_a_deleted_map_gets_no_rows(self, patched):
        db = patched(Db([make_run(1, range(0, 30))]))
        db.map_live = False
        out = asyncio.run(rs.build_map(db, Reads(3, [(-10_000, "A")]), "m"))
        assert out["status"] == "map_gone" and db.inserted == []
        assert not any(s.startswith("DELETE") or "INSERT" in s for s, _ in db.executed)

    def test_only_the_current_version_can_be_built(self, patched):
        db = patched(Db([make_run(1, range(0, 30))]))
        with pytest.raises(LookupError):
            asyncio.run(rs.build_map(db, Reads(3, [(-10_000, "A")]), "m", version=2))

    def test_failing_scene_read_raises(self, patched):
        class Broken(Reads):
            async def owner_plane(self, *a, **k):
                raise HTTPException(503, "down")
        db = patched(Db([make_run(1, range(0, 30))]))
        with pytest.raises(RuntimeError):
            asyncio.run(rs.build_map(db, Broken(3, [(0, "A")]), "m"))
        assert db.inserted == []


class TestMapsNeedingBuild:
    def need(self, db, reads, fresh=True):
        if fresh:
            rs._VERSIONS.clear()
        return asyncio.run(rs.maps_needing_build(db, reads))

    def test_the_graph_version_is_cached_between_polls(self, patched):
        db = patched(Db([make_run(1, range(0, 20))]))
        db.builds = [("m", 3, "ok", BUILT, NOW, 1)]

        class Counting(Reads):
            calls = 0

            async def topology(self, map_id):
                Counting.calls += 1
                return await super().topology(map_id)
        reads = Counting(3, [(0, "A")])
        assert self.need(db, reads) == [] and self.need(db, reads, fresh=False) == []
        assert Counting.calls == 1
        assert self.need(db, Reads(4, [(0, "A")]), fresh=False) == []          # still the cached version 3 ...
        rs._VERSIONS["m"] = (rs._VERSIONS["m"][0] - config.REGION_STATS_VERSION_TTL_S - 1, 3)
        assert self.need(db, Reads(4, [(0, "A")]), fresh=False) == ["m"]       # ... until the TTL has passed

    def test_cases(self, patched):
        db = patched(Db([make_run(1, range(0, 20))]))
        assert self.need(db, Reads(3, [(0, "A")])) == ["m"]                    # never built
        db.builds = [("m", 3, "ok", BUILT, NOW, 1)]
        assert self.need(db, Reads(3, [(0, "A")])) == []                       # up to date
        assert self.need(db, Reads(4, [(0, "A")])) == ["m"]                    # a new graph version
        db.builds = [("m", 3, "stale", BUILT, NOW, 1)]
        assert self.need(db, Reads(3, [(0, "A")])) == ["m"]                    # a run was deleted
        db.runs = []
        assert self.need(db, Reads(3, [(0, "A")])) == ["m"]                    # ... even when its last run is gone
        db.runs = [make_run(1, range(0, 20))]
        db.builds = [("m", 0, "no_scene_graph", BUILT, NOW, 1)]
        assert self.need(db, Reads(3, [(0, "A")], graph=False)) == []          # nothing to build without a graph
        assert self.need(db, Reads(3, [(0, "A")])) == ["m"]                    # ... until the graph appears

    def test_a_build_of_another_builder_version_is_redone(self, patched):
        db = patched(Db([make_run(1, range(0, 20))]))
        db.builds = [("m", 3, "ok", None, NOW, 1)]                                     # a legacy build has no detail
        assert self.need(db, Reads(3, [(0, "A")])) == ["m"]
        db.builds = [("m", 3, "ok", json.dumps({"builder": rs.BUILDER_VERSION - 1}), NOW, 1)]
        assert self.need(db, Reads(3, [(0, "A")])) == ["m"]
        db.builds = [("m", 3, "ok", BUILT, NOW, 1)]
        assert self.need(db, Reads(3, [(0, "A")])) == []

    def test_archived_runs_do_not_make_a_map_wanted(self, patched):
        db = patched(Db([make_run(1, range(0, 20))]))
        self.need(db, Reads(3, [(0, "A")]))
        sql = [s for s, _ in db.executed if s.startswith("SELECT DISTINCT map_id FROM run_cells")][0]
        assert "mr.archived_at IS NOT NULL" in sql and "NOT EXISTS" in sql

    def test_old_builds_and_expired_inputs_are_redone(self, patched):
        db = patched(Db([make_run(1, range(0, 20))]))
        reads = Reads(3, [(0, "A")])
        day = datetime.timedelta(seconds=config.REGION_STATS_MAX_AGE_S)
        db.builds = [("m", 3, "ok", BUILT, NOW - day - datetime.timedelta(minutes=1), 1)]
        assert self.need(db, reads) == ["m"]                                   # built too long ago
        db.builds = [("m", 3, "ok", BUILT, NOW - day / 2, 1)]
        assert self.need(db, reads) == []
        db.runs = []                                                           # an idle map with an old build ...
        db.builds = [("m", 3, "ok", BUILT, NOW - 2 * day, 1)]
        assert self.need(db, reads) == ["m"]                                   # ... is judged without run rows
        db.builds = [("m", 3, "ok", BUILT, NOW - 2 * day, 0)]
        assert self.need(db, reads) == []                                      # an empty build has nothing to expire
        old = (NOW - datetime.timedelta(days=config.REGION_STATS_WINDOW_DAYS + 1)).isoformat()
        left = json.dumps({"builder": rs.BUILDER_VERSION, "oldest_at": old})
        db.builds = [("m", 3, "ok", left, NOW, 5)]
        assert self.need(db, reads) == ["m"]                                   # its oldest input left the window
        inside = json.dumps({"builder": rs.BUILDER_VERSION, "oldest_at": NOW.isoformat()})
        db.builds = [("m", 3, "ok", inside, NOW, 5)]
        assert self.need(db, reads) == []

    def test_the_build_records_its_oldest_input(self, patched):
        db = patched(Db([make_run(1, range(0, 30)), make_run(2, range(0, 30), age_h=5)]))
        asyncio.run(rs.build_map(db, Reads(3, [(-10_000, "A")]), "m"))
        args = [a for s, a in db.executed if s.startswith("INSERT INTO region_stats_builds")][0]
        detail = json.loads(args[5])
        assert detail["builder"] == rs.BUILDER_VERSION
        assert detail["oldest_at"] == min(r.started_at for r in db.runs).isoformat()


class TestRoute:
    def call(self, db, reads, **kw):
        raw, etag = asyncio.run(rs.region_stats(db, reads, kw.pop("map_id", "m"), **kw))
        return json.loads(raw), etag

    def test_stored_rows_of_the_newest_version(self, patched):
        db = patched(Db([make_run(1, range(0, 30))]))
        asyncio.run(rs.build_map(db, Reads(3, [(-10_000, "A"), (10, "B")]), "m"))
        db.stat_rows = [dict(zip(rs.COLUMNS, row)) for row in db.inserted]
        db.build_rows = [("m", 3, "abc", NOW, "ok", 1)]
        body, etag = self.call(db, Reads(3, [(-10_000, "A"), (10, "B")]))
        assert body["source"] == "stored" and body["graph_version"] == 3 and body["stamp"] == "abc"
        assert body["stale"] is False and body["current_graph_version"] == 3 and body["robot_class"] == "*"
        assert [r["chart_key"] for r in body["regions"]] == ["A", "B"] and body["regions"][0]["runs"] == 1
        assert body["window_days"] == config.REGION_STATS_WINDOW_DAYS and etag.startswith('"')
        body, _ = self.call(db, Reads(4, [(0, "A")]))
        assert body["stale"] is True and body["current_graph_version"] == 4
        body, _ = self.call(db, Reads(3, [(0, "A")]), robot_class=CLASS_A)
        assert body["robot_class"] == CLASS_A and len(body["regions"]) == 2

    def test_robot_is_computed_on_read_and_cached(self, patched):
        db = patched(Db([make_run(1, range(0, 30)), make_run(2, range(0, 30))], robot="r2"))
        reads = Reads(3, [(-10_000, "A"), (10, "B")])
        body, _ = self.call(db, reads, robot="r2")
        assert body["source"] == "computed" and body["robot"] == "r2" and body["graph_version"] == 3
        assert [r["runs"] for r in body["regions"]] == [1, 1]
        n = len(db.executed)
        self.call(db, reads, robot="r2")
        fetches = [s for s, _ in db.executed[n:] if "robot_class, speed_max" in s]
        assert fetches == []                                                   # second answer from the cache

    def test_current_version_not_stored_is_computed(self, patched):
        db = patched(Db([make_run(1, range(0, 30))]))
        body, _ = self.call(db, Reads(3, [(-10_000, "A")]), version=3)
        assert body["source"] == "computed" and body["regions"][0]["chart_key"] == "A"

    def test_404s(self, patched):
        db = patched(Db([make_run(1, range(0, 30))]))
        with pytest.raises(HTTPException) as e:
            self.call(db, Reads(3, [(0, "A")]), map_id="nope")
        assert e.value.status_code == 404
        with pytest.raises(HTTPException) as e:
            self.call(db, Reads(3, [(0, "A")]), version=2)
        assert e.value.status_code == 404


class TestRouteArchived:
    def test_the_computed_cache_key_read_skips_archived_runs(self, patched):
        db = patched(Db([make_run(1, range(0, 30))]))
        asyncio.run(rs.region_stats(db, Reads(3, [(-10_000, "A")]), "m", robot="r1"))
        sql = [s for s, _ in db.executed if s.startswith("SELECT run_id, created_at FROM run_cells")][0]
        assert "mr.archived_at IS NOT NULL" in sql


class TestLoader:
    def test_parse(self):
        row = ("A", "*", 3, 10.0, 25.0, 2.0, 1.0, 1, 2.4, 3.0, 2.5, 0.4, 0.08, 10.0, 0.33, None, 100, 80, NOW, NOW, "s")
        got = loader.parse("m", 3, "*", [row], ("s", "ok", NOW))
        assert got.charts["A"].pace_median_s_per_m == 2.4 and got.charts["A"].speed_max is None
        assert got.stamp == "s" and got.status == "ok" and got.graph_version == 3
        assert loader.parse("m", 3, "*", [], None).charts == {} and loader.parse("m", 3, "*", [], None).stamp is None

    def test_fields_match_the_table_columns(self):
        assert loader.FIELDS == tuple(c for c in rs.COLUMNS if c not in ("map_id", "graph_version"))

    def test_load_sync_and_async(self):
        row = ("A", "*", 3, 10.0, 25.0, 2.0, 1.0, 1, 2.4, 3.0, 2.5, 0.4, 0.08, 10.0, 0.33, None, 100, 80, NOW, NOW, "s")

        class C:
            def __init__(self):
                self.last = None

            def execute(self, sql, args):
                self.last = sql

            def fetchall(self):
                return [row]

            def fetchone(self):
                return ("s", "ok", NOW)

        class AC(C):
            async def execute(self, sql, args):
                self.last = sql

            async def fetchall(self):
                return [row]

            async def fetchone(self):
                return ("s", "ok", NOW)
        assert loader.load(C(), "m", 3).charts["A"].runs == 3
        assert asyncio.run(loader.aload(AC(), "m", 3, "x")).robot_class == "x"
