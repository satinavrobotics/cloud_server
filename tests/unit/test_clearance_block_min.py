"""Node clearance = the minimum over the node's 2 x 2 costmap cells (docs/scene_graph/close_evidence_and_turn_cost_design.md F).

(a) a node whose top-left cell is clearer than another cell of its block is priced by the minimum, in the engine and in the lazy
reader, equal; (b) on random worlds the node clearance is never above the old top-left sample and never below the exact clearance
at the block centre minus 0.035 m; (c) without a price (rate 0) the route is the unpriced one."""
import math

import numpy as np
import pytest
from scipy import ndimage

from packages.scene_graph.api import Safety
from packages.scene_graph.engine import RES, block_min
from packages.services.mission_planner.lazy_plan import open_plan

from .scene_graph.route_safety_worlds import random_world
from .test_lazy_plan import _build

pytestmark = pytest.mark.unit

R_C = 0.25 * math.sqrt(2)
HALF_DIAG = RES * math.sqrt(2) / 2           # 0.0354 m: block centre to a block corner


def _world(seed):
    st, eng, _ = _build(random_world(seed))
    return st, eng, open_plan(st, "m", max_tiles=10 ** 6)


def _full_dt(eng):
    return ndimage.distance_transform_edt((eng._V != 255) & (eng._V < 100)) * RES


def test_block_min_helper_is_the_minimum_of_the_four_cells_and_handles_odd_shapes():
    rnd = np.random.default_rng(3)
    for shape in ((6, 8), (7, 8), (6, 9), (7, 9), (1, 1), (1, 5)):
        a = rnd.random(shape)
        b = block_min(a)
        assert b.shape == (-(-shape[0] // 2), -(-shape[1] // 2))
        for i in range(b.shape[0]):
            for j in range(b.shape[1]):
                assert b[i, j] == a[2 * i:2 * i + 2, 2 * j:2 * j + 2].min()
        assert (b <= a[::2, ::2]).all()


def test_a_corner_hugging_node_is_priced_by_the_block_minimum_in_engine_and_lazy():
    n_less = 0
    for seed in (1, 2, 3, 4):
        st, eng, lazy = _world(seed)
        dt = _full_dt(eng)
        sp = eng._solved.sp
        ix0, iy0 = eng._cx0 // 2, eng._cy0 // 2
        for y, x in zip(sp.ys.tolist(), sp.xs.tolist()):
            blk = dt[2 * y:2 * y + 2, 2 * x:2 * x + 2]
            assert sp.clr[sp.idx[y, x]] == dt[2 * y, 2 * x]               # structure / widths keep the top-left sample
            old, new = float(dt[2 * y, 2 * x]), float(blk.min())
            if new >= old - 1e-9 or old >= 1.0:
                continue
            n_less += 1
            e = eng.deficit_cell(x + ix0, y + iy0, R_C)
            assert e == pytest.approx(max(0.0, R_C - new), abs=1e-9)         # priced by the minimum ...
            assert e >= max(0.0, R_C - old)                                 # ... never less than the old sample
            assert lazy.deficit_cell(x + ix0, y + iy0, R_C) == e            # the lazy reader: the same to the last bit
    assert n_less > 20


def test_block_min_is_not_above_the_top_left_sample_and_not_below_the_exact_centre_clearance_minus_half_diagonal():
    for seed in (5, 6, 7):
        st, eng, lazy = _world(seed)
        dt = _full_dt(eng)
        sp = eng._solved.sp
        ix0, iy0 = eng._cx0 // 2, eng._cy0 // 2
        free = (eng._V != 255) & (eng._V < 100)
        for y, x in zip(sp.ys.tolist(), sp.xs.tolist()):
            g = eng._clearance_cell(x + ix0, y + iy0)
            assert g <= dt[2 * y, 2 * x] + 1e-9
            # exact clearance at the block centre (the middle of the four cell centres) to the nearest blocked cell centre
            cy, cx = 2 * y + 0.5, 2 * x + 0.5
            pts = np.argwhere(~free)
            exact = float(np.sqrt(((pts[:, 0] - cy) ** 2 + (pts[:, 1] - cx) ** 2).min())) * RES
            assert g >= exact - HALF_DIAG - 1e-9
            assert lazy.deficit_cell(x + ix0, y + iy0, 1.0) == max(0.0, 1.0 - g)       # the lazy reader, to the cap


def test_rate_zero_is_the_unpriced_route():
    st, eng, lazy = _world(8)
    keys = eng._solved.keys
    for a, b in ((keys[0], keys[-1]), (keys[1], keys[len(keys) // 2])):
        base = eng.route(a, b)
        z = eng.route(a, b, safety=Safety(R_C, 0.0))
        assert (base is None) == (z is None)
        if base is not None:
            assert z.cost == base.cost and [tuple(p) for p in z.nodes] == [tuple(p) for p in base.nodes]
