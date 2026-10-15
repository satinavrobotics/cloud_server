"""The five checks of a route, for one capture and one goal. Pure: the engine is not needed, only the navigable test."""
import numpy as np

from settings import SEG_SAMPLES, SNAP_M


def check_route(route, length, pose, goal, on_nav):
    """the list of failed checks (empty when all pass).

    route: [[x, y], ...] the engine's lattice route (map frame), length: its geometric length (m), pose: (x, y) of the robot
    at this capture, goal: (x, y) of the goal, on_nav(x, y): True when the point is on the engine's navigable mask."""
    fails = []
    d_start = float(np.hypot(route[0][0] - pose[0], route[0][1] - pose[1]))
    d_goal = float(np.hypot(route[-1][0] - goal[0], route[-1][1] - goal[1]))
    if d_start > SNAP_M + 1e-6:
        fails.append("start_snap")
    if d_goal > SNAP_M + 1e-6:
        fails.append("goal_snap")
    if not all(on_nav(p[0], p[1]) for p in route):
        fails.append("nodes_nav")
    if _segments_leave_nav(route, on_nav):
        fails.append("segments_nav")
    straight = float(np.hypot(route[-1][0] - route[0][0], route[-1][1] - route[0][1]))
    if length + 1e-6 < straight:
        fails.append("length")
    return fails


def _segments_leave_nav(route, on_nav):
    for p, q in zip(route[:-1], route[1:]):
        for t in np.linspace(0.0, 1.0, SEG_SAMPLES):
            if not on_nav(p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t):
                return True
    return False
