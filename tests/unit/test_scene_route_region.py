"""The region cost term in the planner's other pricing places (docs/planner_region_stats_2026-10-27.md 5.3, stage S3): `chord_cost` /
`priced_visible` (string pull), `plan_on_graph(..., region=, region_mode=)`, the nodePolicy legs and the server's snapshot cache.
The search itself is tested in test_lazy_region_cost.py."""
import math
import random

import pytest

from packages.scene_graph.api import Safety
from packages.services.mission_planner import node_policy_plan as npp
from packages.services.mission_planner import scene_route as sr
from packages.services.mission_planner.lazy_plan import open_plan

from .test_lazy_plan import WORLDS, _build
from .test_lazy_region_cost import pricing, random_rho
from .test_scene_route_safety import _trial_pairs, _wp

pytestmark = pytest.mark.unit

R_C = 0.25 * math.sqrt(2)


@pytest.fixture(scope="module", params=["grid", "room_loop", "ring", "pillar_room"])
def graphs(request):
    st, eng, _ = _build(WORLDS[request.param]())
    return request.param, eng, open_plan(st, "m", max_tiles=10 ** 6)


def _plan(graph, p, q, region=None, mode="off", kappa=0.0, trajectory=0, turn=None, overlay=([], [])):
    ids = iter("n%d" % i for i in range(10 ** 5))
    seen = {}
    try:
        return sr.plan_on_graph(graph, "m", p, q, overlay, lambda x, y: seen.setdefault((round(x, 3), round(y, 3)), next(ids)),
                                0.5, 1.5, 3.0, None, 0.05, None, None, kappa, True, trajectory, turn=turn,
                                region=region, region_mode=mode)
    except sr.ChartFallback as err:
        return err.reason


# ---- chord_cost ----------------------------------------------------------------------------------------------
def test_chord_cost_is_the_length_without_a_price(graphs):
    name, eng, lazy = graphs
    reg = pricing(lazy, [0] * len(lazy.keys))
    for cost in (sr.chord_cost(lazy, None), sr.chord_cost(lazy, Safety(0.45, 0.0), reg), sr.chord_cost(lazy, None, None)):
        assert cost is math.dist


def test_chord_cost_adds_the_rho_of_the_chart_per_sample(graphs):
    """On a piece inside owned cells the factor is 1 + rho / scale (one rho everywhere), plus the clearance term when priced."""
    name, eng, lazy = graphs
    reg = pricing(lazy, [500] * len(lazy.keys))
    route = lazy.route(lazy.keys[0], lazy.keys[-1])
    pts = [tuple(p) for p in route.nodes]
    plain = sr.chord_cost(lazy, None, reg)
    sf = Safety(R_C, 1.0 / (R_C - 0.25))
    priced = sr.chord_cost(lazy, sf)
    both = sr.chord_cost(lazy, sf, reg)
    n = 0
    for a, b in zip(pts[:-1], pts[1:]):
        assert plain(a, b) == pytest.approx(math.dist(a, b) * 1.5)           # the lattice cells are owned
        assert both(a, b) == pytest.approx(priced(a, b) + math.dist(a, b) * 0.5)
        n += 1
    assert n
    assert sr.chord_excess(lazy, reg)(pts[0], pts[1]) == pytest.approx(math.dist(pts[0], pts[1]) * 0.5)
    assert sr.chord_excess(lazy, None)(pts[0], pts[1]) == 0.0


def test_chord_cost_of_the_lattice_path_matches_the_search_cost(graphs):
    """The sampled cost of the route's own steps is the search's integer cost (to the sampling of a cell pair)."""
    name, eng, lazy = graphs
    rho = random_rho(lazy, 3, zero_share=0.0)
    reg = pricing(lazy, rho)
    cost = sr.chord_cost(lazy, None, reg)
    n = 0
    for a, b, _, _ in [(x, y, None, None) for x in lazy.keys[:4] for y in lazy.keys[-3:]]:
        route = lazy.route(a, b, None, None, None, region=reg)
        if route is None or len(route.nodes) < 3:
            continue
        pts = [tuple(p) for p in route.nodes]
        sampled = sum(cost(p, q) for p, q in zip(pts[:-1], pts[1:])) / lazy_unit()
        assert sampled == pytest.approx(route.cost, rel=0.15)
        n += 1
    assert n


def lazy_unit():
    from packages.scene_graph.weights import ROUTE_UNIT_M
    return ROUTE_UNIT_M


# ---- plan_on_graph ---------------------------------------------------------------------------------------------
def test_mode_off_and_no_snapshot_are_todays_plan(graphs):
    name, eng, lazy = graphs
    reg = pricing(lazy, random_rho(lazy, 7))
    n = 0
    for p, q in _trial_pairs(eng, 8, 4):
        base = _plan(lazy, p, q)
        for region, mode in ((reg, "off"), (None, "on"), (None, "shadow"), (reg, "bogus")):
            got = _plan(lazy, p, q, region, mode)
            assert type(got) is type(base)
            if not isinstance(base, str):
                assert _wp(got) == _wp(base) and got.pricing == base.pricing and getattr(got, "cost_model", None) is None
                n += 1
    assert n


def test_off_does_not_touch_the_snapshot(graphs):
    name, eng, lazy = graphs

    class Tripwire:
        def __getattr__(self, name):
            raise AssertionError("the snapshot was read: %s" % name)
    for p, q in _trial_pairs(eng, 3, 4):
        _plan(lazy, p, q, Tripwire(), "off")


