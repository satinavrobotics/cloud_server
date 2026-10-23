"""Step 1.5 of docs/node_policies_v2_plan.md: an order leaves out the leading waypoints the robot
has already passed (within allowedDeviationXY, or past the node's gate inside its corridor)."""
import time

import pytest

from cloud_common.objects import common
from packages.controllers.mission import order_start as os_
from tests.unit.test_mission_lifecycle_fixes import _make_robot, _mission, _route, _tree

DEV = 0.35


def _wps(points, corridor=None):
    return [common.Pose2D(x=x, y=y, corridor_width_m=corridor) for x, y in points]


@pytest.mark.unit
def test_a_waypoint_under_the_robot_is_passed():
    assert os_.passed_prefix(_wps([(0, 0), (4, 0), (8, 0)]), 0.2, 0.1, DEV) == 1


@pytest.mark.unit
def test_a_waypoint_behind_the_robot_is_passed_inside_its_corridor_not_outside():
    wps = _wps([(0, 0), (4, 0), (8, 0)], corridor=0.5)
    assert os_.passed_prefix(wps, 1.5, 0.3, DEV) == 1      # crossed the gate of wp0, 0.3 off the line
    assert os_.passed_prefix(wps, 1.5, 0.6, DEV) == 0      # room is 0.5 - 0.10 = 0.4 < 0.6


@pytest.mark.unit
def test_a_waypoint_ahead_of_the_robot_is_not_passed():
    assert os_.passed_prefix(_wps([(5, 0), (9, 0), (12, 0)]), 0.0, 0.0, DEV) == 0


@pytest.mark.unit
def test_zero_corridor_means_distance_only_and_missing_means_the_default():
    behind = (1.5, 0.0)
    assert os_.passed_prefix(_wps([(0, 0), (4, 0), (8, 0)], corridor=0.0), *behind, DEV) == 0
    assert os_.passed_prefix(_wps([(0, 0), (4, 0), (8, 0)], corridor=None), 1.5, 0.6, DEV) == 1


@pytest.mark.unit
def test_checked_in_sequence_stopping_at_the_first_waypoint_not_passed():
    # wp1 is ahead; wp2 would count as "behind" the robot but is never reached by the check
    wps = _wps([(0, 0), (6, 0), (-3, 0), (-6, 0)])
    assert os_.passed_prefix(wps, 1.0, 0.0, DEV) == 1


@pytest.mark.unit
def test_the_last_waypoint_is_never_dropped_and_start_is_respected():
    wps = _wps([(0, 0), (0.1, 0)])
    assert os_.passed_prefix(wps, 0.0, 0.0, DEV) == 1
    assert os_.passed_prefix(_wps([(0, 0)]), 0.0, 0.0, DEV) == 0
    assert os_.passed_prefix(_wps([(0, 0), (4, 0), (8, 0)]), 0.0, 0.0, DEV, start=1) == 1


@pytest.mark.unit
def test_the_gate_of_a_corner_is_the_bisector():
    # route turns 90 degrees at (4, 0): a robot at (4.3, -1.0) (before the corner, beside the
    # outgoing leg's start) has not crossed the bisector gate yet
    wps = _wps([(0, 0), (4, 0), (4, 6)], corridor=1.0)
    assert os_.is_passed(wps, 1, 4.3, 1.0, DEV) is True    # past the corner, inside the corridor
    assert os_.is_passed(wps, 1, 3.0, 0.0, DEV) is False   # still before it


async def _robot_with_pose(n=4, pose=(0.0, 1.0), fresh=True, initialized=True):
    r, _ = _make_robot()
    m = _mission(tree=_tree(_route("a", n=n)))
    r._current_mission = m
    r._robot_object.status.pose.x, r._robot_object.status.pose.y = pose
    r._robot_object.status.position_initialized = initialized
    r._pose_at = time.monotonic() - (0 if fresh else 60)
    return r, m


@pytest.mark.unit
async def test_the_dispatcher_drops_waypoints_the_robot_is_past_and_marks_them_reached():
    r, m = await _robot_with_pose(pose=(1.1, 1.0))         # on top of waypoint 1 (x = 1)
    r._robot_object.status.pose.map_id = ""
    node = m.mission_tree[1]
    built = await r._build_order(node, 1, 0)
    order, record = built
    assert record.waypoint_offset == 2                      # waypoints 0 (behind) and 1 (here)
    assert [n.nodePosition.x for n in order.nodes[1:]] == [2.0, 3.0]
    r._mark_skipped_waypoints_reached(node, 0, record)
    assert m.status.task_status == {"a": 1}


@pytest.mark.unit
@pytest.mark.parametrize("fresh, initialized", [(False, True), (True, False)])
async def test_a_stale_or_uninitialized_pose_drops_nothing(fresh, initialized):
    r, m = await _robot_with_pose(pose=(1.1, 1.0), fresh=fresh, initialized=initialized)
    _, record = await r._build_order(m.mission_tree[1], 1, 0)
    assert record.waypoint_offset == 0
