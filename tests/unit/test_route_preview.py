"""Dry-run route preview (docs/route_preview_2026-10-14.md): planner leg issues, the API module and the route."""
import math
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from packages import config
from packages.api import main as api_main
from packages.api import route_preview
from tests.unit.test_node_policy_plan_server import _wps
from tests.unit.test_planner_chart_route import FakeGraph, FakeStore, _service, on  # noqa: F401


# ---- planner: leg issues ------------------------------------------------------------------------
@pytest.mark.unit
async def test_enrich_reports_no_issues_for_routable_legs(on):
    out = await _service(FakeStore(FakeGraph())).enrich_route("r1", "m", _wps(1.0, 3.0, 6.0))
    assert out["issues"] == []


@pytest.mark.unit
async def test_enrich_reports_an_unreachable_leg(on):
    graph = FakeGraph()
    graph.unreachable = True
    out = await _service(FakeStore(graph)).enrich_route("r1", "m", _wps(1.0, 3.0, 6.0))
    assert out["issues"] == [{"from_index": 0, "to_index": 1, "reason": "unreachable"},
                             {"from_index": 1, "to_index": 2, "reason": "unreachable"}]
    assert not out.get("trajectory")


@pytest.mark.unit
async def test_enrich_reports_a_leg_outside_the_graph_and_a_route_error(on):
    wps = _wps(1.0, 3.0, 6.0)
    wps[1].y = 5.0                                    # does not snap
    out = await _service(FakeStore(FakeGraph())).enrich_route("r1", "m", wps)
    assert {(i["from_index"], i["reason"]) for i in out["issues"]} == {(0, "outside_graph"), (1, "outside_graph")}
    graph = FakeGraph()
    graph.route_error = RuntimeError("tiles")
    out = await _service(FakeStore(graph)).enrich_route("r1", "m", _wps(1.0, 6.0))
    assert out["issues"] == [{"from_index": 0, "to_index": 1, "reason": "error"}]


@pytest.mark.unit
async def test_enrich_without_a_graph_has_no_issues_key(on):
    store = FakeStore(FakeGraph())
    store.version = None
    assert "issues" not in await _service(store).enrich_route("r1", "m", _wps(1.0, 6.0))


# ---- API module --------------------------------------------------------------------------------
def _planner(result=None, error=None):
    fn = AsyncMock(side_effect=error) if error else AsyncMock(return_value=result)
    return SimpleNamespace(enrich_route=fn)


def _ok(waypoints, **extra):
    return {"waypoints": waypoints, "changed": True, "inserted": 0, "graph_version": 7,
            "node_policy": {"total": 0, "skippable": 0, "unavailable": None}, **extra}


RAW = [{"x": 0.0, "y": 0.0, "theta": 0.0}, {"x": 5.0, "y": 0.0, "theta": 0.0}, {"x": 5.0, "y": 4.0, "theta": 1.0}]


@pytest.mark.unit
def test_return_mirrors_the_route_without_repeating_the_turning_point():
    poses = route_preview.build_route("m", RAW, True)
    assert [(p.x, p.y) for p in poses] == [(0, 0), (5, 0), (5, 4), (5, 0), (0, 0)]
    assert math.isclose(abs(poses[3].theta), math.pi) and math.isclose(abs(poses[4].theta), math.pi)
    assert all(p.map_id == "m" for p in poses)
    assert len(route_preview.build_route("m", RAW, False)) == 3


@pytest.mark.unit
def test_planned_waypoints_are_dropped_and_invalid_input_refused():
    raw = [RAW[0], {"x": 2.0, "y": 0.0, "origin": "planned"}, RAW[1]]
    assert [p.x for p in route_preview.build_route("m", raw, False)] == [0.0, 5.0]
    for args, status in [(("", RAW), 400), (("m", []), 400), (("m", [{"x": "a"}]), 400),
                         (("m", [{"x": 1, "y": 1, "origin": "bogus"}]), 400),
                         (("m", [{"x": float("inf"), "y": 0}]), 400),
                         (("m", [{"x": 0, "y": 0}] * (config.ROUTE_PREVIEW_MAX_WAYPOINTS + 1)), 413)]:
        with pytest.raises(route_preview.PreviewError) as err:
            route_preview.build_route(args[0], args[1], False)
        assert err.value.status == status


