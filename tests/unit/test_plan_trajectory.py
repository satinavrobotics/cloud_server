"""The planner's dense lattice path per leg (docs/plan_trajectory_2026-10-14.md): derivation, go-here plan,
enrich, storage on the route node, digest."""
import math

import pytest

from cloud_common.objects import common
from cloud_common.objects import mission as mission_object
from packages import config
from packages.controllers.mission import server as dispatch_server
from packages.services.mission_planner import node_policy_plan as npp
from packages.services.mission_planner import scene_route, trajectory
from tests.unit.test_node_policy_plan import StripGraph
from tests.unit.test_node_policy_plan_server import _wps
from tests.unit.test_planner_chart_route import FakeGraph, FakeStore, _plan, _service, on  # noqa: F401
from tests.unit.test_planner_chart_route_hardening import LGraph


# ---- derivation -------------------------------------------------------------------------------
@pytest.mark.unit
def test_compress_keeps_only_direction_changes_rounded_to_the_cm():
    path = [(0.05 + 0.1 * i, 0.05) for i in range(10)] + [(0.95, 0.05 + 0.1 * j) for j in range(1, 6)]
    assert trajectory.compress(path) == [(0.05, 0.05), (0.95, 0.05), (0.95, 0.55)]
    diagonal = [(0.05 + 0.1 * i, 0.05 + 0.1 * i) for i in range(6)]
    assert trajectory.compress(diagonal) == [(0.05, 0.05), (0.55, 0.55)]
    assert trajectory.compress([(1.0, 1.0), (1.0, 1.0)]) == [(1.0, 1.0)]


@pytest.mark.unit
def test_split_cuts_the_path_at_the_waypoints_and_forces_the_ends():
    path = [(0.1 * i, 0.0) for i in range(11)] + [(1.0, 0.1 * j) for j in range(1, 11)]
    legs = trajectory.split(path, [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)])
    assert legs == [{"from_index": 0, "to_index": 1, "points": [[0.0, 0.0], [1.0, 0.0]]},
                    {"from_index": 1, "to_index": 2, "points": [[1.0, 0.0], [1.0, 1.0]]}]
    # a waypoint inside a run keeps the run's corner of the next leg
    legs = trajectory.split(path, [(0.0, 0.0), (0.5, 0.0), (1.0, 1.0)])
    assert legs[1]["points"] == [[0.5, 0.0], [1.0, 0.0], [1.0, 1.0]]
    assert trajectory.split(path, [(0.0, 0.0), (0.55, 0.0)]) is None       # not on the path
    assert trajectory.split(path, [(1.0, 0.0), (0.0, 0.0)]) is None        # wrong order


@pytest.mark.unit
def test_leg_end_is_forced_to_an_off_lattice_waypoint():
    got = trajectory.leg(0, 1, [(0.15, 0.05), (0.25, 0.05)], (0.12, 0.07), (0.31, 0.09))
    assert got["points"][0] == [0.12, 0.07] and got["points"][-1] == [0.31, 0.09]


@pytest.mark.unit
def test_capped_omits_the_whole_trajectory():
    legs = [{"from_index": 0, "to_index": 1, "points": [[0, 0], [1, 0], [1, 1]]}]
    assert trajectory.capped(legs, 3) == legs
    assert trajectory.capped(legs, 2) is None and trajectory.capped(legs, 0) is None


# ---- go-here ----------------------------------------------------------------------------------
def _go_here(graph, max_points=3000, goal=(15.0, 0.05), gap=3.0):
    return scene_route.plan_on_graph(graph, "m", (0.05, 0.05), goal, ([], []),
                                     lambda x, y: f"{x:.2f},{y:.2f}", 0.5, 1.5, gap,
                                     trajectory_max_points=max_points)


@pytest.mark.unit
def test_go_here_trajectory_has_one_leg_per_waypoint_pair_ending_at_the_waypoints():
    plan = _go_here(StripGraph())
    wps = plan.waypoints
    assert len(wps) >= 4 and plan.trajectory is not None
    assert [(l["from_index"], l["to_index"]) for l in plan.trajectory] == [(k, k + 1) for k in range(len(wps) - 1)]
    for item in plan.trajectory:
        a, b = wps[item["from_index"]], wps[item["to_index"]]
        assert item["points"][0] == [round(a.x, 2), round(a.y, 2)]
        assert item["points"][-1] == [round(b.x, 2), round(b.y, 2)]
        assert len(item["points"]) == 2                  # a straight run collapses to its ends


@pytest.mark.unit
def test_go_here_trajectory_reproduces_the_lattice_path_around_a_corner():
    plan = scene_route.plan_on_graph(
        LGraph(1.0), "m", (0.55, 0.55), (9.55, 9.5), ([], []), lambda x, y: f"{x:.2f},{y:.2f}", 0.5, 1.5, 100.0,
        trajectory_max_points=3000)
    wps = plan.waypoints
    assert [(round(w.x, 2), round(w.y, 2)) for w in wps] == [(0.55, 0.55), (9.55, 0.95), (9.55, 9.45)]
    # the lattice path's direction changes are all there (the diagonal step into the vertical arm included), the
    # waypoints are the leg ends, and nothing else is
    assert plan.trajectory == [
        {"from_index": 0, "to_index": 1, "points": [[0.55, 0.55], [9.45, 0.55], [9.55, 0.65], [9.55, 0.95]]},
        {"from_index": 1, "to_index": 2, "points": [[9.55, 0.95], [9.55, 9.45]]}]


