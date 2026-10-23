"""plan_route (chart planner) carries the nodePolicy report and marks the go-here waypoints."""
import pytest

from packages import config
from tests.unit.test_planner_chart_route import FakeGraph, FakeStore, _plan, _service, on  # noqa: F401


@pytest.mark.unit
async def test_plan_carries_the_node_policy_report_and_marks_every_waypoint_but_the_goal(on):
    svc = _service(FakeStore(FakeGraph()))
    robot = await svc.get_robot_status("r1")
    robot.status.factsheet.width = 0.4
    out = await _plan(svc)
    wps = out["waypoints"]
    assert len(wps) > 2
    # FakeGraph: every leg is 0.8 wide -> (0.8 - 0.4 - 0.05) / 2 = 0.175 -> 0.17 per side
    assert all(w.skippable is True and w.corridor_width_m == 0.17 for w in wps[:-1])
    assert wps[-1].skippable is None and wps[-1].corridor_width_m is None
    assert out["node_policy"]["unavailable"] is None
    assert out["node_policy"]["skippable"] == len(wps) - 1


def _wps(*xs, **kw):
    from cloud_common.objects import common
    return [common.Pose2D(x=x, y=0.0, map_id="m", **kw) for x in xs]


@pytest.mark.unit
async def test_enrich_gives_a_user_skippable_waypoint_its_corridor_and_is_idempotent(on):
    svc = _service(FakeStore(FakeGraph()))
    (await svc.get_robot_status("r1")).status.factsheet.width = 0.4
    wps = _wps(1.0, 3.0, 6.0)
    wps[1].skippable = True
    first = await svc.enrich_route("r1", "m", wps)
    assert first["changed"] and first["inserted"] == 0
    assert (first["waypoints"][1].skippable, first["waypoints"][1].corridor_width_m) == (True, 0.17)
    assert first["waypoints"][0].skippable is None          # the operator's others untouched
    again = await svc.enrich_route("r1", "m", first["waypoints"])
    assert not again["changed"]
    assert [w.dict() for w in again["waypoints"]] == [w.dict() for w in first["waypoints"]]


@pytest.mark.unit
async def test_enrich_drops_old_planned_waypoints_and_replans_them(on):
    from cloud_common.objects import common
    svc = _service(FakeStore(FakeGraph()))
    wps = _wps(1.0, 6.0)
    stale = common.Pose2D(x=3.0, y=0.0, map_id="m", origin="planned", skippable=True)
    out = await svc.enrich_route("r1", "m", [wps[0], stale, wps[1]])
    assert [w.x for w in out["waypoints"]] == [1.0, 6.0] and out["changed"]


@pytest.mark.unit
async def test_enrich_without_a_graph_returns_the_route_untouched(on):
    store = FakeStore(FakeGraph())
    store.version = None
    svc = _service(store)
    wps = _wps(1.0, 3.0, 6.0)
    wps[1].skippable = True
    out = await svc.enrich_route("r1", "m", wps)
    assert not out["changed"] and out["inserted"] == 0
    assert out["node_policy"] == {"total": 1, "skippable": 0, "unavailable": "no_scene_graph"}
    assert out["waypoints"][1].skippable is True            # the operator's intent is kept


@pytest.mark.unit
async def test_enrich_with_the_planner_off_changes_nothing(monkeypatch):
    monkeypatch.setattr(config, "SCENE_GRAPH_PLANNER_ENABLED", False)
    svc = _service(FakeStore(FakeGraph()))
    out = await svc.enrich_route("r1", "m", _wps(1.0, 6.0))
    assert not out["changed"] and out["node_policy"]["unavailable"] == "no_scene_graph"


@pytest.mark.unit
async def test_unknown_robot_width_means_not_skippable_and_says_so(on):
    svc = _service(FakeStore(FakeGraph()))
    out = await _plan(svc)
    assert out["success"] and out["node_policy"]["unavailable"] == "robot_width_unknown"
    assert all(w.skippable is False and w.corridor_width_m is None
               for w in out["waypoints"][1:-1])


@pytest.mark.unit
async def test_skip_switch_off_still_plans_the_corridors(on, monkeypatch):
    """The corridor is the robot's gate: only the dispatcher's wire forces skippable false."""
    monkeypatch.setattr(config, "NODE_POLICY_SKIP_ENABLED", False)
    svc = _service(FakeStore(FakeGraph()))
    robot = await svc.get_robot_status("r1")
    robot.status.factsheet.width = 0.4
    out = await _plan(svc)
    assert out["node_policy"]["corridor"]["measured"] == len(out["waypoints"]) - 1
    assert all(w.corridor_width_m == 0.17 for w in out["waypoints"][:-1])


@pytest.mark.unit
async def test_legacy_fallback_reports_the_real_reason_nothing_is_skippable(on):
    store = FakeStore(FakeGraph())
    store.version = None                      # no graph
    svc = _service(store)
    out = await _plan(svc)
    assert out["planner"] == "legacy"
    assert out["node_policy"]["unavailable"] == "no_scene_graph"
    assert out["waypoints"][-1].origin == "user"
    assert all(w.origin == "planned" for w in out["waypoints"][:-1])


@pytest.mark.unit
async def test_enrich_keeps_planned_waypoints_when_it_may_not_replan_them(on):
    from cloud_common.objects import common
    svc = _service(FakeStore(FakeGraph()))
    wps = _wps(1.0, 6.0)
    kept = common.Pose2D(x=3.0, y=0.0, map_id="m", origin="planned", skippable=True)
    out = await svc.enrich_route("r1", "m", [wps[0], kept, wps[1]], insert_planned=False)
    assert [w.x for w in out["waypoints"]] == [1.0, 3.0, 6.0]


@pytest.mark.unit
async def test_finalize_goto_gives_a_legacy_route_origins_and_corridors(on):
    """Step 2.2: a route the legacy planner made gets the same data as a chart one when a graph exists."""
    svc = _service(FakeStore(FakeGraph()))
    robot = await svc.get_robot_status("r1")
    robot.status.factsheet.width = 0.4
    result = {"success": True, "planner": "legacy", "map_id": "m",
              "waypoints": _wps(0.05, 3.0, 6.0, 9.0)}
    await svc._finalize_goto(result, "r1", robot)
    wps = result["waypoints"]
    assert [w.origin for w in wps] == ["planned", "planned", "planned", "user"]
    assert all(w.corridor_width_m == 0.17 for w in wps[:-1])
    assert result["node_policy"]["unavailable"] is None
    assert result["node_policy"]["corridor"]["measured"] == 3
