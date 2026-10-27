"""Stage B: the planner's chart keys and graph version travel with the route (Pose2D.chart_key,
MissionRouteNodeV1.graph_version) through the dispatcher, and never reach the robot."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cloud_common.objects import common
from cloud_common.objects import mission as mission_object
from cloud_common.objects import robot
from packages.controllers.mission import order_policy
from packages.controllers.mission import server as mission_server
from packages.controllers.mission.vda5050_types import vda5050_types as v
from tests.unit.test_mission_goto import State, _make_robot, _run_busy_then_goto
from tests.unit.test_node_policy_enrich_dispatch import _enriched, _mission, _start_mission, _wp


@pytest.fixture(autouse=True)
def _policy_on(monkeypatch):
    monkeypatch.setattr(order_policy, "_current", order_policy.OrderPolicy(node_policy_mode="on"))


def _poses(charted=True):
    return [common.Pose2D(x=float(i), y=0.0, map_id="m", node_id=f"n{i}",
                          chart_key=f"c{i}" if charted else None) for i in range(3)]


def _order(route, node_policy):
    robo = robot.RobotObjectV1(name="r1", spec={}, status={})
    return v.VDA5050Order.from_route(route, robo, "m1", 3, node_policy=node_policy,
                                     policy=order_policy.OrderPolicy())


@pytest.mark.unit
@pytest.mark.parametrize("node_policy", [False, True])
def test_the_order_sent_to_the_robot_never_carries_chart_keys_or_the_graph_version(node_policy):
    charted = mission_object.MissionRouteNodeV1(waypoints=_poses(True), graph_version=42)
    bare = mission_object.MissionRouteNodeV1(waypoints=_poses(False))
    assert charted.waypoints[1].chart_key == "c1" and charted.graph_version == 42     # not vacuous
    order = _order(charted, node_policy)
    text = order.json()
    for word in ("chart", "c1", "graph_version", "42"):
        assert word not in text, word
    assert order.dict() == _order(bare, node_policy).dict()


@pytest.mark.unit
def test_the_route_digest_ignores_the_recording_fields():
    base = mission_object.MissionRouteNodeV1(waypoints=_poses(False))
    charted = mission_object.MissionRouteNodeV1(waypoints=_poses(True), graph_version=5)
    assert mission_server._route_digest(base) == mission_server._route_digest(charted)
    moved = mission_object.MissionRouteNodeV1(waypoints=_poses(False)[:2])
    assert mission_server._route_digest(base) != mission_server._route_digest(moved)
    assert mission_server._same_waypoints(base.waypoints, charted.waypoints)
    assert not mission_server._same_waypoints(base.waypoints, moved.waypoints)


@pytest.mark.unit
async def test_the_dispatch_time_replan_keeps_the_charts_and_the_graph_version():
    planner = SimpleNamespace(plan=AsyncMock(return_value={
        "success": True, "planned_path": ["7", "8"], "graph_version": 11,
        "waypoints": [{"x": 51.0, "y": 0.0, "theta": 0.0, "chart_key": "cA"},
                      {"x": 60.0, "y": 0.0, "theta": 0.0, "chart_key": "cB"}]}))
    r, db = _make_robot(planner)
    goto = await _run_busy_then_goto(r, move_to_x=50.0)
    route = goto.mission_tree[0].route
    assert route.graph_version == 11 and [w.chart_key for w in route.waypoints] == ["cA", "cB"]
    stored = db.update_spec_fields.await_args_list[-1].args[2]["mission_tree"][0]["route"]
    assert stored["graph_version"] == 11 and stored["waypoints"][1]["chart_key"] == "cB"
    # the order that went to the robot has none of it
    sent = [c.args[1] for c in r._mqtt_client.publish.call_args_list if c.args[0].endswith("/order")]
    assert sent and not any("cA" in s or "graph_version" in s for s in sent)


@pytest.mark.unit
async def test_an_old_planner_answer_leaves_both_none():
    planner = SimpleNamespace(plan=AsyncMock(return_value={
        "success": True, "planned_path": ["7", "8"],
        "waypoints": [{"x": 51.0, "y": 0.0, "theta": 0.0}, {"x": 60.0, "y": 0.0, "theta": 0.0}]}))
    r, _ = _make_robot(planner)
    goto = await _run_busy_then_goto(r, move_to_x=50.0)
    route = goto.mission_tree[0].route
    assert route.graph_version is None and [w.chart_key for w in route.waypoints] == [None, None]


@pytest.mark.unit
async def test_the_enrich_answer_graph_version_and_charts_are_stored_with_the_route():
    answer = _enriched(_wp(0.0, chart_key="c0"), _wp(10.0, chart_key="c1"), changed=False, inserted=0)
    answer["graph_version"] = 6
    answer["trajectory"] = [{"from_index": 0, "to_index": 1, "points": [[0.0, 0.0], [10.0, 0.0]]}]
    planner = SimpleNamespace(enrich_route=AsyncMock(return_value=answer))
    r, db = _make_robot(planner)
    m = _mission([_wp(0.0), _wp(10.0)])
    await _start_mission(r, m)
    route = m.mission_tree[0].route
    assert route.graph_version == 6 and [w.chart_key for w in route.waypoints] == ["c0", "c1"]
    stored = db.update_spec_fields.await_args.args[2]["mission_tree"][0]["route"]
    assert stored["graph_version"] == 6
    r._cancel_mission_timeout()


@pytest.mark.unit
def test_pose2d_and_route_defaults_are_none_and_survive_a_json_round_trip():
    assert common.Pose2D().chart_key is None and mission_object.MissionRouteNodeV1(waypoints=_poses(False)).graph_version is None
    route = mission_object.MissionRouteNodeV1(waypoints=_poses(True), graph_version=3)
    again = mission_object.MissionRouteNodeV1(**json.loads(route.json()))
    assert again.graph_version == 3 and again.waypoints[2].chart_key == "c2"
