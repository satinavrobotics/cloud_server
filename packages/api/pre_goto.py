"""Automatic go-to before a mission (the topomap node nearest its first waypoint).

`queue_pre_goto(service, mission)` runs in POST /api/v1/missions before the mission row is
written. When every guard holds it asks the planner (service.navigate) for a go-to to the graph
node closest to the mission's first waypoint; the go-to is a normal `kind: goto` mission named
`goto-<mission>-1` and, being created first, sorts ahead of the mission in the robot's queue
(spec.created_at, then name: packages/api/mission_index.py::dispatch_key). Nothing here ever
raises: any failure is logged and the mission is queued as before.
"""
import asyncio
import logging
import math
import uuid
from typing import Any, List, Optional, Tuple

from cloud_common.objects.mission import MissionObjectV1
from cloud_common.objects.robot import RobotObjectV1
from packages import config
from packages.utils import map_geo, map_sessions

logger = logging.getLogger("ApiDelegationService")

GOTO_PREFIX = "goto-"


def goto_name(mission_name: str, n: int = 1) -> str:
    return f"{GOTO_PREFIX}{mission_name}-{n}"


def free_goto_name(mission_name: str, taken) -> str:
    """`goto-<mission>-N` with the smallest N >= 1 not in `taken` (deleted missions keep their
    row, so a reused mission name must not collide with the old go-to)."""
    n = 1
    while goto_name(mission_name, n) in taken:
        n += 1
    return goto_name(mission_name, n)


def first_waypoint(mission: Any) -> Optional[Any]:
    """The first waypoint (Pose2D) of the first route node of the mission tree, or None."""
    for node in mission.mission_tree:
        route = getattr(node, "route", None)
        if route is not None and route.waypoints:
            return route.waypoints[0]
    return None


async def _robot_xy_in_map(service: Any, mission: Any, map_id: str
                           ) -> Optional[Tuple[float, float]]:
    """The robot's position in the map frame through its placed open session on `map_id`;
    None when it has none, it is not placed, or it is on another map."""
    robot = await service.database.get_object(RobotObjectV1, mission.robot)
    async with service.database.connection() as conn:
        cursor = await conn.execute(map_sessions.ROBOT_SESSION_SQL, (mission.robot,))
        session = map_sessions.robot_session_from_row(await cursor.fetchone())
    if session is None or session["map_name"] != map_id or not map_sessions.is_placed(session):
        return None
    pose = robot.status.pose
    return map_geo.apply_transform(session["map_t_session"], pose.x, pose.y)


async def queue_pre_goto(service: Any, mission: Any) -> Optional[str]:
    """Queue the go-to for `mission` (not yet written); its name, or None when not applicable
    or it failed. The whole planner / ArangoDB / SQL work is bounded by PRE_GOTO_TIMEOUT_S: on
    a timeout the mission is queued without a go-to (and a go-to the planner may already have
    written is removed)."""
    chosen: List[str] = []
    try:
        return await asyncio.wait_for(_queue_pre_goto(service, mission, chosen),
                                      config.PRE_GOTO_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.info("Pre-mission go-to for %s skipped: no answer in %.1f s",
                    getattr(mission, "name", "?"), config.PRE_GOTO_TIMEOUT_S)
        if chosen:
            await discard_pre_goto(service, chosen[0])
        return None
    except Exception as err:  # pylint: disable=broad-except
        logger.warning("Pre-mission go-to for %s skipped: %s", getattr(mission, "name", "?"), err)
        return None


async def discard_pre_goto(service: Any, name: str) -> None:
    """Best effort: delete the go-to `name` (the mission it precedes could not be written).
    Never raises."""
    try:
        from packages.api import run_admin    # pylint: disable=import-outside-toplevel
        await run_admin.delete_mission(service.database, name, with_reruns=False,
                                       publisher_id=uuid.uuid4())
        logger.info("Pre-goto %s deleted: its mission was not created", name)
    except Exception as err:  # pylint: disable=broad-except
        logger.warning("Pre-goto %s could not be deleted (the robot may drive to its node): %s",
                       name, err)


async def _robot_has_open_mission(service: Any, robot: str, rows: List[Any]) -> bool:
    return any(m.robot == robot and not m.status.state.done for m in rows)


async def _queue_pre_goto(service: Any, mission: Any, chosen: List[str]) -> Optional[str]:
    if not config.PRE_GOTO_ENABLED:
        return None
    if getattr(mission.mode, "value", mission.mode) != "mapped":
        return None
    if mission.kind is not None or mission.name.startswith(GOTO_PREFIX):
        return None
    wp = first_waypoint(mission)
    map_id = getattr(wp, "map_id", "") if wp is not None else ""
    if wp is None or not map_id:
        return None
    min_d = config.PRE_GOTO_MIN_DISTANCE_M

    # A queued or active mission of the robot makes its start pose stale: no go-to then.
    rows = await service.database.list_objects(MissionObjectV1, include_deleted=True)
    if await _robot_has_open_mission(
            service, mission.robot, [m for m in rows if m.lifecycle.value != "DELETED"]):
        logger.info("Pre-goto for %s: robot %s has a queued or active mission",
                    mission.name, mission.robot)
        return None
    robot_xy = await _robot_xy_in_map(service, mission, map_id)
    if robot_xy is None:
        logger.info("Pre-goto for %s: robot not placed on %s", mission.name, map_id)
        return None
    if math.hypot(robot_xy[0] - wp.x, robot_xy[1] - wp.y) <= min_d:
        return None

    nodes, _ = await asyncio.to_thread(
        service.graph_db.nodes_in_range, wp.x, wp.y, config.PRE_GOTO_NODE_RADIUS_M,
        1, map_id)
    if not nodes:
        logger.info("Pre-goto for %s: no topomap node within %.1f m of the first waypoint",
                    mission.name, config.PRE_GOTO_NODE_RADIUS_M)
        return None
    pose = nodes[0].get("pose") or nodes[0]
    nx, ny = float(pose["x"]), float(pose["y"])
    if math.hypot(robot_xy[0] - nx, robot_xy[1] - ny) <= min_d:
        return None

    name = free_goto_name(mission.name, {m.name for m in rows})
    chosen.append(name)
    result = await service.navigate(
        robot_name=mission.robot, target_x=nx, target_y=ny, map_id=map_id, mission_name=name)
    if not (result or {}).get("success"):
        logger.warning("Pre-goto %s not planned: %s", name, (result or {}).get("error"))
        return None
    logger.info("Pre-goto %s queued ahead of %s (node at %.2f, %.2f)", name, mission.name, nx, ny)
    # The mission must sort after its go-to: its created_at is stamped when it is written.
    mission.created_at = None
    return (result or {}).get("mission_name") or name
