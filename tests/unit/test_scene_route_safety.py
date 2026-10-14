"""Planner side of the clearance-priced route (docs/scene_graph/route_safety.md): `safety_for`, `priced_visible`,
`plan_on_graph(safety_kappa, width_check)`, the config variable and the server pass-through.

P9 pull (the final polyline stays in F and costs no more than the lattice path; `safety=None` leaves `simplify` alone),
P10 planner (kappa 0 is legacy, kappa > 0 adds no fallback reason, config validation, engine and lazy plan the same priced waypoints)."""
import math
import random
from types import SimpleNamespace

import pytest

from packages import config
from packages.scene_graph.api import BBox, Safety
from packages.services.mission_planner import scene_route as sr

from .test_lazy_plan import WORLDS, _build

pytestmark = pytest.mark.unit

SAMPLE_M = 0.025


# ---- safety_for ---------------------------------------------------------------------------------------------
class _G:
    params = SimpleNamespace(lattice=0.1, robot_half_side=0.25)

    def deficit_cell(self, ix, iy, margin):
        return 0.0


def test_safety_for_follows_the_definitions():
    r = 0.25
    s = sr.safety_for(_G(), 0.5, None, 1.0)                     # square 0.5: circumscribed radius 0.5 / sqrt(2), no tolerance
    assert s.margin == pytest.approx(0.5 * math.sqrt(2) / 2)
    assert s.rate == pytest.approx(1.0 / (s.margin - r))
    s = sr.safety_for(_G(), 0.5, 0.8, 3.0)                      # length known
    assert s.margin == pytest.approx(math.hypot(0.8, 0.5) / 2)
    s = sr.safety_for(_G(), None, None, 1.0)                    # unknown: r * sqrt(2)
    assert s.margin == pytest.approx(r * math.sqrt(2))
    assert s.rate == pytest.approx(1.0 / (r * math.sqrt(2) - r))


def test_safety_for_is_none_for_every_legacy_case():
    assert sr.safety_for(_G(), 0.5, None, 0.0) is None                                # kappa 0 disables
    assert sr.safety_for(_G(), 0.5, None, -1.0) is None
    assert sr.safety_for(SimpleNamespace(params=_G.params), 0.5, None, 1.0) is None   # no deficit_cell
    assert sr.safety_for(_G(), 0.5, None, 1.0) is not None
    assert sr.safety_for(_G(), 0.3, None, 1.0) is None                                # margin 0.212 <= r 0.25
    # S12: a margin within h / sqrt 2 = 0.0707 m of r is below the lattice clearance quantisation: unpriced
    assert sr.safety_for(_G(), 0.0001, 0.5, 1.0) is None                              # degenerate 0.5 x 0.0001: margin = r
    assert sr.safety_for(_G(), 0.2, 0.5, 1.0) is None                                 # 0.5 x 0.2: margin 0.269, 0.019 above r
    edge = 0.25 + 0.1 / math.sqrt(2)                                                  # the boundary: margin - r = h / sqrt 2
    length = lambda m: math.sqrt((2 * m) ** 2 - 0.4 ** 2)                             # length of a footprint 0.4 wide with r_c = m
    assert sr.safety_for(_G(), 0.4, length(edge - 1e-6), 1.0) is None
    assert sr.safety_for(_G(), 0.4, length(edge + 1e-6), 1.0) is not None
    assert sr.safety_for(_G(), 2.0, 1.0, 1.0) is None                                 # r_c 1.118 above the 1 m clearance cap
    nop = _G()
    nop.params = SimpleNamespace(lattice=0.1)
    assert sr.safety_for(nop, 0.5, None, 1.0) is None                                 # no inscribed radius known


# ---- planner worlds -----------------------------------------------------------------------------------------
@pytest.fixture(scope="module", params=["grid", "room_loop", "ring", "pillar_room", "two_rooms"])
def graphs(request):
    st, eng, lazy = _build(WORLDS[request.param]())
    from packages.services.mission_planner.lazy_plan import open_plan
    return request.param, eng, open_plan(st, "m", max_tiles=10 ** 6)


def _anchors(eng):
    return [c.anchor for c in eng.query(BBox(-1e3, -1e3, 1e3, 1e3), raster=False).charts]


def _plan(graph, p, q, overlay=(([], [])), kappa=0.0, width=None, length=None, gap=3.0, check=True):
    ids = iter("n%d" % i for i in range(10 ** 5))
    seen = {}
    try:
        return sr.plan_on_graph(graph, "m", p, q, overlay, lambda x, y: seen.setdefault((round(x, 3), round(y, 3)), next(ids)),
                                0.5, 1.5, gap, width, 0.05, length, None, kappa, check)
    except sr.ChartFallback as err:
        return err.reason


