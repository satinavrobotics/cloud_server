"""Stage B of the edge statistics: the leg tracker records the real plan length (the route node's trajectory) and
the chart of each waypoint (utils/plan_length.py, leg_tracker.py)."""
import datetime
import math
from types import SimpleNamespace

import pytest

import cloud_common.objects as api_objects
from cloud_common.objects import mission as mission_object
from packages.controllers.mission import leg_tracker
from packages.utils import plan_length

pytestmark = pytest.mark.unit
T0 = datetime.datetime(2026, 10, 27, 12, 0, tzinfo=datetime.timezone.utc)
RUN = "abcd1234"


def at(s):
    return T0 + datetime.timedelta(seconds=s)


def wp(x, y, chart=None):
    return {"x": x, "y": y, "theta": 0.0, "map_id": "map1", "chart_key": chart}


def entry(i, j, *pts):
    return {"from_index": i, "to_index": j, "points": [list(p) for p in pts]}


def route(waypoints, trajectory=None, graph_version=None):
    return {"waypoints": waypoints, "trajectory": trajectory, "graph_version": graph_version}


def make_mission(*routes):
    mission = api_objects.MissionObjectV1(
        name="m1", robot="r1", status={}, timeout=600,
        mission_tree=[{"name": f"go{i}", "route": r} for i, r in enumerate(routes)])
    mission.status.run_id = RUN
    return mission


def vda(mission, idx, seq):
    return f"m1-r{RUN}-n{idx}-s{seq}"


def feed(tracker, mission, *steps):
    """steps: (idx, seq, t); the legs completed by the last step."""
    legs = []
    for idx, seq, t in steps:
        legs = tracker.observe(
            message=SimpleNamespace(lastNodeId=vda(mission, idx, seq), orderId=f"m1-r{RUN}-n{idx}",
                                    driving=True),
            mission=mission, ts=at(t), received=at(t),
            limits=SimpleNamespace(speed_max=1.0, acceleration_max=0.5, angular_speed_max=1.0))
    return legs


# 3-4-5: waypoints (0,0) -> (3,0) -> (3,4); the plan goes round by (0,4) / around a corner: longer than the line
WPS = [wp(0, 0, "c1"), wp(3, 0, "c1"), wp(3, 4, "c2")]
TRAJ = [entry(0, 1, (0, 0), (0, 1), (3, 1), (3, 0)), entry(1, 2, (3, 0), (4, 0), (4, 4), (3, 4))]


def test_polyline_of_the_trajectory_entry_is_the_planned_length():
    mission = make_mission(route(WPS, TRAJ, graph_version=7))
    tr = leg_tracker.LegTracker()
    [leg] = feed(tr, mission, (0, 0, 0), (0, 2, 1), (0, 4, 5))
    assert leg.straight_m == 3.0 and leg.planned_m == pytest.approx(1 + 3 + 1)
    assert leg.plan_source == "trajectory" and leg.graph_version == 7
    assert (leg.from_chart, leg.to_chart) == ("c1", "c1")
    # expected_s comes from the planned length: 5 m / 1 m/s + 1 / 0.5 + 0 rad
    assert leg.expected_s == pytest.approx(5.0 + 2.0)
    [leg2] = feed(tr, mission, (0, 6, 9))
    assert leg2.planned_m == pytest.approx(1 + 4 + 1) and leg2.plan_source == "trajectory"
    assert (leg2.from_chart, leg2.to_chart) == ("c1", "c2")


def test_first_leg_has_no_plan_and_no_from_chart():
    mission = make_mission(route(WPS, TRAJ, graph_version=7))
    [leg] = feed(leg_tracker.LegTracker(), mission, (0, 0, 0), (0, 2, 3))
    assert (leg.straight_m, leg.planned_m, leg.expected_s, leg.plan_source) == (None, None, None, None)
    assert leg.from_chart is None and leg.to_chart == "c1" and leg.graph_version == 7


def test_no_trajectory_is_straight_and_charts_may_be_missing():
    mission = make_mission(route([wp(0, 0), wp(3, 0)]))
    [leg] = feed(leg_tracker.LegTracker(), mission, (0, 0, 0), (0, 2, 1), (0, 4, 5))
    assert (leg.planned_m, leg.plan_source) == (3.0, "straight")
    assert (leg.from_chart, leg.to_chart, leg.graph_version) == (None, None, None)


def test_trajectory_of_another_leg_only_is_straight():
    mission = make_mission(route(WPS, [TRAJ[1]]))             # no entry (0, 1)
    tr = leg_tracker.LegTracker()
    [leg] = feed(tr, mission, (0, 0, 0), (0, 2, 1), (0, 4, 5))
    assert (leg.planned_m, leg.plan_source) == (3.0, "straight")
    [leg2] = feed(tr, mission, (0, 6, 9))
    assert leg2.plan_source == "trajectory"


