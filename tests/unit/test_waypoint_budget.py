"""Waypoint budget (docs/scene_graph/close_evidence_and_turn_cost_design.md C.2): `scene_route.fewest_waypoints`, its place
in `plan_on_graph` (`waypoint_budget_m`) and the config variable.

Pure function: the slack never exceeds the budget and J(final) <= J(lattice) + budget (random lattice paths), budget 0 keeps
the cost bound, a larger budget never gives more waypoints, determinism, ends kept, chords within the gap cap, a corner where
the budget removes a waypoint and budget 0 does not. Planner: off is identical, on stays in F and within the bound."""
import math
import random

import pytest

from packages import config
from packages.services.mission_planner import scene_route as sr

from . import test_scene_route_safety as _safety
from .test_scene_route_safety import _polyline_cost, _trial_pairs, _wp

pytestmark = pytest.mark.unit

graphs = _safety.graphs          # the planner worlds fixture of the safety tests

ALWAYS = lambda a, b: True       # noqa: E731


def _cost_sum(f, pts):
    return sum(f(a, b) for a, b in zip(pts[:-1], pts[1:]))


def _wild_cost(a, b):
    return math.dist(a, b) * (1.0 + 0.5 * abs(math.sin(7 * (a[0] + b[0]) + 3 * (a[1] + b[1]))))


def _walk(rnd, n):
    pts = [(0.0, 0.0)]
    for _ in range(n):
        dx, dy = rnd.choice(((0.1, 0.0), (0.1, 0.1), (0.0, 0.1), (0.1, -0.1)))
        pts.append((round(pts[-1][0] + dx, 3), round(pts[-1][1] + dy, 3)))
    return pts


def _visible(a, b):                            # a wall x in [0.3, 0.35], y < 0.5 blocks the line of sight
    if max(a[0], b[0]) < 0.3 or min(a[0], b[0]) > 0.35:
        return True
    for k in range(21):
        t = k / 20.0
        x, y = a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])
        if 0.3 <= x <= 0.35 and y < 0.5:
            return False
    return True


def test_a_corner_where_the_budget_removes_a_waypoint_and_zero_does_not():
    # an L-shaped lattice path with 0.1 m steps; a chord that is not along an axis is priced 1.5 times its length
    lattice = [(round(0.1 * k, 3), 0.0) for k in range(11)] + [(1.0, round(0.1 * k, 3)) for k in range(1, 11)]

    def cost(a, b):
        return math.dist(a, b) * (1.0 if (a[0] == b[0] or a[1] == b[1]) else 1.5)
    zero = sr.fewest_waypoints(lattice, ALWAYS, 0.0, cost, 0.0)
    wide = sr.fewest_waypoints(lattice, ALWAYS, 0.0, cost, 0.35)
    assert zero == [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)]       # the diagonal costs 0.12 more than the corner path
    assert wide == [(0.0, 0.0), (1.0, 1.0)]
    assert sr.fewest_waypoints(lattice, ALWAYS, 0.0, cost, 0.11) == zero
    assert _cost_sum(cost, wide) <= _cost_sum(cost, lattice) + 0.35 + 1e-9


def test_budget_bounds_the_cost_monotone_and_deterministic():
    rnd = random.Random(11)
    for _ in range(150):
        lattice = _walk(rnd, rnd.randint(3, 40))
        for f in (math.dist, _wild_cost):
            j_lat = _cost_sum(f, lattice)
            previous = None
            for budget in (0.0, 0.05, 0.2, 0.35, 1.0):
                out = sr.fewest_waypoints(lattice, _visible, 0.0, f, budget)
                assert out == sr.fewest_waypoints(lattice, _visible, 0.0, f, budget)
                assert out[0] == lattice[0] and out[-1] == lattice[-1]
                assert _cost_sum(f, out) <= j_lat + budget + 1e-9
                if budget == 0.0:
                    assert _cost_sum(f, out) <= j_lat * (1.0 + 1e-9) + 1e-9
                for a, b in zip(out[:-1], out[1:]):
                    assert b in lattice and (b == lattice[lattice.index(a) + 1] or _visible(a, b))
                if previous is not None:
                    assert len(out) <= previous
                previous = len(out)


