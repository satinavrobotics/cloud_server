import numpy as np
import pytest

from packages.scene_graph import Params
from packages.scene_graph.graph import coarsen, fields, lattice_graph
from packages.scene_graph.partition import _Ctx, basins, bisect, decompose, dilation, saddles, settle

pytestmark = pytest.mark.unit
P = Params()


def _setup(val):
    f = fields(val, P)
    c = coarsen(f, P.block)
    res = P.block * P.res
    g, idx = lattice_graph(c.nav, c.cost, res)
    rows, cols = np.nonzero(c.nav)
    h = c.clear[rows, cols]
    from dataclasses import replace
    pc = replace(P, res=res, budget=P.budget // P.block ** 2)
    return g, rows, cols, c.nav.shape, h, pc


def _two_rooms(door_cells=18):
    v = np.zeros((100, 220), np.uint8)
    v[0, :] = v[-1, :] = v[:, 0] = v[:, -1] = 100
    v[:, 110] = 100
    mid = 50
    v[mid - door_cells // 2:mid + door_cells // 2, 110] = 0
    return v


def test_basins_and_saddle_of_two_rooms_joined_by_a_door():
    g, rows, cols, shape, h, pc = _setup(_two_rooms())
    lab, peaks = basins(g, h)
    assert len(set(lab.tolist())) >= 2
    sad = saddles(g, lab, h)
    # the highest crossing between the two big basins is the door: half of 18 cells x 0.05 m, within a block
    assert max(sad.values()) == pytest.approx(0.45, abs=0.15)


def test_decompose_keeps_the_rooms_apart_and_merges_the_noise():
    g, rows, cols, shape, h, pc = _setup(_two_rooms())
    lab = decompose(_Ctx(g, rows, cols, shape, h, pc), np.arange(g.n))
    assert len(np.unique(lab)) == 2
    mid = shape[1] // 2
    assert len(np.unique(lab[cols < mid - 3])) == 1 and len(np.unique(lab[cols > mid + 3])) == 1
    assert lab[cols < mid - 3][0] != lab[cols > mid + 3][0]


def test_open_room_is_one_region_and_a_ring_is_cut():
    room = np.zeros((100, 160), np.uint8)
    room[0, :] = room[-1, :] = room[:, 0] = room[:, -1] = 100
    g, rows, cols, shape, h, pc = _setup(room)
    assert len(np.unique(decompose(_Ctx(g, rows, cols, shape, h, pc), np.arange(g.n)))) == 1
    ring = np.zeros((200, 200), np.uint8)
    ring[0, :] = ring[-1, :] = ring[:, 0] = ring[:, -1] = 100
    ring[50:150, 50:150] = 100                      # block in the middle: a ring corridor 50 cells (2.5 m) wide
    g, rows, cols, shape, h, pc = _setup(ring)
    assert len(np.unique(decompose(_Ctx(g, rows, cols, shape, h, pc), np.arange(g.n)))) >= 2


def test_dilation_of_convex_l_and_u_shapes():
    def region(mask):
        val = np.where(mask, 0, 100).astype(np.uint8)
        g, rows, cols, shape, h, pc = _setup(val)
        return dilation(g, shape, rows, cols, np.arange(g.n), pc.res, 0.0)
    sq = np.zeros((120, 120), bool); sq[10:110, 10:110] = True
    ell = np.zeros((120, 120), bool); ell[10:110, 10:40] = True; ell[80:110, 10:110] = True
    uu = np.zeros((120, 120), bool); uu[10:110, 10:30] = True; uu[10:110, 90:110] = True; uu[90:110, 10:110] = True
    assert region(sq) < 1.15
    assert 1.2 < region(ell) < 1.6
    assert region(uu) > 2.0


def test_bisect_cuts_a_u_into_charts():
    u = np.zeros((120, 120), bool); u[10:110, 10:30] = True; u[10:110, 90:110] = True; u[90:110, 10:110] = True
    g, rows, cols, shape, h, pc = _setup(np.where(u, 0, 100).astype(np.uint8))
    ctx = _Ctx(g, rows, cols, shape, h, pc)
    parts = bisect(ctx, np.arange(g.n), 1.5)
    assert len(parts) >= 2
    assert all(ctx.dil(q) <= 1.5 * 1.3 for q in parts)
    assert sorted(np.concatenate(parts).tolist()) == list(range(g.n))   # a partition


def test_settle_is_stable_when_nothing_changes():
    g, rows, cols, shape, h, pc = _setup(_two_rooms())
    first = settle(g, rows, cols, shape, h, pc, np.full(g.n, -1), 0)
    again = settle(g, rows, cols, shape, h, pc, first, int(first.max()) + 1)
    assert (again == first).all()


def test_settle_new_cells_join_the_nearest_region_and_unreached_ones_start_a_new_one():
    g, rows, cols, shape, h, pc = _setup(_two_rooms())
    first = settle(g, rows, cols, shape, h, pc, np.full(g.n, -1), 0)
    prior = first.copy()
    prior[(cols > shape[1] // 2 - 4) & (cols < shape[1] // 2 + 4)] = -1       # forget a strip around the door
    out = settle(g, rows, cols, shape, h, pc, prior, int(first.max()) + 1)
    assert (out >= 0).all() and len(np.unique(out)) == len(np.unique(first))
