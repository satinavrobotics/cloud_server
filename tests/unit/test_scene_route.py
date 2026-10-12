"""packages/services/mission_planner/scene_route.py: lattice route -> plain waypoint poses."""
import math

from packages.services.mission_planner import scene_route as sr


def always(a, b):
    return True


def test_simplify_straight_line_keeps_ends():
    pts = [(float(i), 0.0) for i in range(11)]
    assert sr.simplify(pts, always, 0) == [(0.0, 0.0), (10.0, 0.0)]


def test_simplify_gap_cap_splits_long_segments():
    pts = [(float(i), 0.0) for i in range(11)]
    out = sr.simplify(pts, always, 4.0)
    assert out[0] == (0.0, 0.0) and out[-1] == (10.0, 0.0)
    assert all(math.dist(a, b) <= 4.0 for a, b in zip(out, out[1:]))
    assert len(out) == 4


def test_simplify_respects_line_of_sight():
    pts = [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0), (2.0, 1.0), (2.0, 2.0)]
    corner_free = lambda a, b: a[0] == b[0] or a[1] == b[1]   # no diagonals
    assert sr.simplify(pts, corner_free, 0) == [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0)]


def test_simplify_short_inputs_unchanged():
    assert sr.simplify([], always, 1) == []
    assert sr.simplify([(0.0, 0.0)], always, 1) == [(0.0, 0.0)]
    two = [(0.0, 0.0), (9.0, 0.0)]
    assert sr.simplify(two, always, 1) == two


def test_build_waypoints_bearing_goal_heading_and_ids():
    pts = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)]
    ids = {(0.0, 0.0): "a", (1.0, 0.0): "b", (1.0, 1.0): "c"}
    poses, planned = sr.build_waypoints(pts, "m1", lambda x, y: ids[(x, y)], goal_theta=1.5)
    assert planned == ["a", "b", "c"]
    assert [round(p.theta, 3) for p in poses] == [0.0, round(math.pi / 2, 3), 1.5]
    assert all(p.map_id == "m1" for p in poses)
    assert [p.node_id for p in poses] == ["a", "b", "c"]


def test_planned_path_none_when_any_waypoint_has_no_leaf():
    pts = [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0)]
    poses, planned = sr.build_waypoints(pts, "m", lambda x, y: None if x == 1.0 else "n")
    assert planned is None
    assert [p.node_id for p in poses] == ["n", None, "n"]


def test_last_waypoint_keeps_previous_bearing_without_goal_heading():
    poses, _ = sr.build_waypoints([(0.0, 0.0), (0.0, 2.0)], "m", lambda x, y: None)
    assert poses[1].theta == poses[0].theta


def test_single_point_and_empty():
    poses, planned = sr.build_waypoints([(3.0, 4.0)], "m", lambda x, y: "n")
    assert len(poses) == 1 and poses[0].theta == 0.0 and planned == ["n"]
    assert sr.build_waypoints([], "m", lambda x, y: "n") == ([], None)


def test_simplify_drops_duplicates_and_scales_with_window():
    pts = [(0.0, 0.0), (0.0, 0.0), (1.0, 0.0)]
    assert sr.simplify(pts, always, 0) == [(0.0, 0.0), (1.0, 0.0)]
    calls = []

    def counting(a, b):
        calls.append(1)
        return True
    long_route = [(i * 0.1, 0.0) for i in range(5000)]
    out = sr.simplify(long_route, counting, 5.0)
    assert len(out) > 90 and len(calls) < 5000 * 60      # windowed, not 5000 x 5000 / 2


def test_planned_path_none_when_two_waypoints_share_a_leaf():
    poses, planned = sr.build_waypoints([(0.0, 0.0), (1.0, 0.0)], "m", lambda x, y: "same")
    assert len(poses) == 2 and planned is None


def snap(key="a:1", d=0.2, crosses=False):
    from types import SimpleNamespace
    return SimpleNamespace(key=key, distance=d, crosses_blocked=crosses)


def test_snap_problem_rules():
    assert sr.snap_problem(snap(), 0.5) is None
    assert sr.snap_problem(snap(d=0.9), 0.5) == sr.SNAP_TOO_FAR
    assert sr.snap_problem(snap(key=None, d=0.9), 1.5) == sr.SNAP_TOO_FAR      # key None beyond max_dist
    assert sr.snap_problem(snap(key=None, d=float("inf")), 1.5) == sr.SNAP_NO_SPACE
    assert sr.snap_problem(snap(d=0.3, crosses=True), 0.5) == sr.SNAP_CROSSES_BLOCKED
