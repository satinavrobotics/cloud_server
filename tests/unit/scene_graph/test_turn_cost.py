"""Turn cost of the lattice route (docs/scene_graph/close_evidence_and_turn_cost_design.md B.1, B.5): `ChartGraph.route(...,
safety=Safety(margin, rate, turn=lambda))` against the independent (cell, heading) reference (`route_safety_worlds.Reference.
dijkstra_turn`: heapq over python ints, its own heading arithmetic).

T1 reduction at lambda 0, T2 optimum and tie rule equal to the reference (with and without an overlay, priced and turn-only),
T3 the restricted search equals the whole-graph expanded search, T4 monotone total turning in lambda, T5 determinism and the
weight cache, plus `Safety.turn` and `api.turn_weight`."""
import dataclasses
import math
import random

import numpy as np
import pytest

from packages.scene_graph import turn as turn_search
from packages.scene_graph import weights
from packages.scene_graph.api import BBox, Blocked, Leaf, Safety, SceneParams, turn_weight
from packages.scene_graph.engine import ChartGraph

from . import chart_worlds as cw
from .route_safety_worlds import Reference, pairs, random_world
from .test_route_safety import ALL_WORLDS, R_C, world

pytestmark = pytest.mark.unit

MU = 1.0 / (R_C - 0.25)                     # kappa 1 at the default margin
TURNS = [Safety(R_C, 0.0, turn=0.5), Safety(R_C, MU, turn=0.5), Safety(0.6, 40.0, turn=1.0), Safety(R_C, MU, turn=5.0)]


