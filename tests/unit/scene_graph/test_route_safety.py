"""Clearance-priced route (docs/scene_graph/route_safety.md): `ChartGraph.route(..., safety=Safety(margin, rate))` and `deficit_cell`.

Tests P1-P6 of section 4: reduction, completeness, optimality against an independent reference, dominance, the monotone family in
the rate, invariance. The reference (route_safety_worlds.Reference) recomputes clearance, edges and priced cost with scipy EDT and
heapq on python ints."""
import dataclasses
import math
import random

import numpy as np
import pytest

from packages.scene_graph.api import BBox, Blocked, Leaf, Safety, SceneGraph, SceneParams
from packages.scene_graph.engine import ChartGraph

from . import chart_worlds as cw
from .route_safety_worlds import Reference, engine_of, pairs, random_world

pytestmark = pytest.mark.unit

SEEDS = range(8)
STRUCTURAL = dict(two_rooms=cw.two_rooms, pillar=cw.pillar_room, ring=cw.ring, room_loop=cw.room_loop)
_cache = {}


def world(name):
    if name not in _cache:
        w = random_world(int(name[1:])) if name[1:].isdigit() else STRUCTURAL[name]()
        _cache[name] = engine_of(w)
    return _cache[name]


ALL_WORLDS = list(STRUCTURAL) + ["r%d" % s for s in SEEDS]
R_C = 0.25 * math.sqrt(2)          # the circumscribed radius of the map's robot: the planner's default margin
SAFETIES = [Safety(R_C, 1.0 / (R_C - 0.25)), Safety(0.45, 15.0), Safety(0.6, 40.0)]


def _route_pairs(g, n=8, seed=3):
    return pairs(g, n, seed)


