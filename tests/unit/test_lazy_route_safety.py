"""Clearance-priced route on the lazy planner graph (docs/scene_graph/route_safety.md): `PlanGraph.route(..., safety=)` and
`PlanGraph.deficit_cell` against the restored engine and the independent reference.

P1 reduction (lazy), P2 completeness (lazy), P7 lazy == engine on the nine equality worlds with and without an overlay, P8 halo
exactness of the clearance at tile edges."""
import random

import pytest

from packages.scene_graph.api import Blocked, Safety
from packages.services.mission_planner.lazy_plan import CorridorTooLarge, PlanGraph, open_plan

from .scene_graph.route_safety_worlds import Reference
from .test_lazy_plan import _build
from .test_lazy_plan_equality import ALL, _extent, _overlays, _point_pairs

pytestmark = pytest.mark.unit

SAFETIES = [Safety(0.404, 3.0 / 0.154), Safety(0.6, 40.0)]


@pytest.fixture(scope="module", params=sorted(ALL))
def built(request):
    st, eng, _ = _build(ALL[request.param]())
    return request.param, st, eng, open_plan(st, "m", max_tiles=10 ** 6)


def _cases(eng, name, n_anchor=40):
    """(a, b, start, goal) of anchor pairs and of point pairs (the latter straddle tile boundaries on the shifted worlds)"""
    keys, ext = _extent(eng)
    rnd = random.Random(77 + len(name))
    allp = [(a, b) for a in keys for b in keys]
    rnd.shuffle(allp)
    out = [(a, b, None, None) for a, b in allp[:n_anchor]]
    for p, q in _point_pairs(eng, ext, 11 + len(name))[:50]:
        sa, sb = eng.snap(*p, 1.0), eng.snap(*q, 1.0)
        if sa.key is not None and sb.key is not None:
            out.append((sa.key, sb.key, (sa.x, sa.y), (sb.x, sb.y)))
    return out


def _same(e, l):
    assert (e is None) == (l is None)
    if e is not None:
        assert l.cost == e.cost
        assert l.charts == e.charts
        assert [tuple(p) for p in l.nodes] == [tuple(p) for p in e.nodes]
        assert l.length == pytest.approx(e.length, abs=1e-9)


# ---- P1 -----------------------------------------------------------------------------------------------------
def test_p1_rate_zero_gives_the_unpriced_lazy_route(built):
    name, st, eng, lazy = built
    n = 0
    for a, b, s, g in _cases(eng, name, 25):
        base = lazy.route(a, b, None, s, g)
        _same(base, lazy.route(a, b, None, s, g, safety=Safety(0.45, 0.0)))
        # and the unpriced lazy route is the engine's, cost included (the integer cost the canonical tie rule needs)
        _same(eng.route(a, b, None, s, g), base)
        n += base is not None
    assert n


# ---- P7 -----------------------------------------------------------------------------------------------------
def test_p7_priced_lazy_route_equals_the_engine_cells_cost_charts(built):
    name, st, eng, lazy = built
    cases = _cases(eng, name)
    n = priced = 0
    for a, b, s, g in cases:
        base = eng.route(a, b, None, s, g)
        for sf in SAFETIES:
            e = eng.route(a, b, None, s, g, safety=sf)
            l = lazy.route(a, b, None, s, g, safety=sf)
            _same(e, l)
            assert lazy.last["exact"]
            if e is not None:
                n += 1
                priced += [tuple(p) for p in e.nodes] != [tuple(p) for p in base.nodes]
    assert n
    if name in ("grid", "grid+shift", "room_loop", "ring"):
        assert priced > 0                   # the price really moves the route on these worlds


@pytest.mark.parametrize("ov", ["disc", "segment"])
def test_p7_priced_lazy_route_equals_the_engine_under_an_overlay(built, ov):
    name, st, eng, lazy = built
    rnd = random.Random(5 + len(name))
    n = used = 0
    for a, b, s, g in _cases(eng, name, 30):
        free = eng.route(a, b, None, s, g)
        if free is None or len(free.nodes) < 6 or used >= 25:
            continue
        used += 1
        bl = _overlays(free, rnd)[ov]
        for sf in SAFETIES:
            e = eng.route(a, b, bl, s, g, safety=sf)
            l = lazy.route(a, b, bl, s, g, safety=sf)
            _same(e, l)
            assert lazy.last["exact"]
            n += e is not None
    assert n


def test_p7_the_priced_lazy_cost_is_the_independent_optimum(built):
    name, st, eng, lazy = built
    ref = Reference(eng)
    n = 0
    for a, b, _, _ in _cases(eng, name, 12):
        src, dst = ref.endpoints(a, b)
        for sf in SAFETIES:
            cost, path = ref.dijkstra(src, dst, sf)
            l = lazy.route(a, b, None, None, None, safety=sf)
            assert (l is None) == (cost is None)
            if l is not None:
                assert l.cost == cost and ref.cells_of(l) == path
                n += 1
    assert n


