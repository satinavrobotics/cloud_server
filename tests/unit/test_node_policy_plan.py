"""Planner side of the nodePolicy skip data: corridor width from the scene graph, go-here
marking, planned waypoints between operator waypoints (docs/node_policies_2026-10-13.md)."""
import math
from types import SimpleNamespace

import pytest

from cloud_common.objects import common
from packages.scene_graph.api import Route, Snap
from packages.services.mission_planner import node_policy_plan as npp
from packages.services.mission_planner import scene_route

H = 0.1
CHART_M = 2.0           # charts are 2 m wide slices of a corridor along x


class StripGraph:
    """A free strip 0 <= x <= 20, |y| <= 0.5, cut into charts c0, c1, ... of CHART_M metres.
    `width_at(x)` gives the passage width at x; a route's min_width is the min over its x range."""
    params = SimpleNamespace(lattice=H)

    def __init__(self, width_at=lambda x: 1.6, fail_routes=False):
        self.width_at = width_at
        self.fail_routes = fail_routes
        self.routes = 0

    def _key(self, x):
        return f"c{int(min(max(x, 0.0), 19.99) // CHART_M)}"

    def snap(self, x, y, max_dist=None):
        px = (math.floor(min(max(x, 0.0), 19.99) / H) + 0.5) * H
        py = (math.floor(min(max(y, -0.5), 0.49) / H) + 0.5) * H
        d = math.hypot(px - x, py - y)
        if max_dist is not None and d > max_dist:
            return Snap(None, px, py, d)
        return Snap(self._key(px), px, py, d)

    def free_cell(self, i, j):
        return 0 <= i < 200 and -5 <= j < 5

    def route(self, a, b, blocked=None, start=None, goal=None):
        self.routes += 1
        if self.fail_routes:
            raise RuntimeError("tiles")
        (x0, y0), (x1, y1) = start, goal
        n = max(1, int(round(abs(x1 - x0) / H)))
        nodes = [(x0 + (x1 - x0) * k / n, y0 + (y1 - y0) * k / n) for k in range(n + 1)]
        charts = []
        for x, _ in nodes:
            k = self._key(x)
            if not charts or charts[-1] != k:
                charts.append(k)
        width = min(self.width_at(x) for x, _ in nodes)
        return Route(charts=charts, length=abs(x1 - x0), min_width=width, nodes=nodes)


def _poses(*xs, **kw):
    return [common.Pose2D(x=x, y=0.05, map_id="m", **kw) for x in xs]


# ---- corridor value --------------------------------------------------------------------------
@pytest.mark.unit
def test_corridor_per_side_is_the_room_of_the_centre_line():
    # 1.6 m passage, 0.6 m robot, 0.05 margin: (1.6 - 0.6 - 0.05) / 2 = 0.475 -> 0.47
    assert npp.corridor_per_side(1.6, 0.6, 0.05) == 0.47
    assert npp.corridor_per_side(0.6, 0.6, 0.05) == 0.0     # measured, no room
    assert npp.corridor_per_side(None, 0.6) is None
    assert npp.corridor_per_side(1.6, None) is None
    assert npp.corridor_per_side(float("nan"), 0.6) is None


@pytest.mark.unit
def test_leg_widths_are_the_route_width_of_each_leg_and_none_on_failure():
    g = StripGraph(width_at=lambda x: 0.9 if 4.0 <= x < 6.0 else 2.0)
    pts = [(0.05, 0.05), (3.0, 0.05), (5.0, 0.05), (9.0, 0.05)]
    assert npp.leg_widths(g, pts, 0.5) == pytest.approx([2.0, 0.9, 0.9])
    assert npp.leg_widths(StripGraph(fail_routes=True), pts, 0.5) == [None, None, None]
    # out of the strip: the snap is refused
    assert npp.leg_widths(g, [(0.05, 5.0), (3.0, 0.05)], 0.5) == [None]


@pytest.mark.unit
def test_leg_widths_stop_at_the_budget():
    ticks = iter([0.0, 0.5, 2.0, 3.0, 4.0])
    out = npp.leg_widths(StripGraph(), [(0.05, 0.05), (1.0, 0.05), (2.0, 0.05), (3.0, 0.05)],
                         0.5, budget_s=1.0, clock=lambda: next(ticks))
    assert out[0] is not None and out[1:] == [None, None]


