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


# ---- apex candidates (C.2, "Apex candidates") ---------------------------------------------------------------------------

def _near_wall_clearance(p):
    """a synthetic clearance: the distance to the line y = 1.6 (obstacle above), capped at 1"""
    return min(1.0, abs(1.6 - p[1]))


def _l_lattice():
    return [(round(0.1 * k, 3), 0.0) for k in range(11)] + [(1.0, round(0.1 * k, 3)) for k in range(1, 11)]


def _l_visible(a, b):                         # the L corridor: y <= 0.05 or x >= 0.95 (the inside of the corner is blocked)
    n = max(1, int(math.dist(a, b) / 0.01))
    for k in range(n + 1):
        x, y = a[0] + k / n * (b[0] - a[0]), a[1] + k / n * (b[1] - a[1])
        if not (y <= 0.05 + 1e-9 or x >= 0.95 - 1e-9):
            return False
    return True


def test_apex_replaces_a_corner_vertex_by_the_arc_apex_on_the_inward_bisector():
    lattice = _l_lattice()                         # a right angle at (1, 0): turn 90 degrees, inward is up-left
    clearance = lambda p: min(1.0, math.dist(p, (1.4, -0.4)))    # noqa: E731  an obstacle beyond the corner: apex is farther
    info = {}
    out = sr.fewest_waypoints(lattice, _l_visible, 0.0, math.dist, 0.35, 0.0, 0.1, clearance, None, info)
    assert info == {"corners": 1, "candidates": 1, "apexes_used": 1}
    assert len(out) == 3 and out[0] == (0.0, 0.0) and out[-1] == (1.0, 1.0)
    d = 0.1 * (1.0 / math.cos(math.pi / 4) - 1.0)
    assert out[1] == pytest.approx((1.0 - d / math.sqrt(2), d / math.sqrt(2)), abs=1e-5)
    assert clearance(out[1]) >= clearance((1.0, 0.0))
    # a corner whose apex would be closer to the obstacle keeps the vertex
    near = lambda p: min(1.0, math.dist(p, (0.9, 0.1)))   # noqa: E731
    info = {}
    out = sr.fewest_waypoints(lattice, _l_visible, 0.0, math.dist, 0.35, 0.0, 0.1, near, None, info)
    assert out[1] == (1.0, 0.0) and info == {"corners": 1, "candidates": 0, "apexes_used": 0}


def test_apex_radius_is_clipped_to_the_goal_tolerance_and_the_chord_half():
    lattice = _l_lattice()
    _, apex = sr.apex_candidates(lattice, _l_visible, 0.0, 50.0, lambda p: 1.0 - 0.1 * p[0])
    (a,) = apex.values()
    assert math.dist(a, (1.0, 0.0)) <= 0.35 + 1e-9
    short = [(0.0, 0.0), (0.1, 0.0), (0.2, 0.0), (0.2, 0.1), (0.2, 0.2)]
    _, apex = sr.apex_candidates(short, lambda u, v: u[0] == v[0] or u[1] == v[1], 0.0, 50.0, lambda p: 1.0 - 0.1 * p[0])
    (a,) = apex.values()                                       # t <= half of the 0.2 m chord: R <= 0.1 / tan(45 degrees)
    assert math.dist(a, (0.2, 0.0)) == pytest.approx(0.1 * (math.sqrt(2) - 1.0), abs=1e-5)


def test_apex_off_radius_zero_or_no_corner_is_the_budget_solver():
    rnd = random.Random(21)
    better = lambda p: 1.0 + p[0]          # noqa: E731
    for _ in range(80):
        lattice = _walk(rnd, rnd.randint(3, 30))
        for f in (math.dist, _wild_cost):
            plain = sr.fewest_waypoints(lattice, _visible, 0.0, f, 0.35)
            assert sr.fewest_waypoints(lattice, _visible, 0.0, f, 0.35, 0.0, 0.0, better) == plain
            assert sr.fewest_waypoints(lattice, _visible, 0.0, f, 0.35, 0.0, 0.5, None) == plain
    straight = [(round(0.1 * k, 3), 0.0) for k in range(20)]
    info = {}
    out = sr.fewest_waypoints(straight, ALWAYS, 0.0, math.dist, 0.35, 0.0, 0.5, better, None, info)
    assert out == [(0.0, 0.0), (1.9, 0.0)] and info == {"corners": 0, "candidates": 0, "apexes_used": 0}