def _overlay_at_midpoint(r, radius=0.35):
    mid = r.nodes[len(r.nodes) // 2]
    return Blocked(discs=[(mid[0], mid[1], radius)], segments=[])


# ---- Safety value object ------------------------------------------------------------------------------------
def test_safety_validates_and_is_frozen():
    Safety(0.4, 0.0)
    for bad in ((0.0, 1.0), (-0.1, 1.0), (float("nan"), 1.0), (0.4, -1.0), (0.4, float("nan"))):
        with pytest.raises(ValueError):
            Safety(*bad)
    with pytest.raises(dataclasses.FrozenInstanceError):
        Safety(0.4, 1.0).margin = 1.0


def test_contract_signature_lists_safety_and_deficit_cell():
    import inspect
    assert "safety" in inspect.signature(SceneGraph.route).parameters
    assert "safety" in inspect.signature(ChartGraph.route).parameters
    assert inspect.signature(SceneGraph.deficit_cell).parameters.keys() == inspect.signature(ChartGraph.deficit_cell).parameters.keys()
    with pytest.raises(NotImplementedError):
        SceneGraph().deficit_cell(0, 0, 0.4)


# ---- deficit_cell -------------------------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["two_rooms", "pillar", "r0", "r3"])
def test_deficit_cell_is_the_independent_clearance_deficit_and_none_off_the_lattice(name):
    g = world(name)
    ref = Reference(g)
    sp = g._solved.sp
    margin = 0.45
    seen_pos = seen_zero = 0
    for y in range(sp.H):
        for x in range(sp.W):
            got = g.deficit_cell(x + g._cx0 // 2, y + g._cy0 // 2, margin)
            if not ref.nav[y, x]:
                assert got is None
                continue
            assert got == pytest.approx(ref.deficit(y, x, margin), abs=1e-12)
            seen_pos += got > 0.0
            seen_zero += got == 0.0
    assert seen_pos and seen_zero
    assert g.deficit_cell(10 ** 6, 0, margin) is None and g.deficit_cell(0, -10 ** 6, margin) is None


# ---- P1 reduction -------------------------------------------------------------------------------------------
@pytest.mark.parametrize("name", ALL_WORLDS)
def test_p1_rate_zero_gives_the_unpriced_route(name):
    g = world(name)
    n = 0
    for a, b in _route_pairs(g):
        r0 = g.route(a, b)
        assert r0 is not None
        ovs = [None, _overlay_at_midpoint(r0)]
        for ov in ovs:
            base = g.route(a, b, ov)
            for sf in (Safety(0.45, 0.0), Safety(1.2, 0.0)):         # no rate: the unpriced route, whatever the margin
                rs = g.route(a, b, ov, safety=sf)
                assert (rs is None) == (base is None)
                if base is not None:
                    assert (rs.nodes, rs.cost, rs.charts, rs.length) == (base.nodes, base.cost, base.charts, base.length)
                    assert rs.min_width == base.min_width
            n += 1
    assert n


def test_p1_the_default_route_is_the_unpriced_one_and_does_not_see_a_cached_priced_cost():
    g = world("pillar")
    a, b = _route_pairs(g)[0]
    before = g.route(a, b)
    g.route(a, b, safety=Safety(0.6, 40.0))                           # fills the priced cost cache
    after = g.route(a, b)
    assert (after.nodes, after.cost) == (before.nodes, before.cost)
    # two different safeties in turn: the cache keeps the last key and each answer matches a fresh computation
    s1, s2 = Safety(0.45, 15.0), Safety(0.6, 40.0)
    r1, r2 = g.route(a, b, safety=s1), g.route(a, b, safety=s2)
    assert g.route(a, b, safety=s1).cost == r1.cost and g.route(a, b, safety=s2).cost == r2.cost


# ---- P2 completeness ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("name", ALL_WORLDS)
def test_p2_a_priced_route_exists_iff_an_unpriced_one_does(name):
    g = world(name)
    reachable = 0
    for a, b in _route_pairs(g, 10, 7):
        r0 = g.route(a, b)
        ovs = [None]
        if r0 is not None:
            ovs += [_overlay_at_midpoint(r0, 0.35), _overlay_at_midpoint(r0, 1.5)]      # the big one may cut the pair
        for ov in ovs:
            base = g.route(a, b, ov)
            for sf in SAFETIES:
                rs = g.route(a, b, ov, safety=sf)
                assert (rs is None) == (base is None), (name, a, b)
                if rs is not None:
                    reachable += 1
                    assert math.isfinite(rs.cost) and rs.cost > 0
    assert reachable


def test_p2_the_overlay_cuts_some_pair_and_the_priced_search_agrees():
    cut = 0
    for name in ALL_WORLDS:
        g = world(name)
        for a, b in _route_pairs(g, 12, 11):
            r0 = g.route(a, b)
            ov = _overlay_at_midpoint(r0, 3.0)                  # a disc of 3 m radius on the route: cuts many pairs
            base = g.route(a, b, ov, start=r0.nodes[0], goal=r0.nodes[-1])
            rs = g.route(a, b, ov, start=r0.nodes[0], goal=r0.nodes[-1], safety=SAFETIES[1])
            assert (rs is None) == (base is None)
            cut += base is None
    assert cut > 0


# ---- P3 optimality -----------------------------------------------------------------------------------------
@pytest.mark.parametrize("name", ALL_WORLDS)
def test_p3_priced_cost_is_the_independent_optimum_and_the_path_is_canonical(name):
    g = world(name)
    ref = Reference(g)
    checked = 0
    for a, b in _route_pairs(g, 6, 5):
        src, dst = ref.endpoints(a, b)
        for sf in SAFETIES:
            r = g.route(a, b, safety=sf)
            cost, path = ref.dijkstra(src, dst, sf)
            assert r.cost == cost, (name, a, b, sf)
            assert ref.cells_of(r) == path
            assert ref.path_cost(ref.cells_of(r), sf) == r.cost          # Route.cost is the evaluated J of its own cells
            r0 = g.route(a, b)
            assert r.cost <= ref.path_cost(ref.cells_of(r0), sf)         # J(priced) <= J(unpriced route, same weights)
            checked += 1
    assert checked


@pytest.mark.parametrize("name", ["r1", "r4", "ring"])
def test_p3_under_an_overlay_the_whole_graph_is_searched_and_equals_the_reference(name):
    g = world(name)
    ref = Reference(g)
    s = g._solved
    sf = SAFETIES[1]
    n = 0
    for a, b in _route_pairs(g, 6, 9):
        r0 = g.route(a, b)
        ov = _overlay_at_midpoint(r0, 0.4)
        alive = ~g._blocked_mask(ov)
        alive_nav = np.zeros_like(ref.nav)
        alive_nav[s.sp.ys, s.sp.xs] = alive[s.sp.ys, s.sp.xs]
        src, dst = ref.endpoints(a, b)
        cost, path = ref.dijkstra(src, dst, sf, alive_nav)
        r = g.route(a, b, ov, safety=sf)
        assert (r is None) == (cost is None)
        if r is not None:
            assert r.cost == cost and ref.cells_of(r) == path
            n += 1
    assert n


# ---- P4 dominance ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("name", ALL_WORLDS)
def test_p4_a_priced_route_is_never_shorter_than_the_unpriced_one(name):
    g = world(name)
    n = 0
    for a, b in _route_pairs(g, 10, 2):
        r0 = g.route(a, b)
        for sf in SAFETIES:
            rs = g.route(a, b, safety=sf)
            assert rs.length >= r0.length - 1e-9, (name, a, b, sf)
            assert rs.cost >= r0.cost            # a priced weight is never below the unpriced one
            n += 1
    assert n


def test_p4_pricing_does_change_some_route_of_the_worlds():
    longer = 0
    for name in ALL_WORLDS:
        g = world(name)
        for a, b in _route_pairs(g, 10, 2):
            longer += g.route(a, b, safety=SAFETIES[1]).length > g.route(a, b).length + 1e-9
    assert longer > 0


# ---- P5 monotone family ------------------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["pillar", "ring", "room_loop", "r0", "r2", "r5", "r6"])
def test_p5_the_family_in_the_rate_is_monotone_and_concave(name):
    g = world(name)
    ref = Reference(g)
    margin = 0.45
    mus = [0.0, 1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0]
    for a, b in _route_pairs(g, 5, 13):
        J, L, A, N = [], [], [], []
        src, dst = ref.endpoints(a, b)
        for mu in mus:
            if mu == 0.0:                       # rate 0 is today's (corridor) route, not necessarily the optimum: take the optimum
                _, cells = ref.dijkstra(src, dst, Safety(margin, 0.0))
                cost = ref.path_cost(cells, Safety(margin, 0.0))
                length = sum(math.hypot(p[0] - q[0], p[1] - q[1]) * 0.1 for p, q in zip(cells[:-1], cells[1:]))
            else:
                r = g.route(a, b, safety=Safety(margin, mu))
                cells, cost, length = ref.cells_of(r), r.cost, r.length
            J.append(cost); L.append(length); A.append(ref.intrusion(cells, margin)); N.append(len(cells) - 1)
        slack = max(N)                                   # each edge is rounded up by < 1 unit: the integer J is the real J within n units
        for i in range(1, len(mus)):
            assert J[i] >= J[i - 1]                      # a priced weight is never below the unpriced one: J* is non-decreasing exactly
            # the Pareto frontier: longer and less intrusive. The integer cost rounds each edge up by < 1 unit, which moves the
            # exchange argument (adding the two optimality inequalities) by at most n units: A_j <= A_i + n * unit / (mu_j - mu_i),
            # and L_j >= L_i - (1 + mu_i / (mu_j - mu_i)) * n * unit; the grid steps are >= 1 and double.
            assert L[i] >= L[i - 1] - 2.0 * slack * UNIT_M
            assert A[i] <= A[i - 1] + slack * UNIT_M
        # concavity of J*(mu) on equal steps: J*(mu_i) >= (J*(mu_{i-1}) + J*(mu_{i+1})) / 2 up to the rounding of both ends
        for mu_lo, mu_mid, mu_hi in ((0.0, 1.0, 2.0), (2.0, 4.0, 6.0), (8.0, 16.0, 24.0)):
            vals = [g.route(a, b, safety=Safety(margin, m)).cost for m in (mu_lo, mu_mid, mu_hi)] if mu_lo > 0.0 else \
                [ref.dijkstra(src, dst, Safety(margin, m))[0] for m in (mu_lo, mu_mid, mu_hi)]
            assert vals[1] >= (vals[0] + vals[2]) / 2.0 - slack


UNIT_M = 1e-4


# ---- P6 invariance -----------------------------------------------------------------------------------------
def test_p6_batching_arrival_order_and_translation_give_the_same_priced_route():
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

    sf = Safety(0.45, 15.0)

    def cells(g, shift=0.0, ov=None):
        v = g.query(BBox(-1e9, -1e9, 1e9, 1e9), raster=False)
        a, b = min(v.charts, key=lambda c: c.anchor[0]), max(v.charts, key=lambda c: c.anchor[0])
        r = g.route(a.key, b.key, ov, start=a.anchor, goal=b.anchor, safety=sf)
        return None if r is None else ([(round(x - shift, 6), round(y, 6)) for x, y in r.nodes], r.cost)

    base = cells(build(7))
    assert base is not None and base[1] > 0
    assert cells(build(1)) == base and cells(build(48)) == base
    order = list(range(len(leaves)))
    random.Random(2).shuffle(order)
    assert cells(build(5, order=order)) == base
    assert cells(build(7, shift=10.0), shift=10.0) == base
    ov = Blocked(discs=[(base[0][len(base[0]) // 2][0], base[0][len(base[0]) // 2][1], 0.4)], segments=[])
    assert cells(build(7), ov=ov) == cells(build(3), ov=ov)
