"""Robot-vs-map footprint compatibility of a scene-graph plan (pure; no database).

A map freezes the robot half side r it was built for (`graph.params.robot_half_side`) and the chart
radius (`chart_radius`, always >= r: the engine rejects anything else). The planner already prices a
plan with the REQUESTING robot's own half smaller side (scene_route.inscribed_radius); this tells the
caller how that robot compares with the map.
"""
import math
from typing import Any, Dict, Optional

TOLERANCE_M = 1e-6

OK = "ok"
LARGER_THAN_MAP = "larger_than_map"          # warning: the plan proceeds
UNKNOWN_FOOTPRINT = "unknown_footprint"      # warning: the plan proceeds with the map's r
EXCEEDS_CHART = "exceeds_chart"              # refused: the engine cannot honour it


def _positive(v: Any) -> Optional[float]:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v > 0.0 else None


def footprint_check(graph: Any, robot_width_m: Optional[float],
                    robot_length_m: Optional[float]) -> Optional[Dict[str, Any]]:
    """{robot_half_side_m, map_half_side_m, chart_radius_m, status, message}, or None when the map
    holds no robot_half_side (nothing to compare with)."""
    params = getattr(graph, "params", None)
    map_r = _positive(getattr(params, "robot_half_side", None))
    if map_r is None:
        return None
    chart = _positive(getattr(params, "chart_radius", None))
    width, length = _positive(robot_width_m), _positive(robot_length_m)
    robot_r = None if width is None else min(width, length or width) / 2.0
    out: Dict[str, Any] = {"robot_half_side_m": robot_r, "map_half_side_m": map_r, "chart_radius_m": chart}
    if robot_r is None:
        out["status"] = UNKNOWN_FOOTPRINT
        out["message"] = ("The robot's footprint is unknown (no factsheet width); planning with the map's "
                          "half side %.3f m." % map_r)
    elif chart is not None and robot_r > chart + TOLERANCE_M:
        out["status"] = EXCEEDS_CHART
        out["message"] = ("The robot's half side %.3f m exceeds the map's chart radius %.3f m; "
                          "this map cannot be planned for this robot." % (robot_r, chart))
    elif robot_r > map_r + TOLERANCE_M:
        out["status"] = LARGER_THAN_MAP
        out["message"] = ("The robot's half side %.3f m is larger than the %.3f m the map was built for; "
                          "narrow passages may be unsafe." % (robot_r, map_r))
    else:
        out["status"] = OK
        out["message"] = "The robot fits the map (half side %.3f m, map %.3f m)." % (robot_r, map_r)
    return out
