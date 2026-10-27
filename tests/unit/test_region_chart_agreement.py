"""The chart a cell belongs to must be the same for the statistics builder and the planner's search.

Builder side: `drive_stats.regions_of` (the owner plane, read per 160 m chunk by `region_stats.collect`) turns the cells of
run_cells into chart keys. Planner side: `lazy_plan.PlanGraph._cells` (the `ch` array the search prices with, stored owner id
-> `_lut` -> chart index -> `keys`) and `owner_window`. If the two ever disagreed, a chart's statistics would price another
chart's cells (docs/planner_region_stats_2026-10-27.md, prerequisite P2). Checked on the lazy-plan fixture worlds, with the
owner plane served from the same stored version through a fake `reads`."""
import asyncio
import datetime
import os

os.environ.setdefault("POSTGRES_PASSWORD", "test")

import numpy as np
import pytest

pytest.importorskip("psycopg")

from packages.api import drive_stats as ds
from packages.api import region_stats as rs
from packages.api import run_cells as rc
from packages.scene_graph.api import BBox
from packages.utils import cell_raster as cr
from packages.utils import run_cells_db as rdb

from .test_lazy_plan import WORLDS, _build

pytestmark = pytest.mark.unit

T0 = datetime.datetime(2026, 10, 20, 12, 0, tzinfo=datetime.timezone.utc)


class StoredViewReads:
    """`reads` (owner_plane / view) over a stored view: what SceneGraphReads serves, for one version."""

    def __init__(self, view):
        self.view_ = view
        self.version = view.version

    async def owner_plane(self, map_id, bbox, level=0):
        r = self.view_.query(bbox, 0, raster=True, parents=False).raster
        if r is None:
            return None
        return {"version": self.version, "origin": (float(r.origin[0]), float(r.origin[1])), "step": float(r.step),
                "shape": tuple(r.index.shape), "keys": list(r.keys), "index": np.asarray(r.index),
                "bbox": [bbox.x0, bbox.y0, bbox.x1, bbox.y1]}

    async def view(self, map_id, bbox, level=0, version=None, raster=False, epoch=None, parents=True):
        res = self.view_.query(bbox, 0, raster=False, parents=False)
        return {"version": self.version, "charts": [{"key": c.key, "cells": int(c.cells)} for c in res.charts]}


@pytest.fixture(scope="module", params=sorted(WORLDS))
def world(request):
    w, way = WORLDS[request.param]()
    st, eng, lazy = _build((w, way))
    return request.param, way, StoredViewReads(st.view("m")), lazy


def planner_cells(lazy):
    """(global lattice x, y, chart key) of every cell the planner's search owns, from `PlanGraph._cells`."""
    tiles = sorted(tuple(int(v) for v in k.split(",")) for k in lazy.view.man["tiles"])
    _key, ch, ys, xs = lazy._cells(tiles)
    assert (ch >= 0).all()
    return xs.astype(np.int64), ys.astype(np.int64), [lazy.keys[int(c)] for c in ch]


def builder_charts(reads, ax, ay):
    ids, names, cells, version, status = asyncio.run(ds.regions_of(reads, "m", ax, ay))
    assert status == "ok" and version == reads.version
    return [names[i - 1] if i > 0 else None for i in ids.tolist()], names, cells


def test_every_cell_the_planner_owns_is_in_the_same_chart_for_the_builder(world):
    name, _way, reads, lazy = world
    xs, ys, planner = planner_cells(lazy)
    assert len(xs) > 500
    builder, _names, _cells = builder_charts(reads, xs, ys)
    assert builder == planner


def test_cells_the_planner_does_not_own_belong_to_no_chart(world):
    name, _way, reads, lazy = world
    xs, ys, _ = planner_cells(lazy)
    owned = set(zip(xs.tolist(), ys.tolist()))
    gx, gy = np.meshgrid(np.arange(xs.min() - 5, xs.max() + 6), np.arange(ys.min() - 5, ys.max() + 6))
    gx, gy = gx.ravel().astype(np.int64), gy.ravel().astype(np.int64)
    free = np.array([(a, b) not in owned for a, b in zip(gx.tolist(), gy.tolist())])
    assert free.any()
    builder, _n, _c = builder_charts(reads, gx[free], gy[free])
    assert set(builder) == {None}


def test_owner_window_agrees_too(world):
    name, _way, reads, lazy = world
    xs, ys, planner = planner_cells(lazy)
    h = float(lazy.params.lattice)
    ix0, iy0, idx, keys = lazy.owner_window(xs.min() * h, ys.min() * h, xs.max() * h, ys.max() * h)
    got = [keys[int(idx[y - iy0, x - ix0])] for x, y in zip(xs.tolist(), ys.tolist())]
    assert got == planner


def test_the_chart_cell_counts_and_the_visited_cells_of_a_full_cover_run_match(world):
    """A run that covered every owned cell: per chart, the builder's `visited` and `cells` equal the planner's cell count."""
    name, _way, reads, lazy = world
    xs, ys, planner = planner_cells(lazy)
    key = cr.cell_key(xs, ys)
    order = np.argsort(key)
    n = len(key)
    row = rdb.CellRow(run_id="r", map_id="m", robot_name="r1", robot_class="X|1.00x1.00", speed_max=1.0,
                      started_at=T0, ended_at=T0, source="track", algo_version=2, cell_m=0.1, params={}, frames=[],
                      key=key[order], len_m=np.full(n, 0.1), time_s=np.full(n, 0.2), stopped_s=np.zeros(n),
                      recovery_s=np.zeros(n), blocks=np.zeros(n, np.int64), totals={}, created_at=T0)
    ids, names, cells, version, status = asyncio.run(ds.regions_of(reads, "m", *cr.key_cells(row.key)))
    stats = rs.aggregate("m", version, [row], [ids], names, cells, "s")
    pooled = {s["chart_key"]: s for s in stats if s["robot_class"] == "*"}
    expect = {}
    for k in planner:
        expect[k] = expect.get(k, 0) + 1
    assert {k: s["visited"] for k, s in pooled.items()} == expect
    assert {k: s["cells"] for k, s in pooled.items()} == {k: cells[k] for k in expect}
    assert set(pooled) <= set(lazy.keys)


def test_the_cells_of_a_real_track_get_the_planners_chart(world):
    """Run cells made by the summariser from a track along the fixture's drive path."""
    name, way, reads, lazy = world
    xs, ys, planner = planner_cells(lazy)
    chart_of = {(x, y): k for x, y, k in zip(xs.tolist(), ys.tolist(), planner)}
    pts = [way[0]]
    for a, b in zip(way[:-1], way[1:]):
        n = max(1, int(np.hypot(b[0] - a[0], b[1] - a[1]) / 0.3))
        pts += [(a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n) for i in range(1, n + 1)]
    e0 = T0.timestamp()
    track = [(e0 + 1.0 * i, x, y, "s", "e", "m") for i, (x, y) in enumerate(pts)]
    data = rc.RunData("run", "r1", "m", T0, T0, track, [], [], [])
    [row] = rc.summarise_rows(data, lambda m: {}, None)
    assert len(row) > 50
    builder, _n, _c = builder_charts(reads, *cr.key_cells(row.key))
    ix, iy = cr.key_cells(row.key)
    owned = [(b, chart_of[(int(a), int(c))]) for b, a, c in zip(builder, ix.tolist(), iy.tolist())
             if (int(a), int(c)) in chart_of]
    assert len(owned) > len(row) * 0.5                               # the path lies in free space
    assert all(b == p for b, p in owned)