def test_mode_on_serves_the_priced_route(graphs):
    """Mode on routes by the region term: the lattice route is the search's priced optimum, and it is cheaper under the model than
    today's route whenever the two differ."""
    name, eng, lazy = graphs
    rho = random_rho(lazy, 9, lo=500, hi=3000, zero_share=0.3)
    reg = pricing(lazy, rho)
    cost = sr.chord_cost(lazy, None, reg)
    differ = n = 0
    for p, q in _trial_pairs(eng, 14, 8):
        base, on = _plan(lazy, p, q, trajectory=500), _plan(lazy, p, q, reg, "on", trajectory=500)
        assert type(on) is type(base)
        if isinstance(base, str):
            assert on == base
            continue
        if not base.trajectory or not on.trajectory:          # start and goal in one cell: no path to compare
            continue
        n += 1
        a = [tuple(pt) for leg in base.trajectory for pt in leg["points"]]
        b = [tuple(pt) for leg in on.trajectory for pt in leg["points"]]
        if _wp(base) != _wp(on):
            differ += 1
        priced = lambda pts: sum(cost(x, y) for x, y in zip(pts[:-1], pts[1:]))        # noqa: E731
        assert priced(b) <= priced(a) * (1 + 1e-6) + 1e-9
    assert n


def test_the_pull_never_costs_more_than_the_lattice_path_under_the_region_term(graphs):
    """`priced_visible` with a region: every shortcut the string pull takes costs no more than the lattice sub-path it replaces, so the
    final polyline costs no more than the lattice path (a pull that ignored rho could cut across a slow chart)."""
    name, eng, lazy = graphs
    rho = random_rho(lazy, 13, lo=0, hi=3000, zero_share=0.4)
    reg = pricing(lazy, rho)
    cost = sr.chord_cost(lazy, None, reg)
    n = 0
    for p, q in _trial_pairs(eng, 14, 6):
        plan = _plan(lazy, p, q, reg, "on", trajectory=2000)
        if isinstance(plan, str) or not plan.trajectory:
            continue
        lattice = [tuple(pt) for leg in plan.trajectory for pt in leg["points"]]
        pulled = _wp(plan)
        assert sum(cost(a, b) for a, b in zip(pulled[:-1], pulled[1:])) <= \
            sum(cost(a, b) for a, b in zip(lattice[:-1], lattice[1:])) * (1 + 1e-6) + 1e-6
        n += 1
    assert n


def test_mode_on_with_a_snapshot_of_another_version_is_neutral(graphs):
    name, eng, lazy = graphs
    other = pricing(lazy, random_rho(lazy, 9), version=lazy.version + 7)
    for p, q in _trial_pairs(eng, 6, 4):
        base = _plan(lazy, p, q)
        got = _plan(lazy, p, q, other, "on")
        assert type(got) is type(base)
        if not isinstance(base, str):
            assert _wp(got) == _wp(base)


def test_the_engine_graph_takes_no_region_term(graphs):
    """`ChartGraph.route` has no region term (the equality tests run without): a graph without `region_arg` plans as today."""
    name, eng, lazy = graphs
    reg = pricing(lazy, random_rho(lazy, 9))
    for p, q in _trial_pairs(eng, 4, 4):
        base, on = _plan(eng, p, q), _plan(eng, p, q, reg, "on")
        assert type(base) is type(on)
        if not isinstance(base, str):
            assert _wp(base) == _wp(on)


# ---- nodePolicy legs ------------------------------------------------------------------------------------------
def test_node_policy_legs_use_the_same_term(graphs):
    """`_route_leg` / insert_planned route the leg with the region term: the leg's cost is the priced optimum."""
    name, eng, lazy = graphs
    rho = random_rho(lazy, 17, lo=500, hi=3000, zero_share=0.2)
    reg = pricing(lazy, rho)
    n = changed = 0
    for p, q in _trial_pairs(eng, 12, 2):
        sa, sb, base, _ = npp._route_leg(lazy, p, q, 0.5)
        _, _, got, _ = npp._route_leg(lazy, p, q, 0.5, region=reg)
        _, _, want, _ = (sa, sb, lazy.route(sa.key, sb.key, None, start=(sa.x, sa.y), goal=(sb.x, sb.y), region=reg), None)
        assert (got is None) == (base is None)
        if got is None:
            continue
        assert got.cost == want.cost and [tuple(x) for x in got.nodes] == [tuple(x) for x in want.nodes]
        changed += got.cost != base.cost
        n += 1
    assert n and changed
    # a leg without a region is unchanged, and a neutral snapshot too
    sa, sb, base, _ = npp._route_leg(lazy, *_trial_pairs(eng, 1, 2)[0], 0.5)
    zero = pricing(lazy, [0] * len(lazy.keys))
    assert npp._route_leg(lazy, *_trial_pairs(eng, 1, 2)[0], 0.5, region=zero)[2].cost == base.cost


def test_insert_planned_and_the_cut_see_the_region_term(graphs):
    name, eng, lazy = graphs
    from cloud_common.objects import common
    rho = random_rho(lazy, 19, lo=500, hi=3000, zero_share=0.2)
    reg = pricing(lazy, rho)
    visible = sr.blocked_free_los(lazy, ([], []))
    for p, q in _trial_pairs(eng, 8, 12):
        poses = [common.Pose2D(x=p[0], y=p[1], theta=0.0, map_id="m"), common.Pose2D(x=q[0], y=q[1], theta=0.0, map_id="m")]
        out, _ = npp.insert_planned(lazy, poses, visible, 3.0, 0.5, sr.simplify, None, None, None, reg)
        assert out[0] is poses[0] and out[-1] is poses[1]
        none, _ = npp.insert_planned(lazy, poses, visible, 3.0, 0.5, sr.simplify)
        zero, _ = npp.insert_planned(lazy, poses, visible, 3.0, 0.5, sr.simplify, None, None, None,
                                     pricing(lazy, [0] * len(lazy.keys)))
        assert [(w.x, w.y) for w in zero] == [(w.x, w.y) for w in none]
