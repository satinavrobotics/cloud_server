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

from .server import MissionPlannerService
from packages.utils.service_utils import (
    HealthResponse, create_health_response, create_root_response,
    configure_service_logging, DependencyHealthChecker
)
from packages.utils.fastapi_helpers import add_error_handlers
from packages.config import (
    ARANGO_HOST, ARANGO_PORT, ARANGO_USERNAME, ARANGO_PASSWORD, DATA_BASE_NAME,
    POSTGRES_DATABASE_NAME, POSTGRES_DATABASE_USERNAME, POSTGRES_DATABASE_PASSWORD,
    POSTGRES_DATABASE_HOST, POSTGRES_DATABASE_PORT,
    DEFAULT_MAP_ID, KNN_K, RANGE_SEARCH_RADIUS,
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
    map_id: Optional[str] = Field(None, description="Map ID to use for navigation (uses default if not provided)")
    mission_name: Optional[str] = Field(None, description="Optional mission name (auto-generated if not provided)")
    timeout_seconds: int = Field(300, description="Mission timeout in seconds", ge=1, le=3600)
    register_map: bool = Field(True, description="Whether the graph builder should record topology during this mission")


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


# HealthResponse is now imported from packages.utils.service_utils


class StatsResponse(BaseModel):
    """Response model for statistics."""
    service: str
    graph_db_url: str
    database_url: str
    default_map_id: str
    knn_k: int
    range_search_radius: float


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
        arango_password=ARANGO_PASSWORD,
        arango_database=DATA_BASE_NAME,
        postgres_db=POSTGRES_DATABASE_NAME,
        postgres_user=POSTGRES_DATABASE_USERNAME,
        postgres_password=POSTGRES_DATABASE_PASSWORD,
        postgres_host=POSTGRES_DATABASE_HOST,
        postgres_port=POSTGRES_DATABASE_PORT,
        default_map_id=DEFAULT_MAP_ID,
        knn_k=KNN_K,
        range_search_radius=RANGE_SEARCH_RADIUS,
    )

    await service.database.async_init()

    health_checker = DependencyHealthChecker(timeout=TIMEOUT_HTTP_REQUEST_SHORT)
    health_checker.add_dependency(
        "graph_db",
        lambda: service.graph_db.is_healthy(timeout=TIMEOUT_HTTP_REQUEST_SHORT),
        critical=True
    )
    health_checker.add_dependency(
        "mission_database",
        lambda: service.database.is_running(timeout=TIMEOUT_HTTP_REQUEST_SHORT),
        critical=True
    )

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
        return NavigationResponse(**result)

    except Exception as e:
        logging.error(f"Navigation request failed: {e}")
        raise HTTPException(status_code=500, detail=f"Navigation request failed: {str(e)}")


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
        map_id: Map ID to use for node lookup (uses default if not provided)

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

