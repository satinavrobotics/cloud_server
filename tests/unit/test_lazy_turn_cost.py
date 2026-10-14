"""Turn cost on the lazy planner graph (docs/scene_graph/close_evidence_and_turn_cost_design.md B.5): `PlanGraph.route(...,
safety=Safety(..., turn=lambda))` against the restored engine and the independent reference, on the nine equality worlds (anchor,
point and tile-straddling pairs; free, disc and segment overlays), the tile cap and the cold cache."""
import math
import random

import pytest

from packages.scene_graph.api import Safety
from packages.services.mission_planner.lazy_plan import CorridorTooLarge, PlanGraph, open_plan

from .scene_graph.route_safety_worlds import Reference
from .test_lazy_plan import _build
from .test_lazy_plan_equality import ALL, _extent, _overlays
from .test_lazy_route_safety import _cases, _same

pytestmark = pytest.mark.unit

R_C = 0.25 * math.sqrt(2)
MU = 1.0 / (R_C - 0.25)
TURNS = [Safety(R_C, 0.0, turn=0.5), Safety(R_C, MU, turn=0.5), Safety(0.6, 40.0, turn=1.0)]


@pytest.fixture(scope="module", params=sorted(ALL))
def built(request):
    st, eng, _ = _build(ALL[request.param]())
    return request.param, st, eng, open_plan(st, "m", max_tiles=10 ** 6)


def test_lambda_zero_is_the_lazy_route_of_today(built, monkeypatch):
    name, st, eng, lazy = built
    monkeypatch.setattr(lazy, "_turn", lambda *a, **kw: pytest.fail("turn stage ran at lambda 0"))
    n = 0
    for a, b, s, g in _cases(eng, name, 20):
        _same(lazy.route(a, b, None, s, g), lazy.route(a, b, None, s, g, safety=Safety(0.45, 0.0, turn=0.0)))
        _same(lazy.route(a, b, None, s, g, safety=Safety(R_C, MU)), lazy.route(a, b, None, s, g, safety=Safety(R_C, MU, turn=0.0)))
        n += 1
    assert n


def test_the_turn_lazy_route_equals_the_engine(built):
    name, st, eng, lazy = built
    n = moved = 0
    for a, b, s, g in _cases(eng, name):
        plain = eng.route(a, b, None, s, g, safety=Safety(R_C, MU))
        for sf in TURNS:
            e = eng.route(a, b, None, s, g, safety=sf)
            l = lazy.route(a, b, None, s, g, safety=sf)
            _same(e, l)
            assert lazy.last["exact"] and (l is None or l.exact)
            if e is not None:
                n += 1
                moved += sf.rate > 0 and [tuple(p) for p in e.nodes] != [tuple(p) for p in plain.nodes]
    assert n
    if name in ("grid", "grid+shift", "room_loop", "ring", "pillar_room"):
        assert moved


@pytest.mark.parametrize("ov", ["disc", "segment"])
def test_the_turn_lazy_route_equals_the_engine_under_an_overlay(built, ov):
    name, st, eng, lazy = built
    rnd = random.Random(5 + len(name))
    n = used = 0
    for a, b, s, g in _cases(eng, name, 30):
        free = eng.route(a, b, None, s, g)
        if free is None or len(free.nodes) < 6 or used >= 20:
            continue
        used += 1
        bl = _overlays(free, rnd)[ov]
        for sf in TURNS[:2]:
            e = eng.route(a, b, bl, s, g, safety=sf)
            l = lazy.route(a, b, bl, s, g, safety=sf)
            _same(e, l)
            assert lazy.last["exact"]
            n += e is not None
    assert n


def test_the_turn_lazy_cost_is_the_independent_optimum(built):
    name, st, eng, lazy = built
    ref = Reference(eng)
    n = 0
    for a, b, _, _ in _cases(eng, name, 6)[:6]:                  # anchor pairs (the reference is a python heapq)
        src, dst = ref.endpoints(a, b)
        for sf in TURNS[:2]:
            cost, path = ref.dijkstra_turn(src, dst, sf)
            l = lazy.route(a, b, None, None, None, safety=sf)
            assert (l is None) == (cost is None)
            if l is not None:
                assert l.cost == cost and ref.cells_of(l) == path
                n += 1
    assert n


def test_a_cold_graph_gives_the_warm_answer(built):
    name, st, eng, lazy = built
    cases = _cases(eng, name, 10)
    warm = [lazy.route(a, b, None, s, g, safety=TURNS[1]) for a, b, s, g in cases]
    cold = open_plan(st, "m", max_tiles=10 ** 6)
    for (a, b, s, g), w in zip(cases, warm):
        _same(w, cold.route(a, b, None, s, g, safety=TURNS[1]))


# ---- the cap ------------------------------------------------------------------------------------------------
def test_a_turn_search_over_the_cap_raises_or_returns_a_valid_route(built):
    name, st, eng, _ = built
    small = PlanGraph(st.view("m"), max_tiles=3)
    ref = Reference(eng)
    exact = raised = 0
    for a, b, s, g in _cases(eng, name, 20):
        try:
            l = small.route(a, b, None, s, g, safety=TURNS[1])
        except CorridorTooLarge as exc:
            assert exc.tiles > 3
            raised += 1
            continue
        e = eng.route(a, b, None, s, g, safety=TURNS[1])
        assert (l is None) == (e is None)
        if l is not None:
            assert l.cost >= e.cost and ref.turn_cost(ref.cells_of(l), TURNS[1]) == l.cost     # a valid route, its own J
            if small.last["exact"]:
                assert l.cost == e.cost
                exact += 1
    assert exact + raised


def test_the_exact_turn_stage_over_the_cap_keeps_the_incumbent_and_says_inexact(monkeypatch):
    from .test_lazy_plan import WORLDS
    st, eng, _ = _build(WORLDS["room_loop"]())
    lazy = open_plan(st, "m", max_tiles=10 ** 6)
    ref = Reference(eng)
    real = lazy._search
    stages = []

    def search(*a, **kw):
        t = kw.get("turn")
        stages.append(None if t is None else t[1])
        if t is not None and t[1] == "exact":
            raise CorridorTooLarge(99, 1)
        return real(*a, **kw)
    monkeypatch.setattr(lazy, "_search", search)
    keys, _ = _extent(eng)
    seen = inexact = 0
    for a in keys:
        for b in keys:
            if a == b:
                continue
            r = lazy.route(a, b, None, None, None, safety=TURNS[1])
            e = eng.route(a, b, None, None, None, safety=TURNS[1])
            assert (r is None) == (e is None)
            if r is not None:
                seen += 1
                assert r.cost >= e.cost and ref.turn_cost(ref.cells_of(r), TURNS[1]) == r.cost
                assert lazy.last["exact"] == r.exact
                inexact += not r.exact
    assert seen and inexact and "band" in stages and "exact" in stages


def test_the_band_incumbent_is_never_worse_than_the_plain_route_with_its_turns(built):
    name, st, eng, lazy = built
    ref = Reference(eng)
    for a, b, s, g in _cases(eng, name, 10):
        p0 = lazy.route(a, b, None, s, g, safety=Safety(R_C, MU))
        r = lazy.route(a, b, None, s, g, safety=TURNS[1])
        if r is not None:
            assert r.cost <= ref.turn_cost(ref.cells_of(p0), TURNS[1])
