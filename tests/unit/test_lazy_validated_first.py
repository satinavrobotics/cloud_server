"""Validated-first on the lazy planner graph (docs/scene_graph/close_evidence_and_turn_cost_design.md part G): `PlanGraph.route(...,
safety=Safety(validated_first=True))` equals `ChartGraph.route` on the nine equality worlds, with and without an overlay, at tile edges, with
a price and with a turn cost; the independent tuple-distance reference agrees; and ignoring the `val` plane in the lazy reader breaks it."""
import random

import pytest

from packages.scene_graph.api import Safety, SceneParams
from packages.scene_graph.engine import ChartGraph
from packages.scene_graph.store import SceneGraphStore
from packages.scene_graph.store_backends import MemoryBackend
from packages.services.mission_planner import lazy_plan
from packages.services.mission_planner.lazy_plan import PlanGraph

from .scene_graph import chart_worlds as cw
from .scene_graph.route_safety_worlds import Reference
from .test_lazy_plan_equality import ALL, _overlays
from .test_lazy_route_safety import R_C, _cases

pytestmark = pytest.mark.unit

NEAR = 0.4
VF = Safety(0.45, 0.0, validated_first=True)
VF_PRICED = Safety(R_C, 1.0 / (R_C - 0.25), validated_first=True)
VF_TURN = Safety(0.45, 0.0, turn=0.4, validated_first=True)
VF_BOTH = Safety(0.6, 40.0, turn=0.4, validated_first=True)
ALL_SAFETIES = [VF, VF_PRICED, VF_TURN, VF_BOTH]


def _build_rule(world_way, near=NEAR):
    w, way = world_way
    leaves = cw.drive(w, way)
    be = MemoryBackend()
    st = SceneGraphStore(be, be)
    g = ChartGraph(SceneParams(near_range_m=near))
    v = None
    for i in range(0, len(leaves), 9):
        v = st.save("m", g, g.fold_many(leaves[i:i + 9]), v, {"n": 1})
    return st, st.load("m"), g


@pytest.fixture(scope="module", params=sorted(ALL))
def built(request):
    st, eng, _ = _build_rule(ALL[request.param]())
    return request.param, st, eng, PlanGraph(st.view("m"), max_tiles=10 ** 6)


def _same(e, l):
    assert (e is None) == (l is None)
    if e is not None:
        assert l.cost == e.cost
        assert l.charts == e.charts
        assert [tuple(p) for p in l.nodes] == [tuple(p) for p in e.nodes]
        assert l.unvalidated == e.unvalidated
        assert l.unvalidated_m == pytest.approx(e.unvalidated_m, abs=1e-9)
        assert l.length == pytest.approx(e.length, abs=1e-9)


def _compare(name, eng, lazy, n=22):
    seen = changed = 0
    for a, b, s, g in _cases(eng, name, n):
        base = eng.route(a, b, None, s, g)
        for sf in ALL_SAFETIES:
            e = eng.route(a, b, None, s, g, safety=sf)
            l = lazy.route(a, b, None, s, g, safety=sf)
            _same(e, l)
            assert lazy.last["exact"]
            if e is not None:
                seen += 1
                assert e.unvalidated is not None
                changed += [tuple(p) for p in e.nodes] != [tuple(p) for p in base.nodes]
    return seen, changed


def test_the_lazy_validated_first_route_equals_the_engine_on_the_nine_worlds(built):
    name, st, eng, lazy = built
    seen, changed = _compare(name, eng, lazy)
    assert seen
    if name in ("grid", "grid+shift"):
        assert changed > 0                                   # the flag really moves routes where the drive leaves space unvalidated


@pytest.mark.parametrize("ov", ["disc", "segment"])
def test_the_lazy_validated_first_route_equals_the_engine_under_an_overlay(built, ov):
    name, st, eng, lazy = built
    rnd = random.Random(9 + len(name))
    n = used = 0
    for a, b, s, g in _cases(eng, name, 30):
        free = eng.route(a, b, None, s, g)
        if free is None or len(free.nodes) < 6 or used >= 14:
            continue
        used += 1
        bl = _overlays(free, rnd)[ov]
        for sf in (VF, VF_PRICED, VF_TURN):
            e = eng.route(a, b, bl, s, g, safety=sf)
            l = lazy.route(a, b, bl, s, g, safety=sf)
            _same(e, l)
            assert lazy.last["exact"]
            n += e is not None
    assert n


def test_the_lazy_validated_first_cost_and_count_are_the_independent_lexicographic_optimum(built):
    name, st, eng, lazy = built
    ref = Reference(eng)
    n = 0
    for a, b, _, _ in _cases(eng, name, 10):
        src, dst = ref.endpoints(a, b)
        for sf in (VF, VF_PRICED):
            score, path = ref.lex_dijkstra(src, dst, sf if sf.rate > 0 else None)
            l = lazy.route(a, b, None, None, None, safety=sf)
            assert (l is None) == (score is None)
            if l is not None:
                assert (l.unvalidated, l.cost) == score and ref.cells_of(l) == path
                n += 1
    assert n


