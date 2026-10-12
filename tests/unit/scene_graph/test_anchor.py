"""The anchor proposal (chart_solver.Local.propose / anchor_ok): projected centroid of a chart, held to kernel and band.

Claims tested: the drive-length objective is convex (octile is a norm), the proposal lies in kernel and band and loses at most 1 % of
the full-scan optimum, anchor_ok equals membership in the full-scan kernel-and-band set, a solved state is stationary under the
proposal map, ties are broken by cell index and so are translation invariant, replay changes nothing."""
import dataclasses

import numpy as np
import pytest

from packages.scene_graph.api import BBox, SceneParams
from packages.scene_graph.chart_solver import octile
from packages.scene_graph.engine import ChartGraph

from . import chart_worlds as cw
from .test_engine import ALL, built

pytestmark = pytest.mark.unit
NAMES = ["two_rooms", "corridor", "pillar", "ring", "island"]


def _graph(name):
    return built(name)[0]


def _l_shape():
    w = cw._blank(6.0, 6.0); cw._rect(w, 2.2, 2.2, 5.8, 5.8, 100)
    g = ChartGraph(SceneParams()); g.fold_many(cw.drive(w, [(1.0, 1.0), (1.0, 5.0), (1.0, 1.0), (5.0, 1.0)]))
    return g


def _charts(g):
    s = g._solved
    for j, a in enumerate(s.A):
        yield s.sp, a, np.nonzero(s.own == j)[0]


def _J(sp, mine, c):
    return float(octile(sp.ys[c] - sp.ys[mine], sp.xs[c] - sp.xs[mine], sp.h).sum())


def test_the_objective_is_convex_and_octile_is_within_8_percent_of_euclid():
    rng = np.random.default_rng(0)
    X = rng.uniform(-10, 10, (40, 2))
    f = lambda p: octile(p[0] - X[:, 0], p[1] - X[:, 1], 1.0).sum()
    for _ in range(200):
        a, b = rng.uniform(-12, 12, 2), rng.uniform(-12, 12, 2)
        assert f((a + b) / 2) <= (f(a) + f(b)) / 2 + 1e-9
    d = rng.uniform(-9, 9, (500, 2)); e = np.hypot(d[:, 0], d[:, 1])
    assert (octile(d[:, 0], d[:, 1], 1.0) / e).max() <= 1.0825 and (octile(d[:, 0], d[:, 1], 1.0) >= e - 1e-9).all()


@pytest.mark.parametrize("name", NAMES + ["L"])
def test_proposal_is_in_kernel_and_band_and_close_to_the_full_scan_optimum(name):
    g = _l_shape() if name == "L" else _graph(name)
    n = 0
    for sp, a, mine in _charts(g):
        b, band = sp.pick(mine)
        p = sp.propose(mine)
        assert (p is None) == (b is None)
        if p is None: continue
        n += 1
        assert p in set(band.tolist())
        assert _J(sp, mine, p) <= 1.01 * _J(sp, mine, b)
        assert sp.anchor_ok(mine, p)
    assert n


@pytest.mark.parametrize("name", NAMES + ["L"])
def test_anchor_ok_equals_membership_in_the_full_scan_set(name):
    g = _l_shape() if name == "L" else _graph(name)
    rng = np.random.default_rng(1)
    for sp, a, mine in _charts(g):
        _, band = sp.pick(mine); inband = set(band.tolist())
        assert sp.anchor_ok(mine, a) == (a in inband) and a in inband
        for c in rng.choice(mine, min(12, len(mine)), replace=False):
            assert sp.anchor_ok(mine, int(c)) == (int(c) in inband)


@pytest.mark.parametrize("name", ["two_rooms", "island", "ring", "L"])
def test_solved_state_is_stationary_under_the_proposal_map(name):
    g = _l_shape() if name == "L" else _graph(name)
    s = g._solved; sp, A = s.sp, s.A
    own, rnd = sp.full_state(A)
    for j, a in enumerate(A):
        if len(A) > 1:
            A2, o2, _r, S, _ = sp.apply(A, own, rnd, ("remove", j), local=False)
            assert not sp.feasible_after(A2, o2, S)
        mine = np.nonzero(own == j)[0]
        b = sp.propose(mine)
        if b is not None and b != a and b not in A:
            A2, o2, _r, S, _ = sp.apply(A, own, rnd, ("move", j, b), local=False)
            if sp.feasible_after(A2, o2, S):
                aff = np.union1d(S, np.nonzero((own == j) | (o2 == j))[0])
                old = sp.objective(A, own, aff); new = sp.objective(A2, o2, aff)
                assert not new < (old[0], old[1] - 1e-9)


def test_tie_break_is_by_cell_index_and_translation_invariant():
    w = cw._blank(3.0, 3.0)
    leaves = cw.drive(w, [(1.5, 1.5), (1.7, 1.5)], step=0.1)
    dx, dy = 0.4, 0.2                                           # whole lattice cells (0.1 m), even in full-res cells
    moved = [dataclasses.replace(lf, x=lf.x + dx, y=lf.y + dy, origin=(lf.origin[0] + dx, lf.origin[1] + dy)) for lf in leaves]
    g1, g2 = ChartGraph(), ChartGraph()
    g1.fold_many(leaves); g2.fold_many(moved)
    c1 = sorted(c.anchor for c in g1.query(ALL, raster=False).charts)
    c2 = sorted(c.anchor for c in g2.query(BBox(-5, -5, 20, 20), raster=False).charts)
    assert len(c1) == len(c2) >= 1
    for (x1, y1), (x2, y2) in zip(c1, c2):
        assert abs(x2 - x1 - dx) < 1e-9 and abs(y2 - y1 - dy) < 1e-9


def test_replaying_known_leaves_moves_no_anchor():
    g, _, leaves = built("two_rooms")
    before = sorted((c.key, c.anchor) for c in g.query(ALL, raster=False).charts)
    d = g.fold_many(leaves)
    after = sorted((c.key, c.anchor) for c in g.query(ALL, raster=False).charts)
    assert before == after and d.changed == []
