"""plan_route (chart planner) carries the nodePolicy report and marks the go-here waypoints."""
import pytest

from packages import config
from tests.unit.test_planner_chart_route import FakeGraph, FakeStore, _plan, _service, on  # noqa: F401


@pytest.mark.unit
async def test_plan_carries_the_node_policy_report_and_marks_interior_waypoints(on):
    svc = _service(FakeStore(FakeGraph()))
    robot = await svc.get_robot_status("r1")
    robot.status.factsheet.width = 0.4
    out = await _plan(svc)
    wps = out["waypoints"]
    assert len(wps) > 2
    # FakeGraph: every leg is 0.8 wide -> (0.8 - 0.4 - 0.05) / 2 = 0.175 -> 0.17 per side
    assert all(w.skippable is True and w.corridor_width_m == 0.17 for w in wps[1:-1])
    assert wps[0].skippable is None and wps[-1].skippable is None
    assert out["node_policy"]["unavailable"] is None
    assert out["node_policy"]["skippable"] == len(wps) - 2


@pytest.mark.unit
async def test_unknown_robot_width_means_not_skippable_and_says_so(on):
    svc = _service(FakeStore(FakeGraph()))
    out = await _plan(svc)
    assert out["success"] and out["node_policy"]["unavailable"] == "robot_width_unknown"
    assert all(w.skippable is False and w.corridor_width_m is None
               for w in out["waypoints"][1:-1])


@pytest.mark.unit
async def test_skip_switch_off_leaves_the_plan_without_policy(on, monkeypatch):
    monkeypatch.setattr(config, "NODE_POLICY_SKIP_ENABLED", False)
    svc = _service(FakeStore(FakeGraph()))
    out = await _plan(svc)
    assert "node_policy" not in out
    assert all(w.skippable is None for w in out["waypoints"])


@pytest.mark.unit
async def test_legacy_fallback_reports_why_nothing_is_skippable(on):
    store = FakeStore(FakeGraph())
    store.version = None                      # no graph
    svc = _service(store)
    out = await _plan(svc)
    assert out["planner"] == "legacy"
    assert out["node_policy"] == {"total": 0, "skippable": 0, "unavailable": "legacy_planner"}