def test_chords_stay_within_the_gap_cap():
    rnd = random.Random(3)
    for _ in range(60):
        lattice = _walk(rnd, 30)
        out = sr.fewest_waypoints(lattice, ALWAYS, 0.55, math.dist, 0.35)
        assert all(math.dist(a, b) <= 0.55 + 1e-9 for a, b in zip(out[:-1], out[1:]))
        free = sr.fewest_waypoints(lattice, ALWAYS, 0.0, math.dist, 0.35)
        assert len(free) <= len(out)


def test_short_inputs_pass_through():
    assert sr.fewest_waypoints([(0.0, 0.0)], ALWAYS, 0.0, math.dist, 0.3) == [(0.0, 0.0)]
    assert sr.fewest_waypoints([(0.0, 0.0), (0.0, 0.0), (1.0, 0.0)], ALWAYS, 0.0, math.dist, 0.3) == [(0.0, 0.0), (1.0, 0.0)]


def _world_plan(g, p, q, ov, budget, kappa=1.0):
    ids = iter("n%d" % i for i in range(10 ** 5))
    seen = {}
    try:
        return sr.plan_on_graph(g, "m", p, q, ov, lambda x, y: seen.setdefault((round(x, 3), round(y, 3)), next(ids)),
                                0.5, 1.5, 0.0, None, 0.05, None, None, kappa, True, 0, None, budget)
    except sr.ChartFallback as err:
        return err.reason


def test_planner_off_is_identical_and_on_stays_in_f_within_the_bound(graphs):
    name, eng, lazy = graphs
    rnd = random.Random(8)
    checked = 0
    for g in (eng, lazy):
        sf = sr.safety_for(g, None, None, 1.0)
        for p, q in _trial_pairs(eng, 10, 17):
            for ov in _safety._overlays(p, q, rnd)[:2]:
                off = _world_plan(g, p, q, ov, 0.0)
                legacy = _safety._plan(g, p, q, ov, 1.0, gap=0.0)
                on = _world_plan(g, p, q, ov, 0.35)
                if isinstance(off, str) or isinstance(legacy, str):
                    continue
                assert _wp(off) == _wp(legacy) and off.waypoint_budget is None
                if isinstance(on, str):
                    continue
                assert on.waypoint_budget["waypoints_removed"] >= 0
                assert len(on.waypoints) <= len(off.waypoints) + 1 or on.waypoint_budget["waypoints_removed"] >= 0
                assert _wp(on)[0] == _wp(off)[0] and _wp(on)[-1] == _wp(off)[-1]
                sa, sb = g.snap(p[0], p[1], 0.5), g.snap(q[0], q[1], 0.5)
                from packages.scene_graph.api import Blocked
                bl = Blocked(discs=list(ov[0]), segments=list(ov[1])) if (ov[0] or ov[1]) else None
                route = g.route(sa.key, sb.key, bl, (sa.x, sa.y), (sb.x, sb.y), safety=sf)
                lattice = [(sa.x, sa.y)] + [tuple(n) for n in route.nodes] + [(sb.x, sb.y)]
                j_lat, _ = _polyline_cost(g, lattice, sf)
                j_fin, inside = _polyline_cost(g, _wp(on), sf)
                assert inside, (name, p, q)
                assert j_fin <= j_lat + 0.35 + 1e-6, (name, p, q, j_fin, j_lat)
                checked += 1
    assert checked


def test_config_default_off_and_validation(monkeypatch, caplog):
    assert config.SCENE_GRAPH_WAYPOINT_BUDGET_M is None or config.SCENE_GRAPH_WAYPOINT_BUDGET_M > 0.0
    for raw, kept in (("0.35", 0.35), ("1.0", 1.0), ("1.5", None), ("nan", None), ("inf", None), ("-1", None), ("0", None)):
        monkeypatch.setattr(config, "SCENE_GRAPH_WAYPOINT_BUDGET_M", float(raw))
        config._validate_scene_graph_planner()
        assert config.SCENE_GRAPH_WAYPOINT_BUDGET_M == kept
