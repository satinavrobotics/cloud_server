"""A route's `trajectory` is display data: it never reaches the VDA5050 order the robot receives
(docs/plan_trajectory_2026-10-14.md). The order has the start node plus one node per waypoint and one edge per
route leg, whatever dense path the planner attached."""
import pytest

from cloud_common.objects import common, mission, robot
from packages.controllers.mission import order_policy
from packages.controllers.mission.vda5050_types import vda5050_types as v

WAYPOINTS = [(0.0, 0.0), (4.0, 0.0), (4.0, 3.0)]
# interior points off the straight line between the waypoints, so a node built from one of them cannot be mistaken
# for a waypoint; the end points sit on the waypoints (the route drops a leg whose ends do not)
LEGS = [
    ((0, 1), [[0.0, 0.0], [1.0, 0.6], [2.0, 0.9], [3.0, 0.6], [4.0, 0.0]]),
    ((1, 2), [[4.0, 0.0], [4.4, 1.0], [4.4, 2.0], [4.0, 3.0]]),
]
INTERIOR = {(p[0], p[1]) for _, pts in LEGS for p in pts[1:-1]}


def _waypoints():
    return [common.Pose2D(x=x, y=y, map_id="m") for x, y in WAYPOINTS]


def _route(with_trajectory):
    trajectory = [mission.RouteTrajectoryLegV1(from_index=i, to_index=j, points=pts) for (i, j), pts in LEGS] \
        if with_trajectory else None
    return mission.MissionRouteNodeV1(waypoints=_waypoints(), trajectory=trajectory)


def _order(route, node_policy):
    robo = robot.RobotObjectV1(name="r1", spec={}, status={})
    return v.VDA5050Order.from_route(route, robo, "m1", 3, node_policy=node_policy,
                                     policy=order_policy.OrderPolicy())


@pytest.mark.unit
def test_the_route_keeps_its_trajectory_so_the_checks_below_are_not_vacuous():
    route = _route(True)
    assert route.trajectory is not None and len(route.trajectory) == 2
    assert route.size == len(WAYPOINTS)


@pytest.mark.unit
@pytest.mark.parametrize("node_policy", [False, True])
def test_the_order_has_only_the_start_node_and_the_waypoint_nodes(node_policy):
    route = _route(True)
    order = _order(route, node_policy)
    assert len(order.nodes) == 1 + len(route.waypoints)
    assert len(order.edges) == route.size
    start = order.nodes[0].nodePosition
    allowed = {(x, y) for x, y in WAYPOINTS} | {(start.x, start.y)}
    positions = [(n.nodePosition.x, n.nodePosition.y) for n in order.nodes]
    assert all(p in allowed for p in positions)
    assert not any(p in INTERIOR for p in positions)
    assert positions[1:] == WAYPOINTS                      # the route's waypoints, in order, nothing between them


@pytest.mark.unit
@pytest.mark.parametrize("node_policy", [False, True])
def test_the_serialised_order_never_mentions_a_trajectory(node_policy):
    text = _order(_route(True), node_policy).json()
    assert "trajectory" not in text.lower()


@pytest.mark.unit
@pytest.mark.parametrize("node_policy", [False, True])
def test_the_order_is_identical_with_and_without_the_trajectory(node_policy):
    with_traj = _order(_route(True), node_policy)
    without = _order(_route(False), node_policy)
    assert with_traj.dict() == without.dict()