# ---- marking ---------------------------------------------------------------------------------
@pytest.mark.unit
def test_interior_waypoints_get_the_narrower_of_their_two_legs():
    g = StripGraph(width_at=lambda x: 1.0 if 4.0 <= x < 6.0 else 2.0)
    poses = _poses(0.05, 3.0, 5.0, 8.0, 10.0)
    report = npp.annotate_corridors(g, poses, range(1, 4), 0.6, 0.05, 0.5)
    assert report == {"total": 3, "skippable": 3, "unavailable": None,
                      "corridor": {"measured": 4, "narrow": 0, "unknown": 0}}
    # wp1 is on legs (0->3: 2.0) and (3->5: 1.0): min 1.0 -> (1.0-.6-.05)/2 = .175 -> .17
    # wp0 has its one leg only (0->3: 2.0) -> (2.0-.6-.05)/2 = .675 -> .67, not a candidate here
    assert [p.corridor_width_m for p in poses] == [0.67, 0.17, 0.17, 0.17, None]
    assert [p.skippable for p in poses] == [None, True, True, True, None]


@pytest.mark.unit
def test_a_narrow_passage_is_a_zero_corridor_and_not_skippable_but_not_unavailable():
    g = StripGraph(width_at=lambda x: 0.6)
    poses = _poses(0.05, 3.0, 8.0)
    report = npp.annotate_corridors(g, poses, [1], 0.6, 0.05, 0.5)
    assert report == {"total": 1, "skippable": 0, "unavailable": None,
                      "corridor": {"measured": 0, "narrow": 2, "unknown": 0}}
    assert poses[1].skippable is False and poses[1].corridor_width_m == 0.0


@pytest.mark.unit
def test_an_unknown_width_leaves_the_corridor_missing_with_a_reason():
    unknown = _poses(0.05, 3.0, 8.0)
    report = npp.annotate_corridors(StripGraph(), unknown, [1], None, 0.05, 0.5)
    assert report["unavailable"] == npp.UNAVAILABLE_ROBOT_WIDTH
    assert report["corridor"] == {"measured": 0, "narrow": 0, "unknown": 2}
    assert unknown[1].skippable is False and unknown[1].corridor_width_m is None
    failing = _poses(0.05, 3.0, 8.0)
    assert npp.annotate_corridors(StripGraph(fail_routes=True), failing, [1], 0.6, 0.05,
                                  0.5)["unavailable"] == npp.UNAVAILABLE_WIDTH
    assert failing[1].corridor_width_m is None


@pytest.mark.unit
def test_operator_waypoints_get_a_corridor_without_becoming_skippable():
    g = StripGraph(width_at=lambda x: 1.0 if 4.0 <= x < 6.0 else 2.0)
    poses = _poses(0.05, 3.0, 5.0, 8.0)          # waypoint 1 is an operator's, not a candidate
    report = npp.annotate_corridors(g, poses, [2], 0.6, 0.05, 0.5)
    assert report["total"] == 1 and report["skippable"] == 1
    assert poses[1].skippable is None and poses[1].corridor_width_m == 0.17
    assert poses[2].skippable is True


@pytest.mark.unit
def test_first_and_last_never_get_a_computed_corridor_and_own_corridor_is_kept():
    g = StripGraph()
    poses = _poses(0.05, 3.0, 8.0)
    poses[0].corridor_width_m = 0.3                  # an operator override on the first one
    report = npp.annotate_corridors(g, poses, [0, 1, 2], 0.6, 0.05, 0.5)
    assert poses[0].corridor_width_m == 0.3 and poses[0].skippable is True
    assert poses[2].skippable is None and poses[2].corridor_width_m is None   # the last: no policy
    assert report["total"] == 2 and report["skippable"] == 2


# ---- planned waypoints -----------------------------------------------------------------------
def _visible(a, b):
    return True


@pytest.mark.unit
def test_neighbouring_waypoints_get_nothing_inserted():
    g = StripGraph()
    poses = _poses(0.5, 3.0, 5.5)          # c0 -> c1 -> c2: each pair neighbours
    out, n = npp.insert_planned(g, poses, _visible, 0.0, 0.5, scene_route.simplify)
    assert n == 0 and out == poses


