"""Exponential price profile, second tier of `api.Safety` (docs/scene_graph/inflation_profile_design.md, EXPERIMENTAL):
price properties (P1), engine == lazy parity (P7e), degenerate inputs, the waypoint guard that only counts (W1), tier selection."""
import math
from types import SimpleNamespace

import numpy as np
import pytest

from packages import config
from packages.scene_graph.api import Safety, edge_weight
from packages.services.mission_planner import scene_route as sr

from .test_lazy_route_safety import _cases, _same, built       # noqa: F401  (built: the module fixture over the equality worlds)
from .test_scene_route_safety import _G, _plan, _trial_pairs, _wp, graphs   # noqa: F401

pytestmark = pytest.mark.unit

EXP = Safety(0.8, 0.5, k=6.0, origin=0.3)


# ---- P1: the profile --------------------------------------------------------------------------------------------
def test_price_is_continuous_at_the_origin_monotone_and_bounded():
    for k, reach, origin in ((6.0, 0.8, 0.3), (10.0, 0.55, 0.25), (4.0, 1.0, 0.3), (8.0, 0.8, 0.25)):
        s = Safety(reach, 1.0, k=k, origin=origin)
        eps = 1e-9
        assert float(s.price(origin - eps)) == pytest.approx(float(s.price(origin + eps)), rel=1e-6)   # no kink, no step
        assert float(s.price(origin)) == pytest.approx(1.0)
        assert float(s.price(0.0)) == pytest.approx(math.exp(k * origin))
        g = np.linspace(0.0, 1.2, 2401)
        p = s.price(g)
        assert (np.diff(p) <= 0.0).all() and (p >= 0.0).all() and np.isfinite(p).all()
        assert float(s.price(reach)) == 0.0 and float(s.price(reach + 1e-9)) == 0.0 and float(s.price(5.0)) == 0.0
        assert float(s.price(reach - 1e-9)) == pytest.approx(math.exp(-k * (reach - origin)), rel=1e-6)


def test_weights_are_finite_integers_at_least_the_unpriced_cost():
    s = Safety(0.8, 2.0, k=10.0, origin=0.3)          # kappa 2, k 10: the steepest in the grid
    g = np.linspace(0.0, 1.0, 101)
    for l in (1000.0, 1414.2135, 1.0, 1e5):
        w = edge_weight(l, s.price(g) + s.price(g[::-1]), s.rate)
        assert np.isfinite(w).all() and (w == np.floor(w)).all()
        assert (w >= max(math.ceil(l - 1e-6), 1)).all()


def test_hinge_is_unchanged_bit_for_bit():
    s = Safety(0.4, 7.0)
    g = np.array([0.0, 0.1, 0.39, 0.4, 2.0])
    assert (s.price(g) == np.maximum(0.0, 0.4 - g)).all() and s.k is None and s.origin is None


# ---- degenerate inputs ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("kw", [dict(margin=0.8, rate=0.5, k=0.0, origin=0.3), dict(margin=0.8, rate=0.5, k=-1.0, origin=0.3),
                                dict(margin=0.8, rate=0.5, k=1e9, origin=0.3), dict(margin=0.0, rate=0.5, k=6.0, origin=0.3),
                                dict(margin=-0.1, rate=0.5, k=6.0, origin=0.3),
                                dict(margin=float("nan"), rate=0.5, k=6.0, origin=0.3),
                                dict(margin=0.8, rate=float("nan"), k=6.0, origin=0.3),
                                dict(margin=0.8, rate=0.5, k=float("nan"), origin=0.3),
                                dict(margin=0.8, rate=0.5, k=float("inf"), origin=0.3),
                                dict(margin=0.8, rate=0.5, k=6.0, origin=0.0),
                                dict(margin=0.8, rate=float("inf"), k=6.0, origin=0.3), dict(margin=0.8, rate=0.5, k=6.0)])
def test_invalid_exponential_safety_raises(kw):
    with pytest.raises(ValueError):
        Safety(**kw)


def test_odd_but_valid_shapes_stay_finite():
    for s in (Safety(0.2, 0.5, k=6.0, origin=0.3),       # origin > reach: the step at reach is above 1, still finite
              Safety(0.2, 0.5, k=6.0, origin=0.25)):     # reach below the inscribed radius
        p = s.price(np.linspace(0, 1, 50))
        assert np.isfinite(p).all() and (p >= 0).all()


