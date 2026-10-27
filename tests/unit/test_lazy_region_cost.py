"""The region cost term on the lazy planner graph (docs/planner_region_stats_2026-10-27.md 5, stage S3): `PlanGraph.route(..., region=)`.

region None (or a snapshot that prices nothing) is today's route, bit for bit; with a snapshot the route is the optimum of the priced
lattice, checked against an independent heapq Dijkstra over the owner raster (also under an overlay, which runs the ellipse refinement)
and, with the turn cost, against the reference of the turn tests; the tighter bound from `rho_min` never cuts the optimum off; the
answer is deterministic and does not depend on a warm cache."""
import dataclasses
import heapq
import math
import random

import numpy as np
import pytest

from packages.scene_graph.api import Safety
from packages.scene_graph.weights import ROUTE_UNIT_M, edge_costs, region_costs
from packages.services.mission_planner import lazy_plan
from packages.services.mission_planner.lazy_plan import PlanGraph, open_plan
from packages.utils import region_cost

from .scene_graph.route_safety_worlds import Reference
from .test_lazy_plan import _build
from .test_lazy_plan_equality import ALL, _extent, _overlays
from .test_lazy_route_safety import _cases, _same

pytestmark = pytest.mark.unit

R_C = 0.25 * math.sqrt(2)
MU = 1.0 / (R_C - 0.25)
SCALE = 1000


def pricing(lazy, rho, version=None, status="ok"):
    """A RegionPricing of `lazy`'s charts with the given int rho per chart index."""
    arr = np.asarray(rho, np.int32)
    return region_cost._snapshot("m", lazy.version if version is None else version, "CARRIER|0.50x0.50", "stamp-1",
                                 region_cost.CostModel(), status, lazy.keys, arr, ["class"] * len(arr))


def random_rho(lazy, seed, lo=0, hi=2500, zero_share=0.25):
    rnd = random.Random(seed)
    return [0 if rnd.random() < zero_share else rnd.randint(lo, hi) for _ in lazy.keys]


@pytest.fixture(scope="module", params=sorted(ALL))
def built(request):
    st, eng, _ = _build(ALL[request.param]())
    return request.param, st, eng, open_plan(st, "m", max_tiles=10 ** 6)


def reference_cost(lazy, a, b, rho, blocked=None):
    """Independent optimum cost of the region-priced lattice between the anchor cells of charts a and b: a heapq Dijkstra over
    the owner raster (8 neighbours, a diagonal needs both orthogonal cells), the edge cost written out here."""
    ja, jb = lazy._index[a], lazy._index[b]
    src, dst = tuple(int(v) for v in lazy._cell[ja]), tuple(int(v) for v in lazy._cell[jb])
    h = float(lazy.params.lattice)
    x0 = min(lazy._anchor[:, 0]) - 4.0
    y0 = min(lazy._anchor[:, 1]) - 4.0
    x1 = max(lazy._anchor[:, 0]) + 4.0
    y1 = max(lazy._anchor[:, 1]) + 4.0
    ix0, iy0, idx, keys = lazy.owner_window(x0, y0, x1, y1)
    ch = np.full(idx.shape, -1, np.int64)
    for i, k in enumerate(keys):
        ch[idx == i] = lazy._index[k]
    owned = ch >= 0                       # a diagonal needs both orthogonal cells OWNED (the engine's plane); the cells a route uses must be alive too
    nav = owned.copy()
    if blocked:
        yy, xx = np.nonzero(nav)
        alive = PlanGraph._alive(blocked, yy + iy0, xx + ix0)
        nav[yy[~alive], xx[~alive]] = False

    def free(c, plane=nav):
        y, x = c[0] - iy0, c[1] - ix0
        return 0 <= y < plane.shape[0] and 0 <= x < plane.shape[1] and bool(plane[y, x])

    def rho_at(c):
        return int(rho[ch[c[0] - iy0, c[1] - ix0]])

    def weight(c, d):
        units = math.hypot(c[0] - d[0], c[1] - d[1]) * h / ROUTE_UNIT_M
        return max(math.ceil(units - 1e-6), 1) + max(math.ceil(units * (rho_at(c) + rho_at(d)) / (2.0 * SCALE) - 1e-6), 0)

    def around(c):
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                d = (c[0] + dy, c[1] + dx)
                if (dy or dx) and free(d) and (not (dy and dx) or (free((c[0], d[1]), owned) and free((d[0], c[1]), owned))):
                    yield d

    dist = {dst: 0}
    pq = [(0, dst)]
    while pq:
        d, u = heapq.heappop(pq)
        if d > dist[u]:
            continue
        for v in around(u):
            nd = d + weight(u, v)
            if nd < dist.get(v, math.inf):
                dist[v] = nd
                heapq.heappush(pq, (nd, v))
    return dist.get(src)