def test_apex_properties_cost_bound_visibility_clearance_and_determinism():
    rnd = random.Random(5)
    used = 0
    for _ in range(200):
        lattice = _walk(rnd, rnd.randint(4, 40))
        for f in (math.dist, _wild_cost):
            j_lat = _cost_sum(f, lattice)
            for budget in (0.0, 0.2, 0.35):
                info = {}
                out = sr.fewest_waypoints(lattice, _visible, 1.5, f, budget, 0.0, 0.6, _near_wall_clearance, None, info)
                assert out == sr.fewest_waypoints(lattice, _visible, 1.5, f, budget, 0.0, 0.6, _near_wall_clearance)
                assert out[0] == lattice[0] and out[-1] == lattice[-1]
                assert _cost_sum(f, out) <= j_lat + budget + 1e-9
                for a, b in zip(out[:-1], out[1:]):
                    assert _visible(a, b) or (a in lattice and b in lattice and b == lattice[lattice.index(a) + 1])
                off = [p for p in out if p not in lattice]
                assert len(off) == info["apexes_used"]
                used += len(off)
                for apex_pt in off:
                    near = min(lattice, key=lambda p: math.dist(p, apex_pt))
                    assert _near_wall_clearance(apex_pt) >= _near_wall_clearance(near) - 0.1 - 1e-9
                    assert math.dist(apex_pt, near) <= sr.APEX_TOL_M + 1e-6
                plain = sr.fewest_waypoints(lattice, _visible, 1.5, f, budget)
                assert len(out) <= len(plain)         # apex nodes are alternatives: the plain path stays feasible
    assert used > 0


def test_apex_a_larger_radius_never_lowers_the_clearance_of_a_corner_waypoint():
    lattice = _l_lattice()
    clearance = lambda p: 1.0 / (1.0 + math.dist(p, (1.6, -0.6)))   # noqa: E731  increases toward the inside
    prev = -1.0
    for radius in (0.0, 0.1, 0.2, 0.4, 0.8, 1.6):
        out = sr.fewest_waypoints(lattice, _l_visible, 0.0, math.dist, 0.35, 0.0, radius, clearance)
        assert len(out) == 3
        c = clearance(out[1])
        assert c >= prev - 1e-9 and c >= clearance((1.0, 0.0)) - 1e-9
        prev = c


def _apex_plan(g, p, q, ov, lam, apex, budget=0.35, kappa=1.0):
    ids = iter("n%d" % i for i in range(10 ** 5))
    seen = {}
    turn = {"lambda_m_per_rad": lam, "source": "config", "speed_max": None, "angular_speed_max": None, "reason": ""}
    try:
        return sr.plan_on_graph(g, "m", p, q, ov, lambda x, y: seen.setdefault((round(x, 3), round(y, 3)), next(ids)),
                                0.5, 1.5, 0.0, None, 0.05, None, None, kappa, True, 100000, turn, budget, apex)
    except sr.ChartFallback as err:
        return err.reason