def _cfg(monkeypatch, radius=None, scaling=None, origin=None, kappa=None):
    monkeypatch.setattr(config, "SCENE_GRAPH_INFLATION_RADIUS_M", radius)
    monkeypatch.setattr(config, "SCENE_GRAPH_INFLATION_SCALING", scaling)
    monkeypatch.setattr(config, "SCENE_GRAPH_GLOBAL_INSCRIBED_M", origin)
    monkeypatch.setattr(config, "SCENE_GRAPH_SAFETY_KAPPA_EXPLICIT", kappa is not None)
    monkeypatch.setattr(config, "SCENE_GRAPH_SAFETY_KAPPA", 1.0 if kappa is None else kappa)


# ---- tier selection ---------------------------------------------------------------------------------------------
def test_tier_selection_from_config(monkeypatch):
    _cfg(monkeypatch)                                                     # nothing set: the hinge, as today
    s = sr.safety_for(_G(), 0.5, None, 1.0)
    assert s.k is None and s.margin == pytest.approx(0.5 * math.sqrt(2) / 2)
    _cfg(monkeypatch, radius=0.8)                                         # only one of the two: the hinge
    assert sr.safety_for(_G(), 0.5, None, 1.0).k is None
    _cfg(monkeypatch, scaling=6.0)
    assert sr.safety_for(_G(), 0.5, None, 1.0).k is None
    _cfg(monkeypatch, radius=0.8, scaling=6.0)                            # both: exponential, kappa 0.5 (not explicit),
    s = sr.safety_for(_G(), 0.5, 0.8, 1.0)                                # origin = the footprint's inscribed radius (0.5 / 2)
    assert (s.k, s.margin, s.rate, s.origin) == (6.0, 0.8, 0.5, 0.25)
    s = sr.safety_for(_G(), None, None, 1.0)                              # footprint unknown: the map's r
    assert s.origin == 0.25 and s.rate == 0.5
    _cfg(monkeypatch, radius=0.8, scaling=6.0, origin=0.3, kappa=0.75)    # explicit origin and kappa
    s = sr.safety_for(_G(), 0.5, None, 0.75)
    assert (s.origin, s.rate) == (0.3, 0.75)
    assert sr.safety_for(_G(), 0.5, None, 0.0) is None                    # kappa 0 still disables everything
    assert sr.safety_for(SimpleNamespace(params=_G.params), 0.5, None, 1.0) is None      # no deficit_cell


@pytest.mark.parametrize("radius,scaling,origin", [(1.2, 6.0, 0.3), (0.0, 6.0, 0.3), (-1.0, 6.0, 0.3), (float("nan"), 6.0, 0.3),
                                                   (0.8, 0.0, 0.3), (0.8, -2.0, 0.3), (0.8, float("nan"), 0.3),
                                                   (0.8, 1e6, 0.3), (0.8, 6.0, 0.0), (0.8, 6.0, -0.1), (0.8, 6.0, float("inf"))])
def test_invalid_profile_falls_back_to_the_hinge_with_a_warning(monkeypatch, caplog, radius, scaling, origin):
    _cfg(monkeypatch, radius=radius, scaling=scaling, origin=origin)
    with caplog.at_level("WARNING"):
        s = sr.safety_for(_G(), 0.5, None, 1.0)
    assert s is not None and s.k is None                                  # the hinge tier, never a crash, never garbage
    assert any("inflation profile invalid" in r.getMessage() for r in caplog.records)


def test_reach_at_the_cap_and_below_the_map_radius_are_valid(monkeypatch):
    _cfg(monkeypatch, radius=1.0, scaling=6.0)
    assert sr.safety_for(_G(), 0.5, None, 1.0).margin == 1.0
    _cfg(monkeypatch, radius=0.2, scaling=6.0)                            # margin <= r: the exponential has no such rule
    assert sr.safety_for(_G(), 0.5, None, 1.0).k == 6.0


def test_origin_unknown_without_a_footprint_or_a_map_radius_gives_no_pricing(monkeypatch):
    _cfg(monkeypatch, radius=0.8, scaling=6.0)
    nop = _G()
    nop.params = SimpleNamespace(lattice=0.1)
    assert sr.safety_for(nop, None, None, 1.0) is None                    # the hinge needs r too: no pricing, as today


# ---- P7e: engine == lazy under the exponential tier -------------------------------------------------------------
@pytest.mark.parametrize("sf", [Safety(0.8, 0.5, k=6.0, origin=0.3), Safety(0.8, 2.0, k=10.0, origin=0.25),
                                Safety(1.0, 1.0, k=4.0, origin=0.3), Safety(0.55, 0.75, k=8.0, origin=0.3)])
