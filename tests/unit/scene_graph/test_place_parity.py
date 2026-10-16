"""Parity under the place rule (packages/scene_graph/place.py, its module docstring: a region is a place when the robot fits
in its peak or has driven through it; a region that is neither is removed from the navigable set before the charts are cut).

Two checks, both with `place_peak_m` / `place_path_m` on:
  * the lazy planner reader (`lazy_plan.open_plan` on a store round trip) answers the navigable set and the chart keys of
    every lattice cell as the engine does: the place rule is applied in the stored owner plane, so a reader that only reads
    tiles must see the same cells as the engine that wrote them;
  * folding the leaves one batch at a time gives the same navigable planes and charts (cells, anchors, chart membership)
    as folding them in one batch (the rule is evaluated on every path that cuts charts, so the incremental fold must not
    depend on the batching).

The world has a room that is a place (its peak is far above the robot's circumscribed radius) and an isolated pocket that is
not one (its peak is below it and its driven footprint spans less than the robot's length). The store is the in-memory
backend the other store tests use (MemoryBackend), so no MinIO is needed.
"""
import numpy as np
import pytest

from packages.scene_graph import store as S
from packages.scene_graph.api import BBox, SceneParams
from packages.scene_graph.engine import ChartGraph
from packages.services.mission_planner.lazy_plan import open_plan

from . import chart_worlds as cw

pytestmark = pytest.mark.unit

R = cw.R
H_LAT = 2 * R                                   # the lattice step (m)
PARAMS = dict(near_range_m=1.5, place_peak_m=0.354, place_path_m=0.5)
ROOM_CELL = (2.0, 2.0)                          # m, inside the room
POCKET_CELL = (5.3, 2.0)                        # m, inside the pocket


def _world():
    """a 9 x 4 m world: a 4.2 x 3.6 m room, and a 0.6 x 0.4 m pocket with its own walls, 0.6 m east of the room. The pocket's
    navigable cells have a peak of 0.2 m (below the robot's circumscribed radius) and a driven span of 0.4 m (below its length)"""
    w = np.full((int(4.0 / R), int(9.0 / R)), 100, np.uint8)
    cw._rect(w, 0.2, 0.2, 4.4, 3.8, 0)
    cw._rect(w, 5.0, 1.85, 5.6, 2.25, 0)
    room_way = [(1.0, 2.0), (3.6, 2.0), (3.6, 3.2), (1.0, 3.2)]
    pocket_way = [(5.2, 2.05), (5.4, 2.05)]
    return w, cw.drive(w, room_way, session="a") + cw.drive(w, pocket_way, session="p", t0=100000)


def _fold(leaves, batch):
    g = ChartGraph(SceneParams(**PARAMS))
    for i in range(0, len(leaves), batch):
        g.fold_many(leaves[i:i + batch])
    return g


def _engine_cells(g):
    """(ix, iy) global lattice cell -> chart key, from the engine's own raster"""
    v = g.query(BBox(-50, -50, 50, 50), raster=True).raster
    ox, oy = int(round(v.origin[0] / H_LAT)), int(round(v.origin[1] / H_LAT))
    out = {}
    for i, key in enumerate(v.keys, start=1):
        ys, xs = np.nonzero(v.index == i)
        for y, x in zip(ys.tolist(), xs.tolist()):
            out[(ox + x, oy + y)] = key
    return out


def _engine_nav(g):
    """the navigable set the charts are cut from: the engine's navigable lattice after the place rule, in global cells"""
    nav = g._place_filter(g._build_F((0, 0))[0])
    ys, xs = np.nonzero(nav)
    return {(int(x) + g._cx0 // 2, int(y) + g._cy0 // 2) for y, x in zip(ys, xs)}


def test_lazy_reader_matches_the_engine_on_every_cell_under_the_place_rule():
    """Lazy reader parity under the place rule (store round trip, MemoryBackend). The lazy reader's free cells equal the
    engine's navigable set, and its chart keys equal the engine's, on every lattice cell of the world."""
    _, leaves = _world()
    be = S.MemoryBackend()
    st = S.SceneGraphStore(be, be)
    g = _fold(leaves, 9)
    st.save("m", g, g.fold_many([]), None, {"n": 1})
    plan = open_plan(st, "m")

    engine_nav = _engine_nav(g)
    engine_keys = _engine_cells(g)
    unfiltered = g._build_F((0, 0))[0]
    assert unfiltered.sum() > len(engine_nav)                                   # the rule removes cells in this world
    assert set(engine_keys) == engine_nav                                       # the charts cover the place-filtered set
    assert (int(ROOM_CELL[0] / H_LAT), int(ROOM_CELL[1] / H_LAT)) in engine_nav  # the room is a place: kept
    pocket = (int(POCKET_CELL[0] / H_LAT), int(POCKET_CELL[1] / H_LAT))
    assert pocket not in engine_nav                                             # the pocket is not: removed

    lazy_free = {(ix, iy) for ix in range(-5, 100) for iy in range(-5, 50) if plan.free_cell(ix, iy)}
    assert lazy_free == engine_nav

    tiles = st.tiles_in("m", BBox(-1e3, -1e3, 1e3, 1e3))
    ky, ch, ys, xs = plan._cells(tiles)
    lazy_keys = {(int(x), int(y)): plan.keys[int(c)] for x, y, c in zip(xs, ys, ch)}
    assert lazy_keys == engine_keys
    assert sorted(set(lazy_keys.values())) == sorted(set(engine_keys.values())) and len(plan.keys) == len(set(plan.keys))


def _partition(g):
    """the charts as a partition of the navigable cells: the set of cells of each chart, and the chart anchors. Key names are
    not compared: a chart is named after the leaf that gave it birth, and that leaf depends on the batching (the same names
    differ with the place rule off), so the chart identity is its cells."""
    cells = {}
    for cell, key in _engine_cells(g).items():
        cells.setdefault(key, set()).add(cell)
    anchors = sorted((round(c.anchor[0], 6), round(c.anchor[1], 6)) for c in g.query(BBox(-50, -50, 50, 50), raster=False).charts)
    return sorted(sorted(s) for s in cells.values()), anchors


def test_incremental_fold_equals_one_batch_under_the_place_rule():
    """Folding the leaves one at a time equals folding them in one batch, with the place rule on: the navigable planes (global
    cells), the charts (their cells and anchors) and the per-cell chart membership all match."""
    _, leaves = _world()
    one_by_one = _fold(leaves, 1)
    one_batch = _fold(leaves, len(leaves))
    assert _engine_nav(one_by_one) == _engine_nav(one_batch) and _engine_nav(one_batch)
    cells_a, anchors_a = _partition(one_by_one)
    cells_b, anchors_b = _partition(one_batch)
    assert cells_a == cells_b and cells_a
    assert anchors_a == anchors_b and len(anchors_a) == len(cells_a)
    assert len(one_by_one._keys) == len(one_batch._keys) == len(cells_a)
