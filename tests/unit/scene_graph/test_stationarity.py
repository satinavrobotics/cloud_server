"""The canonical property of a solved state, stated from the definition and checked on a FRESH solver (no memo, no row cache, no
incremental state), after every batch of an incremental fold:

  consistent   the engine's ownership equals the full re-simulation of its anchors
  cover        F1/F2 hold globally (no pocket holds a ball of the place scale, every anchor is in the kernel and on the band)
  removal      no anchor can be removed: the global re-simulation of A \\ {a} violates F1/F2
  move         no anchor's move to propose(chart) is globally feasible AND strictly lowers (#dependents, sum of D)
  fixpoint     a fresh descend over every anchor with an empty memo changes nothing

Note what is NOT claimed: that the proposal has a lower chart-only drive-length sum than the anchor. That is a different, weaker
statement (it ignores the cells the move hands to the neighbours); a move that lowers it can be infeasible or lower the global
objective not at all, and the anchor then correctly stays. The test below pins both."""
import numpy as np
import pytest

from packages.scene_graph import chart_solver as L
from packages.scene_graph.api import BBox, SceneParams
from packages.scene_graph.engine import ChartGraph

from . import chart_worlds as cw

pytestmark = pytest.mark.unit


def _fresh(g):
    tr = g._track
    return L.Local(tr.nav.copy(), tr.clr.copy(), h=tr.h, r=tr.r, mult=tr.mult, sight_range=tr.sight_range)


def stationarity_report(g):
    s = g._solved
    F = _fresh(g); A = list(s.A)
    own, rnd = F.full_state(A)
    base = F.objective(A, own)
    rep = dict(n=len(A), consistent=bool((own == s.own).all()), cover=F.violation(A, own), removal=[], move=[], flagged_by_chart_J=0)
    for j, a in enumerate(A):
        if len(A) > 1:
            A2 = [x for k, x in enumerate(A) if k != j]
            o2, _ = F.full_state(A2)
            if F.violation(A2, o2) is None: rep['removal'].append(int(a))
        mine = np.nonzero(own == j)[0]
        b = F.propose(mine)
        if b is None or b == a or b in A: continue
        c = F._chart(mine)
        Jf = lambda y: float(L.octile(F.ys[y] - c['ym'], F.xs[y] - c['xm'], F.h).sum())
        if Jf(b) < Jf(a) - 1e-9: rep['flagged_by_chart_J'] += 1
        A2 = list(A); A2[j] = b
        o2, _ = F.full_state(A2)
        if F.violation(A2, o2) is None and F.objective(A2, o2) < (base[0], base[1] - 1e-9): rep['move'].append((int(a), int(b)))
    F2 = _fresh(g); o3, r3 = F2.full_state(A)
    A3, *_ = F2.descend(A, o3, r3, queue=set(A), tried={}, local=False)
    rep['fixpoint'] = sorted(A3) == sorted(A)
    return rep


def _hall(k=3):
    R = 0.05
    def rect(w, x0, y0, x1, y1, v): w[int(round(y0 / R)):int(round(y1 / R)), int(round(x0 / R)):int(round(x1 / R))] = v
    RW, RH = 4.0, 3.6
    W, H = k * RW + 0.4, RH + 0.4
    w = np.full((int(H / R), int(W / R)), 100, np.uint8); rect(w, 0.2, 0.2, W - 0.2, H - 0.2, 0)
    for i in range(1, k):
        x = 0.2 + i * RW
        rect(w, x - 0.075, 0.2, x + 0.075, H - 0.2, 100); rect(w, x - 0.075, 0.2 + RH / 2 - 0.5, x + 0.075, 0.2 + RH / 2 + 0.5, 0)
    way = []
    for i in range(k):
        x0 = 0.2 + i * RW
        for j, xo in enumerate((0.8, 2.0, 3.2)):
            ys = (0.6, RH - 0.2) if (i + j) % 2 == 0 else (RH - 0.2, 0.6)
            way += [(x0 + xo, ys[0] + 0.2), (x0 + xo, ys[1] + 0.2)]
        way += [(x0 + RW - 0.3, 0.2 + RH / 2)]
    return w, way


WORLDS = dict(two_rooms=cw.two_rooms, pillar=cw.pillar_room, ring=cw.ring, island=cw.island, hall=_hall)


@pytest.mark.parametrize("name", list(WORLDS))
@pytest.mark.parametrize("bs", [3, 12])
def test_every_batch_of_an_incremental_fold_is_stationary_by_the_global_definition(name, bs):
    w, way = WORLDS[name]()
    leaves = cw.drive(w, way, win=120)
    g = ChartGraph(SceneParams())
    for i in range(0, len(leaves), bs):
        g.fold_many(leaves[i:i + bs])
        if g._solved is None: continue
        r = stationarity_report(g)
        assert r['consistent'] and r['cover'] is None and r['removal'] == [] and r['move'] == [] and r['fixpoint'], (name, bs, i, r)


def test_arrival_order_does_not_break_stationarity():
    w, way = _hall()
    leaves = cw.drive(w, way, win=120)
    rng = np.random.default_rng(4); order = rng.permutation(len(leaves))
    g = ChartGraph(SceneParams())
    for i in range(0, len(order), 5):
        g.fold_many([leaves[k] for k in order[i:i + 5]])
        if g._solved is None: continue
        r = stationarity_report(g)
        assert r['consistent'] and r['cover'] is None and r['removal'] == [] and r['move'] == [] and r['fixpoint'], (i, r)


def test_a_map_with_free_cells_but_no_place_of_the_scale_folds_without_an_anchor():
    """a 0.55 m corridor only: F exists (the 0.5 m square fits) but no ball of radius s = 0.375 m fits, so there is no anchor yet.
    The per-batch bookkeeping of the changed cells indexed an empty anchor list (IndexError, engine faulted)."""
    R = 0.05
    w = np.full((int(1.5 / R), int(8.0 / R)), 100, np.uint8)
    cw._rect(w, 0.2, 0.5, 7.8, 1.05, 0)
    leaves = cw.drive(w, [(0.8, 0.775), (7.0, 0.775)], win=100)
    g = ChartGraph(SceneParams())
    for i in range(0, len(leaves), 6):
        g.fold_many(leaves[i:i + 6])
    assert g.faulted is None and g.query(BBox(-5, -5, 20, 20), raster=False).charts == []


def test_a_lower_chart_only_drive_sum_is_not_a_violation():
    """the weaker measure fires (a proposal with a lower chart-only sum than its anchor) while the state is stationary by the
    definition: the move is infeasible or does not lower the global objective, and the anchor correctly stays"""
    w, way = _hall()
    leaves = cw.drive(w, way, win=120)
    g = ChartGraph(SceneParams())
    flagged = 0
    for i in range(0, len(leaves), 6):
        g.fold_many(leaves[i:i + 6])
        r = stationarity_report(g); flagged += r['flagged_by_chart_J']
        assert r['move'] == [] and r['removal'] == []
    assert flagged >= 1          # the weaker measure does fire on this world (deterministic: 2), and the state is stationary anyway