def _overlay_at_midpoint(r, radius=0.4):
    mid = r.nodes[len(r.nodes) // 2]
    return Blocked(discs=[(mid[0], mid[1], radius)], segments=[])


def _turning(cells):
    """total turning of a cell path in multiples of pi / 4"""
    out = 0
    for p, q, r in zip(cells[:-2], cells[1:-1], cells[2:]):
        a1 = math.atan2(q[0] - p[0], q[1] - p[1])
        a2 = math.atan2(r[0] - q[0], r[1] - q[1])
        out += int(round(abs((a2 - a1 + math.pi) % (2 * math.pi) - math.pi) / (math.pi / 4)))
    return out


# ---- the value object and the weight -------------------------------------------------------------------------
def test_safety_turn_validates_and_defaults_to_zero():
    assert Safety(0.4, 1.0).turn == 0.0 and Safety(0.4, 1.0, turn=5.0).turn == 5.0
    for bad in (-0.1, float("nan"), float("inf"), 5.0001):
        with pytest.raises(ValueError):
            Safety(0.4, 1.0, turn=bad)
    with pytest.raises(dataclasses.FrozenInstanceError):
        Safety(0.4, 1.0, turn=0.5).turn = 1.0
    assert Safety(0.4, 1.0, turn=0.5) != Safety(0.4, 1.0, turn=0.25)       # part of the value (and of any key built on it)
    assert hash(Safety(0.4, 1.0, turn=0.5)) == hash(Safety(0.4, 1.0, turn=0.5))


def test_turn_weight_is_the_one_formula():
    assert turn_weight(0.5, 0) == 0.0 and turn_weight(0.0, 4) == 0.0
    assert turn_weight(0.5, 2) == math.ceil(0.5 * math.pi / 2 / 1e-4 - 1e-6) == 7854
    assert turn_weight(5.0, 4) == 157080                                     # the largest weight a valid Safety can add
    assert list(turn_weight(1.0, np.arange(5))) == [0.0, 7854.0, 15708.0, 23562.0, 31416.0]
    tab = turn_search.table(0.5)
    assert tab.shape == (8, 8) and (tab == tab.T).all() and (np.diag(tab) == 0).all()
    assert tab[0, 4] == turn_weight(0.5, 4) and tab[1, 7] == turn_weight(0.5, 2) and tab[7, 0] == turn_weight(0.5, 1)


# ---- T1 reduction -------------------------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["two_rooms", "ring", "r0", "r3"])
def test_t1_lambda_zero_is_bit_identical_and_takes_the_old_path(name, monkeypatch):
    g = world(name)
    calls = []
    real = turn_search.route
    monkeypatch.setattr(turn_search, "route", lambda *a, **kw: (calls.append(1), real(*a, **kw))[1])
    n = 0
    for a, b in pairs(g, 8, 3):
        r0 = g.route(a, b)
        for ov in (None, _overlay_at_midpoint(r0)):
            base = g.route(a, b, ov)
            for sf in (Safety(0.45, 0.0, turn=0.0), Safety(R_C, MU, turn=0.0)):
                ref = base if sf.rate == 0.0 else g.route(a, b, ov, safety=Safety(sf.margin, sf.rate))
                got = g.route(a, b, ov, safety=sf)
                assert (got is None) == (ref is None)
                if got is not None:
                    assert (got.nodes, got.cost, got.charts, got.length, got.min_width) == \
                        (ref.nodes, ref.cost, ref.charts, ref.length, ref.min_width)
                    n += 1
    assert n and not calls                     # the turn search never ran


@pytest.mark.parametrize("name", ["pillar", "r1", "r5"])
def test_t1_a_turn_price_below_one_unit_changes_nothing(name):
    g = world(name)
    for a, b in pairs(g, 6, 4):
        r = g.route(a, b, safety=Safety(R_C, MU))
        t = g.route(a, b, safety=Safety(R_C, MU, turn=1e-12))          # every turn weight rounds to 0
        assert (t.nodes, t.cost) == (r.nodes, r.cost)


# ---- T2 optimality and the tie rule ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", ALL_WORLDS)
def test_t2_the_turn_cost_route_is_the_independent_optimum_and_canonical(name):
    g = world(name)
    ref = Reference(g)
    n = changed = 0
    for a, b in pairs(g, 5, 5):
        src, dst = ref.endpoints(a, b)
        plain = g.route(a, b, safety=Safety(R_C, MU))
        for sf in TURNS:
            r = g.route(a, b, safety=sf)
            cost, path = ref.dijkstra_turn(src, dst, sf)
            assert r.cost == cost, (name, a, b, sf)
            assert ref.cells_of(r) == path, (name, a, b, sf)                 # the (y, x) tie rule, headings not in the order
            assert ref.turn_cost(path, sf) == r.cost                         # Route.cost is J of its own cells, turns included
            n += 1
            changed += sf.rate > 0 and ref.cells_of(r) != ref.cells_of(plain)
    assert n
    if name in ("r0", "r1", "r2", "r3"):
        assert changed                                                      # the turn cost really moves routes here


@pytest.mark.parametrize("name", ["r1", "r4", "ring", "room_loop"])
def test_t2_under_an_overlay_the_turn_route_equals_the_reference(name):
    g = world(name)
    ref = Reference(g)
    s = g._solved
    n = 0
    for a, b in pairs(g, 6, 9):
        r0 = g.route(a, b)
        ov = _overlay_at_midpoint(r0)
        alive = ~g._blocked_mask(ov)
        alive_nav = np.zeros_like(ref.nav)
        alive_nav[s.sp.ys, s.sp.xs] = alive[s.sp.ys, s.sp.xs]
        src, dst = ref.endpoints(a, b)
        for sf in TURNS[:2]:
            cost, path = ref.dijkstra_turn(src, dst, sf, alive_nav)
            r = g.route(a, b, ov, safety=sf)
            assert (r is None) == (cost is None)
            if r is not None:
                assert r.cost == cost and ref.cells_of(r) == path
                n += 1
    assert n


@pytest.mark.parametrize("name", ["r0", "r2", "pillar"])
def test_t2_point_to_point_routes_equal_the_reference(name):
    g = world(name)
    ref = Reference(g)
    sp = g._solved.sp
    rnd = random.Random(8)
    n = 0
    for _ in range(12):
        i, j = rnd.randrange(sp.N), rnd.randrange(sp.N)
        p, q = g._cell_xy(int(sp.ys[i]), int(sp.xs[i])), g._cell_xy(int(sp.ys[j]), int(sp.xs[j]))
        sa, sb = g.snap(*p), g.snap(*q)
        r = g.route(sa.key, sb.key, None, (sa.x, sa.y), (sb.x, sb.y), safety=TURNS[1])
        cost, path = ref.dijkstra_turn((int(sp.ys[i]), int(sp.xs[i])), (int(sp.ys[j]), int(sp.xs[j])), TURNS[1])
        assert r.cost == cost and ref.cells_of(r) == path
        n += 1
    assert n


# ---- T3 restriction -----------------------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["r0", "r2", "r6", "ring", "room_loop"])
def test_t3_the_restricted_search_equals_the_whole_graph_expanded_search(name):
    g = world(name)
    s = g._solved
    sp = s.sp
    R, C, _ = s.edges
    for sf in TURNS[1:3]:
        d = sf.price(sp.clr_price)
        W = weights.edge_costs(s.edges[2], d[R] + d[C], sf.rate)
        tg = turn_search.Graph(sp.N, R, C, W, turn_search.heading(sp.ys[C] - sp.ys[R], sp.xs[C] - sp.xs[R]), sp.ys, sp.xs)
        tab = turn_search.table(sf.turn)
        everything = np.ones(sp.N, bool)
        for a, b in pairs(g, 6, 12):
            src, dst = int(s.A[s.keys.index(a)]), int(s.A[s.keys.index(b)])
            whole = tg.expanded(everything, src, dst, tab)
            r = g.route(a, b, safety=sf)
            cells = [int(sp.idx[y, x]) for y, x in Reference(g).cells_of(r)]
            assert (r.cost, cells) == (whole[0], whole[1])
            # and the ellipse of the plain priced distances is a strict part of the graph on a real pair
            j_inc, _ = tg.incumbent(src, dst, *_plain(g, a, b, sf), tab)
            assert j_inc >= r.cost


def _plain(g, a, b, sf):
    s = g._solved
    r = g.route(a, b, safety=Safety(sf.margin, sf.rate))
    cells = [int(s.sp.idx[y, x]) for y, x in Reference(g).cells_of(r)]
    return cells, r.cost


# ---- T4 monotone turning ------------------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["pillar", "ring", "room_loop", "r0", "r2", "r5", "r6"])
def test_t4_more_lambda_never_increases_the_total_turning(name):
    """Exchange argument: the optima P1 at lambda_1 < lambda_2 at P2 satisfy (lambda_2 - lambda_1) (Theta_2 - Theta_1) <= U (m_1 + m_2)
    (Theta the total turning in rad, m the number of turns: each turn weight rounds up by < 1 unit U = 1e-4 m); so Theta_2 <=
    Theta_1 + U (m_1 + m_2) / (lambda_2 - lambda_1); on these steps (>= 0.05 m/rad) that is at most 0.002 rad per turn, far below
    one 45 degree turn."""
    g = world(name)
    ref = Reference(g)
    lams = [0.0, 0.05, 0.25, 0.5, 1.0, 2.0, 5.0]             # 0: the priced route (no turn term)
    fewer = 0
    for a, b in pairs(g, 5, 13):
        th, m = [], []
        for lam in lams:
            r = g.route(a, b, safety=Safety(R_C, MU, turn=lam))
            cells = ref.cells_of(r)
            th.append(_turning(cells) * math.pi / 4)
            m.append(sum(1 for p, q, s in zip(cells[:-2], cells[1:-1], cells[2:]) if (q[0] - p[0], q[1] - p[1]) != (s[0] - q[0], s[1] - q[1])))
        for i in range(1, len(lams)):
            tol = 1e-4 * (m[i] + m[i - 1]) / (lams[i] - lams[i - 1])
            assert th[i] <= th[i - 1] + tol + 1e-12, (name, a, b, lams[i], th)
        fewer += th[-1] < th[0]
    assert fewer


def test_t4_the_turn_cost_lowers_the_turning_of_the_lattice_routes():
    before = after = 0
    for name in ("r0", "r1", "r2", "r3"):
        g = world(name)
        ref = Reference(g)
        for a, b in pairs(g, 8, 21):
            before += _turning(ref.cells_of(g.route(a, b, safety=Safety(R_C, MU))))
            after += _turning(ref.cells_of(g.route(a, b, safety=Safety(R_C, MU, turn=0.5))))
    assert after < before


# ---- T5 determinism and the cache ---------------------------------------------------------------------------
def test_t5_batching_arrival_order_and_translation_give_the_same_turn_route():
    w, way = random_world(3)
    leaves = cw.drive(w, way)

    def build(batch, shift=0.0, order=None):
        ls = list(leaves) if order is None else [leaves[k] for k in order]
        if shift:
            ls = [Leaf(**{**l.__dict__, "x": l.x + shift, "origin": (l.origin[0] + shift, l.origin[1])}) for l in ls]
        g = ChartGraph(SceneParams())
        for k in range(0, len(ls), batch):
            g.fold_many(ls[k:k + batch])
        return g

    sf = Safety(0.45, 15.0, turn=0.5)

    def cells(g, shift=0.0):
        v = g.query(BBox(-1e9, -1e9, 1e9, 1e9), raster=False)
        a, b = min(v.charts, key=lambda c: c.anchor[0]), max(v.charts, key=lambda c: c.anchor[0])
        r = g.route(a.key, b.key, None, start=a.anchor, goal=b.anchor, safety=sf)
        return [(round(x - shift, 6), round(y, 6)) for x, y in r.nodes], r.cost

    base = cells(build(7))
    assert base[1] > 0
    assert cells(build(1)) == base and cells(build(48)) == base
    order = list(range(len(leaves)))
    random.Random(2).shuffle(order)
    assert cells(build(5, order=order)) == base
    assert cells(build(7, shift=10.0), shift=10.0) == base


def test_t5_alternating_lambdas_never_see_each_others_answer():
    g = world("r2")
    a, b = pairs(g, 3, 1)[0]
    fresh = {lam: world("r2").route(a, b, safety=Safety(R_C, MU, turn=lam)) for lam in (0.0, 0.5, 2.0)}
    for lam in (0.5, 0.0, 2.0, 0.5, 2.0, 0.0):
        r = g.route(a, b, safety=Safety(R_C, MU, turn=lam))
        assert (r.nodes, r.cost) == (fresh[lam].nodes, fresh[lam].cost)
    assert g._cache["route_w"][0][0] == MU                               # the weights do not depend on lambda: the rate keys them
