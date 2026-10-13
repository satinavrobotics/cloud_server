#!/usr/bin/env python3
"""
Mission Planner Service - FastAPI Application

Exposes the Mission Planner Service via REST API.
"""

import argparse
import logging
from contextlib import asynccontextmanager
from typing import Optional, Dict, Any

import uvicorn
from fastapi import FastAPI, HTTPException, Body
from pydantic import BaseModel, Field

from cloud_common.objects.common import Pose2D
from .server import MapResolutionError, MissionPlannerService
from packages.utils.service_utils import (
    HealthResponse, create_health_response, create_root_response,
    configure_service_logging, DependencyHealthChecker
)
from packages.utils.fastapi_helpers import add_error_handlers
from packages.config import (
    ARANGO_HOST, ARANGO_PORT, ARANGO_USERNAME, DATA_BASE_NAME,
    POSTGRES_DATABASE_NAME, POSTGRES_DATABASE_USERNAME, postgres_database_password, require_secret,
    POSTGRES_DATABASE_HOST, POSTGRES_DATABASE_PORT,
    KNN_K, RANGE_SEARCH_RADIUS,
    PORT_MISSION_PLANNER, DEFAULT_HOST, LOG_LEVEL_DEFAULT, TIMEOUT_HTTP_REQUEST_SHORT,
)


# ==================== Request/Response Models ====================

class NavigationRequest(BaseModel):
    """Request model for navigation command.

    Provide either (target_x, target_y) in local metres or
    (target_lat, target_lon) in WGS84 degrees. GPS requires the map to have
    a datum registered via PUT /api/v1/maps/{map_id}/datum.
    """
    robot_name: str = Field(..., description="Name of the robot to navigate")
    target_x: Optional[float] = Field(None, description="Target x coordinate in meters (local frame)")
    target_y: Optional[float] = Field(None, description="Target y coordinate in meters (local frame)")
    target_lat: Optional[float] = Field(None, description="Target WGS84 latitude in degrees")
    target_lon: Optional[float] = Field(None, description="Target WGS84 longitude in degrees")
    map_id: Optional[str] = Field(None, description="Map ID to use for navigation; without it the robot's current map, else 400")
    mission_name: Optional[str] = Field(None, description="Optional mission name (auto-generated if not provided)")
    timeout_seconds: Optional[int] = Field(None, description="Optional mission time limit in seconds; omitted or null = no limit", ge=1, le=3600)
    register_map: bool = Field(True, description="Whether the graph builder should record topology during this mission")


class PlanRequest(BaseModel):
    """Request model for a plan without a mission (the dispatcher replans a go-to as it starts)."""
    robot_name: str = Field(..., description="Name of the robot to plan for")
    target_x: Optional[float] = Field(None, description="Goal x in the map frame, metres")
    target_y: Optional[float] = Field(None, description="Goal y in the map frame, metres")
    target_lat: Optional[float] = Field(None, description="Goal WGS84 latitude in degrees")
    target_lon: Optional[float] = Field(None, description="Goal WGS84 longitude in degrees")
    map_id: Optional[str] = Field(None, description="Map to plan on; without it the robot's open session map, else 400")
    robot_x: Optional[float] = Field(None, description="The robot's x in its own run frame, if newer than the stored robot row")
    robot_y: Optional[float] = Field(None, description="The robot's y in its own run frame, if newer than the stored robot row")
    ignore_exclusions: bool = Field(False, description="Plan through graph nodes a robot reported blocked recently (normally avoided)")


class PlanResponse(BaseModel):
    """A planned route: what a go-to mission's route node and planned_path would hold."""
    success: bool
    robot_name: str
    waypoints: Optional[list] = Field(None, description="Pose2D waypoints in the map frame")
    planned_path: Optional[list] = Field(None, description="Topological node ids of the route")
    goal: Optional[Dict[str, Any]] = Field(None, description="{x, y, map_id, node_id}")
    map_id: Optional[str] = None
    start_node_id: Optional[str] = None
    end_node_id: Optional[str] = None
    error: Optional[str] = None
    failed_at: Optional[str] = None
    blocked_nodes: Optional[list] = Field(None, description="With failed_at 'blocked_nodes': the blocked graph nodes that made planning fail (on the goal, or on the shortest path with no detour around them)")
    planner: Optional[str] = Field(None, description="'scene_graph' or 'legacy': which planner produced the route")
    min_width: Optional[float] = Field(None, description="Narrowest passage along a scene-graph route, metres; null for legacy routes")
    chart_keys: Optional[list] = Field(None, description="Scene-graph chart keys the route crosses, in order; null for legacy routes")
    graph_version: Optional[int] = Field(None, description="Scene-graph version the route was planned on")
    planner_fallback: Optional[str] = Field(None, description="Why the legacy planner was used while the scene-graph planner is enabled (e.g. 'no_graph', 'load_timeout'); null otherwise")
    node_policy: Optional[Dict[str, Any]] = Field(None, description="nodePolicy skip data of the route: {total: waypoints that should be skippable, skippable: how many got a corridor width, unavailable: why the others did not (e.g. 'width_unknown', 'legacy_planner'), else null}")