def test_p2_priced_lazy_route_exists_iff_the_unpriced_one_does(built):
    name, st, eng, lazy = built
    rnd = random.Random(31)
    n = 0
    for a, b, s, g in _cases(eng, name, 20):
        free = lazy.route(a, b, None, s, g)
        ovs = [None]
        if free is not None and len(free.nodes) >= 6:
            ovs += [_overlays(free, rnd)["disc"], Blocked(discs=[(*free.nodes[len(free.nodes) // 2], 1.5)], segments=[])]
        for bl in ovs:
            base = lazy.route(a, b, bl, s, g)
            for sf in SAFETIES:
                assert (lazy.route(a, b, bl, s, g, safety=sf) is None) == (base is None)
                n += 1
    assert n


# ---- P8 -----------------------------------------------------------------------------------------------------
def test_p8_lazy_deficit_cell_equals_the_engines_for_every_navigable_cell_including_tile_edges(built):
    name, st, eng, lazy = built
    sp = eng._solved.sp
    ix0, iy0 = eng._cx0 // 2, eng._cy0 // 2
    edge = 0
    for margin in (0.404, 1.3):
        for y, x in zip(sp.ys.tolist(), sp.xs.tolist()):
            ix, iy = x + ix0, y + iy0
            e = eng.deficit_cell(ix, iy, margin)
            assert e is not None
            assert lazy.deficit_cell(ix, iy, margin) == e, (name, ix, iy, margin)       # equal to the last bit
            edge += (ix % 64 in (0, 63)) or (iy % 64 in (0, 63))
    assert name in ("two_rooms", "room_loop", "ring", "pillar_room", "two_rooms+shift", "room_loop+shift", "ring+shift",
                    "pillar_room+shift", "grid", "grid+shift")
    if name.endswith("+shift") or name == "grid":
        assert edge > 0                                  # cells on a tile edge are among the compared ones


def test_p8_non_navigable_cells_and_absent_tiles_have_no_deficit(built):
    name, st, eng, lazy = built
    sp = eng._solved.sp
    ix0, iy0 = eng._cx0 // 2, eng._cy0 // 2
    rnd = random.Random(4)
    none = 0
    for _ in range(400):
        y, x = rnd.randrange(sp.H), rnd.randrange(sp.W)
        got = lazy.deficit_cell(x + ix0, y + iy0, 0.45)
        assert (got is None) == (sp.idx[y, x] < 0) == (eng.deficit_cell(x + ix0, y + iy0, 0.45) is None)
        none += got is None
    assert none
    assert lazy.deficit_cell(10 ** 5, 10 ** 5, 0.45) is None and lazy.deficit_cell(-10 ** 5, 0, 0.45) is None


def test_p8_the_clearance_cache_is_bounded_and_does_not_change_answers():
    from .test_lazy_plan import WORLDS
    st, eng, _ = _build(WORLDS["grid"]())
    tiny = PlanGraph(st.view("m"), max_tiles=10 ** 6, tile_cache=8)
    sp = eng._solved.sp
    ix0, iy0 = eng._cx0 // 2, eng._cy0 // 2
    for y, x in list(zip(sp.ys.tolist(), sp.xs.tolist()))[::37]:
        assert tiny.deficit_cell(x + ix0, y + iy0, 0.5) == eng.deficit_cell(x + ix0, y + iy0, 0.5)
    assert len(tiny._clr) <= 8


# ---- the cap ------------------------------------------------------------------------------------------------
def test_a_priced_search_over_the_cap_raises_or_keeps_the_corridor_route(built):
    name, st, eng, _ = built
    sf = SAFETIES[0]
    small = PlanGraph(st.view("m"), max_tiles=3)
    exact = raised = 0
    for a, b, s, g in _cases(eng, name, 25):
        try:
            l = small.route(a, b, None, s, g, safety=sf)
        except CorridorTooLarge as exc:
            assert exc.tiles > 3
            raised += 1
            continue
        e = eng.route(a, b, None, s, g, safety=sf)
        assert (l is None) == (e is None)
        if l is not None:
            assert l.cost >= e.cost
            if small.last["exact"]:
                assert l.cost == e.cost
                exact += 1
    assert exact + raised


def test_the_ellipse_stage_over_the_cap_returns_the_corridor_route_and_says_inexact(monkeypatch):
    from .test_lazy_plan import WORLDS
    st, eng, _ = _build(WORLDS["room_loop"]())
    lazy = open_plan(st, "m", max_tiles=10 ** 6)
    real = lazy._search
    calls = []

    def search(*a, **kw):
        calls.append(kw.get("limit"))
        if kw.get("limit") is not None:
            raise CorridorTooLarge(99, 1)
        return real(*a, **kw)
    monkeypatch.setattr(lazy, "_search", search)
    keys, _ = _extent(eng)
    sf = SAFETIES[0]
    seen = 0
    for a in keys:
        for b in keys:
            if a == b:
                continue
            r = lazy.route(a, b, None, None, None, safety=sf)
            e = eng.route(a, b, None, None, None, safety=sf)
            assert (r is None) == (e is None)
            if r is not None:
                seen += 1
                assert lazy.last["exact"] is False
                assert r.cost >= e.cost and r.nodes and r.cost > 0
    assert seen and any(c is not None for c in calls)