def _trial_pairs(eng, n, seed):
    pts = _anchors(eng)
    rnd = random.Random(seed)
    out = []
    for _ in range(n):
        p, q = rnd.choice(pts), rnd.choice(pts)
        out.append((p, q))
    return out


def _overlays(p, q, rnd):
    mid = ((p[0] + q[0]) / 2, (p[1] + q[1]) / 2)
    return [([], []), ([(mid[0], mid[1], 0.4)], []), ([], [((mid[0] - 0.7, mid[1]), (mid[0] + 0.7, mid[1] + 0.3), 0.15)])]


def _wp(plan):
    return [(w.x, w.y) for w in plan.waypoints]


# ---- P10 ----------------------------------------------------------------------------------------------------
def test_p10_kappa_zero_is_the_legacy_output_and_never_prices(graphs, monkeypatch):
    name, eng, lazy = graphs
    served = 0
    rnd = random.Random(3)
    for g in (eng, lazy):
        for p, q in _trial_pairs(eng, 12, 5):
            for ov in _overlays(p, q, rnd):
                legacy = _plan(g, p, q, ov)                                   # the defaults: kappa 0
                zero = _plan(g, p, q, ov, kappa=0.0)
                assert type(legacy) is type(zero)
                if isinstance(legacy, str):
                    assert legacy == zero
                    continue
                served += 1
                assert _wp(legacy) == _wp(zero) and legacy.charts == zero.charts and legacy.min_width == zero.min_width
                assert [w.node_id for w in legacy.waypoints] == [w.node_id for w in zero.waypoints]
    assert served

    def boom(*a, **k):
        raise AssertionError("priced_visible with kappa 0")
    monkeypatch.setattr(sr, "priced_visible", boom)
    p, q = _trial_pairs(eng, 1, 1)[0]
    _plan(eng, p, q, kappa=0.0)                                               # simplify keeps the plain line of sight


def test_p10_a_graph_without_deficit_cell_is_planned_as_before_whatever_the_kappa(graphs):
    name, eng, lazy = graphs

    class Legacy:
        def __init__(self, g):
            self._g = g
            self.params = g.params
            self.snap = g.snap
            self.route = g.route
            if hasattr(g, "free_cell"):
                self.free_cell = g.free_cell

    for p, q in _trial_pairs(eng, 6, 8):
        a, b = _plan(Legacy(eng), p, q, kappa=1.0), _plan(Legacy(eng), p, q)
        assert type(a) is type(b)
        if not isinstance(a, str):
            assert _wp(a) == _wp(b)


def test_p10_kappa_above_zero_adds_no_fallback_reason_and_serves_what_was_served(graphs):
    name, eng, lazy = graphs
    rnd = random.Random(9)
    n = priced_differs = 0
    for g in (eng, lazy):
        for p, q in _trial_pairs(eng, 14, 17):
            for ov in _overlays(p, q, rnd):
                for width in (None, 0.5):
                    base = _plan(g, p, q, ov, 0.0, width=width)
                    priced = _plan(g, p, q, ov, 1.0, width=width)
                    n += 1
                    if isinstance(base, str):
                        assert isinstance(priced, str) and priced == base, (name, p, q, base, priced)
                    else:
                        assert not isinstance(priced, str), (name, p, q, priced)
                        priced_differs += _wp(priced) != _wp(base)
    assert n
    if name in ("grid", "room_loop", "ring"):
        assert priced_differs > 0


def test_p10_engine_and_lazy_plan_the_same_priced_waypoints(graphs):
    name, eng, lazy = graphs
    assert callable(getattr(lazy, "deficit_cell", None))
    n = 0
    for p, q in _trial_pairs(eng, 14, 23):
        a, b = _plan(eng, p, q, kappa=1.0), _plan(lazy, p, q, kappa=1.0)
        assert type(a) is type(b)
        if isinstance(a, str):
            assert a == b
        else:
            n += 1
            assert _wp(a) == _wp(b) and a.charts == b.charts
    assert n