@pytest.mark.unit
async def test_preview_passes_the_planner_answer_through():
    legs = [{"from_index": 0, "to_index": 1, "points": [[0, 0], [1, 0]]}]
    issues = [{"from_index": 1, "to_index": 2, "reason": "unreachable"}]
    planner = _planner(_ok([dict(w, origin="planned") for w in RAW], trajectory=legs, issues=issues))
    out = await route_preview.preview(planner, "m", RAW, False, "r1")
    assert (out["planner"], out["reason"], out["trajectory"], out["issues"]) == ("scene_graph", None, legs, issues)
    assert out["graph_version"] == 7
    args = planner.enrich_route.await_args
    assert args.args[:2] == ("r1", "m") and args.kwargs["timeout"] == config.NODE_POLICY_ENRICH_TIMEOUT_S
    assert [w["map_id"] for w in args.args[2]] == ["m"] * 3


@pytest.mark.unit
async def test_preview_never_fails_for_a_planner_that_is_down_or_has_no_graph():
    out = await route_preview.preview(_planner(error=RuntimeError("down")), "m", RAW)
    assert (out["planner"], out["reason"], out["trajectory"], out["issues"]) == \
        ("none", "planner_unavailable", [], [])
    assert len(out["waypoints"]) == 3
    assert (await route_preview.preview(None, "m", RAW))["planner"] == "none"
    nograph = {"waypoints": RAW, "changed": False, "inserted": 0,
               "node_policy": {"total": 0, "skippable": 0, "unavailable": "no_scene_graph"}}
    out = await route_preview.preview(_planner(nograph), "m", RAW)
    assert (out["planner"], out["reason"]) == ("none", "no_scene_graph")


# ---- the route ---------------------------------------------------------------------------------
@pytest.fixture
def client(monkeypatch):
    planner = _planner(_ok(RAW, issues=[]))
    monkeypatch.setattr(api_main, "service", SimpleNamespace(mission_planner=planner))
    return TestClient(api_main.app), planner


@pytest.mark.unit
def test_route_answers_200_with_the_documented_shape(client):
    c, planner = client
    r = c.post("/api/v1/route/preview", json={"map_id": "m", "waypoints": RAW, "return": True, "robot_name": "r1"})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"waypoints", "trajectory", "node_policy", "issues", "planner", "reason", "graph_version"}
    assert body["planner"] == "scene_graph"
    assert len(planner.enrich_route.await_args.args[2]) == 5          # outbound + mirrored


@pytest.mark.unit
def test_route_refuses_bad_requests_and_touches_nothing(client):
    c, planner = client
    assert c.post("/api/v1/route/preview", json={"map_id": "m", "waypoints": []}).status_code == 400
    assert c.post("/api/v1/route/preview", json={"waypoints": RAW}).status_code == 422
    big = [{"x": 0, "y": 0}] * (config.ROUTE_PREVIEW_MAX_WAYPOINTS + 1)
    assert c.post("/api/v1/route/preview", json={"map_id": "m", "waypoints": big}).status_code == 413
    planner.enrich_route.assert_not_awaited()


# ---- reroute: corridors measured again ------------------------------------------------------------
def _route(*xs, **kw):
    return {"waypoints": [{"x": x, "y": 0.0, "theta": 0.0, "map_id": "m", **kw} for x in xs]}


@pytest.mark.unit
async def test_a_reroute_gets_its_corridors_measured_without_replanning():
    planner = SimpleNamespace(enrich_route=AsyncMock(side_effect=lambda r, m, wps, **kw: {
        "waypoints": [{**w, "corridor_width_m": 0.4, "corridor_source": "measured"} for w in wps]}))
    out = await route_preview.measure_reroute(planner, "r1", {"n1": _route(0, 3, 6)})
    assert [w["corridor_width_m"] for w in out["n1"]["waypoints"]] == [0.4] * 3
    args, kwargs = planner.enrich_route.call_args
    assert args[0] == "r1" and args[1] == "m" and kwargs["insert_planned"] is False


@pytest.mark.unit
async def test_a_reroute_is_stored_as_given_when_nothing_can_be_measured():
    given = {"n1": _route(0, 3, 6, corridor_width_m=0.9, corridor_source="measured")}
    down = SimpleNamespace(enrich_route=AsyncMock(side_effect=RuntimeError("down")))
    assert await route_preview.measure_reroute(down, "r1", given) == given
    assert await route_preview.measure_reroute(None, "r1", given) == given
    wrong_count = SimpleNamespace(enrich_route=AsyncMock(return_value={"waypoints": []}))
    assert await route_preview.measure_reroute(wrong_count, "r1", given) == given


@pytest.mark.unit
async def test_a_reroute_without_one_map_is_not_sent_to_the_planner():
    planner = SimpleNamespace(enrich_route=AsyncMock())
    mixed = _route(0, 3)
    mixed["waypoints"][1]["map_id"] = "other"
    mapless = _route(0, 3, map_id="")
    out = await route_preview.measure_reroute(planner, "r1", {"a": mixed, "b": mapless})
    planner.enrich_route.assert_not_called()
    assert out["a"] == mixed and out["b"] == mapless