class EnrichRequest(BaseModel):
    """A route to complete with nodePolicy data."""
    robot_name: str = Field(..., description="Robot the route is for (its footprint sets the corridor widths)")
    map_id: str = Field(..., description="Map of the waypoints (their frame)")
    waypoints: list = Field(..., description="Pose2D waypoints in the map frame; waypoints with origin 'planned' are dropped and re-planned")
    insert_planned: bool = Field(True, description="Insert planned waypoints between waypoints that are not in the same or a neighbouring chart")


class EnrichResponse(BaseModel):
    success: bool
    waypoints: list = Field(..., description="The completed route (the input unchanged when the scene graph is not available)")
    inserted: int = Field(0, description="Planned waypoints inserted")
    changed: bool = Field(False, description="Whether the route differs from the request's")
    node_policy: Optional[Dict[str, Any]] = Field(None, description="{total, skippable, unavailable}; see POST /api/v1/plan")
    graph_version: Optional[int] = None


class NavigationResponse(BaseModel):
    """Response model for navigation command."""
    success: bool = Field(..., description="Whether the mission was planned and submitted successfully")
    robot_name: str = Field(..., description="Name of the robot")
    target: Dict[str, float] = Field(..., description="Target coordinates")
    mission_name: Optional[str] = Field(None, description="Name of the created mission")
    start_node_id: Optional[str] = Field(None, description="Starting node ID (closest to robot)")
    end_node_id: Optional[str] = Field(None, description="Ending node ID (closest to target)")
    path: Optional[list] = Field(None, description="List of node IDs forming the path")
    path_length: Optional[int] = Field(None, description="Number of nodes in the path")
    waypoints_count: Optional[int] = Field(None, description="Number of waypoints in the mission")
    robot_position: Optional[Dict[str, float]] = Field(None, description="Robot's current position")
    target_node_position: Optional[Dict[str, float]] = Field(None, description="Target node position")
    error: Optional[str] = Field(None, description="Error message if failed")
    failed_at: Optional[str] = Field(None, description="Stage where planning failed")
    message: Optional[str] = Field(None, description="Success message")
    planner: Optional[str] = Field(None, description="'scene_graph' or 'legacy': which planner produced the route")
    min_width: Optional[float] = Field(None, description="Narrowest passage along a scene-graph route, metres; null for legacy routes")
    chart_keys: Optional[list] = Field(None, description="Scene-graph chart keys the route crosses, in order; null for legacy routes")
    graph_version: Optional[int] = Field(None, description="Scene-graph version the route was planned on")
    planner_fallback: Optional[str] = Field(None, description="Why the legacy planner was used while the scene-graph planner is enabled (e.g. 'no_graph', 'load_timeout'); null otherwise")
    node_policy: Optional[Dict[str, Any]] = Field(None, description="nodePolicy skip data of the route: {total, skippable, unavailable}; see POST /api/v1/plan")


# HealthResponse is now imported from packages.utils.service_utils


class StatsResponse(BaseModel):
    """Response model for statistics."""
    service: str
    graph_db_url: str
    database_url: str
    default_map_id: Optional[str] = None
    knn_k: int
    range_search_radius: float
    scene_graph: Optional[Dict[str, Any]] = Field(None, description="Scene graph planner counters (outcomes, cache, plan_inflight); present only when the planner flag is on")


class NavigationPlanResponse(BaseModel):
    """Response model for navigation plan retrieval."""
    mission_id: str = Field(..., description="Mission identifier")
    mission_name: str = Field(..., description="Mission name")
    state: str = Field(..., description="Current mission state (PENDING, RUNNING, COMPLETED, FAILED, CANCELED)")
    path: list = Field(..., description="List of node IDs forming the navigation path (reconstructed from waypoints using KNN search)")
    start_node_id: Optional[str] = Field(None, description="Starting node ID (closest to first waypoint)")
    end_node_id: Optional[str] = Field(None, description="Ending node ID (closest to last waypoint)")
    start_position: Dict[str, float] = Field(..., description="Robot's start position coordinates")
    target_position: Dict[str, float] = Field(..., description="Target coordinates")
    end_position: Dict[str, float] = Field(..., description="Target node position coordinates")
    robot_name: str = Field(..., description="Name of the robot")
    created_at: Optional[str] = Field(None, description="Timestamp when mission was started")
    updated_at: Optional[str] = Field(None, description="Timestamp when mission ended")


# ==================== FastAPI Application ====================