def test_reroute_start_from_the_old_route_is_stale():
    mission = make_mission(route(WPS, TRAJ, graph_version=7))
    tr = leg_tracker.LegTracker()
    feed(tr, mission, (0, 0, 0), (0, 2, 1))
    # the route is rewritten: waypoint 0 moves 1 m, the new trajectory starts there; the robot reached the OLD waypoint 0
    moved = [wp(1, 0, "c9"), wp(3, 0, "c1"), wp(3, 4, "c2")]
    mission.mission_tree[0].route = mission_object.MissionRouteNodeV1(
        **route(moved, [entry(0, 1, (1, 0), (1, 1), (3, 1), (3, 0))], graph_version=8))
    [leg] = feed(tr, mission, (0, 4, 5))
    assert leg.straight_m == 3.0 and leg.planned_m == 3.0 and leg.plan_source == "stale"
    assert leg.expected_s == pytest.approx(3.0 + 2.0)
    assert leg.from_chart == "c1" and leg.graph_version == 8      # the start's chart is the old route's


def test_pose_within_tolerance_still_matches():
    mission = make_mission(route(WPS, TRAJ))
    tr = leg_tracker.LegTracker()
    feed(tr, mission, (0, 0, 0), (0, 2, 1))
    tr.anchor.pose = SimpleNamespace(x=0.03, y=0.0, theta=0.0, map_id="map1")   # < EFFICIENCY_STALE_TOL_M
    [leg] = feed(tr, mission, (0, 4, 5))
    assert leg.plan_source == "trajectory"
    tr.anchor.pose = SimpleNamespace(x=0.0, y=0.5, theta=0.0, map_id="map1")    # far over it
    [leg] = feed(tr, mission, (0, 6, 9))
    assert leg.plan_source == "stale"


def test_leg_across_route_nodes_is_straight_with_the_previous_waypoints_chart():
    a = route([wp(0, 0, "c1"), wp(3, 0, "c1")], [entry(0, 1, (0, 0), (0, 1), (3, 1), (3, 0))], graph_version=3)
    b = route([wp(3, 4, "c5"), wp(6, 4, "c5")], [entry(0, 1, (3, 4), (3, 6), (6, 6), (6, 4))], graph_version=4)
    mission = make_mission(a, b)
    tr = leg_tracker.LegTracker()
    feed(tr, mission, (0, 0, 0), (0, 2, 1), (0, 4, 5))
    feed(tr, mission, (1, 0, 6))                                   # the next route node starts: no leg
    [cross] = feed(tr, mission, (1, 2, 12))
    assert cross.from_vda_node.endswith("-n0-s4") and cross.to_vda_node.endswith("-n1-s2")
    assert cross.plan_source == "straight" and cross.planned_m == pytest.approx(4.0)
    assert (cross.from_chart, cross.to_chart) == ("c1", "c5") and cross.graph_version == 4
    [inner] = feed(tr, mission, (1, 4, 20))
    assert inner.plan_source == "trajectory" and inner.planned_m == pytest.approx(2 + 3 + 2)
    assert inner.graph_version == 4


def test_route_table_carries_trajectory_and_graph_version():
    mission = make_mission(route(WPS, TRAJ, graph_version=7), route([wp(9, 9)]))
    table, planned = leg_tracker.route_table(mission)
    assert planned is None
    assert table[0].offset == 0 and table[1].offset == 3 and table[0].graph_version == 7
    assert set(table[0].legs) == {(0, 1), (1, 2)} and table[1].legs == {} and table[1].graph_version is None


def test_plan_length_helper_rules():
    pts = [[0, 0], [0, 1], [3, 1], [3, 0]]
    assert plan_length.polyline_m(pts) == pytest.approx(5.0) and plan_length.polyline_m([[1, 1]]) == 0.0
    assert plan_length.ends_match(pts, (0, 0), (3.0, 0.02), 0.05)
    assert not plan_length.ends_match(pts, (0, 0), (3.0, 0.2), 0.05)
    assert plan_length.resolve(pts, 3.0, True) == (pytest.approx(5.0), "trajectory")
    assert plan_length.resolve(pts, 3.0, False) == (3.0, "stale")
    assert plan_length.resolve(None, 3.0, False) == (3.0, "straight")
    assert plan_length.resolve(pts, None, True) == (None, None)
    assert plan_length.resolve(None, None, False) == (None, None)
    t = plan_length.parse_trajectory([[0, 0], [3, 0]], [{"from_index": 0, "to_index": 1, "points": pts}])
    assert t.waypoints == [(0.0, 0.0), (3.0, 0.0)] and t.legs == {(0, 1): pts}
    assert plan_length.parse_trajectory([[0, 0]], [{"from_index": 0}]) is None      # malformed
    assert plan_length.parse_trajectory([[0, 0]], []) is None
    assert math.isclose(plan_length.polyline_m([[0, 0], [3, 4]]), 5.0)
