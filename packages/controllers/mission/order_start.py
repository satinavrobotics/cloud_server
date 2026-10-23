"""Where a route order starts (docs/node_policies_v2_plan.md, step 1.5): the waypoints the
robot has already passed are left out of a new order, so it never drives back to a point
beside or behind it. The rule mirrors the robot's own (agreed 2026-10-23): a pass-through node
counts as traversed when the robot is within allowedDeviationXY of it, or has crossed its gate
(a line across the route, on the bisector at a corner, as wide as the node's corridor) inside
the corridor. Pure functions over waypoints in the robot's frame."""
import math
from typing import Optional, Sequence, Tuple

from cloud_common.objects import common
from packages.controllers.mission.order_policy import NODE_POLICY_CORRIDOR_DEFAULT_M, \
    NODE_POLICY_LOCALISATION_MARGIN_M


def _unit(dx: float, dy: float) -> Optional[Tuple[float, float]]:
    length = math.hypot(dx, dy)
    return (dx / length, dy / length) if length > 1e-9 else None


def _gate_normal(waypoints: Sequence[common.Pose2D], j: int) -> Optional[Tuple[float, float]]:
    """The direction of travel through waypoint j: the bisector of the legs j-1 -> j and
    j -> j+1 (the second alone for the first waypoint, which has no leg before it)."""
    here, after = waypoints[j], waypoints[j + 1]
    out = _unit(after.x - here.x, after.y - here.y)
    if out is None:
        return None
    if j == 0:
        return out
    inn = _unit(here.x - waypoints[j - 1].x, here.y - waypoints[j - 1].y)
    if inn is None:
        return out
    # A U-turn has no bisector: no gate, the distance rule only (the safe side).
    return _unit(inn[0] + out[0], inn[1] + out[1])


def is_passed(waypoints: Sequence[common.Pose2D], j: int, x: float, y: float,
              deviation_xy_m: float) -> bool:
    """Whether the robot at (x, y) has traversed waypoint j (never the last one)."""
    here = waypoints[j]
    if math.hypot(x - here.x, y - here.y) <= deviation_xy_m:
        return True
    corridor = here.corridor_width_m
    corridor = NODE_POLICY_CORRIDOR_DEFAULT_M if corridor is None else corridor
    room = corridor - NODE_POLICY_LOCALISATION_MARGIN_M
    if not math.isfinite(room) or room <= 0:
        return False                      # measured, no room: the distance rule only
    normal = _gate_normal(waypoints, j)
    if normal is None:
        return False
    dx, dy = x - here.x, y - here.y
    along = dx * normal[0] + dy * normal[1]
    side = abs(dx * normal[1] - dy * normal[0])
    return along > 0 and side <= room


def passed_prefix(waypoints: Sequence[common.Pose2D], x: float, y: float,
                  deviation_xy_m: float, start: int = 0) -> int:
    """The index of the first waypoint, from `start` on, the robot has not passed: checked
    strictly in sequence, stopping at the first one not passed. The last waypoint is never
    passed (the order always has somewhere to go)."""
    j = max(start, 0)
    while j < len(waypoints) - 1 and is_passed(waypoints, j, x, y, deviation_xy_m):
        j += 1
    return j
