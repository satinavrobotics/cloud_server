"""The planner's `goal_far` block reaches the plan / navigate responses; a null unverified_length_m stays null."""
import json
from unittest.mock import patch

import pytest

from packages import config
from packages.api import main as api_main
from packages.services.mission_planner import main as planner_main
from tests.unit.test_footprint_check import _svc

FAR = {"goal_xy": (10.0, 0.0), "free_xy": (4.0, 0.0), "distance_from_free_m": 6.0,
       "unverified_length_m": None, "limit_m": 2.0}
BASE = {"success": True, "robot_name": "r1", "target": {"x": 10.0, "y": 0.0}}
MODELS = [planner_main.PlanResponse, planner_main.NavigationResponse, api_main.NavigationResponse]


@pytest.mark.unit
@pytest.mark.parametrize("model", MODELS)
def test_goal_far_null_unverified_length_round_trips_as_null(model):
    resp = model(**BASE, goal_far=dict(FAR))
    assert resp.goal_far["unverified_length_m"] is None
    out = json.loads(resp.json())["goal_far"]
    assert out["unverified_length_m"] is None and "unverified_length_m" in out
    assert out["goal_xy"] == [10.0, 0.0] and out["free_xy"] == [4.0, 0.0]
    assert out["distance_from_free_m"] == 6.0 and out["limit_m"] == 2.0


@pytest.mark.unit
@pytest.mark.parametrize("model", MODELS)
def test_goal_far_zero_and_value_preserved(model):
    for v in (0.0, 3.5):
        resp = model(**BASE, goal_far={**FAR, "unverified_length_m": v})
        assert json.loads(resp.json())["goal_far"]["unverified_length_m"] == v


@pytest.mark.unit
@pytest.mark.parametrize("model", MODELS)
def test_absent_goal_far_stays_none(model):
    resp = model(**BASE)
    assert resp.goal_far is None
    assert json.loads(resp.json())["goal_far"] is None


@pytest.mark.unit
async def test_plan_only_passes_goal_far(monkeypatch):
    monkeypatch.setattr(config, "SCENE_GRAPH_PLANNER_ENABLED", True)
    svc = _svc(0.5, 0.5)
    real = svc.plan_route

    async def with_far(*a, **k):
        out = await real(*a, **k)
        out["goal_far"] = dict(FAR)
        return out
    svc.plan_route = with_far
    with patch.object(planner_main, "service", svc):
        resp = await planner_main.plan_only(planner_main.PlanRequest(
            robot_name="r1", target_x=10.0, target_y=0.0, map_id="m"))
    assert resp.success and resp.goal_far["unverified_length_m"] is None
    assert resp.goal_far["limit_m"] == 2.0


@pytest.mark.unit
async def test_plan_only_without_goal_far(monkeypatch):
    monkeypatch.setattr(config, "SCENE_GRAPH_PLANNER_ENABLED", True)
    with patch.object(planner_main, "service", _svc(0.5, 0.5)):
        resp = await planner_main.plan_only(planner_main.PlanRequest(
            robot_name="r1", target_x=10.0, target_y=0.0, map_id="m"))
    assert resp.success and resp.goal_far is None