@pytest.mark.unit
def test_far_apart_waypoints_get_planned_waypoints_each_pair_within_two_charts():
    g = StripGraph()
    poses = _poses(0.5, 15.0)               # c0 -> c7
    out, n = npp.insert_planned(g, poses, _visible, 0.0, 0.5, scene_route.simplify)
    assert n >= 3 and len(out) == n + 2
    assert out[0] is poses[0] and out[-1] is poses[-1]
    inner = out[1:-1]
    assert all(p.origin == "planned" and p.skippable is True for p in inner)
    assert all(p.map_id == "m" for p in inner)
    keys = [g.snap(p.x, p.y).key for p in out]
    for a, b in zip(keys[:-1], keys[1:]):
        assert abs(int(a[1:]) - int(b[1:])) <= 1
    xs = [p.x for p in out]
    assert xs == sorted(xs)
    # theta is the bearing to the next point
    assert inner[0].theta == pytest.approx(0.0, abs=0.2)


@pytest.mark.unit
def test_line_of_sight_and_gap_cap_shape_the_planned_points():
    g = StripGraph()
    out, n = npp.insert_planned(g, _poses(0.5, 15.0), _visible, 2.5, 0.5, scene_route.simplify)
    xs = [p.x for p in out]
    assert all(b - a <= 2.5 + H for a, b in zip(xs[:-1], xs[1:]))


@pytest.mark.unit
def test_unroutable_pair_is_left_alone():
    out, n = npp.insert_planned(StripGraph(fail_routes=True), _poses(0.5, 15.0), _visible, 0.0,
                                0.5, scene_route.simplify)
    assert n == 0 and len(out) == 2
    out, n = npp.insert_planned(StripGraph(), _poses(0.5, 15.0)[:1] + _poses(15.0)[:0], _visible,
                                0.0, 0.5, scene_route.simplify)
    assert n == 0


@pytest.mark.unit
def test_operator_waypoints_keep_their_policy_and_planned_ones_get_corridors():
    g = StripGraph()
    poses = _poses(0.5, 15.0)
    poses[0].max_wait_s = 3.0
    poses[0].skippable = False
    out, _ = npp.insert_planned(g, poses, _visible, 0.0, 0.5, scene_route.simplify)
    planned = [i for i, p in enumerate(out) if p.origin == "planned"]
    npp.annotate_corridors(g, out, planned, 0.6, 0.05, 0.5)
    assert out[0].max_wait_s == 3.0 and out[0].skippable is False and out[0].origin is None
    assert all(out[i].corridor_width_m == 0.47 for i in planned)


# ---- go-here through plan_on_graph -----------------------------------------------------------
@pytest.mark.unit
def test_go_here_plan_marks_every_waypoint_but_the_goal():
    g = StripGraph()
    ids = iter(range(1000))
    plan = scene_route.plan_on_graph(
        g, "m", (0.05, 0.05), (15.0, 0.05), ([], []), lambda x, y: f"n{next(ids)}", 0.5, 1.5, 3.0,
        node_policy_cfg={"robot_width": 0.6, "margin": 0.05, "snap_m": 0.5, "budget_s": 0})
    wps = plan.waypoints
    assert len(wps) >= 4
    assert wps[-1].skippable is None and wps[-1].corridor_width_m is None
    assert all(w.skippable is True and w.corridor_width_m == 0.47 for w in wps[:-1])
    assert plan.node_policy == {"total": len(wps) - 1, "skippable": len(wps) - 1,
                                "unavailable": None,
                                "corridor": {"measured": len(wps) - 1, "narrow": 0, "unknown": 0}}


@pytest.mark.unit
def test_go_here_plan_without_the_cfg_is_unchanged():
    g = StripGraph()
    ids = iter(range(1000))
    plan = scene_route.plan_on_graph(g, "m", (0.05, 0.05), (15.0, 0.05), ([], []),
                                     lambda x, y: f"n{next(ids)}", 0.5, 1.5, 3.0)
    assert plan.node_policy is None
    assert all(w.skippable is None and w.corridor_width_m is None for w in plan.waypoints)
