"""packages/utils/blocked_geometry.py: segment-versus-disc rules for blocked reports."""
import math

from packages.utils import blocked_geometry as bg


def row(**kw):
    base = {"x": 10.0, "y": 0.0, "robot_x": 0.0, "robot_y": 0.0}
    base.update(kw)
    return base


def test_point_segment_distance():
    assert bg.point_segment_distance((5, 3), (0, 0), (10, 0)) == 3.0
    assert bg.point_segment_distance((-4, 3), (0, 0), (10, 0)) == 5.0     # clamps to the end
    assert bg.point_segment_distance((1, 1), (2, 2), (2, 2)) == math.hypot(1, 1)  # degenerate


def test_segment_distance_crossing_parallel_and_apart():
    assert bg.segment_segment_distance((0, 0), (4, 4), (0, 4), (4, 0)) == 0.0
    assert bg.segment_segment_distance((0, 0), (4, 0), (0, 2), (4, 2)) == 2.0
    assert bg.segment_segment_distance((0, 0), (1, 0), (3, 0), (4, 0)) == 2.0   # collinear, apart


def test_row_segment_starts_ahead_of_robot_and_clips():
    assert bg.row_segment(row(), lead_m=0.5) == ((0.5, 0.0), (10.0, 0.0))
    assert bg.row_segment(row(x=0.2), lead_m=0.5) == ((0.2, 0.0), (0.2, 0.0))   # shorter than the lead
    assert bg.LEAD_M > bg.HALF_WIDTH_M
    assert bg.row_segment({"x": 1.0, "y": 1.0}) is None                          # legacy row


def test_a_long_leg_through_the_obstacle_is_hit_but_waypoints_alone_miss_it():
    r = row()
    leg = [(-5.0, 0.2), (20.0, 0.2)]                    # passes the obstacle, waypoints far off both ends
    assert bg.route_hits_row(r, leg)
    assert not bg.route_hits_row(r, [(-5.0, 3.0), (20.0, 3.0)])       # a detour 3 m aside


def test_detour_leaving_from_robot_position_is_not_blocked_by_its_own_start():
    r = row()
    detour = [(0.0, 0.0), (0.0, 4.0), (10.0, 4.0)]
    assert not bg.route_hits_row(r, detour)


def test_single_point_route_and_empty():
    assert bg.route_hits_row(row(), [(5.0, 0.3)])
    assert not bg.route_hits_row(row(), [])
    assert not bg.route_hits_row({"x": 1.0, "y": 1.0}, [(1.0, 1.0)])   # legacy: the disc rule decides


def test_overlay_splits_segments_and_discs():
    legacy = {"x": 3.0, "y": 4.0}
    nodeonly = {"graph_node_id": "n"}
    discs, segs = bg.overlay([row(), legacy, nodeonly], disc_radius_m=0.5)
    assert discs == [(3.0, 4.0, 0.5)]
    assert segs == [((bg.LEAD_M, 0.0), (10.0, 0.0), bg.HALF_WIDTH_M)]