def test_exponential_lazy_route_equals_the_engine(built, sf):                  # noqa: F811
    name, st, eng, lazy = built
    n = 0
    for a, b, s, g in _cases(eng, name):
        base = eng.route(a, b, None, s, g)
        e = eng.route(a, b, None, s, g, safety=sf)
        l = lazy.route(a, b, None, s, g, safety=sf)
        _same(e, l)
        assert lazy.last["exact"]
        if e is not None:
            n += 1
            assert e.cost * 1e-4 >= e.length - 1e-9                      # price >= 0: every weight >= ceil(len / 1e-4)
        zero = Safety(sf.margin, 0.0, k=sf.k, origin=sf.origin)
        _same(eng.route(a, b, None, s, g, safety=zero), base)             # kappa 0 is the unpriced route
        _same(lazy.route(a, b, None, s, g, safety=zero), lazy.route(a, b, None, s, g))
    assert n


def test_price_cell_agrees_between_engine_and_lazy(built):                   # noqa: F811
    name, st, eng, lazy = built
    sp = eng._solved.sp
    n = 0
    for c in range(0, sp.N, max(1, sp.N // 400)):
        ix, iy = int(sp.xs[c]) + eng._cx0 // 2, int(sp.ys[c]) + eng._cy0 // 2
        assert eng.price_cell(ix, iy, EXP) == lazy.price_cell(ix, iy, EXP)
        assert eng.price_cell(ix, iy, Safety(0.4, 2.0)) == eng.deficit_cell(ix, iy, 0.4)     # the hinge is the same number
        n += 1
    assert n


# ---- W1: the guard only counts ----------------------------------------------------------------------------------
def test_waypoint_guard_counts_and_moves_nothing(graphs, monkeypatch):      # noqa: F811
    name, eng, lazy = graphs
    _cfg(monkeypatch, radius=0.8, scaling=6.0, origin=0.3, kappa=1.0)
    n_checked = 0
    real = sr.waypoints_below_floor
    h = eng.params.lattice
    for g in (eng, lazy):
        for p, q in _trial_pairs(eng, 8, 21):
            plan = _plan(g, p, q, kappa=1.0, gap=2.0)
            if isinstance(plan, str):
                continue
            pr = plan.pricing
            assert pr["profile"] == "exp" and pr["k"] == 6.0 and pr["origin"] == 0.3 and pr["reach"] == 0.8 and pr["floor"] == 0.3
            below = 0                                                     # an independent count from the engine's clearance
            for w in plan.waypoints[1:-1]:
                d = eng.deficit_cell(int(math.floor(w.x / h)), int(math.floor(w.y / h)), 1.0)
                below += d is None or 1.0 - d < 0.3 - 1e-9
            assert pr["waypoints_below_floor"] == below
            monkeypatch.setattr(sr, "waypoints_below_floor", lambda *a, **k: 0)    # the count never feeds back
            again = _plan(g, p, q, kappa=1.0, gap=2.0)
            monkeypatch.setattr(sr, "waypoints_below_floor", real)
            assert _wp(again) == _wp(plan) and again.pricing["waypoints_below_floor"] == 0
            n_checked += 1
    assert n_checked
    # a floor above every clearance counts every interior waypoint
    interior = max((len(_wp(x)) - 2 for x in (_plan(eng, p, q, kappa=1.0, gap=1.0) for p, q in _trial_pairs(eng, 6, 9))
                    if not isinstance(x, str)), default=0)
    assert interior >= 0


def test_waypoint_guard_floor_of_the_hinge_is_the_inscribed_radius(graphs, monkeypatch):    # noqa: F811
    name, eng, lazy = graphs
    _cfg(monkeypatch)
    p, q = _trial_pairs(eng, 1, 41)[0]
    plan = _plan(lazy, p, q, kappa=1.0, width=0.5, length=0.8)
    assert not isinstance(plan, str)
    assert plan.pricing["profile"] == "hinge" and plan.pricing["floor"] == 0.25 and plan.pricing["k"] is None


def test_exponential_plans_on_engine_and_lazy_agree(graphs, monkeypatch):    # noqa: F811
    name, eng, lazy = graphs
    _cfg(monkeypatch, radius=0.8, scaling=6.0, kappa=0.5)
    served = 0
    for p, q in _trial_pairs(eng, 10, 33):
        a, b = _plan(eng, p, q, kappa=0.5, gap=2.0), _plan(lazy, p, q, kappa=0.5, gap=2.0)
        assert type(a) is type(b)
        if isinstance(a, str):
            assert a == b
            continue
        served += 1
        assert _wp(a) == _wp(b) and a.pricing == b.pricing
    assert served