@pytest.mark.unit
def test_go_here_trajectory_off_by_default_or_over_the_cap():
    assert _go_here(StripGraph(), max_points=0).trajectory is None
    assert _go_here(StripGraph(), max_points=2).trajectory is None       # needs >= 2 points per leg
    assert _go_here(StripGraph()).waypoints                              # the plan itself never fails for it


@pytest.mark.unit
async def test_plan_route_returns_the_trajectory_and_the_switch_removes_it(on, monkeypatch):
    svc = _service(FakeStore(FakeGraph()))
    out = await _plan(svc)
    assert out["trajectory"] and out["trajectory"][0]["from_index"] == 0
    monkeypatch.setattr(config, "PLAN_TRAJECTORY_ENABLED", False)
    assert "trajectory" not in await _plan(svc)


# ---- enrich -----------------------------------------------------------------------------------
@pytest.mark.unit
def test_gap_fill_records_the_lattice_path_cut_at_the_planned_points_and_widths_do_not_reroute():
    g = StripGraph()
    routes = npp.LegRoutes()
    poses = [common.Pose2D(x=0.5, y=0.05, map_id="m"), common.Pose2D(x=15.0, y=0.05, map_id="m")]
    out, n = npp.insert_planned(g, poses, lambda a, b: True, 3.0, 0.5, scene_route.simplify, None, routes)
    assert n >= 3
    after_insert = g.routes
    legs = trajectory.from_routes(routes.nodes, out)
    assert [(l["from_index"], l["to_index"]) for l in legs] == [(k, k + 1) for k in range(len(out) - 1)]
    assert g.routes == after_insert                       # the trajectory cost no route call
    npp.annotate_corridors(g, out, range(1, len(out) - 1), 0.4, 0.0, 0.5, routes=routes)
    # the pieces have no cached width: routed once each, never twice
    assert g.routes - after_insert <= 2 * (len(out) - 2)


@pytest.mark.unit
def test_pairs_in_one_chart_are_routed_once_for_both_the_cut_and_the_width():
    g = StripGraph()
    routes = npp.LegRoutes()
    poses = _poses_xy(0.5, 3.0, 5.5)
    out, n = npp.insert_planned(g, poses, lambda a, b: True, 0.0, 0.5, scene_route.simplify, None, routes)
    assert n == 0 and g.routes == 2
    npp.annotate_corridors(g, out, [1], 0.4, 0.0, 0.5, routes=routes)
    assert g.routes == 2                                  # both widths came from the cache
    assert sorted((l["from_index"], l["to_index"]) for l in trajectory.from_routes(routes.nodes, out)) == [(0, 1), (1, 2)]


def _poses_xy(*xs):
    return [common.Pose2D(x=x, y=0.05, map_id="m") for x in xs]


@pytest.mark.unit
async def test_enrich_returns_a_trajectory_for_an_operator_route_even_when_nothing_changed(on):
    svc = _service(FakeStore(FakeGraph()))
    out = await svc.enrich_route("r1", "m", _wps(1.0, 3.0, 6.0))
    assert not out["changed"] and out["inserted"] == 0
    legs = out["trajectory"]
    assert [(l["from_index"], l["to_index"]) for l in legs] == [(0, 1), (1, 2)]
    assert legs[0]["points"][0] == [1.0, 0.0] and legs[1]["points"][-1] == [6.0, 0.0]


@pytest.mark.unit
async def test_enrich_trajectory_is_absent_without_a_graph_or_when_switched_off(on, monkeypatch):
    store = FakeStore(FakeGraph())
    store.version = None
    assert "trajectory" not in await _service(store).enrich_route("r1", "m", _wps(1.0, 6.0))
    monkeypatch.setattr(config, "PLAN_TRAJECTORY_ENABLED", False)
    assert "trajectory" not in await _service(FakeStore(FakeGraph())).enrich_route("r1", "m", _wps(1.0, 6.0))


# ---- storage on the route node ----------------------------------------------------------------
def _route(**kw):
    wps = [{"x": 0.0, "y": 0.0, "map_id": "m"}, {"x": 1.0, "y": 0.0, "map_id": "m"},
           {"x": 1.0, "y": 1.0, "map_id": "m"}]
    return mission_object.MissionRouteNodeV1(waypoints=wps, **kw)


LEGS = [{"from_index": 0, "to_index": 1, "points": [[0.0, 0.0], [1.0, 0.0]]},
        {"from_index": 1, "to_index": 2, "points": [[1.0, 0.0], [1.0, 1.0]]}]


@pytest.mark.unit
def test_route_node_round_trips_its_trajectory_and_old_routes_have_none():
    route = _route(trajectory=LEGS)
    again = mission_object.MissionRouteNodeV1.parse_raw(route.json())
    assert [l.dict() for l in again.trajectory] == LEGS
    assert _route().trajectory is None


@pytest.mark.unit
def test_stale_legs_are_dropped_not_an_error():
    stale = [dict(LEGS[0], points=[[0.0, 0.0], [5.0, 0.0]]),             # no longer ends at its waypoint
             {"from_index": 0, "to_index": 9, "points": [[0.0, 0.0], [1.0, 0.0]]},
             LEGS[1]]
    route = _route(trajectory=stale)
    assert [(l.from_index, l.to_index) for l in route.trajectory] == [(1, 2)]
    assert _route(trajectory=[stale[0]]).trajectory is None


@pytest.mark.unit
def test_the_route_digest_ignores_the_trajectory():
    assert dispatch_server._route_digest(_route(trajectory=LEGS)) == dispatch_server._route_digest(_route())