def test_p10_the_footprint_gives_the_margin_even_when_the_width_check_is_off(graphs):
    name, eng, lazy = graphs
    seen = []
    real = sr.safety_for

    def spy(*a):
        out = real(*a)
        seen.append(out)
        return out
    import pytest as _p
    mp = _p.MonkeyPatch()
    mp.setattr(sr, "safety_for", spy)
    try:
        p, q = _trial_pairs(eng, 1, 3)[0]
        off = _plan(eng, p, q, kappa=1.0, width=0.6, length=0.9, check=False)
        on = _plan(eng, p, q, kappa=1.0, width=0.6, length=0.9, check=True)
    finally:
        mp.undo()
    assert seen and seen[0].margin == pytest.approx(math.hypot(0.9, 0.6) / 2)
    assert type(off) is type(on) or isinstance(on, str)       # the gate only adds the too_narrow comparison


# ---- P9 -----------------------------------------------------------------------------------------------------
def _polyline_cost(g, pts, safety):
    """independent evaluator of section 3.4: every SAMPLE_M along each segment, 1 + rate * deficit of the sample's lattice cell
    (a non-navigable cell: None); also returns whether every sample is in F"""
    h = float(g.params.lattice)
    cost, inside = 0.0, True
    for a, b in zip(pts[:-1], pts[1:]):
        length = math.dist(a, b)
        if length == 0.0:
            continue
        n = max(1, math.ceil(length / SAMPLE_M - 1e-9))
        acc = 0.0
        for k in range(n):
            t = (k + 0.5) / n
            x, y = a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])
            d = g.deficit_cell(int(math.floor(x / h)), int(math.floor(y / h)), safety.margin)
            if d is None:
                inside = False
                d = safety.margin
            acc += 1.0 + safety.rate * d
        cost += length * acc / n
    return cost, inside


def test_p9_the_priced_polyline_stays_in_f_and_costs_no_more_than_the_lattice_path(graphs):
    name, eng, lazy = graphs
    rnd = random.Random(4)
    checked = unpriced_worse = 0
    for g in (eng, lazy):
        sf = sr.safety_for(g, None, None, 1.0)
        for p, q in _trial_pairs(eng, 12, 31):
            for ov in _overlays(p, q, rnd)[:2]:
                plan = _plan(g, p, q, ov, 1.0, gap=0.0)
                base = _plan(g, p, q, ov, 0.0, gap=0.0)
                if isinstance(plan, str):
                    continue
                sa, sb = g.snap(p[0], p[1], 0.5), g.snap(q[0], q[1], 0.5)
                from packages.scene_graph.api import Blocked
                bl = Blocked(discs=list(ov[0]), segments=list(ov[1])) if (ov[0] or ov[1]) else None
                route = g.route(sa.key, sb.key, bl, (sa.x, sa.y), (sb.x, sb.y), safety=sf)
                lattice = [(sa.x, sa.y)] + [tuple(n) for n in route.nodes] + [(sb.x, sb.y)]
                j_lat, in_lat = _polyline_cost(g, lattice, sf)
                j_fin, in_fin = _polyline_cost(g, _wp(plan), sf)
                assert in_lat and in_fin, (name, p, q)              # the final polyline never leaves F
                assert j_fin <= j_lat * (1.0 + 1e-6) + 1e-6, (name, p, q, j_fin, j_lat)
                checked += 1
                if not isinstance(base, str):
                    j_base, _ = _polyline_cost(g, _wp(base), sf)
                    unpriced_worse += j_base > j_lat * (1.0 + 1e-6) + 1e-6
    assert checked
    if name in ("grid", "room_loop", "ring"):
        assert unpriced_worse > 0          # the unpriced pull does cut corners the price forbids: the test has teeth


def test_p9_priced_visible_is_the_plain_line_of_sight_on_a_free_corridor_and_refuses_a_costlier_shortcut():
    class Flat:
        params = SimpleNamespace(lattice=0.1)

        def __init__(self, bad):
            self.bad = bad

        def deficit_cell(self, ix, iy, margin):
            return 0.3 if (ix, iy) in self.bad else 0.0

    sf = Safety(0.5, 10.0)
    # the lattice path goes around a costly cell; the straight shortcut crosses it
    path = [(0.05, 0.05), (0.15, 0.15), (0.25, 0.25), (0.35, 0.15), (0.45, 0.05)]
    always = lambda a, b: True
    free = sr.priced_visible(always, Flat(set()), path, sf)
    assert free(path[0], path[-1])                       # equal cost (all zero deficit) is accepted
    assert free(path[0], path[2]) and free(path[1], path[4])
    # a costly cell under the shortcut but not on the detour
    bad = {(2, 0), (1, 0), (3, 0)}                       # cells along y = 0.05 between the ends
    pri = sr.priced_visible(always, Flat(bad), [(0.05, 0.05), (0.15, 0.25), (0.25, 0.25), (0.35, 0.25), (0.45, 0.05)], sf)
    assert not pri((0.05, 0.05), (0.45, 0.05))
    # the plain line of sight is applied first
    never = sr.priced_visible(lambda a, b: False, Flat(set()), path, sf)
    assert not never(path[0], path[1])
    # a point off the route is judged by the line of sight alone
    assert free((9.0, 9.0), (9.5, 9.0))