def path_cost(route, lazy, rho):
    """The cost of a route's own cells under the priced lattice (independent of the search's arrays)."""
    h = float(lazy.params.lattice)
    cells = [(int(math.floor(y / lazy_plan.RES)) // 2, int(math.floor(x / lazy_plan.RES)) // 2) for x, y in route.nodes]
    total = 0
    for p, q in zip(cells[:-1], cells[1:]):
        units = math.hypot(p[0] - q[0], p[1] - q[1]) * h / ROUTE_UNIT_M
        rp, rq = rho[lazy.chart_index_cell(p[1], p[0])], rho[lazy.chart_index_cell(q[1], q[0])]
        total += max(math.ceil(units - 1e-6), 1) + max(math.ceil(units * (rp + rq) / (2.0 * SCALE) - 1e-6), 0)
    return total


def test_weights_region_costs_formula():
    """The one formula: 0 for rho 0, ceil(l * (ra + rb) / 2 scale) otherwise, never negative, so every edge costs at least its length."""
    length = np.array([0.1, 0.1 * math.sqrt(2), 0.1])
    got = region_costs(length, [0, 1000, 3000], [0, 1000, 0], SCALE)
    assert got.tolist() == [0.0, math.ceil(length[1] / ROUTE_UNIT_M * 1.0 - 1e-6), math.ceil(1000 * 1.5 - 1e-6)]
    assert (region_costs(length, [0, 0, 0], [0, 0, 0]) == 0).all()
    assert (edge_costs(length, None, 0.0) + region_costs(length, [5, 5, 5], [7, 7, 7]) >= length / ROUTE_UNIT_M - 1e-6).all()


# ---- region None is today's route ------------------------------------------------------------------------
def test_no_region_is_bit_identical(built):
    name, st, eng, lazy = built
    n = 0
    zero = pricing(lazy, [0] * len(lazy.keys))
    inactive = pricing(lazy, random_rho(lazy, 1), status="no_stats")
    other_version = pricing(lazy, random_rho(lazy, 2), version=lazy.version + 1)
    other_keys = dataclasses.replace(pricing(lazy, random_rho(lazy, 3)), keys=tuple(reversed(lazy.keys)))
    for a, b, s, g in _cases(eng, name, 20):
        base = lazy.route(a, b, None, s, g)
        for neutral in (None, zero, inactive, other_version, other_keys):
            got = lazy.route(a, b, None, s, g, region=neutral)
            _same(base, got)
            if base is not None:
                assert got.exact == base.exact and got.min_width == base.min_width
        n += base is not None
    assert n
    lazy.route(*_cases(eng, name, 1)[0], region=other_version)
    assert lazy.last["region"] == "version_mismatch"
    lazy.route(*_cases(eng, name, 1)[0], region=other_keys)
    assert lazy.last["region"] == "keys_mismatch"
    lazy.route(*_cases(eng, name, 1)[0], region=zero)
    assert lazy.last["region"] == "neutral"


def test_region_with_safety_none_equals_rate_zero_safety_priced_by_region(built):
    """A region with a rate-0 safety is the same search as the region alone (rate 0 and turn 0 is no price)."""
    name, st, eng, lazy = built
    reg = pricing(lazy, random_rho(lazy, 4))
    for a, b, s, g in _cases(eng, name, 12):
        _same(lazy.route(a, b, None, s, g, region=reg), lazy.route(a, b, None, s, g, safety=Safety(0.45, 0.0), region=reg))


# ---- the optimum of the priced lattice ----------------------------------------------------------------------
def test_the_region_route_is_the_brute_force_optimum(built):
    name, st, eng, lazy = built
    n = changed = 0
    for seed in (11, 12):
        rho = random_rho(lazy, seed)
        reg = pricing(lazy, rho)
        for a, b, _, _ in _cases(eng, name, 12)[:10]:
            want = reference_cost(lazy, a, b, rho)
            got = lazy.route(a, b, None, None, None, region=reg)
            assert (got is None) == (want is None)
            if got is None:
                continue
            assert got.cost == want
            assert path_cost(got, lazy, rho) == got.cost            # and the cells really cost that
            assert lazy.last["exact"] and lazy.last["region"] == "priced"
            base = lazy.route(a, b)
            assert got.cost >= base.cost                            # every edge costs at least its length
            changed += [tuple(p) for p in got.nodes] != [tuple(p) for p in base.nodes]
            n += 1
    assert n
    if name in ("grid", "grid+shift", "room_loop", "ring", "pillar_room", "room_loop+shift", "ring+shift", "pillar_room+shift"):
        assert changed                                              # the term really steers on worlds with alternatives


def test_the_region_route_under_an_overlay_is_the_brute_force_optimum(built):
    """An overlay (and the priced search) runs the ellipse refinement: the answer is still the optimum of the cells left alive."""
    name, st, eng, lazy = built
    rnd = random.Random(5 + len(name))
    rho = random_rho(lazy, 21)
    reg = pricing(lazy, rho)
    n = used = 0
    for a, b, s, g in _cases(eng, name, 30):
        free = lazy.route(a, b, None, None, None)
        if free is None or len(free.nodes) < 6 or used >= 8 or s is not None:
            continue
        used += 1
        for kind, bl in _overlays(free, rnd).items():
            want = reference_cost(lazy, a, b, rho, bl)
            got = lazy.route(a, b, bl, None, None, region=reg)
            assert (got is None) == (want is None), kind
            if got is not None:
                assert got.cost == want and lazy.last["exact"], kind
                n += 1
    assert used                                  # (a world whose only door the overlay blocks has no route either way)
    assert n or name.startswith("two_rooms")


def test_a_clearance_price_and_the_region_term_add_up(built):
    """Under a clearance price the cost is the engine's priced cost plus the region term of the same cells, and no path is cheaper."""
    name, st, eng, lazy = built
    rho = random_rho(lazy, 31)
    reg = pricing(lazy, rho)
    sf = Safety(R_C, MU)
    n = 0
    for a, b, _, _ in _cases(eng, name, 8)[:6]:
        both = lazy.route(a, b, None, None, None, safety=sf, region=reg)
        only_c = lazy.route(a, b, None, None, None, safety=sf)
        if both is None:
            continue
        assert lazy.last["exact"]
        # the optimum of the sum is at most the cost of the clearance-only optimum's cells under the sum
        ref = Reference(eng)
        cells = ref.cells_of(only_c)
        clear_only = ref.path_cost(cells, sf)
        assert clear_only == only_c.cost
        sum_cost = only_c.cost + path_cost(only_c, lazy, rho) - path_cost(only_c, lazy, [0] * len(rho))
        assert both.cost <= sum_cost
        n += 1
    assert n


# ---- bounds ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("lo", [0, 400])
def test_the_rho_min_bound_keeps_the_optimum(built, lo):
    """rho_min > 0 divides the ellipse bound; the answer is the same as with the plain bound (rho_min overridden to 0), and the bound
    holds the optimum's length."""
    name, st, eng, lazy = built
    rho = random_rho(lazy, 41 + lo, lo=lo, hi=lo + 2500, zero_share=0.0)
    reg = pricing(lazy, rho)
    assert reg.rho_min >= lo
    loose = dataclasses.replace(reg, rho_min=0)
    rnd = random.Random(7)
    n = 0
    for a, b, s, g in _cases(eng, name, 16):
        free = lazy.route(a, b, None, s, g)
        if free is None or len(free.nodes) < 6:
            continue
        for bl in [None] + ([list(_overlays(free, rnd).values())[0]] if s is None else []):
            tight, wide = lazy.route(a, b, bl, s, g, region=reg), lazy.route(a, b, bl, s, g, region=loose)
            assert (tight is None) == (wide is None)
            if tight is None:
                continue
            _same(wide, tight)
            assert tight.exact and wide.exact
            reg_arg = lazy.region_arg(reg)
            assert lazy._geo_bound(tight.cost, reg_arg) >= tight.length - 1e-9
            assert lazy._geo_bound(tight.cost, reg_arg) <= lazy._geo_bound(tight.cost, lazy.region_arg(loose)) + 1e-9
            n += 1
    assert n


def test_every_edge_costs_at_least_its_length_times_the_floor():
    """The property the bounds rest on, edge by edge: cost >= units * (1 + rho_min / scale) up to the 1e-6 of the ceil tolerance."""
    rnd = random.Random(3)
    for _ in range(2000):
        length = rnd.choice((0.1, 0.1 * math.sqrt(2)))
        r0 = rnd.randint(0, 3000)
        ra, rb = r0 + rnd.randint(0, 500), r0 + rnd.randint(0, 500)
        cost = (edge_costs([length], None, 0.0) + region_costs([length], [ra], [rb], SCALE))[0]
        assert cost >= length / ROUTE_UNIT_M * (1.0 + r0 / SCALE) - 2e-6


def test_the_bound_is_tighter_only_with_a_positive_floor(built):
    name, st, eng, lazy = built
    rho = np.asarray(random_rho(lazy, 5, lo=0, hi=2000, zero_share=0.0), np.int64)
    assert lazy._geo_bound(12345, None) == 12345 * ROUTE_UNIT_M
    assert lazy._geo_bound(12345, (rho, SCALE, 0)) == 12345 * ROUTE_UNIT_M
    assert lazy._geo_bound(12345, (rho, SCALE, 1000)) == pytest.approx(12345 * ROUTE_UNIT_M / 2.0, rel=1e-5)


# ---- determinism ---------------------------------------------------------------------------------------------
def test_deterministic_and_cache_independent(built):
    name, st, eng, lazy = built
    rho = random_rho(lazy, 51)
    reg = pricing(lazy, rho)
    cases = _cases(eng, name, 10)
    warm = [lazy.route(a, b, None, s, g, region=reg) for a, b, s, g in cases]
    again = [lazy.route(a, b, None, s, g, region=reg) for a, b, s, g in cases]
    cold = open_plan(st, "m", max_tiles=10 ** 6)
    third = [cold.route(a, b, None, s, g, region=pricing(cold, rho)) for a, b, s, g in cases]
    for w, x, y in zip(warm, again, third):
        assert (w is None) == (x is None) == (y is None)
        if w is not None:
            _same(w, x)
            _same(w, y)


# ---- the turn search ---------------------------------------------------------------------------------------------
TURNS = [Safety(R_C, 0.0, turn=0.5), Safety(R_C, MU, turn=0.5)]


def test_turn_search_with_rho_is_the_independent_optimum(built):
    """Total cost with the turn term and the region term: the reference's turn Dijkstra with the region cost added to its edge weight."""
    name, st, eng, lazy = built
    rho = random_rho(lazy, 61)
    reg = pricing(lazy, rho)
    ref = Reference(eng)
    base_weight = ref.weight
    # canvas cell -> global lattice cell: the offset between the reference's canvas and the lazy reader's lattice
    probe = lazy.route(*_cases(eng, name, 1)[0][:2])
    c0 = ref.cells_of(probe)[0]
    x, y = probe.nodes[0]
    off = (int(math.floor(y / lazy_plan.RES)) // 2 - c0[0], int(math.floor(x / lazy_plan.RES)) // 2 - c0[1])
    h = float(lazy.params.lattice)

    def weight(p, q, safety):
        units = math.hypot(p[0] - q[0], p[1] - q[1]) * h / ROUTE_UNIT_M
        rp = rho[lazy.chart_index_cell(p[1] + off[1], p[0] + off[0])]
        rq = rho[lazy.chart_index_cell(q[1] + off[1], q[0] + off[0])]
        return base_weight(p, q, safety) + max(math.ceil(units * (rp + rq) / (2.0 * SCALE) - 1e-6), 0)
    ref.weight = weight
    n = 0
    for a, b, _, _ in _cases(eng, name, 6)[:6]:
        src, dst = ref.endpoints(a, b)
        for sf in TURNS:
            cost, _ = ref.dijkstra_turn(src, dst, sf)
            got = lazy.route(a, b, None, None, None, safety=sf, region=reg)
            assert (got is None) == (cost is None)
            if got is not None:
                assert got.cost == cost and lazy.last["exact"]
                n += 1
    assert n


def test_turn_search_with_rho_has_a_cold_and_overlay_answer(built):
    name, st, eng, lazy = built
    rho = random_rho(lazy, 71)
    reg = pricing(lazy, rho)
    rnd = random.Random(9)
    n = 0
    for a, b, s, g in _cases(eng, name, 20):
        free = lazy.route(a, b, None, s, g)
        if free is None or len(free.nodes) < 6 or n >= 6:
            continue
        for bl in (None, _overlays(free, rnd)["disc"]):
            warm = lazy.route(a, b, bl, s, g, safety=TURNS[1], region=reg)
            cold = open_plan(st, "m", max_tiles=10 ** 6).route(a, b, bl, s, g, safety=TURNS[1], region=pricing(lazy, rho))
            assert (warm is None) == (cold is None)
            if warm is not None:
                _same(warm, cold)
                assert warm.exact
        n += 1
    assert n
