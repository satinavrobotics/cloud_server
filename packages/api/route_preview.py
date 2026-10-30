"""Dry-run preview of a route (docs/route_preview_2026-10-14.md): what the mission dispatcher's route enrichment
would store for these waypoints (planned waypoints, corridor widths, lattice trajectory), computed by the planner's
`POST /api/v1/route/enrich` and returned without storing anything. No database access, no events."""
import asyncio
import logging
import math
from typing import Any, Dict, List, Optional, Tuple

from cloud_common.objects import common
from packages import config
from cloud_common import node_policy as node_policy_rules

_log = logging.getLogger("RoutePreview")

PLANNER_SCENE_GRAPH = "scene_graph"
PLANNER_NONE = "none"
REASON_PLANNER_UNAVAILABLE = "planner_unavailable"
REASON_NO_SCENE_GRAPH = "no_scene_graph"


class PreviewError(Exception):
    """A request the preview refuses: `status` is the HTTP status (400 invalid, 413 too many waypoints)."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def build_route(map_id: str, raw: List[Dict[str, Any]], return_leg: bool) -> List[common.Pose2D]:
    """The operator's waypoints as Pose2D in map `map_id` (planned ones dropped: the planner re-plans them), with
    the way back appended when `return_leg`: the same waypoints in reverse order (the last one is the turning point,
    not repeated), heading turned by pi. The mission model has no return route of its own, so the preview treats
    a return as outbound plus its mirror, which is one ordinary route to the planner."""
    if not isinstance(map_id, str) or not map_id.strip():
        raise PreviewError(400, "map_id is required")
    if not raw:
        raise PreviewError(400, "waypoints must not be empty")
    if len(raw) > config.ROUTE_PREVIEW_MAX_WAYPOINTS:
        raise PreviewError(413, f"at most {config.ROUTE_PREVIEW_MAX_WAYPOINTS} waypoints")
    poses: List[common.Pose2D] = []
    for i, item in enumerate(raw):
        try:
            pose = common.Pose2D(**{**item, "map_id": map_id})
        except Exception as err:  # pylint: disable=broad-except
            raise PreviewError(400, f"waypoint {i} is invalid: {err}") from err
        if not (math.isfinite(pose.x) and math.isfinite(pose.y) and math.isfinite(pose.theta)):
            raise PreviewError(400, f"waypoint {i} has a non-finite coordinate")
        if pose.origin != common.WAYPOINT_ORIGIN_PLANNED:
            poses.append(pose)
    if not poses:
        raise PreviewError(400, "waypoints must contain at least one non-planned waypoint")
    if return_leg:
        for pose in reversed(poses[:-1]):
            back = pose.copy()
            back.theta = math.atan2(math.sin(pose.theta + math.pi), math.cos(pose.theta + math.pi))
            poses.append(back)
    return poses


async def preview(planner: Any, map_id: str, raw: List[Dict[str, Any]], return_leg: bool = False,
                  robot_name: Optional[str] = None) -> Dict[str, Any]:
    """The response of POST /api/v1/route/preview. Raises PreviewError for a refused request; a planner that is
    down, slow or without a scene graph is an answer (planner "none" with a reason), never an exception."""
    poses = build_route(map_id, raw, return_leg)
    asked = len(node_policy_rules.skippable_candidates(poses))
    waypoints = [p.dict() for p in poses]
    unmeasured = [dict(w) for w in waypoints]          # the answer where nothing is measured: no stale measured corridor
    node_policy_rules.clear_stale_measured(unmeasured)
    none = {"waypoints": unmeasured, "trajectory": [], "issues": [],
            "node_policy": {"total": asked, "skippable": 0, "unavailable": None},
            "planner": PLANNER_NONE, "reason": REASON_PLANNER_UNAVAILABLE, "graph_version": None}
    if planner is None:
        return none
    try:
        result = await planner.enrich_route(
            robot_name or "", map_id, waypoints, insert_planned=True,
            timeout=config.NODE_POLICY_ENRICH_TIMEOUT_S)
        returned = list(result["waypoints"])
    except Exception as err:  # pylint: disable=broad-except
        _log.warning("Route preview: planner call failed (%r)", err)
        return none
    policy = dict(result.get("node_policy") or none["node_policy"])
    if result.get("graph_version") is None:
        # the planner answered without a scene graph (or gave up): the route came back as it was
        reason = policy.get("unavailable") or REASON_NO_SCENE_GRAPH
        return {**none, "waypoints": returned or unmeasured, "node_policy": policy, "reason": reason}
    return {"waypoints": returned, "trajectory": list(result.get("trajectory") or []),
            "issues": list(result.get("issues") or []), "node_policy": policy,
            "planner": PLANNER_SCENE_GRAPH, "reason": None, "graph_version": result.get("graph_version"),
            # additive: the map's keep-out zones {applied, unresolved, crossed: [{from_index, to_index, zones}]}; absent without any
            **({"keepout": result["keepout"]} if result.get("keepout") is not None else {})}


async def _measure_route(planner: Any, robot_name: str, name: str,
                         route: Any) -> Tuple[Dict[str, Any], bool, Optional[Dict[str, Any]]]:
    """One route of a reroute: (route dict, measured on a scene graph, the planner's keepout
    answer or None). Never raises: a planner that fails leaves the route as given."""
    route = route.dict() if hasattr(route, "dict") else dict(route or {})
    waypoints = [dict(w) if isinstance(w, dict) else w.dict() for w in route.get("waypoints") or []]
    maps = {w.get("map_id") or "" for w in waypoints}
    if planner is not None and len(waypoints) >= 2 and len(maps) == 1 and "" not in maps:
        try:
            result = await planner.enrich_route(
                robot_name, next(iter(maps)), waypoints, insert_planned=False,
                timeout=config.NODE_POLICY_ENRICH_TIMEOUT_S)
            measured = [w if isinstance(w, dict) else w.dict() for w in result["waypoints"]]
            if len(measured) == len(waypoints):
                version = result.get("graph_version")
                out = {**route, "waypoints": measured}
                if version is not None:
                    # chart_keys are of THIS graph version: the route now claims it
                    out["graph_version"] = version
                return out, version is not None, result.get("keepout")
            node_policy_rules.clear_stale_measured(waypoints)
        except Exception as err:  # pylint: disable=broad-except
            _log.warning("Reroute of %s: corridors not measured (%r); route stored as given",
                         name, err)
            node_policy_rules.clear_stale_measured(waypoints)
    else:
        node_policy_rules.clear_stale_measured(waypoints)       # nothing to measure it against
    return {**route, "waypoints": waypoints}, False, None


def keepout_warnings(keepout_by_node: Dict[str, Optional[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Non-blocking warnings for the legs of a reroute that still enter a keep-out zone (the
    planner's `keepout.crossed`); one entry per route that has any."""
    out = []
    for name, keepout in keepout_by_node.items():
        crossed = (keepout or {}).get("crossed") or []
        if crossed:
            zones = sorted({z for leg in crossed for z in leg.get("zones") or []})
            out.append({"code": "ROUTE_CROSSES_KEEPOUT", "node": name, "crossed": crossed,
                        "message": f"The new route of node {name} crosses keep-out zone(s) "
                                   f"{', '.join(map(str, zones))}."})
    return out


async def measure_reroute_detailed(planner: Any, robot_name: str, reroute: Dict[str, Any]
                                   ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """A reroute's routes with their corridors measured again for the waypoints where they are now
    (`update_nodes` of PUT /missions/{name}): the editor seeds a reroute with the stored route, so
    a waypoint the operator moved still carries the corridor measured for its old place. The
    waypoints are not re-planned or moved (`insert_planned` off); an operator's own corridor is
    kept. A planner that is down, slow or without a scene graph leaves the route as it was given,
    like a route with nothing to measure. Returns (routes, info): the routes as dicts, and
    info = {"measured": [node names measured on a scene graph (their chart_key / graph_version
    are fresh)], "warnings": keepout_warnings}. The routes are measured concurrently, each with
    its own NODE_POLICY_ENRICH_TIMEOUT_S, so a reroute of N routes costs one call, not N."""
    names = list(reroute)
    results = await asyncio.gather(*(_measure_route(planner, robot_name, n, reroute[n])
                                     for n in names))
    routes = {n: r[0] for n, r in zip(names, results)}
    info = {"measured": [n for n, r in zip(names, results) if r[1]],
            "warnings": keepout_warnings({n: r[2] for n, r in zip(names, results)})}
    return routes, info
