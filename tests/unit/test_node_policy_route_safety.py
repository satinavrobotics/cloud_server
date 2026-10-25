"""nodePolicy planning (corridor widths, planned waypoints) uses the planner's clearance pricing (docs/scene_graph/route_safety.md):
the same `Safety` on both `graph.route` calls of node_policy_plan and the same cost-aware line of sight in its cut."""
import math
from types import SimpleNamespace

import pytest

from cloud_common.objects import common
from packages import config
from packages.scene_graph.api import Route, Safety
from packages.services.mission_planner import node_policy_plan as npp
from packages.services.mission_planner import scene_route as sr

from .test_node_policy_plan import StripGraph, _poses
from .test_planner_chart_route import FakeGraph, FakeStore, _service

H = 0.1
SAFETY = Safety(0.4, 10.0)


class PricedStrip(StripGraph):
    """StripGraph that takes `safety`, remembers it and has a deficit field"""
    params = SimpleNamespace(lattice=H, robot_half_side=0.25)

    def __init__(self, **kw):
        super().__init__(**kw)
        self.seen = []

    def route(self, a, b, blocked=None, start=None, goal=None, safety=None):
        self.seen.append(safety)
        return super().route(a, b, blocked, start, goal)

    def deficit_cell(self, ix, iy, margin):
        return 0.0


def test_corridor_widths_route_with_the_given_safety_and_without_it_pass_nothing():
    g = PricedStrip()
    pts = [(0.05, 0.05), (3.05, 0.05), (6.05, 0.05)]
    npp._leg_width(g, pts[0], pts[1], 0.5, safety=SAFETY)
    assert g.seen == [SAFETY]
    g.seen.clear()
    poses = _poses(0.05, 3.05, 6.05)
    npp.annotate_corridors(g, poses, [1], 0.4, 0.05, 0.5, safety=SAFETY)
    assert g.seen and all(s is SAFETY for s in g.seen)

    class Legacy(StripGraph):                          # a graph whose route() has no `safety` parameter
        pass
    legacy = Legacy()
    npp._leg_width(legacy, pts[0], pts[1], 0.5)
    npp.annotate_corridors(legacy, _poses(0.05, 3.05, 6.05), [1], 0.4, 0.05, 0.5)
    ins, _ = npp.insert_planned(legacy, _poses(0.05, 10.05), lambda a, b: True, 20.0, 0.5, sr.simplify)
    assert legacy.routes >= 2 and ins


class Bent(PricedStrip):
    """the route from start to goal runs along y = 0.05 but climbs over a costly patch (x in 4..6, y < 0.6): the straight cut across
    the foot of the hump is shorter and, with the patch priced, dearer"""

    @staticmethod
    def _y(x):
        if x < 3.5 or x > 6.5:
            return 0.05
        if x < 4.0:
            return 0.05 + (x - 3.5) * 2.0
        if x <= 6.0:
            return 1.05
        return 1.05 - (x - 6.0) * 2.0

    def route(self, a, b, blocked=None, start=None, goal=None, safety=None):
        self.seen.append(safety)
        (x0, _), (x1, _) = start, goal
        n = int(round(abs(x1 - x0) / H))
        nodes = [(round(x0 + (x1 - x0) * k / n, 6), round(self._y(x0 + (x1 - x0) * k / n), 6)) for k in range(n + 1)]
        charts = []
        for x, _ in nodes:
            c = self._key(x)
            if not charts or charts[-1] != c:
                charts.append(c)
        return Route(charts=charts, length=0.0, min_width=1.6, nodes=nodes)

    def deficit_cell(self, ix, iy, margin):
        x, y = (ix + 0.5) * H, (iy + 0.5) * H
        return 0.3 if 4.0 <= x < 6.0 and y < 0.6 else 0.0


def _cost(g, pts, safety):
    """independent sampled cost, 0.025 m, deficit of the sample's cell"""
    total = 0.0
    for a, b in zip(pts[:-1], pts[1:]):
        length = math.dist(a, b)
        n = max(1, math.ceil(length / 0.025 - 1e-9))
        acc = sum(1.0 + safety.rate * g.deficit_cell(int(math.floor((a[0] + (k + .5) / n * (b[0] - a[0])) / H)),
                                                      int(math.floor((a[1] + (k + .5) / n * (b[1] - a[1])) / H)),
                                                      safety.margin) for k in range(n))
        total += length * acc / n
    return total


def test_planned_waypoints_use_the_priced_cut():
    g = Bent()
    poses = _poses(0.05, 10.05)
    free = lambda a, b: True
    plain, n_plain = npp.insert_planned(g, poses, free, 3.5, 0.5, sr.simplify)
    priced, n_priced = npp.insert_planned(g, poses, free, 3.5, 0.5, sr.simplify, SAFETY)
    assert g.seen[-1] is SAFETY and g.seen[0] is None
    lattice = [tuple(p) for p in g.route("c0", "c4", None, (0.05, 0.05), (10.05, 0.05)).nodes]
    j_lat = _cost(g, lattice, SAFETY)
    j_plain = _cost(g, [(p.x, p.y) for p in plain], SAFETY)
    j_priced = _cost(g, [(p.x, p.y) for p in priced], SAFETY)
    assert j_priced <= j_lat * (1 + 1e-6) + 1e-6          # S11: the cut never costs more than the lattice path
    assert j_plain > j_lat                                  # the plain cut does (so the test has teeth)
    assert n_priced >= n_plain and [(p.x, p.y) for p in priced] != [(p.x, p.y) for p in plain]


async def test_enrich_and_plan_route_pass_one_safety_from_the_footprint(monkeypatch):
    monkeypatch.setattr(config, "SCENE_GRAPH_PLANNER_ENABLED", True)
    class Priced(FakeGraph):
        params = SimpleNamespace(lattice=H, robot_half_side=0.25)

        def __init__(self):
            super().__init__()
            self.safeties = []

        def deficit_cell(self, ix, iy, margin):
            return 0.0

        def route(self, a, b, blocked=None, start=None, goal=None, safety=None):
            self.safeties.append(safety)
            return super().route(a, b, blocked, start, goal)

    monkeypatch.setattr(config, "SCENE_GRAPH_SAFETY_KAPPA", 2.0)
    graph = Priced()
    svc = _service(FakeStore(graph))
    robot = await svc.get_robot_status("r1")
    robot.status.factsheet.width = 0.4
    robot.status.factsheet.length = 0.8
    wps = [common.Pose2D(x=x, y=0.0, map_id="m") for x in (1.0, 3.0, 6.0)]
    wps[1].skippable = True
    await svc.enrich_route("r1", "m", wps)
    got = [s for s in graph.safeties if s is not None]
    assert got and len(set(got)) == 1
    margin = math.hypot(0.8, 0.4) / 2
    assert got[0].margin == pytest.approx(margin) and got[0].rate == pytest.approx(2.0 / (margin - 0.25))
    graph.safeties.clear()
    out = await svc.plan_route("r1", target_x=10.0, target_y=0.0, map_id="m")
    assert out["success"] and out["planner"] == sr.PLANNER_SCENE_GRAPH
    assert graph.safeties and all(s is not None and s.margin == pytest.approx(margin) for s in graph.safeties[:1])
    # kappa 0: the same calls carry no pricing
    monkeypatch.setattr(config, "SCENE_GRAPH_SAFETY_KAPPA", 0.0)
    graph.safeties.clear()
    await svc.enrich_route("r1", "m", wps)
    assert graph.safeties and all(s is None for s in graph.safeties)
    svc.cleanup()
