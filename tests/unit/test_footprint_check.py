"""Robot-vs-map footprint compatibility block of the planner responses."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from packages import config
from packages.services.mission_planner import footprint_check as fc
from packages.services.mission_planner import main as planner_main
from tests.unit.test_planner_chart_route import FakeGraph, FakeStore, _plan, _service


def _graph(r=0.25, chart=2.0):
    return SimpleNamespace(params=SimpleNamespace(robot_half_side=r, chart_radius=chart))


@pytest.mark.unit
def test_ok_when_robot_not_larger():
    out = fc.footprint_check(_graph(), 0.5, 0.8)       # half smaller side 0.25
    assert out["status"] == "ok" and out["robot_half_side_m"] == 0.25
    assert out["map_half_side_m"] == 0.25 and out["chart_radius_m"] == 2.0 and out["message"]


@pytest.mark.unit
def test_tolerance_and_just_over():
    assert fc.footprint_check(_graph(), 0.5 + 1e-9, 0.5 + 1e-9)["status"] == "ok"
    assert fc.footprint_check(_graph(), 0.52, 0.52)["status"] == "larger_than_map"


@pytest.mark.unit
def test_width_alone_when_length_unknown():
    assert fc.footprint_check(_graph(), 0.6, None)["robot_half_side_m"] == 0.3


@pytest.mark.unit
def test_unknown_footprint():
    for w, l in ((None, None), (-1.0, 0.5), (float("nan"), None), (0.0, 0.0)):
        out = fc.footprint_check(_graph(), w, l)
        assert out["status"] == "unknown_footprint" and out["robot_half_side_m"] is None
        assert out["map_half_side_m"] == 0.25


@pytest.mark.unit
def test_exceeds_chart():
    out = fc.footprint_check(_graph(0.25, 1.0), 2.4, 3.0)   # r 1.2 > 1.0
    assert out["status"] == "exceeds_chart" and "1.0" in out["message"]


@pytest.mark.unit
def test_legacy_map_without_params_gives_no_block():
    assert fc.footprint_check(SimpleNamespace(), 0.5, 0.5) is None
    assert fc.footprint_check(SimpleNamespace(params=SimpleNamespace(lattice=0.1)), 0.5, 0.5) is None


@pytest.mark.unit
def test_missing_chart_radius_cannot_exceed():
    g = SimpleNamespace(params=SimpleNamespace(robot_half_side=0.25))
    out = fc.footprint_check(g, 5.0, 5.0)
    assert out["status"] == "larger_than_map" and out["chart_radius_m"] is None


def _svc(width, length, r=0.25, chart=2.0):
    graph = FakeGraph()
    graph.params = SimpleNamespace(lattice=0.1, robot_half_side=r, chart_radius=chart)
    svc = _service(FakeStore(graph))
    fs = svc.get_robot_status.return_value.status.factsheet
    fs.width, fs.length = width, length
    return svc


@pytest.mark.unit
async def test_block_reaches_plan_response(monkeypatch):
    monkeypatch.setattr(config, "SCENE_GRAPH_PLANNER_ENABLED", True)
    svc = _svc(0.9, 0.9)                               # r 0.45 > 0.25
    with patch.object(planner_main, "service", svc):
        resp = await planner_main.plan_only(planner_main.PlanRequest(
            robot_name="r1", target_x=10.0, target_y=0.0, map_id="m"))
    assert resp.success and resp.footprint["status"] == "larger_than_map"
    assert resp.footprint["robot_half_side_m"] == pytest.approx(0.45)


@pytest.mark.unit
async def test_exceeds_chart_refuses_plan(monkeypatch):
    monkeypatch.setattr(config, "SCENE_GRAPH_PLANNER_ENABLED", True)
    svc = _svc(5.0, 5.0, chart=2.0)
    out = await _plan(svc)
    assert out["success"] is False and out["failed_at"] == "footprint"
    assert out["footprint"]["status"] == "exceeds_chart" and out["error"]
    svc.graph_db.shortest_path.assert_not_called()      # no legacy fallback


@pytest.mark.unit
async def test_unknown_footprint_plans_with_warning(monkeypatch):
    monkeypatch.setattr(config, "SCENE_GRAPH_PLANNER_ENABLED", True)
    out = await _plan(_svc(None, None))
    assert out["success"] and out["footprint"]["status"] == "unknown_footprint"