# ---- config -------------------------------------------------------------------------------------------------
def test_p10_config_defaults_and_range_validation(monkeypatch, caplog):
    assert config.SCENE_GRAPH_SAFETY_KAPPA == 1.0 and not hasattr(config, "SCENE_GRAPH_SAFETY_TOLERANCE_M")
    for v in (0.0, 1.0, 10.0):
        monkeypatch.setattr(config, "SCENE_GRAPH_SAFETY_KAPPA", v)
        config._validate_scene_graph_planner()
        assert config.SCENE_GRAPH_SAFETY_KAPPA == v
    for v in (-0.1, 10.5, float("nan"), float("inf")):
        monkeypatch.setattr(config, "SCENE_GRAPH_SAFETY_KAPPA", v)
        caplog.clear()
        with caplog.at_level("WARNING", logger="config"):
            config._validate_scene_graph_planner()
        assert config.SCENE_GRAPH_SAFETY_KAPPA == 1.0
        assert len([r for r in caplog.records if r.name == "config" and "SCENE_GRAPH_SAFETY_KAPPA" in r.getMessage()]) == 1


def test_p10_config_reads_the_environment():
    from .test_config import _reload_config
    cfg = _reload_config({"SCENE_GRAPH_SAFETY_KAPPA": "7.5"})
    assert cfg.SCENE_GRAPH_SAFETY_KAPPA == 7.5
    cfg = _reload_config({"SCENE_GRAPH_SAFETY_KAPPA": "99"})
    assert cfg.SCENE_GRAPH_SAFETY_KAPPA == 1.0                             # out of range: the default
    cfg = _reload_config({"SCENE_GRAPH_SAFETY_KAPPA": "0"})
    assert cfg.SCENE_GRAPH_SAFETY_KAPPA == 0.0                             # 0 disables, kept


async def test_the_planner_service_passes_kappa_and_the_footprint_even_with_the_width_check_off(monkeypatch):
    from .test_planner_chart_route import FakeGraph, FakeStore, _plan as svc_plan, _service
    from packages.services.mission_planner.server import MissionPlannerService
    monkeypatch.setattr(config, "SCENE_GRAPH_PLANNER_ENABLED", True)
    monkeypatch.setattr(config, "SCENE_GRAPH_WIDTH_CHECK_ENABLED", False)
    monkeypatch.setattr(config, "SCENE_GRAPH_SAFETY_KAPPA", 6.0)
    monkeypatch.setattr(MissionPlannerService, "_robot_width", staticmethod(lambda robot: 0.5))
    monkeypatch.setattr(MissionPlannerService, "_robot_length", staticmethod(lambda robot: 0.8))
    seen = {}

    def fake(*args, **kw):
        seen["args"] = args
        raise sr.ChartFallback("unreachable")
    monkeypatch.setattr(sr, "plan_on_graph", fake)
    svc = _service(FakeStore(FakeGraph()))
    await svc_plan(svc)
    width, _margin, length, _policy, kappa, check = seen["args"][-7:-1]
    assert (width, length, kappa, check) == (0.5, 0.8, 6.0, False)         # the gate stays off, the footprint arrives
    svc.cleanup()


def test_edge_weight_is_the_one_cost_formula():
    import numpy as np
    from packages.scene_graph.api import edge_weight
    assert edge_weight(1000.0, 0.0, 0.0) == 1000.0 and edge_weight(0.0, 0.0, 5.0) == 1.0
    assert edge_weight(1414.2135, 0.0, 3.0) == 1415.0
    got = edge_weight(np.array([1000.0, 1000.0]), np.array([0.2, 0.0]), 10.0)
    assert list(got) == [2000.0, 1000.0]