def test_the_validated_first_route_never_has_more_unvalidated_edges_than_the_plain_one_and_exists_whenever_it_does(built):
    name, st, eng, lazy = built
    ref = Reference(eng)
    for a, b, s, g in _cases(eng, name, 16):
        plain = lazy.route(a, b, None, s, g)
        vf = lazy.route(a, b, None, s, g, safety=VF)
        assert (plain is None) == (vf is None)
        if plain is not None:
            assert vf.unvalidated <= ref.unvalidated_of(ref.cells_of(plain))[0]


def test_a_store_without_the_rule_ignores_the_flag():
    st, _, _ = _build_rule(ALL["grid"](), near=None)
    lazy = PlanGraph(st.view("m"), max_tiles=10 ** 6)
    eng = st.load("m")
    for a, b, s, g in _cases(eng, "grid", 8):
        r = lazy.route(a, b, None, s, g, safety=VF)
        _same(eng.route(a, b, None, s, g), r)
        assert r is None or r.unvalidated is None


# ---- mutation check: the suite must notice a lazy reader that ignores `val` -------------------------------------
def test_g_ignoring_val_in_the_lazy_reader_is_detected(monkeypatch):
    st, eng, _ = _build_rule(ALL["grid"]())
    ok = PlanGraph(st.view("m"), max_tiles=10 ** 6)
    _compare("grid", eng, ok, 8)                                         # the genuine reader passes the comparison ...
    real = lazy_plan.read_planes

    def drop_val(t):
        out = real(t)
        out.pop("val", None)
        return out
    monkeypatch.setattr(lazy_plan, "read_planes", drop_val)
    mutant = PlanGraph(st.view("m"), max_tiles=10 ** 6)                  # ... one that drops the plane does not
    with pytest.raises(AssertionError):
        _compare("grid", eng, mutant, 22)
    # and the direct reading differs too (every node of the mutant reads validated)
    tiles = sorted(tuple(int(v) for v in n.split(",")) for n in st.manifest("m")["tiles"])
    assert mutant._cells(tiles)[4].all() and not ok._cells(tiles)[4].all()


# ---- the detour world and the tile cap ---------------------------------------------------------------------------
def _detour():
    from .scene_graph.test_validated_first import detour_world
    return detour_world()


def test_the_lazy_planner_takes_the_validated_detour():
    st, eng, _ = _build_rule(_detour(), near=0.5)
    lazy = PlanGraph(st.view("m"), max_tiles=10 ** 6)
    a, b = eng.snap(2.0, 4.0), eng.snap(10.0, 4.0)
    args = (a.key, b.key, None, (a.x, a.y), (b.x, b.y))
    plain, vf = lazy.route(*args), lazy.route(*args, safety=VF)
    _same(eng.route(*args, safety=VF), vf)
    assert plain.length < 8.5 < 12.0 < vf.length and vf.unvalidated == 0 and min(p[1] for p in vf.nodes) < 1.8


def test_over_the_tile_cap_the_corridor_route_stands_and_is_flagged_inexact():
    st, eng, _ = _build_rule(ALL["grid"]())
    full = PlanGraph(st.view("m"), max_tiles=10 ** 6)
    capped = PlanGraph(st.view("m"), max_tiles=7)
    inexact = 0
    for a, b, s_, g in _cases(eng, "grid", 30):
        try:
            r = capped.route(a, b, None, s_, g, safety=VF)
        except lazy_plan.CorridorTooLarge:
            continue                                          # the corridor itself is over the cap: the planner's legacy fallback
        f = full.route(a, b, None, s_, g, safety=VF)
        assert (r is None) == (f is None)
        if r is not None and not capped.last["exact"]:
            inexact += 1
            assert r.unvalidated >= f.unvalidated             # valid, never better than the optimum, and said so
    assert inexact


def test_a_rule_store_of_the_earlier_token_is_read_without_validity_not_wrongly(monkeypatch):
    """a +near4 store (not yet rebuilt) has no `val`: the lazy reader reads every node as validated, so the flag changes nothing"""
    from packages.scene_graph import store as S
    w, way = ALL["grid"]()
    leaves = cw.drive(w, way)
    be = MemoryBackend()
    st = SceneGraphStore(be, be)
    g = ChartGraph(SceneParams(near_range_m=NEAR))
    with monkeypatch.context() as m:
        m.setattr(S, "PIPELINE_NEAR_VAL", "morse/2+near4")
        v = None
        for i in range(0, len(leaves), 9):
            v = st.save("m", g, g.fold_many(leaves[i:i + 9]), v, {"n": 1})
    assert st.manifest("m")["pipeline"] == "morse/2+near4"
    lazy = PlanGraph(st.view("m"), max_tiles=10 ** 6)
    assert not lazy._has_val
    plain = PlanGraph(st.view("m"), max_tiles=10 ** 6)
    for a, b, s_, g_ in _cases(g, "grid", 8):
        r = lazy.route(a, b, None, s_, g_, safety=VF)
        base = plain.route(a, b, None, s_, g_)
        assert (r is None) == (base is None)
        if r is not None:
            assert [tuple(p) for p in r.nodes] == [tuple(p) for p in base.nodes] and r.unvalidated is None