service: Optional[MissionPlannerService] = None
health_checker: Optional[DependencyHealthChecker] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize and clean up service resources."""
    global service, health_checker

    service = MissionPlannerService(
        arango_host=ARANGO_HOST,
        arango_port=ARANGO_PORT,
        arango_username=ARANGO_USERNAME,
        arango_password=require_secret("ARANGO_PASSWORD"),
        arango_database=DATA_BASE_NAME,
        postgres_db=POSTGRES_DATABASE_NAME,
        postgres_user=POSTGRES_DATABASE_USERNAME,
        postgres_password=postgres_database_password(),
        postgres_host=POSTGRES_DATABASE_HOST,
        postgres_port=POSTGRES_DATABASE_PORT,
        knn_k=KNN_K,
        range_search_radius=RANGE_SEARCH_RADIUS,
    )

    await service.database.async_init()

    health_checker = DependencyHealthChecker(timeout=TIMEOUT_HTTP_REQUEST_SHORT)
    health_checker.add_dependency(
        "graph_db",
        lambda: service.graph_db.is_healthy(),
        critical=True
    )
    health_checker.add_dependency(
        "mission_database",
        lambda: service.database.is_running(),
        critical=True
    )

    service.start_scene_prewarm()      # background, no-op unless SCENE_GRAPH_PLANNER_ENABLED

    logging.info("Mission Planner Service started")
    yield
    if service:
        service.cleanup()
    logging.info("Mission Planner Service shutting down")


app = FastAPI(
    title="Mission Planner Service",
    description="Plans navigation missions by querying graph database and submitting to Mission Dispatcher",
    version="1.0.0",
    lifespan=lifespan,
)

# Add standardized error handlers
add_error_handlers(app)


# ==================== API Endpoints ====================

@app.get("/health", response_model=HealthResponse)
async def health_check():
    """
    Health check endpoint.

    Returns the health status of the service and its dependencies.
    """
    dependencies = health_checker.check_all() if health_checker else {}
    return create_health_response(
        service_name="mission_planner",
        dependencies=dependencies
    )


@app.get("/stats", response_model=StatsResponse)
async def get_stats():
    """
    Get service statistics.
    
    Returns configuration and status information.
    """
    stats = service.get_stats()
    return StatsResponse(**stats)


@app.post("/api/v1/navigate", response_model=NavigationResponse)
async def navigate(request: NavigationRequest):
    """
    Plan and execute a navigation mission.

    Accepts either local Cartesian (target_x, target_y) or GPS
    (target_lat, target_lon) coordinates. GPS requires the map to have a
    datum registered.
    """
    has_xy = request.target_x is not None and request.target_y is not None
    has_gps = request.target_lat is not None and request.target_lon is not None
    if not has_xy and not has_gps:
        raise HTTPException(
            status_code=400,
            detail="Provide either (target_x, target_y) or (target_lat, target_lon).",
        )

    try:
        result = await service.plan_and_execute_mission(
            robot_name=request.robot_name,
            target_x=request.target_x,
            target_y=request.target_y,
            target_lat=request.target_lat,
            target_lon=request.target_lon,
            map_id=request.map_id,
            mission_name=request.mission_name,
            timeout_seconds=request.timeout_seconds,
            register_map=request.register_map,
        )
        if result.get("failed_at") == "map_resolution":
            raise HTTPException(status_code=400, detail=result.get("error"))
        if result.get("failed_at") == "robot_not_placed":  # maps §14: place the robot first
            raise HTTPException(status_code=409, detail=result.get("error"))
        return NavigationResponse(**result)

    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Navigation request failed: {e}")
        raise HTTPException(status_code=500, detail=f"Navigation request failed: {str(e)}")


@app.post("/api/v1/plan", response_model=PlanResponse)
async def plan_only(request: PlanRequest):
    """
    Plan a route from the robot's position to a goal and return it; no mission is created.

    The same planning as POST /api/v1/navigate (including dropping a first node that is
    behind the robot). `robot_x`/`robot_y` replace the stored robot position when given.
    A planning failure is `success: false` with `error`/`failed_at` (200), except the same
    400 (no map) and 409 (robot not placed) as navigate.
    """
    has_xy = request.target_x is not None and request.target_y is not None
    has_gps = request.target_lat is not None and request.target_lon is not None
    if not has_xy and not has_gps:
        raise HTTPException(
            status_code=400,
            detail="Provide either (target_x, target_y) or (target_lat, target_lon).",
        )
    robot_pose = (request.robot_x, request.robot_y) \
        if request.robot_x is not None and request.robot_y is not None else None
    try:
        result = await service.plan_route(
            robot_name=request.robot_name,
            target_x=request.target_x,
            target_y=request.target_y,
            target_lat=request.target_lat,
            target_lon=request.target_lon,
            map_id=request.map_id,
            robot_pose=robot_pose,
            ignore_exclusions=request.ignore_exclusions,
        )
        if result.get("failed_at") == "map_resolution":
            raise HTTPException(status_code=400, detail=result.get("error"))
        if result.get("failed_at") == "robot_not_placed":
            raise HTTPException(status_code=409, detail=result.get("error"))
        return PlanResponse(
            success=bool(result.get("success")),
            robot_name=request.robot_name,
            waypoints=[w.dict() for w in result.get("waypoints") or []] or None,
            planned_path=result.get("path"),
            goal=result.get("goal"),
            map_id=result.get("map_id"),
            start_node_id=result.get("start_node_id"),
            end_node_id=result.get("end_node_id"),
            error=result.get("error"),
            failed_at=result.get("failed_at"),
            blocked_nodes=result.get("blocked_nodes"),
            planner=result.get("planner"),
            min_width=result.get("min_width"),
            chart_keys=result.get("chart_keys"),
            graph_version=result.get("graph_version"),
            planner_fallback=result.get("planner_fallback"),
            node_policy=result.get("node_policy"),
        )
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Plan request failed: {e}")
        raise HTTPException(status_code=500, detail=f"Plan request failed: {str(e)}")


@app.post("/api/v1/route/enrich", response_model=EnrichResponse)
async def enrich_route(request: EnrichRequest):
    """
    Complete a route with the nodePolicy data the robot needs (docs/node_policies_2026-10-13.md):
    planned waypoints (origin 'planned', skippable) between waypoints that are not in the same or
    a neighbouring scene-graph chart, and a corridor width from the scene graph for every waypoint
    that should be skippable. Without a scene graph the route comes back unchanged with
    `node_policy.unavailable` saying why; it never fails because of that.
    """
    try:
        waypoints = [Pose2D(**w) for w in request.waypoints]
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid waypoints: {e}")
    try:
        result = await service.enrich_route(
            request.robot_name, request.map_id, waypoints, request.insert_planned)
    except Exception as e:
        logging.error(f"Route enrich failed: {e}")
        raise HTTPException(status_code=500, detail=f"Route enrich failed: {str(e)}")
    return EnrichResponse(
        success=True, waypoints=[w.dict() for w in result["waypoints"]],
        inserted=result["inserted"], changed=result["changed"],
        node_policy=result.get("node_policy"), graph_version=result.get("graph_version"))


@app.get("/api/v1/missions/{mission_id}/plan", response_model=NavigationPlanResponse)
async def get_mission_plan(mission_id: str, map_id: Optional[str] = None):
    """
    Get navigation plan for a mission by ID.

    This endpoint queries the mission from the database and reconstructs the navigation
    plan by finding the closest graph nodes to each waypoint using KNN search.

    Returns:
    - The path (list of node IDs reconstructed from waypoints)
    - Start and end node coordinates
    - Target coordinates
    - Mission name
    - Current mission state/status (PENDING, RUNNING, COMPLETED, FAILED, CANCELED)

    Note: The path is reconstructed by performing KNN search (k=1) on the graph database
    to find the closest node to each waypoint stored in the mission. This may not exactly
    match the original planned path if the graph has changed since mission creation.

    Args:
        mission_id: Mission identifier (mission name)
        map_id: Map ID to use for node lookup (else the mission robot's current map, else 400)

    Returns:
        NavigationPlanResponse with mission plan details and current state

    Raises:
        HTTPException: If mission not found (404) or other errors (500)
    """
    try:
        plan = await service.get_mission_plan(mission_id, map_id=map_id)

        if plan is None:
            raise HTTPException(
                status_code=404,
                detail=f"Mission '{mission_id}' not found or has no navigation plan"
            )

        return NavigationPlanResponse(**plan)

    except HTTPException:
        # Re-raise HTTP exceptions
        raise
    except MapResolutionError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logging.error(f"Failed to get mission plan for '{mission_id}': {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get mission plan: {str(e)}"
        )


@app.get("/")
async def root():
    """Root endpoint with service information."""
    return create_root_response(
        service_name="Mission Planner",
        version="1.0.0",
        description="Plans navigation missions by querying graph database and submitting to Mission Dispatcher",
        endpoints={
            "navigate": "POST /api/v1/navigate",
            "plan": "POST /api/v1/plan",
            "mission_plan": "GET /api/v1/missions/{mission_id}/plan",
            "health": "GET /health",
            "stats": "GET /stats"
        }
    )


# ==================== Main Entry Point ====================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Mission Planner Service")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Host to bind to")
    parser.add_argument("--port", type=int, default=PORT_MISSION_PLANNER, help="Port to bind to")
    parser.add_argument("--log-level", default=LOG_LEVEL_DEFAULT,
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Logging level")
    args = parser.parse_args()

    configure_service_logging("mission_planner", args.log_level)
    uvicorn.run(app, host=args.host, port=args.port)

