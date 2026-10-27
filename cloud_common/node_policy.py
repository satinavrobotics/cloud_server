"""Pure nodePolicy rules the planner and the dispatcher share, so they cannot drift apart.

`skip_allowed`: whether a corridorWidth (per side, m) is wide enough for `skippable=true`. The robot
(order_start.is_passed) needs corridor - NODE_POLICY_LOCALISATION_MARGIN_M (0.10) > 0 to use a gate at all, so a
corridor in (0, 0.10] could never be used. The threshold is one config value, NODE_POLICY_SKIP_MIN_CORRIDOR_M
(default = that margin; agreed with the robot team 2026-10-27). Only `skippable` depends on it: the corridorWidth
itself is still sent, and 0 still means "measured, no room".

`sanitize_width`: a measured passage width (Route.min_width) as the planner uses it.
"""
import math
from typing import Any, Iterable, List, Optional, Sequence

from cloud_common.objects import common


def skip_allowed(corridor: Optional[float], threshold: Optional[float] = None) -> bool:
    """True only for a finite corridor strictly above `threshold` (default: config.NODE_POLICY_SKIP_MIN_CORRIDOR_M)."""
    if corridor is None:
        return False
    try:
        corridor = float(corridor)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(corridor):
        return False
    if threshold is None:
        from packages import config      # not at module level: cloud_common stays importable without the services' config
        threshold = config.NODE_POLICY_SKIP_MIN_CORRIDOR_M
    return corridor > max(0.0, float(threshold))


def sanitize_width(width) -> Optional[float]:
    """A measured passage width: finite and >= 0 is kept (0 is a real value: measured, no room), anything else
    (None, NaN, inf, negative, not a number) is None, unknown."""
    try:
        width = float(width)
    except (TypeError, ValueError):
        return None
    return width if math.isfinite(width) and width >= 0 else None


def skippable_candidates(poses: Sequence[Any]) -> List[int]:
    """The indexes of the waypoints that should be skippable: all but the last that are planned or ask for it.
    `poses` are Pose2D or their dicts. The one definition of the `total` of a nodePolicy report."""
    def get(p: Any, name: str) -> Any:
        return p.get(name) if isinstance(p, dict) else getattr(p, name, None)
    return [i for i, p in enumerate(poses[:-1])
            if get(p, "origin") == common.WAYPOINT_ORIGIN_PLANNED or get(p, "skippable")]


def clear_stale_measured(poses: Iterable[Any]) -> int:
    """Where the planner did not measure (no graph, timeout, error), a positive measured corridor is for the place the
    waypoint had when it was measured and may be stale: drop it (corridor and source become None; the robot default
    applies and the waypoint is not skippable). A measured 0 (no room: the safe side) and the operator's values
    stay. `poses` are Pose2D or dicts, changed in place. Returns how many were cleared."""
    n = 0
    for p in poses:
        d = isinstance(p, dict)
        width = p.get("corridor_width_m") if d else p.corridor_width_m
        source = p.get("corridor_source") if d else p.corridor_source
        if source == common.CORRIDOR_SOURCE_MEASURED and width is not None and width > 0:
            if d:
                p["corridor_width_m"], p["corridor_source"] = None, None
            else:
                p.corridor_width_m, p.corridor_source = None, None
            n += 1
    return n
