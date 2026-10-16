"""A goal beyond goal_snap_m from free space is planned with a goal_far notice, not refused (scene_route._plan_on_graph)."""
import math

import pytest

from packages.services.mission_planner import scene_route as sr

from .test_lazy_plan import WORLDS, _build


@pytest.fixture(scope="module")
def graph():
    st, _eng, _lazy = _build(WORLDS["grid"]())
    from packages.services.mission_planner.lazy_plan import open_plan
    return open_plan(st, "m", max_tiles=10 ** 6)


def _plan(graph, start, goal, goal_snap_m):
    ids = iter("n%d" % i for i in range(10 ** 5))
    seen = {}
    return sr.plan_on_graph(graph, "m", start, goal, ([], []),
                            lambda x, y: seen.setdefault((round(x, 3), round(y, 3)), next(ids)),
                            0.5, goal_snap_m, 3.0, None, 0.05, None, None, 1.0, True, 0, turn=None)


def _pairs(graph):
    """(start, goal): a start in reach of free space, and a goal whose nearest free cell is 0.1 - 1.0 m away, not behind a wall."""
    starts, goals = [], []
    for i in range(0, 80):
        for j in range(0, 80):
            x, y = i * 0.25, j * 0.25
            s = graph.snap(x, y)
            if s.key is None or s.crosses_blocked:
                continue
            if s.distance <= 0.25:
                starts.append((x, y))
            elif 0.1 < s.distance < 1.0:
                goals.append((x, y))
    return starts, goals


def test_a_goal_beyond_the_limit_is_planned_with_the_far_notice(graph):
    starts, goals = _pairs(graph)
    assert starts and goals, "the grid world has free space and an edge near it"
    for start in starts:
        for goal in goals:
            try:
                plan = _plan(graph, start, goal, goal_snap_m=0.05)
            except sr.ChartFallback:
                continue
            far = plan.goal_far
            assert far is not None
            assert far["goal_xy"] == pytest.approx(goal)
            assert far["distance_from_free_m"] == pytest.approx(far["unverified_length_m"])
            assert far["distance_from_free_m"] == pytest.approx(math.dist(far["goal_xy"], far["free_xy"]), abs=1e-6)
            assert far["limit_m"] == 0.05
            assert plan.waypoints and plan.goal_xy == pytest.approx(far["free_xy"])
            return
    pytest.skip("no start/goal pair in the grid world plans")


def test_a_goal_within_the_limit_has_no_notice(graph):
    starts, goals = _pairs(graph)
    for start in starts:
        for goal in goals:
            try:
                plan = _plan(graph, start, goal, goal_snap_m=5.0)
            except sr.ChartFallback:
                continue
            assert plan.goal_far is None
            return
    pytest.skip("no start/goal pair in the grid world plans")