def test_the_plan_reports_its_pricing(graphs):
    name, eng, lazy = graphs
    from packages.services.mission_planner import lazy_plan
    for g in (eng, lazy):
        p, q = _trial_pairs(eng, 1, 41)[0]
        plain = _plan(g, p, q, kappa=0.0)
        assert not isinstance(plain, str) and plain.pricing is None
        known = _plan(g, p, q, kappa=2.0, width=0.5, length=0.8)
        unknown = _plan(g, p, q, kappa=2.0)
        for plan_, source, margin in ((known, "factsheet", math.hypot(0.8, 0.5) / 2), (unknown, "map_r", 0.25 * math.sqrt(2))):
            assert not isinstance(plan_, str)
            pr = plan_.pricing
            assert set(pr) == {"kappa", "margin", "rate", "margin_source", "cost", "exact", "profile", "k", "origin", "reach", "floor",
                               "waypoints_below_floor"}
            assert pr["profile"] == "hinge" and pr["k"] is None and pr["origin"] is None and pr["reach"] == pr["margin"]
            assert pr["kappa"] == 2.0 and pr["margin_source"] == source and pr["exact"] is True
            assert pr["margin"] == pytest.approx(margin) and pr["rate"] == pytest.approx(2.0 / (margin - 0.25))
            assert isinstance(pr["cost"], int) and pr["cost"] >= 0
    assert lazy_plan._CLR_CAP_M == 1.0


def _stats_response_fields():
    """The fields `StatsResponse` (packages/services/mission_planner/main.py) declares, read from the source: importing that
    module builds the service and parses the command line."""
    import ast
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                        "packages", "services", "mission_planner", "main.py")
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "StatsResponse":
            return {item.target.id for item in node.body if isinstance(item, ast.AnnAssign)}
    raise AssertionError("StatsResponse not found")


async def test_the_service_reports_priced_plans_in_the_response_the_log_and_the_stats(monkeypatch, caplog):
    from .test_planner_chart_route import FakeGraph, FakeStore, _plan as svc_plan, _service
    from types import SimpleNamespace as NS
    from packages.scene_graph.api import Route

    class Priced(FakeGraph):
        params = NS(lattice=0.1, robot_half_side=0.25)
        last = {"tiles": 7}

        def deficit_cell(self, ix, iy, margin):
            return 0.0

        def route(self, a, b, blocked=None, start=None, goal=None, safety=None):
            r = super().route(a, b, blocked, start, goal)
            return Route(charts=r.charts, length=r.length, min_width=r.min_width, nodes=r.nodes, cost=12345, exact=False)

    monkeypatch.setattr(config, "SCENE_GRAPH_PLANNER_ENABLED", True)
    monkeypatch.setattr(config, "SCENE_GRAPH_SAFETY_KAPPA", 1.0)
    svc = _service(FakeStore(Priced(), version=9))
    with caplog.at_level("INFO"):
        out = await svc_plan(svc)
    assert out["success"] and out["planner"] == sr.PLANNER_SCENE_GRAPH
    pr = out["pricing"]
    assert pr["kappa"] == 1.0 and pr["margin_source"] == "map_r" and pr["cost"] == 12345 and pr["exact"] is False
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Priced plan")]
    assert len(lines) == 1
    for part in ("map=m", "graph_version=9", "kappa=1.0", "margin_source=map_r", "cost=12345", "exact=False", "tiles=7"):
        assert part in lines[0]
    assert svc.get_stats()["build_sha"] == config.BUILD_SHA     # the build identity deploy.sh verifies after a deploy
    # ... and it must survive the HTTP response model: `GET /stats` returns StatsResponse(**get_stats()), which drops any
    # key the model does not declare (the first deploy of build_sha failed its own check on exactly that)
    assert set(svc.get_stats()) <= _stats_response_fields()
    stats = svc.get_stats()["scene_graph"]
    assert stats["priced_plans"] == 1 and stats["priced_plans_inexact"] == 1
    monkeypatch.setattr(config, "SCENE_GRAPH_SAFETY_KAPPA", 0.0)
    out0 = await svc_plan(svc)
    assert "pricing" not in out0 and svc.get_stats()["scene_graph"]["priced_plans"] == 1
    svc.cleanup()


def test_build_identity_defaults_to_unknown_and_the_deploy_scripts_parse():
    """BUILD_SHA is the commit compose got from scripts/deploy.sh (GIT_SHA); both scripts must stay valid bash."""
    import os
    import subprocess
    assert config.BUILD_SHA == os.environ.get("BUILD_SHA", "unknown").strip() or config.BUILD_SHA == "unknown"
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for script in ("scripts/deploy.sh", "scripts/run_unit_tests_pinned.sh"):
        assert subprocess.run(["bash", "-n", os.path.join(root, script)], capture_output=True).returncode == 0, script