def test_apex_planner_off_is_identical_and_on_keeps_the_guarantees(graphs):
    name, eng, lazy = graphs
    rnd = random.Random(14)
    from packages.scene_graph.api import Blocked
    checked = 0
    lam = 0.67
    for g in (eng, lazy):
        sf = sr.safety_for(g, None, None, 1.0, lam)
        for p, q in _trial_pairs(eng, 10, 23):
            for ov in _safety._overlays(p, q, rnd)[:2]:
                base = _apex_plan(g, p, q, ov, lam, False)
                on = _apex_plan(g, p, q, ov, lam, True)
                if isinstance(base, str) or isinstance(on, str):
                    continue
                assert base.waypoint_apex is None
                zero = _apex_plan(g, p, q, ov, 0.0, True)
                zero_off = _apex_plan(g, p, q, ov, 0.0, False)
                assert not isinstance(zero, str) and zero.waypoint_apex is None and _wp(zero) == _wp(zero_off)
                assert _wp(on) == _wp(_apex_plan(g, p, q, ov, lam, True))
                assert on.waypoint_apex["apexes_used"] <= on.waypoint_apex["corners"]
                assert len(on.waypoints) <= len(base.waypoints) + 1
                sa, sb = g.snap(p[0], p[1], 0.5), g.snap(q[0], q[1], 0.5)
                bl = Blocked(discs=list(ov[0]), segments=list(ov[1])) if (ov[0] or ov[1]) else None
                route = g.route(sa.key, sb.key, bl, (sa.x, sa.y), (sb.x, sb.y), safety=sf)
                lattice = [(sa.x, sa.y)] + [tuple(n) for n in route.nodes] + [(sb.x, sb.y)]
                j_lat, _ = _polyline_cost(g, lattice, sf)
                j_fin, inside = _polyline_cost(g, _wp(on), sf)
                assert inside, (name, p, q)
                assert j_fin <= j_lat + 0.35 + 1e-6
                checked += 1
    assert checked


def test_apex_config_default_off_and_server_pass_through():
    import inspect
    from packages.services.mission_planner import server
    assert config.SCENE_GRAPH_WAYPOINT_APEX_ENABLED is False
    src = inspect.getsource(server)
    assert "waypoint_apex=config.SCENE_GRAPH_WAYPOINT_APEX_ENABLED" in src and '"apexes_used"' in src


# ---- candidate thinning ---------------------------------------------------------------------------------------------------

def test_thinned_candidates_keep_the_cost_bound_and_never_beat_the_full_dp_nor_lose_to_the_pull():
    rnd = random.Random(31)
    more = 0
    for _ in range(120):
        lattice = _walk(rnd, rnd.randint(4, 60))
        for f in (math.dist, _wild_cost):
            j_lat = _cost_sum(f, lattice)
            for budget in (0.0, 0.35):
                for gap in (0.0, 1.5):
                    full = sr.fewest_waypoints(lattice, _visible, gap, f, budget)
                    thin = sr.fewest_waypoints(lattice, _visible, gap, f, budget, 0.0, 0.0, None, _visible, None, 0.05)
                    assert thin == sr.fewest_waypoints(lattice, _visible, gap, f, budget, 0.0, 0.0, None, _visible, None, 0.05)
                    assert thin[0] == lattice[0] and thin[-1] == lattice[-1]
                    assert _cost_sum(f, thin) <= j_lat + budget + 1e-9
                    assert len(thin) >= len(full)
                    for a, b in zip(thin[:-1], thin[1:]):
                        assert b == lattice[lattice.index(a) + 1] or _visible(a, b)
                    if f is math.dist:
                        pull = sr.simplify(lattice, _visible, gap)
                        assert len(thin) <= len(pull)
                    more += len(thin) > len(full)
    assert more >= 0


def test_thin_indices_are_sorted_keep_the_ends_the_pull_and_the_spacing():
    pts = [(round(0.1 * k, 3), 0.0) for k in range(60)] + [(5.9, round(0.1 * k, 3)) for k in range(1, 30)]
    idx = sr.thin_indices(pts, 0.05, [30], 5.0)
    assert idx == sorted(set(idx)) and idx[0] == 0 and idx[-1] == len(pts) - 1 and 30 in idx and 59 in idx
    assert all(math.dist(pts[a], pts[b]) <= sr.THIN_SPACING_M + 1e-9 for a, b in zip(idx[:-1], idx[1:]))
    assert len(idx) < len(pts)
