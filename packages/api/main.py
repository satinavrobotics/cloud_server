#!/usr/bin/env python3
"""
API Delegation Service - Main Entry Point

FastAPI application that provides REST and WebSocket endpoints for clients.
"""

import asyncio
import logging
import uuid
import argparse
from contextlib import asynccontextmanager
from typing import Optional, Dict, Any, List, Literal
import os
from datetime import datetime

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, ValidationError
import uvicorn

from packages.api.server import ApiDelegationService
from packages.api import fleet_reads, maps, recorder_health, recording, run_admin, sites
from packages.api.idempotency import IdempotencyMiddleware, IdempotencyStore
from packages.api.robot_delete import RobotDeleter
from packages.utils.service_utils import (
    HealthResponse, create_health_response, create_root_response,
    configure_service_logging, DependencyHealthChecker
)
from packages.utils.fastapi_helpers import add_error_handlers
from packages.utils import map_sessions as ms
from packages.config import (
    SLAM_RECONCILE_INTERVAL_S,
    ARANGO_HOST, ARANGO_PORT, ARANGO_USERNAME, ARANGO_PASSWORD, DATA_BASE_NAME,
    URL_MISSION_PLANNER, URL_LIVEKIT,
    MINIO_HOST, MINIO_PORT, MINIO_ACCESS_KEY, MINIO_SECRET_KEY, MINIO_SECURE,
    MQTT_BROKER, MQTT_PORT, MQTT_KEEPALIVE,
    POSTGRES_DATABASE_NAME, POSTGRES_DATABASE_USERNAME, POSTGRES_DATABASE_PASSWORD,
    POSTGRES_DATABASE_HOST, POSTGRES_DATABASE_PORT,
    DEFAULT_MAP_ID, PORT_API_DELEGATION, DEFAULT_HOST, LOG_LEVEL_DEFAULT,
    IDEMPOTENCY_TTL_S, IDEMPOTENCY_LEASE_S, IDEMPOTENCY_PURGE_INTERVAL_S,
)
from cloud_common.objects.robot import (
    FACTSHEET_PHYSICAL_FIELDS, CustomActionV1, RobotObjectV1, RobotStatusV1)
from cloud_common.objects.mission import (
    EDITABLE_SPEC_FIELDS, MissionNodeStatusV1, MissionObjectV1, MissionSpecV1, MissionStateV1,
    MissionStatusV1)
from cloud_common.objects.detection_results import DetectionResultsObjectV1
from cloud_common.objects import common as common_objects
from cloud_common.objects.map import MapObjectV1
from cloud_common.objects.settings import SettingsObjectV1, SettingsSpecV1, GLOBAL_SETTINGS_NAME
from cloud_common.objects.site import SiteObjectV1
from cloud_common.objects.object import ObjectLifecycleV1


# ==================== Request/Response Models ====================

class LoadMapRequest(BaseModel):
    """Request model for loading a map."""
    map_id: Optional[str] = Field(None, description="Map ID to load (uses default if not provided)")
    description: Optional[str] = Field(None, description="Human-readable map name or label")
    datum_latitude: Optional[float] = Field(None, description="WGS84 origin latitude in degrees")
    datum_longitude: Optional[float] = Field(None, description="WGS84 origin longitude in degrees")
    datum_bearing_deg: float = Field(0.0, description="Angle of the map +X axis from east (grid east for utm), CCW, in degrees")
    datum_frame: Optional[Literal["enu", "utm"]] = Field(
        None, description="Frame of the map's local x/y: 'utm' = UTM grid offsets from the "
                          "datum (robot with GNSS), 'enu' = tangent-plane east/north (sim). "
                          "Default 'enu'.")
    datum_utm_zone: Optional[int] = Field(
        None, ge=1, le=60, description="UTM zone of a 'utm' datum (default: the datum's own)")
    datum_utm_north: Optional[bool] = Field(
        None, description="UTM hemisphere of a 'utm' datum (default: the datum's own)")
    datum_utm_easting: Optional[float] = Field(
        None, description="Exact UTM easting of a 'utm' datum (default: projected)")
    datum_utm_northing: Optional[float] = Field(
        None, description="Exact UTM northing of a 'utm' datum (default: projected)")


class LoadMapResponse(BaseModel):
    """Response model for map loading."""
    success: bool
    map_id: str
    stats: Optional[Dict[str, Any]] = None
    message: Optional[str] = None
    error: Optional[str] = None
    nodes: Optional[List[Dict[str, Any]]] = None
    edges: Optional[List[Dict[str, Any]]] = None
    transform: Optional[Dict[str, Any]] = None


class NavigationRequest(BaseModel):
    """Request model for navigation. Provide either (target_x, target_y) in the map's
    local Cartesian frame, or (target_lat, target_lon) in WGS84 degrees.
    GPS coordinates require the map to have a datum registered."""
    robot_name: str = Field(..., description="Name of the robot to navigate")
    target_x: Optional[float] = Field(None, description="Target x coordinate in meters (local frame)")
    target_y: Optional[float] = Field(None, description="Target y coordinate in meters (local frame)")
    target_lat: Optional[float] = Field(None, description="Target WGS84 latitude in degrees")
    target_lon: Optional[float] = Field(None, description="Target WGS84 longitude in degrees")
    map_id: Optional[str] = Field(None, description="Map ID to use for navigation (uses default if not provided)")
    mission_name: Optional[str] = Field(None, description="Optional mission name")
    timeout_seconds: int = Field(300, description="Mission timeout in seconds")


class NavigationResponse(BaseModel):
    """Response model for navigation."""
    success: bool
    mission_name: Optional[str] = None
    error: Optional[str] = None
    failed_at: Optional[str] = None


class DirectWaypointsRequest(BaseModel):
    """Request model for mapless direct-waypoint navigation."""
    robot_name: str = Field(..., description="Name of the robot")
    waypoints: List[Dict[str, Any]] = Field(
        ..., description="Ordered list of Pose2D waypoints. Required keys: x, y, theta, map_id. "
                         "Optional: latitude, longitude (WGS84 degrees).")
    mission_name: Optional[str] = Field(None, description="Optional mission name")
    timeout_seconds: int = Field(300, description="Mission timeout in seconds")


class DirectWaypointsResponse(BaseModel):
    """Response model for mapless direct-waypoint navigation."""
    success: bool
    robot_name: Optional[str] = None
    mission_name: Optional[str] = None
    waypoints_count: Optional[int] = None
    message: Optional[str] = None
    error: Optional[str] = None



class InvokeActionRequest(BaseModel):
    """Request model for invoking a custom VDA5050 action."""
    action_type: str = Field(..., description="Type of action to invoke")
    action_parameters: Optional[Dict[str, str]] = Field(default={}, description="Action parameters as key-value pairs")
    blocking_type: Optional[str] = Field(default="HARD", description="Blocking type: NONE, SOFT, or HARD")


class InvokeActionResponse(BaseModel):
    """Response model for action invocation."""
    success: bool
    action_id: Optional[str] = None
    robot_name: Optional[str] = None
    action_type: Optional[str] = None
    error: Optional[str] = None


class CreateTokenRequest(BaseModel):
    """Request model for LiveKit token creation."""
    participantName: str = Field(..., description="Unique identifier for the participant")
    roomName: str = Field(default="quickstart-room", description="Name of the room to join")
    ttl: Optional[int] = Field(default=None, description="Token time-to-live in seconds")
    metadata: Optional[str] = Field(default=None, description="Optional metadata for the participant")
    canPublish: bool = Field(default=True, description="Whether participant can publish tracks")
    canSubscribe: bool = Field(default=True, description="Whether participant can subscribe to tracks")
    canPublishData: bool = Field(default=True, description="Whether participant can publish data messages")


class CreateTokenResponse(BaseModel):
    """Response model for LiveKit token creation."""
    token: str = Field(..., description="JWT access token")
    ttl: int = Field(..., description="Token time-to-live in seconds")
    server_url: str = Field(..., description="LiveKit server WebSocket URL")


class CreateBagEntryRequest(BaseModel):
    """Request model for creating a ROS bag upload entry."""
    robot_name: str = Field(..., description="Name of the robot that produced the bag")
    description: Optional[str] = Field(None, description="Human-readable description of the recording")
    recorded_at: Optional[str] = Field(None, description="ISO-8601 timestamp when recording started")


class CreateBagEntryResponse(BaseModel):
    """Response model for ROS bag upload URL creation."""
    bag_id: str
    upload_url: str = Field(..., description="Presigned PUT URL — upload directly with rclone or curl")
    expires_in: int = Field(..., description="URL expiry in seconds")
    map_id: Optional[str] = Field(None, description="Map the robot was on at upload time, or null")
    robot_name: str
    datum_latitude: Optional[float] = None
    datum_longitude: Optional[float] = None
    datum_bearing_deg: Optional[float] = None


class BagMetadataResponse(BaseModel):
    """Response model for ROS bag metadata."""
    bag_id: str
    robot_name: str
    map_id: Optional[str] = None
    datum_latitude: Optional[float] = None
    datum_longitude: Optional[float] = None
    datum_bearing_deg: Optional[float] = None
    description: Optional[str] = None
    recorded_at: Optional[str] = None
    size: Optional[int] = None
    last_modified: Optional[str] = None
    download_url: Optional[str] = Field(None, description="Presigned GET URL for direct download")


class BagSummary(BaseModel):
    """Single bag entry in a list response."""
    bag_id: str
    robot_name: str
    map_id: Optional[str] = None
    datum_latitude: Optional[float] = None
    datum_longitude: Optional[float] = None
    datum_bearing_deg: Optional[float] = None
    description: Optional[str] = None
    recorded_at: Optional[str] = None
    size: Optional[int] = None


class BagListResponse(BaseModel):
    """Response model for listing ROS bags."""
    bags: List[BagSummary]
    count: int
    robot_name: Optional[str] = None
    map_id: Optional[str] = None


class CreateModelUploadUrlRequest(BaseModel):
    """Request model for registering a new base model and obtaining a presigned upload URL."""
    name: str = Field(..., description="Human-readable model name")
    description: Optional[str] = Field(None, description="Optional description of the model")
    format: Optional[str] = Field("onnx", description="Model format (e.g. onnx, pt, trt)")


class CreateModelUploadUrlResponse(BaseModel):
    """Response model for model upload URL creation."""
    model_id: str
    upload_url: str = Field(..., description="Presigned PUT URL — upload binary directly with curl or rclone")
    expires_in: int = Field(..., description="URL expiry in seconds")
    name: str
    format: Optional[str] = None


class ModelMetadataResponse(BaseModel):
    """Response model for a single base model's metadata."""
    model_id: str
    name: Optional[str] = None
    description: Optional[str] = None
    format: Optional[str] = None
    created_at: Optional[str] = None
    size: Optional[int] = None
    content_type: Optional[str] = None
    last_modified: Optional[str] = None
    uploaded: bool = False
    download_url: Optional[str] = Field(None, description="Presigned GET URL (None if not yet uploaded)")


class ModelListResponse(BaseModel):
    """Response model for listing base models."""
    models: List[ModelMetadataResponse]
    count: int


# HealthResponse is now imported from packages.utils.service_utils


class StatsResponse(BaseModel):
    """Response model for statistics."""
    service: str
    graph_db_url: str
    minio_host: str
    mission_planner_url: str
    livekit_url: str
    database_url: str
    default_map_id: str
    websocket_connections: Dict[str, int]


# ==================== FastAPI Application ====================

# Global service instance
service: Optional[ApiDelegationService] = None
health_checker: Optional[DependencyHealthChecker] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio
    global service, health_checker

    graph_builder_ws_url = os.getenv("GRAPH_BUILDER_WS_URL", "ws://localhost:8004")
    mission_dispatcher_ws_url = os.getenv("MISSION_DISPATCHER_WS_URL", None)
    mqtt_enabled = os.getenv("MQTT_ENABLED", "true").lower() == "true"

    service = ApiDelegationService(
        arango_host=ARANGO_HOST,
        arango_port=ARANGO_PORT,
        arango_username=ARANGO_USERNAME,
        arango_password=ARANGO_PASSWORD,
        arango_database=DATA_BASE_NAME,
        minio_host=MINIO_HOST,
        minio_port=MINIO_PORT,
        minio_access_key=MINIO_ACCESS_KEY,
        minio_secret_key=MINIO_SECRET_KEY,
        minio_secure=MINIO_SECURE,
        mission_planner_url=URL_MISSION_PLANNER,
        livekit_url=URL_LIVEKIT,
        postgres_db=POSTGRES_DATABASE_NAME,
        postgres_user=POSTGRES_DATABASE_USERNAME,
        postgres_password=POSTGRES_DATABASE_PASSWORD,
        postgres_host=POSTGRES_DATABASE_HOST,
        postgres_port=POSTGRES_DATABASE_PORT,
        default_map_id=DEFAULT_MAP_ID,
        graph_builder_ws_url=graph_builder_ws_url,
        mission_dispatcher_ws_url=mission_dispatcher_ws_url,
        mqtt_enabled=mqtt_enabled,
        mqtt_broker=MQTT_BROKER,
        mqtt_port=MQTT_PORT,
        mqtt_keepalive=MQTT_KEEPALIVE,
    )

    await service.database.async_init()
    service.start_watchers(asyncio.get_event_loop())
    # Map deletes left DELETING by a previous run (WP11 F1); one runner per map across workers.
    service.map_deleter.start_resume()
    # 3D reconstruction dispatcher (R3); only when the service is configured, one leader per
    # cluster (advisory lock).
    service.reconstruction.start_dispatcher()
    # SLAM saves lost to an offline robot / a restart (R6); one worker per cluster.
    service.mapping_switch.start_slam_reconcile(
        service.database, lambda: service.database.list_objects(RobotObjectV1),
        interval_s=SLAM_RECONCILE_INTERVAL_S)

    health_checker = DependencyHealthChecker(timeout=5.0)
    health_checker.add_dependency("graph_db", lambda: service.graph_db.is_healthy(), critical=True)
    health_checker.add_dependency("minio", lambda: service.image_db.is_healthy(), critical=True)
    health_checker.add_dependency("rosbag_db", lambda: service.rosbag_db.is_healthy(), critical=False)
    health_checker.add_dependency("database", lambda: service.database.is_running(), critical=True)

    app.state.service = service

    logging.info("✅ API Delegation Service started")

    yield

    if service:
        service.stop_watchers()
        await service.map_deleter.stop()
        await service.reconstruction.stop()
        await service.mapping_switch.stop_slam_reconcile()
        await service.stop_telemetry()
        logging.info("✅ API Delegation Service stopped")


app = FastAPI(
    title="API Delegation Service",
    description="Central API gateway for robot fleet management - provides unified REST and WebSocket interfaces",
    version="1.0.0",
    lifespan=lifespan,
)

# Add standardized error handlers
add_error_handlers(app)


def _idempotency_store() -> Optional[IdempotencyStore]:
    if service is None or not service.database.is_running():
        return None
    return IdempotencyStore(service.database.connection, ttl_s=IDEMPOTENCY_TTL_S,
                            lease_s=IDEMPOTENCY_LEASE_S)


# WP11 F3: Idempotency-Key on the side-effecting routes (packages/api/idempotency.py). Without
# the header every request behaves exactly as before.
app.add_middleware(IdempotencyMiddleware, store=_idempotency_store,
                   purge_interval_s=IDEMPOTENCY_PURGE_INTERVAL_S)

from packages.api.orchestrator_proxy import router as orchestrator_proxy_router
app.include_router(orchestrator_proxy_router)


# ==================== Health & Stats ====================

@app.get("/health", response_model=HealthResponse)
async def health_check():
    """Health check endpoint."""
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    dependencies = health_checker.check_all() if health_checker else {}
    return create_health_response(
        service_name="api_delegation",
        dependencies=dependencies
    )


@app.get("/stats", response_model=StatsResponse)
async def get_stats():
    """Get service statistics."""
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")
    
    stats = service.get_stats()
    return StatsResponse(**stats)


@app.get("/")
async def root():
    """Root endpoint with service information."""
    return create_root_response(
        service_name="API Delegation Service",
        version="1.0.0",
        description="Central API gateway for robot fleet management",
        endpoints={
            "health": "GET /health",
            "stats": "GET /stats",
            "list_maps": "GET /api/v1/maps",
            "load_map": "POST /api/v1/map/load",
            "maps": {
                "create": "POST /api/v1/maps",
                "get": "GET /api/v1/maps/{map_id}",
                "update": "PATCH /api/v1/maps/{map_id}",
                "graph": "GET /api/v1/maps/{map_id}/graph",
                "start_session": "POST /api/v1/maps/{map_id}/sessions",
                "session_action": "POST /api/v1/maps/{map_id}/sessions/{session_id}/"
                                  "{pause|resume|finish}",
                "archive": "POST /api/v1/maps/{map_id}/archive",
                "convert_type": "POST /api/v1/maps/{map_id}/type",
                "restore": "POST /api/v1/maps/{map_id}/restore",
                "delete": "DELETE /api/v1/maps/{map_id}",
            },
            "get_image": "GET /api/v1/images/{map_id}/{node_id}",
            "rosbags": {
                "upload_url": "POST /api/v1/rosbags/upload-url",
                "list_all": "GET /api/v1/rosbags",
                "list_by_robot": "GET /api/v1/rosbags/{robot_name}/list",
                "list_by_map": "GET /api/v1/rosbags/map/{map_id}/list",
                "metadata": "GET /api/v1/rosbags/{robot_name}/{bag_id}",
                "delete_one": "DELETE /api/v1/rosbags/{robot_name}/{bag_id}",
                "delete_robot": "DELETE /api/v1/rosbags/{robot_name}",
            },
            "base_models": {
                "upload_url": "POST /api/v1/base_models/upload-url",
                "list": "GET /api/v1/base_models",
                "get": "GET /api/v1/base_models/{model_id}",
                "download_url": "GET /api/v1/base_models/{model_id}/download-url",
                "delete": "DELETE /api/v1/base_models/{model_id}",
            },
            "navigate": "POST /api/v1/navigate",
            "create_livekit_token": "POST /api/createToken",
            "robots": {
                "list": "GET /api/v1/robots",
                "get": "GET /api/v1/robots/{robot_name}",
                "create": "POST /api/v1/robots",
                "update": "PUT /api/v1/robots/{robot_name}",
                "delete": "DELETE /api/v1/robots/{robot_name}",
                "status": "GET /api/v1/robots/{robot_name}/status",
                "invoke_action": "POST /api/v1/robots/{robot_name}/actions"
            },
            "missions": {
                "list": "GET /api/v1/missions",
                "get": "GET /api/v1/missions/{mission_name}",
                "create": "POST /api/v1/missions",
                "update": "PUT /api/v1/missions/{mission_name}",
                "delete": "DELETE /api/v1/missions/{mission_name}",
                "cancel": "POST /api/v1/missions/{mission_name}/cancel",
                "status": "GET /api/v1/missions/{mission_name}/status"
            },
            "detection_results": {
                "list": "GET /api/v1/detection_results",
                "get": "GET /api/v1/detection_results/{name}",
                "delete": "DELETE /api/v1/detection_results/{name}"
            },
            "websockets": {
                "map_updates": "WS /ws/map/{map_id}",
                "mission_status": "WS /ws/mission/{mission_name}",
                "robot_status": "WS /ws/robot/{robot_name}"
            }
        }
    )


# ==================== Map Operations ====================

@app.get("/api/v1/maps")
async def list_maps(type: Optional[str] = None, state: Optional[str] = None,
                    include_archived: bool = False):
    """List all maps registered in Postgres (includes datum and metadata).

    Maps redesign M1: every map carries `type` ('local'|'geo', plus `geo` for a geo map) and
    `status.state`. Optional filters `type=` and `state=`; archived maps are left out unless
    `include_archived=true` or `state=archived`. DELETING maps are always hidden."""
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")
    try:
        found = await service.database.list_objects(MapObjectV1)
        # A DELETING map is on its way out (packages/api/map_delete.py): hidden.
        views = maps.filter_maps(found, type_=type, state=state,
                                 include_archived=include_archived)
        # status.node_count/edge_count in the row are stale: report the graph's, as the detail does.
        views = await asyncio.to_thread(maps.apply_graph_counts, views,
                                        service.graph_db.get_map_stats)
        return {"maps": views, "count": len(views)}
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Failed to list maps: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to list maps: {str(e)}")


@app.post("/api/v1/maps", status_code=201)
async def create_map(body: Dict[str, Any]):
    """Create a map `{name, type: 'local'|'geo', description?}` in state `draft` (maps
    redesign M1). 409 if the name is taken. A geo map gets its UTM zone and origin from its
    first mapping session's datum."""
    _require_service()

    return await _site_call("create map", maps.create_map(
        service.database, body, uuid.uuid4(), recording.request_actor(),
        arango_node_count=_arango_node_count))



@app.post("/api/v1/map/load", response_model=LoadMapResponse)
async def load_map(request: LoadMapRequest):
    """Load a map: creates ArangoDB graph collections and registers in Postgres."""
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")
    # Loading would recreate the graph the background delete is removing.
    await service.ensure_map_not_deleting(request.map_id or service.default_map_id)

    result = await service.load_map(
        map_id=request.map_id,
        description=request.description,
        datum_latitude=request.datum_latitude,
        datum_longitude=request.datum_longitude,
        datum_bearing_deg=request.datum_bearing_deg,
        datum_frame=request.datum_frame,
        datum_utm_zone=request.datum_utm_zone,
        datum_utm_north=request.datum_utm_north,
        datum_utm_easting=request.datum_utm_easting,
        datum_utm_northing=request.datum_utm_northing,
    )
    return LoadMapResponse(**result)


@app.get("/api/v1/maps/{map_id}")
async def get_map(map_id: str):
    """Get a single map by ID, including datum and live node/edge counts."""
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")
    result = await service.get_map(map_id)
    if not result.get("success"):
        raise HTTPException(status_code=404, detail=result.get("error"))
    # Maps redesign M1: the map's mapping sessions (count, open one, newest first).
    # Plus `mapping_state` / `mapping_service(s)` of the open session's robot (its orchestrator).
    result["sessions"] = await _site_call("list map sessions",
                                          maps.session_summary(service.database, map_id,
                                                               service.mapping_switch,
                                                               result.get("type")))
    return result


@app.patch("/api/v1/maps/{map_id}")
async def patch_map(map_id: str, body: Dict[str, Any]):
    """Change a map's `description` and, on a local map, `slam_map` (bool; 409 for a geo map,
    an open mapping session or a pending SLAM save; MAP.SLAM_CHANGED). A map cannot be renamed
    (422). Returns the map view."""
    _require_service()
    return await _site_call("update map", maps.patch_map(
        service.database, map_id, body, uuid.uuid4(), service.mapping_switch,
        recording.request_actor()))


@app.get("/api/v1/maps/{map_id}/graph")
async def get_map_graph(map_id: str):
    """The map's nodes and edges (what POST /api/v1/map/load returns, without its side
    effects), with type/geo/state and the legacy datum `transform`."""
    _require_service()
    return await _site_call("read map graph", service.get_map_graph(map_id))


def _arango_node_count(name: str) -> int:
    stats = service.graph_db.get_map_stats(name)
    return 0 if "error" in stats else int(stats.get("node_count") or 0)


@app.post("/api/v1/maps/{map_id}/sessions", status_code=201)
async def start_map_session(map_id: str, body: Dict[str, Any]):
    """Start a session `{robot, purpose?, services?, placement?, replace?}` on the map (maps
    redesign M1, §14). `purpose`: "mapping" (default: the robot adds data; the map goes to
    `mapping` and graph-builder stores the robot's nodes and images in it) or "operate" (the
    robot uses the map for missions and display and adds nothing; the map state does not
    change). `services` (mapping only: "topo" | "grid" | "slam"; omitted = ["topo"] plus "slam" on a
    slam_map map; [] starts nothing; "slam" on a geo map: 400); `placement` {pose: {x, y, yaw},
    robot_pose: {x, y, theta}} puts the robot on a LOCAL map (422 on a geo map, which is placed
    by the robot's datum); `replace: true` finishes the robot's open session in the same
    transaction. Errors: packages/api/maps.py (module docstring). 409 also while a
    relocalization job runs for the robot (mapping sessions only). A mapping session's services
    are started on the robot's orchestrator OUTSIDE any transaction, AFTER the commit
    (a failed start never fails or undoes the session: it is reported in `robot_actions`, maps
    §14.16). The response: {map_id, map_state, changed, session, replaced_session,
    robot_actions, robot_notified, mapping_service, mapping_services, mapping_state} (+
    `mapping_warning` when a robot action failed, `slam_warning`). The session is
    the robot's map (maps §14.2; robots have no current_map since U6)."""
    _require_service()
    return await _site_call("start map session", maps.start_session(
        service.database, map_id, body, uuid.uuid4(), recording.request_actor(),
        switch=service.mapping_switch, arango_node_count=_arango_node_count,
        reloc_jobs=service.reloc_jobs))


@app.get("/api/v1/maps/{map_id}/sessions")
async def list_map_sessions(map_id: str, limit: Optional[int] = None,
                            before: Optional[str] = None):
    """The map's full session history, newest first, paged (maps §14.3): `limit` (1-200,
    default 50), `before` = the previous page's `next_before`. {map_id, count, items,
    next_before}."""
    _require_service()
    return await _site_call("list map sessions", maps.session_history(
        service.database, map_id, limit, before))


@app.get("/api/v1/maps/{map_id}/sessions/{session_id}/placement-suggestions")
async def map_session_placement_suggestions(map_id: str, session_id: str):
    """"Last position on this map" for an unplaced session (maps §14.3): {map_id, session_id,
    suggestions: [{source: "last_position", basis: unplace_snapshot | state_history |
    finished_session, map_T_session, pose, robot_pose, at, from_session_id}]}, at most one.
    Accept it with POST .../place (`pose` + the live `robot_pose`, optional `source`).
    Empty for a placed session; on a GEO map the one suggestion is the robot's current datum
    ({source: "datum", basis: "robot_datum", map_T_session, pose, robot_pose, at,
    datum_after_unplace}; accept with POST .../place {"source": "datum"}), or none without a
    usable datum. 404 unknown map/session; 409 finished session.
    Plus `reloc`: null, or {available, known, source: "orchestrator"} for an unplaced local-map
    session: `available` true = the robot's orchestrator holds a stored map for this map, so no
    manual initial position is needed (POST .../place with {"source": "reloc"})."""
    _require_service()
    return await _site_call("placement suggestions", maps.placement_suggestions(
        service.database, map_id, session_id, holder=service.orchestrator_maps,
        switch=service.mapping_switch, reloc_jobs=service.reloc_jobs))


@app.get("/api/v1/maps/{map_id}/reloc")
async def map_reloc(map_id: str, robot: str):
    """Does the robot's orchestrator hold a stored map for this map (relocalization)? Before any
    session exists: {available, known, source: "orchestrator"}; `available` true = no manual
    initial position needed, `known` false = could not be asked (then available is false: robot
    unknown/offline, orchestrator unreachable). Geo map: {false, true}. 404 unknown map."""
    _require_service()
    return await _site_call("map reloc", maps.map_reloc(
        service.database, service.orchestrator_maps, map_id, robot,
        switch=service.mapping_switch, reloc_jobs=service.reloc_jobs))


@app.post("/api/v1/maps/{map_id}/sessions/{session_id}/place")
async def place_map_session(map_id: str, session_id: str, body: Dict[str, Any]):
    """Place the session's robot on a local map (maps §14.3) `{pose: {x, y, yaw}, robot_pose:
    {x, y, theta}}`: `pose` in the map frame, `robot_pose` = the robot's own pose the user saw.
    A robot that drives or moved by more than 0.02 m / 0.5 deg is placed anyway, with `warnings`. 409 on a finished session or a geo map. From
    then on graph-builder keeps the session's nodes (the services are not touched).
    `{"source": "reloc"}` (no poses): the robot relocalises itself on the stored map its
    orchestrator holds; identity placement, no still check; 409 when it does not hold the map.
    `{"source": "datum"}` (no poses, GEO maps only): place an unplaced geo session from the
    robot's current GNSS datum (409 when it has none in the map's UTM zone, or the session is
    placed). Other sources on a geo map: 409 (placed by the datum).
    When the robot's orchestrator can start relocalization (`reloc.can_start` of the reloc
    reads), `{"source": "reloc"}` answers **202** `{..., "session", "reloc_job": {id, state, step,
    started_at, deadline, mode}}` and relocalizes in the background (mode "odin": Odin alone;
    "assisted": `{"source": "reloc", "reloc": {"init_pose": {x, y, yaw}}}`, cloud map frame):
    poll GET .../reloc-job, cancel with DELETE .../reloc-job. The 202 body is
    `{map_id, map_state, changed: false, session: <still unplaced>, reloc_job: {id, state, step,
    mode, started_at, deadline}}` (`deadline` is an estimate until the job waits for the robot).
    `can_start` (the reloc reads) is true whenever the robot exists; `warning` (= `can_start_reason`)
    is a non-blocking text on what may make the job fail (offline, no orchestrator, no stored map,
    another job, a pending SLAM save: place answers 409 for the last two). The job proposes the
    placement (state `confirming`) and places on POST .../reloc-job/confirm (or by itself after
    RELOC_CONFIRM_TIMEOUT_S); POST .../reloc-job/edit ends it for manual placement. A manual place
    never refuses a driving or moved robot: the answer carries `warnings`. Jobs live in this process only (single API
    worker)."""
    _require_service()
    out = await _site_call("place map session", maps.place_session(
        service.database, map_id, session_id, body, uuid.uuid4(), recording.request_actor(),
        switch=service.mapping_switch, holder=service.orchestrator_maps,
        reloc_jobs=service.reloc_jobs))
    if "reloc_job" in out:
        return JSONResponse(status_code=202, content=jsonable_encoder(out))
    return out


def _reloc_job_of(map_id: str, session_id: str):
    job = service.reloc_jobs.latest(map_id, session_id)
    if job is None:
        raise HTTPException(404, f"No relocalization job for session \"{session_id}\" on map "
                                 f"\"{map_id}\" (jobs are kept in memory and are lost when the "
                                 "API restarts)")
    return job


@app.get("/api/v1/maps/{map_id}/sessions/{session_id}/reloc-job")
async def get_reloc_job(map_id: str, session_id: str):
    """The session's latest relocalization job: {id, state: preparing | starting | waiting |
    confirming | placed | failed | cancelled | edit, step, mode: odin | assisted, started_at,
    deadline, error?, warnings?, position_initialized?, localization_score?, proposal? {map_T_session,
    pose, robot_pose, localization_score, confirm_deadline}, confirm_deadline?, auto_confirmed?}
    (see packages/api/README.md). 404 when there is none (also after an API
    restart: the registry is in memory)."""
    _require_service()
    return _reloc_job_of(map_id, session_id).view()


@app.delete("/api/v1/maps/{map_id}/sessions/{session_id}/reloc-job")
async def cancel_reloc_job(map_id: str, session_id: str):
    """Cancel the session's running relocalization job: the previous `init_pos` and current map
    are restored on the robot (the legacy relocalization service is NOT stopped; an endpoint-mode
    relocalization session the job started IS stopped, unless a SLAM session replaced it). Returns the job.
    404 no job; 409 it has finished."""
    _require_service()
    return (await service.reloc_jobs.cancel(_reloc_job_of(map_id, session_id))).view()


@app.post("/api/v1/maps/{map_id}/sessions/{session_id}/reloc-job/confirm")
async def confirm_reloc_job(map_id: str, session_id: str):
    """Confirm the proposal of the session's `confirming` relocalization job: the session is placed
    (identity map_T_session, source reloc, MAP.SESSION_PLACED) and the job ends `placed`. Returns
    the job. 404 no job; 409 the job is not `confirming` (or was decided already)."""
    _require_service()
    return (await service.reloc_jobs.confirm(_reloc_job_of(map_id, session_id))).view()


@app.post("/api/v1/maps/{map_id}/sessions/{session_id}/reloc-job/edit")
async def edit_reloc_job(map_id: str, session_id: str):
    """Reject the proposal of the `confirming` job to place by hand: the job ends `edit` (nothing
    is placed and nothing is rolled back: the relocalization driver keeps running). Returns the
    job with its `proposal` so the client can open manual placement prefilled. 404 no job; 409 not
    `confirming`."""
    _require_service()
    return (await service.reloc_jobs.edit(_reloc_job_of(map_id, session_id))).view()


@app.post("/api/v1/maps/{map_id}/sessions/{session_id}/unplace")
async def unplace_map_session(map_id: str, session_id: str):
    """Mark a placed OPERATE session on a LOCAL map as not placed (the old placement is kept for
    "last position"). PURPOSE: a developer / test hook, and "redo my placement": the system
    unplaces by itself when the robot's run changes; this lets the place and relocalization flows
    be re-run without restarting robot services. 409 on a finished session, a geo map,
    or while a relocalization job runs (a mapping session is unplaced with a `warnings` entry); an already unplaced session answers `changed: false`."""
    _require_service()
    return await _site_call("unplace map session", maps.unplace_session(
        service.database, map_id, session_id, uuid.uuid4(), recording.request_actor(),
        reloc_jobs=service.reloc_jobs))


@app.post("/api/v1/maps/{map_id}/sessions/{session_id}/{action}")
async def map_session_action(map_id: str, session_id: str, action: str):
    """`pause`, `resume` or `finish` a session. Finishing the map's only open mapping session
    makes the map `ready`; finishing an operate session is "Stop using" (the map state does
    not change). pause/resume: mapping sessions only (409 on operate). Repeating an action
    that is already in effect changes nothing in the session (a repeated pause/finish retries the
    stop, a repeated resume the start). After the commit resume starts the session's mapping
    services on the robot's orchestrator, pause/finish stop them; a failure never fails or undoes
    the action. The response adds `robot_actions` (what was done on the robot), `robot_notified`
    (false when one failed), `mapping_warning` and `mapping_state`."""
    _require_service()
    return await _site_call(f"{action} map session", maps.session_action(
        service.database, map_id, session_id, action, uuid.uuid4(),
        recording.request_actor(), switch=service.mapping_switch))


@app.post("/api/v1/maps/{map_id}/archive")
async def archive_map(map_id: str):
    """Archive a map: hidden from GET /api/v1/maps by default, nothing deleted. 409 while any
    session is open (mapping or operate); the message names the robots (maps §14, Q-U2)."""
    _require_service()
    return await _site_call("archive map", maps.archive_map(
        service.database, map_id, uuid.uuid4(), recording.request_actor()))


@app.post("/api/v1/maps/{map_id}/type")
async def convert_map_type(map_id: str, body: Dict[str, Any]):
    """Convert a map geo <-> local (docs/satinav-maps-redesign.md §17). Nothing moves: map-frame
    coordinates (nodes, edges, reconstruction, sessions' map_T_session, mission waypoints) stay
    valid. `{"type": "local"}` drops the georeference (kept as `former_datum`); `{"type": "geo",
    latitude, longitude, bearing_deg?, frame?, anchor?: {x, y}, utm_zone?, utm_north?}` puts the
    map-frame `anchor` (default the origin) at (latitude, longitude) with the map's +X axis
    `bearing_deg` from east, CCW (grid east for frame "utm", the default; true east for "enu").
    404 unknown map; 409 deleting, already of that type, or an open mapping session; 422 body.
    Open operate sessions keep their placement (`warnings` says what that means per robot).
    MAP.TYPE_CHANGED."""
    _require_service()
    return await _site_call("convert map type", maps.convert_map_type(
        service.database, map_id, body, uuid.uuid4(), recording.request_actor(),
        service.reloc_jobs))


@app.post("/api/v1/maps/{map_id}/restore")
async def restore_map(map_id: str):
    """Restore an archived map (to `ready`, or `draft` if it never had a session)."""
    _require_service()
    return await _site_call("restore map", maps.restore_map(
        service.database, map_id, uuid.uuid4(), recording.request_actor()))


class UpdateDatumRequest(BaseModel):
    """Request model for registering or updating a map's GPS datum."""
    datum_latitude: float = Field(..., description="WGS84 origin latitude in degrees")
    datum_longitude: float = Field(..., description="WGS84 origin longitude in degrees")
    datum_bearing_deg: float = Field(0.0, description="Angle of the map +X axis from east (grid east for utm), CCW, in degrees")
    datum_frame: Optional[Literal["enu", "utm"]] = Field(
        None, description="Frame of the map's local x/y: 'utm' = UTM grid offsets from the "
                          "datum (robot with GNSS), 'enu' = tangent-plane east/north (sim). "
                          "Default 'enu'.")
    datum_utm_zone: Optional[int] = Field(
        None, ge=1, le=60, description="UTM zone of a 'utm' datum (default: the datum's own)")
    datum_utm_north: Optional[bool] = Field(
        None, description="UTM hemisphere of a 'utm' datum (default: the datum's own)")
    datum_utm_easting: Optional[float] = Field(
        None, description="Exact UTM easting of a 'utm' datum (default: projected)")
    datum_utm_northing: Optional[float] = Field(
        None, description="Exact UTM northing of a 'utm' datum (default: projected)")



@app.put("/api/v1/maps/{map_id}/datum")
async def update_map_datum(map_id: str, request: UpdateDatumRequest):
    """Register or update the GPS datum for an existing map. 409 on a geo map that has nodes
    or mapping sessions: its origin is its first session's datum and fixed."""
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")
    await service.ensure_map_not_deleting(map_id)
    result = await service.update_map_datum(
        map_id,
        request.datum_latitude,
        request.datum_longitude,
        request.datum_bearing_deg,
        datum_frame=request.datum_frame,
        datum_utm_zone=request.datum_utm_zone,
        datum_utm_north=request.datum_utm_north,
        datum_utm_easting=request.datum_utm_easting,
        datum_utm_northing=request.datum_utm_northing,
    )
    if not result.get("success"):
        raise HTTPException(status_code=404, detail=result.get("error"))
    return result


class ApproxLocationRequest(BaseModel):
    """Request model for setting a local map's approximate location (a hint, not a datum)."""
    latitude: common_objects.Latitude = Field(..., description="WGS84 latitude in degrees")
    longitude: common_objects.Longitude = Field(..., description="WGS84 longitude in degrees")
    accuracy_m: Optional[common_objects.AccuracyM] = Field(
        None, description="Rough uncertainty radius in metres")
    source: Literal["manual", "robot"] = Field(
        "manual", description="'manual' (operator) or 'robot' (suggested from a robot's position)")


@app.put("/api/v1/maps/{map_id}/approx_location")
async def update_map_approx_location(map_id: str, request: ApproxLocationRequest):
    """Set the approximate location of a local map (pins, distance sort; never placement).
    404 unknown map, 409 on a geo map (its location is its datum/transform), 422 on (0, 0)."""
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")
    await service.ensure_map_not_deleting(map_id)
    result = await service.update_map_approx_location(
        map_id, request.latitude, request.longitude,
        accuracy_m=request.accuracy_m, source=request.source)
    if not result.get("success"):
        raise HTTPException(status_code=404, detail=result.get("error"))
    return result


@app.delete("/api/v1/maps/{map_id}", status_code=202)
async def delete_map(map_id: str):
    """
    Delete a map and all its data from the graph and image databases.

    409 while any session is open on the map (mapping or operate; the message names the
    robots, maps §14 Q-U2).

    Returns 202 at once: the map is marked DELETING (hidden from GET /api/v1/maps, 409 on
    assign/load/datum) and a background task deletes it from ArangoDB and MinIO, retrying
    with backoff, then removes it from Postgres (packages/api/map_delete.py). Repeating the
    request is harmless; for a map stuck in DELETING it starts a new round of attempts.

    WARNING: This will permanently delete all map data including nodes, edges, and images!
    """
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    try:
        return await service.delete_map(map_id)
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Failed to delete map {map_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to delete map: {str(e)}")


# ==================== 3D reconstruction (R3) ====================
# docs/reconstruction/design.md §9; the logic is packages/api/reconstruction.py. The work runs in
# an external service; these routes own the job. Errors carry {code, message}: 404
# map_not_found / no_active_job / no_reconstruction / file_not_found, 409 map_deleting /
# job_active (with the job) / no_depth, 422 bad parameters / too_many_nodes, 503
# not_configured. The service being down is not an error here: the job queues.

@app.post("/api/v1/maps/{map_id}/reconstruction", status_code=202)
async def start_map_reconstruction(map_id: str, body: Optional[Dict[str, Any]] = None):
    """Start (or rebuild) the map's 3D reconstruction. Body optional: {voxel_m?, max_depth_m?,
    clip_z?}. 202 + the job (queued); the dispatcher sends it to the reconstruction service."""
    _require_service()
    return await _site_call("start reconstruction", service.reconstruction.start(
        map_id, body, recording.request_actor()))


@app.get("/api/v1/maps/{map_id}/reconstruction")
async def get_map_reconstruction(map_id: str):
    """{map_name, configured, reconstruction, job}: the current result (with `stale` and
    `stale_reason`, and file URLs) and the active job, or the newest job if it failed or was
    cancelled after the current result. Poll every 2 s while a job is active."""
    _require_service()
    return await _site_call("read reconstruction", service.reconstruction.status(map_id))


@app.post("/api/v1/maps/{map_id}/reconstruction/cancel")
async def cancel_map_reconstruction(map_id: str):
    """Cancel the active job (queued: at once; running: the service is asked, the job ends
    `cancelled` when it stops, at the latest 60 s later). The job."""
    _require_service()
    return await _site_call("cancel reconstruction", service.reconstruction.cancel(map_id))


@app.delete("/api/v1/maps/{map_id}/reconstruction", status_code=204)
async def delete_map_reconstruction(map_id: str):
    """Delete the current result (its files go) and cancel an active job."""
    _require_service()
    await _site_call("delete reconstruction", service.reconstruction.delete(map_id))
    return Response(status_code=204)


@app.get("/api/v1/maps/{map_id}/reconstruction/files/{name}")
async def get_map_reconstruction_file(map_id: str, name: str, v: Optional[str] = None,
                                      if_none_match: Optional[str] = Header(None)):
    """cloud.ply | ortho.png | height.png | meta.json of the current result, streamed from the
    map bucket. ETag = the job id; with `?v=<job id>` the response is cached forever."""
    _require_service()
    info = await _site_call("read reconstruction file",
                            service.reconstruction.open_file(map_id, name))
    etag = f'"{info["job_id"]}"'
    cache = ("public, max-age=31536000, immutable" if v == info["job_id"] else "no-cache")
    headers = {"ETag": etag, "Cache-Control": cache}
    if if_none_match and etag in [t.strip() for t in if_none_match.split(",")]:
        return Response(status_code=304, headers=headers)
    if info.get("bytes") is not None:
        headers["Content-Length"] = str(info["bytes"])
    if name == "cloud.ply":
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in map_id)
        headers["Content-Disposition"] = f'attachment; filename="{safe}-reconstruction.ply"'
    stream = service.reconstruction.objects.stream(info["bucket"], info["key"])
    return StreamingResponse(stream, media_type=info["content_type"], headers=headers)


# Service -> gateway callbacks (design §6.3). Not under /api/, so the client's nginx never
# forwards them. Authorization: Bearer <per-job HMAC token>. Answers are the bare
# {"action": ...} bodies of handover §3.4 (200 continue|cancel, 410 stop).
@app.post("/internal/reconstruction/jobs/{job_id}/{kind}")
async def reconstruction_callback(job_id: str, kind: str, body: Dict[str, Any],
                                  authorization: Optional[str] = Header(None)):
    _require_service()
    handlers = {"progress": service.reconstruction.on_progress,
                "finish": service.reconstruction.on_finish,
                "fail": service.reconstruction.on_fail}
    if kind not in handlers:
        raise HTTPException(status_code=404, detail="unknown callback")
    service.reconstruction.authorize(job_id, authorization)
    status, answer = await _site_call(f"reconstruction {kind}", handlers[kind](job_id, body))
    return JSONResponse(status_code=status, content=answer)


# ==================== Fleet Settings ====================
# A single operator-editable settings object, always stored under
# GLOBAL_SETTINGS_NAME — see cloud_common/objects/settings.py for why this is
# a singleton simulated by convention rather than a distinct storage mode.

async def _get_or_create_settings() -> SettingsObjectV1:
    try:
        return await service.database.get_object(SettingsObjectV1, GLOBAL_SETTINGS_NAME)
    except Exception:
        settings = SettingsObjectV1(name=GLOBAL_SETTINGS_NAME, lifecycle=ObjectLifecycleV1.ALIVE)
        try:
            await service.database.create_object(settings, uuid.uuid4())
            return settings
        except HTTPException as exc:
            # Another concurrent request already created the row between our
            # get_object miss and this create_object call (UniqueViolation,
            # surfaced as a 400 by the database layer) — that request's copy is
            # just as valid as the one we would have created, so fetch and use
            # it instead of failing this request over a benign race.
            if exc.status_code == 400:
                return await service.database.get_object(SettingsObjectV1, GLOBAL_SETTINGS_NAME)
            raise


def _hook_kwargs(hook) -> Dict[str, Any]:
    """`before_commit` only when there is a hook, so calls without one are unchanged."""
    return {"before_commit": hook} if hook is not None else {}


@app.get("/api/v1/settings")
async def get_settings():
    """Fetch the fleet-wide settings object, creating it with defaults on first read."""
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")
    try:
        settings = await _get_or_create_settings()
        return settings.dict()
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Failed to get settings: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get settings: {str(e)}")


# Keys of the settings object accepted in a PUT body but never written from it, so a client can
# send back what GET returned.
SETTINGS_IGNORED_KEYS = frozenset({"status", "name", "lifecycle"})


def _settings_changes(settings_data: Dict[str, Any]) -> Dict[str, Any]:
    """The spec fields in a PUT body (WP11 F2): 422 listing every unknown key instead of
    silently dropping them, and 422 on a bad `telemetry_recording`."""
    unknown = sorted(k for k in settings_data
                     if k not in SettingsSpecV1.__fields__ and k not in SETTINGS_IGNORED_KEYS)
    if unknown:
        raise HTTPException(status_code=422, detail=[
            {"loc": ["body", k], "msg": "extra fields not permitted", "type": "value_error.extra"}
            for k in unknown])
    recording.check_level(settings_data)
    return {k: v for k, v in settings_data.items() if k in SettingsSpecV1.__fields__}


@app.put("/api/v1/settings")
async def update_settings(settings_data: dict):
    """Update the fleet-wide settings object (creates it first if it doesn't exist yet).

    Partial: only the keys sent change. Unknown keys are a 422 that lists them; name, status
    and lifecycle are accepted and ignored."""
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")
    try:
        changes = _settings_changes(settings_data)
        settings = await _get_or_create_settings()

        publisher_id = uuid.uuid4()
        try:
            spec = SettingsSpecV1(**{**settings.spec.dict(), **changes})
        except ValidationError as e:
            raise HTTPException(status_code=422, detail=[
                {"loc": ["body", *err["loc"]], "msg": err["msg"], "type": err["type"]}
                for err in e.errors()])
        hook = None
        if recording.SPEC_FIELD in changes:
            # TELEMETRY.RECORDING_CHANGED in the same transaction (packages/api/recording.py)
            hook = recording.change_hook(recording.RecordingScope.GLOBAL, None,
                                         recording.request_actor())
        await service.database.update_spec(SettingsObjectV1, settings.name, spec,
                                           publisher_id, **_hook_kwargs(hook))

        updated_settings = await service.database.get_object(SettingsObjectV1, GLOBAL_SETTINGS_NAME)
        return updated_settings.dict()
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Failed to update settings: {e}")
        raise HTTPException(status_code=400, detail=f"Failed to update settings: {str(e)}")


@app.websocket("/ws/map/{map_id}")
async def websocket_map_updates(websocket: WebSocket, map_id: str):
    """
    WebSocket endpoint for real-time map updates.

    Clients connect to this endpoint to receive updates from the graph builder service.
    This endpoint proxies the connection to the backend Graph Builder Service.
    """
    if service is None:
        await websocket.close(code=1011, reason="Service not initialized")
        return

    # Use the new proxy manager to forward updates from backend
    await service.ws_proxy.proxy_map_updates(websocket, map_id)


# ==================== Image Operations ====================

# NOTE: Routes are ordered from most specific to least specific to ensure proper matching
# FastAPI matches routes in order, so more specific routes must come first

@app.get("/api/v1/images/{map_id}/{node_id}/list")
async def list_node_images(map_id: str, node_id: str):
    """
    List all images for a specific node.

    Args:
        map_id: Map ID (path parameter)
        node_id: Node ID (path parameter)

    Returns:
        JSON array of image IDs
    """
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    try:
        image_ids = await service.list_node_images(
            map_id=map_id,
            node_id=node_id
        )

        return {"image_ids": image_ids}
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error listing images for node {node_id} in map {map_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Error listing images: {str(e)}")


@app.get("/api/v1/images/{map_id}/{node_id}/{image_id}/metadata")
async def get_image_metadata(map_id: str, node_id: str, image_id: str):
    """
    Get metadata for a specific image.

    Args:
        map_id: Map ID (path parameter)
        node_id: Node ID (path parameter)
        image_id: Image ID (path parameter)

    Returns:
        JSON object with image metadata including yaw_offset
    """
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    try:
        metadata = await service.get_image_metadata(
            map_id=map_id,
            node_id=node_id,
            image_id=image_id
        )

        if metadata is None:
            raise HTTPException(status_code=404, detail=f"Image {image_id} not found")

        return metadata
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error getting metadata for image {image_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Error getting image metadata: {str(e)}")


@app.get("/api/v1/images/{map_id}/{node_id}")
async def get_image(map_id: str, node_id: str, image_id: Optional[str] = None):
    """
    Retrieve an image from the image database.

    Args:
        map_id: Map ID (path parameter)
        node_id: Node ID (path parameter)
        image_id: Image ID (optional query parameter - gets first image if not provided)

    Returns:
        Image data as binary response
    """
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    try:
        image_data = await service.get_image(
            map_id=map_id,
            node_id=node_id,
            image_id=image_id
        )

        if image_data is None:
            raise HTTPException(status_code=404, detail="Image not found")

        # Ensure image_data is bytes
        if not isinstance(image_data, bytes):
            logging.error(f"Image data is not bytes! Type: {type(image_data)}")
            image_data = bytes(image_data)

        # Use Response with explicit headers to prevent JSON encoding
        return Response(
            content=image_data,
            media_type="image/jpeg",
            headers={
                "Content-Type": "image/jpeg",
                "Content-Length": str(len(image_data))
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error retrieving image for node {node_id} in map {map_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Error retrieving image: {str(e)}")


# ==================== ROS Bag Operations ====================
# Route ordering: fixed-segment paths (/upload-url, /map/) before parameterised ones.

@app.post("/api/v1/rosbags/upload-url", response_model=CreateBagEntryResponse)
async def create_bag_upload_url(request: CreateBagEntryRequest):
    """
    Create a ROS bag entry and return a presigned PUT URL.

    map_id and GPS datum are resolved automatically from the robot's current state
    and stored as sidecar metadata. Both may be null if the robot has no active map
    or has not reported a datum.

    Upload the binary directly to MinIO using the returned URL:
        curl --upload-file bag.bag "{upload_url}"
        rclone copyto bag.bag "{upload_url}"
    """
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    result = await service.create_bag_upload_url(
        robot_name=request.robot_name,
        description=request.description,
        recorded_at=request.recorded_at,
    )
    if result is None:
        raise HTTPException(status_code=500, detail="Failed to create upload URL")

    return CreateBagEntryResponse(**result)


@app.get("/api/v1/rosbags", response_model=BagListResponse)
async def list_all_bags():
    """List all ROS bags across all robots."""
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    bags = await service.list_bags()
    return BagListResponse(bags=[BagSummary(**b) for b in bags], count=len(bags))


@app.get("/api/v1/rosbags/map/{map_id}/list", response_model=BagListResponse)
async def list_bags_for_map(map_id: str):
    """List all ROS bags whose sidecar metadata records the given map_id."""
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    bags = await service.list_bags(map_id=map_id)
    return BagListResponse(bags=[BagSummary(**b) for b in bags], count=len(bags), map_id=map_id)


@app.get("/api/v1/rosbags/{robot_name}/list", response_model=BagListResponse)
async def list_bags_for_robot(robot_name: str):
    """List all ROS bags for a specific robot."""
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    bags = await service.list_bags(robot_name=robot_name)
    return BagListResponse(bags=[BagSummary(**b) for b in bags], count=len(bags), robot_name=robot_name)


@app.get("/api/v1/rosbags/{robot_name}/{bag_id}", response_model=BagMetadataResponse)
async def get_bag_metadata(robot_name: str, bag_id: str):
    """
    Get metadata for a ROS bag, including a presigned download URL.

    Use the download_url to retrieve the binary directly with curl or rclone.
    """
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    meta = await service.get_bag_metadata(robot_name=robot_name, bag_id=bag_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="Bag not found")

    return BagMetadataResponse(**meta)


@app.delete("/api/v1/rosbags/{robot_name}/{bag_id}")
async def delete_bag(robot_name: str, bag_id: str):
    """Delete a single ROS bag and its metadata sidecar."""
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    result = await service.delete_bag(robot_name=robot_name, bag_id=bag_id)
    if not result.get("success"):
        raise HTTPException(status_code=404, detail="Bag not found or could not be deleted")

    return result


@app.delete("/api/v1/rosbags/{robot_name}")
async def delete_robot_bags(robot_name: str):
    """Delete all ROS bags for a specific robot."""
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    return await service.delete_robot_bags(robot_name=robot_name)


# ==================== Base Model Operations ====================

@app.post("/api/v1/base_models/upload-url", response_model=CreateModelUploadUrlResponse)
async def create_model_upload_url(request: CreateModelUploadUrlRequest):
    """
    Register a new base model and obtain a presigned PUT URL for binary upload.

    After receiving the response, upload the model binary directly to MinIO:

        curl --upload-file model.onnx "{upload_url}"

    The model entry (with metadata) is created immediately; the binary is
    considered uploaded once the PUT completes.
    """
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    result = await service.create_model_upload_url(
        name=request.name,
        description=request.description,
        format=request.format,
    )
    if result is None:
        raise HTTPException(status_code=500, detail="Failed to create model upload URL")

    return CreateModelUploadUrlResponse(**result)


@app.get("/api/v1/base_models", response_model=ModelListResponse)
async def list_models():
    """
    List all registered base models with metadata and presigned download URLs.

    Models that have been registered but whose binary has not yet been uploaded
    will appear in the list with uploaded=False and download_url=None.
    """
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    models = await service.list_models()
    return ModelListResponse(
        models=[ModelMetadataResponse(**m) for m in models],
        count=len(models),
    )


@app.get("/api/v1/base_models/{model_id}", response_model=ModelMetadataResponse)
async def get_model_metadata(model_id: str):
    """
    Get metadata for a specific base model, including a presigned download URL.

    Use the download_url to retrieve the binary directly:

        curl -L "{download_url}" -o model.onnx
    """
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    meta = await service.get_model_metadata(model_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="Model not found")

    return ModelMetadataResponse(**meta)


@app.delete("/api/v1/base_models/{model_id}")
async def delete_model(model_id: str):
    """Delete a base model (binary and metadata)."""
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    result = await service.delete_model(model_id)
    if not result.get("success"):
        raise HTTPException(status_code=404, detail="Model not found or could not be deleted")

    return result


@app.get("/api/v1/base_models/{model_id}/download-url")
async def get_base_model_download_url(model_id: str):
    """
    Get only the presigned download URL for a base model's binary.

    Returns 404 if the model does not exist or its binary has not yet been
    uploaded. Used by the robot orchestrator to fetch a model directly from
    object storage.
    """
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    url = service.model_db.get_download_url(model_id)
    if url is None:
        raise HTTPException(status_code=404, detail="Model not found or not yet uploaded")

    return {"download_url": url, "model_id": model_id}


# ==================== Navigation Operations ====================

@app.post("/api/v1/navigate", response_model=NavigationResponse)
async def navigate(request: NavigationRequest):
    """
    Request navigation for a robot (proxy to mission planner).

    Accepts either local Cartesian coordinates (target_x, target_y) or GPS
    coordinates (target_lat, target_lon). GPS requires the map to have a datum
    registered via PUT /api/v1/maps/{map_id}/datum.
    """
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    has_xy = request.target_x is not None and request.target_y is not None
    has_gps = request.target_lat is not None and request.target_lon is not None
    if not has_xy and not has_gps:
        raise HTTPException(
            status_code=400,
            detail="Provide either (target_x, target_y) or (target_lat, target_lon).",
        )

    result = await service.navigate(
        robot_name=request.robot_name,
        target_x=request.target_x,
        target_y=request.target_y,
        target_lat=request.target_lat,
        target_lon=request.target_lon,
        map_id=request.map_id,
        mission_name=request.mission_name,
        timeout_seconds=request.timeout_seconds,
    )

    return NavigationResponse(**result)


@app.post("/api/v1/navigate/waypoints", response_model=DirectWaypointsResponse)
async def navigate_waypoints(request: DirectWaypointsRequest):
    """
    Submit a mapless mission from an explicit ordered list of waypoints.

    Unlike POST /api/v1/navigate, this endpoint requires NO pre-built topological graph.
    The robot follows the provided waypoints directly in order.

    Use this for:
    - GPS-based path following (set x/y from a local projection; carry lat/lon alongside)
    - Indoor coordinate-based navigation without a graph
    - Sessions where mapping is happening concurrently on the robot

    Each waypoint dict:
    - Required: x (m), y (m), theta (rad), map_id (str)
    - Optional: latitude (WGS84°), longitude (WGS84°)

    Monitor progress via the mission status WebSocket once the mission name is returned.
    """
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    result = await service.direct_waypoints(
        robot_name=request.robot_name,
        waypoints=request.waypoints,
        mission_name=request.mission_name,
        timeout_seconds=request.timeout_seconds,
    )

    return DirectWaypointsResponse(**result)


# ==================== LiveKit Operations ====================

@app.post("/api/createToken", response_model=CreateTokenResponse)
async def create_livekit_token(request: CreateTokenRequest):
    """
    Create a LiveKit access token (proxy to LiveKit service).

    This endpoint generates a JWT token that allows a participant to join a LiveKit room.
    The token includes permissions for publishing/subscribing to tracks and data messages.

    Args:
        request: Token creation request with participant details

    Returns:
        Token details including the JWT, server URL, and configuration
    """
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    result = await service.create_livekit_token(
        participant_name=request.participantName,
        room_name=request.roomName,
        ttl=request.ttl,
        metadata=request.metadata,
        can_publish=request.canPublish,
        can_subscribe=request.canSubscribe,
        can_publish_data=request.canPublishData
    )

    return CreateTokenResponse(**result)


# ==================== Robot Operations ====================

async def _robot_views(robots: List[RobotObjectV1],
                       sessions: Optional[Dict[str, Dict[str, Any]]] = None
                       ) -> List[Dict[str, Any]]:
    """robot.dict() plus `mapping_state`: the robot's topo mapping service as its orchestrator
    reports it, with `status` on/off/unreachable (packages/api/mapping_switch.py), or null (robot
    offline, no registered orchestrator, no topo service there); `mapping_services`:
    {service: running | not_running | not_available};
    plus (maps §14) `session`: the robot's open session, derived from map_sessions and never
    stored on the robot: {session_id, map, purpose, state, aligned, map_T_session,
    unplaced_reason} or null (mapless). It is the robot's map (robot.current_map was removed in
    maps U6; packages/utils/map_sessions.py::robot_session_view)."""
    snaps = await service.mapping_switch.snapshots(robots) if service else {}
    out = []
    for robot in robots:
        data = robot.dict()
        snap = snaps.get(robot.name)
        session = (sessions or {}).get(robot.name)
        data["mapping_state"] = snap.state(session) if snap else None
        data["mapping_services"] = snap.mapping_services() if snap else None
        data["session"] = session
        data["localization_warning"] = ms.localization_warning(session, robot.status)
        out.append(data)
    return out


@app.get("/api/v1/robots", response_model=List[dict])
async def list_robots(
    min_battery: Optional[float] = Query(None, description="Minimum battery level"),
    max_battery: Optional[float] = Query(None, description="Maximum battery level"),
    state: Optional[str] = Query(None, description="Robot state filter"),
    online: Optional[bool] = Query(None, description="Online status filter"),
    robot_type: Optional[str] = Query(None, description="Robot type filter")
):
    """
    List all robots (proxy to Mission Dispatcher database).

    Supports filtering by battery level, state, online status, and robot type.
    """
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    try:
        params = {}
        if min_battery is not None:
            params["min_battery"] = min_battery
        if max_battery is not None:
            params["max_battery"] = max_battery
        if state is not None:
            params["state"] = state
        if online is not None:
            params["online"] = online
        if robot_type is not None:
            params["robot_type"] = robot_type

        robots = await service.database.list_objects(RobotObjectV1, query_params=params.items() if params else None)
        sessions = await maps.robot_sessions(service.database)
        return await _robot_views(robots, sessions)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list robots: {str(e)}")


@app.get("/api/v1/robots/{robot_name}")
async def get_robot(robot_name: str):
    """
    Get a specific robot by name (proxy to Mission Dispatcher database).

    Returns the complete robot object including spec and status.
    """
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    try:
        robot = await service.database.get_object(RobotObjectV1, robot_name)
        return (await _robot_views([robot], await maps.robot_sessions(service.database)))[0]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Robot not found: {str(e)}")


@app.get("/api/v1/robots/{robot_name}/status")
async def get_robot_status(robot_name: str):
    """
    Get robot status (proxy to Mission Dispatcher database).

    Returns the current status of a robot including position, battery, etc.
    """
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    try:
        robot = await service.database.get_object(RobotObjectV1, robot_name)
        return robot.status.dict()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Robot not found: {str(e)}")


@app.get("/api/v1/robots/{robot_name}/diagnostics")
async def get_robot_diagnostics(robot_name: str):
    """
    Get the latest cached system diagnostics (jtop/host_stats/ros_health) for a robot.

    Served from an in-memory cache populated by MQTT `<robot_name>/diagnostics`
    messages, not from the database — used to warm the client on load/reconnect
    before the next WebSocket push arrives. Not having received diagnostics yet is
    a normal, expected state (same as an offline robot having no pose) rather than
    an error, so this returns 200 with a null `diagnostics` field instead of a 404.
    """
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    cached = service.diagnostics.get_cached(robot_name)
    if cached is None:
        return {
            "type": "diagnostics_update",
            "robot_name": robot_name,
            "timestamp": None,
            "robot_timestamp": None,
            "diagnostics": None,
        }
    return cached


@app.get("/api/v1/robots/{robot_name}/nav2_bt_tree")
async def get_robot_nav2_bt_tree(robot_name: str):
    """
    Get the latest cached behavior tree XML(s) for a robot.

    Served from an in-memory cache populated by MQTT `<robot_name>/nav2_bt_tree`
    messages (retained, published once at startup then only on an XML change), not
    from the database. Not having received a tree yet is a normal, expected state
    rather than an error, so this returns 200 with a null `trees` field instead of
    a 404.
    """
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    cached = service.diagnostics.get_cached_bt_tree(robot_name)
    if cached is None:
        return {
            "type": "nav2_bt_tree_update",
            "robot_name": robot_name,
            "timestamp": None,
            "trees": None,
        }
    return cached


@app.get("/api/v1/robots/{robot_name}/nav2_bt_state")
async def get_robot_nav2_bt_state(robot_name: str):
    """
    Get the latest cached behavior tree node states for a robot.

    Served from an in-memory cache populated by MQTT `<robot_name>/nav2_bt_state`
    messages (live, coalesced to 5Hz by the publisher), not from the database —
    used to warm the client on load/reconnect before the next WebSocket push
    arrives. Not having received a state update yet is a normal, expected state
    rather than an error, so this returns 200 with a null `nodes` field instead of
    a 404.
    """
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    cached = service.diagnostics.get_cached_bt_state(robot_name)
    if cached is None:
        return {
            "type": "nav2_bt_state_update",
            "robot_name": robot_name,
            "timestamp": None,
            "robot_stamp": None,
            "nodes": None,
        }
    return cached


@app.get("/api/v1/robots/{robot_name}/nav_supervisor")
async def get_robot_nav_supervisor(robot_name: str):
    """
    Get the latest cached NavSupervisor goal-window state for a robot.

    Served from an in-memory cache populated by MQTT `<robot_name>/nav_supervisor`
    messages (live, event-driven on goal-window state transitions), not from the
    database — used to warm the client on load/reconnect before the next WebSocket
    push arrives. Not having received one yet is a normal, expected state (either the
    robot hasn't reported since startup yet, or its nav stack predates the
    NavSupervisor node) rather than an error, so this returns 200 with a null
    `supervisor` field instead of a 404.
    """
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    cached = service.diagnostics.get_cached_nav_supervisor(robot_name)
    if cached is None:
        return {
            "type": "nav_supervisor_update",
            "robot_name": robot_name,
            "timestamp": None,
            "robot_stamp": None,
            "supervisor": None,
        }
    return cached


def _apply_factsheet_limits(factsheet, data: dict) -> None:
    """Copy the registration factsheet's limits (snake_case field names) that are present and
    valid; anything else keeps the stored value (-1 = unknown)."""
    for field, _key, minimum in FACTSHEET_PHYSICAL_FIELDS:
        value = data.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= minimum:
            setattr(factsheet, field, float(value))


@app.post("/api/v1/robots", response_model=dict)
async def create_robot(robot_data: dict):
    """
    Register a robot (upsert). Creates the robot if it doesn't exist, otherwise updates
    its IP address. Intended to be called by the robot on every startup.
    """
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    try:
        if "name" not in robot_data:
            raise HTTPException(status_code=400, detail="Missing required field: name")
        # Validated on registration too; like the other spec fields it only applies when the
        # robot is created (an existing robot's level is changed with PUT).
        recording.check_level(robot_data)

        publisher_id = uuid.uuid4()
        ip_address = robot_data.pop("ip_address", None)
        entrypoint_port = robot_data.pop("entrypoint_port", None)
        position_mode = robot_data.pop("position_mode", None)
        factsheet_data = robot_data.pop("factsheet", None)
        current_model = robot_data.pop("current_model", None)

        # Try to fetch existing robot; any exception (including 404 HTTPException) means not found
        try:
            robot = await service.database.get_object(RobotObjectV1, robot_data["name"])
        except Exception:
            robot = None

        if robot is not None:
            # Robot already exists — update IP, port, and position_mode if provided
            spec_changed = False
            if ip_address is not None:
                robot.ip_address = ip_address
                spec_changed = True
            if entrypoint_port is not None:
                robot.entrypoint_port = entrypoint_port
                spec_changed = True
            if position_mode is not None and robot.position_mode != position_mode:
                robot.position_mode = position_mode
                spec_changed = True
            if current_model is not None and robot.current_model != current_model:
                robot.current_model = current_model
                spec_changed = True
            if spec_changed:
                await service.database.update_spec(RobotObjectV1, robot.name, robot.spec, publisher_id)
            if factsheet_data:
                robot.status.factsheet.agv_class = factsheet_data.get("agv_class", robot.status.factsheet.agv_class)
                _apply_factsheet_limits(robot.status.factsheet, factsheet_data)
                robot.status.factsheet.custom_actions = [
                    CustomActionV1(**a) for a in factsheet_data.get("actions", [])
                ]
                await service.database.update_status(RobotObjectV1, robot.name, robot.status, publisher_id)
            return (await service.database.get_object(RobotObjectV1, robot_data["name"])).dict()
        else:
            # Robot doesn't exist — create it (a `current_map` in the body is ignored: maps U6,
            # the robot's map is its open session)
            status = RobotStatusV1()
            if factsheet_data:
                status.factsheet.agv_class = factsheet_data.get("agv_class", "")
                _apply_factsheet_limits(status.factsheet, factsheet_data)
                status.factsheet.custom_actions = [
                    CustomActionV1(**a) for a in factsheet_data.get("actions", [])
                ]
            robot_data_with_defaults = {"status": status, "lifecycle": ObjectLifecycleV1.ALIVE, **robot_data}
            if ip_address is not None:
                robot_data_with_defaults["ip_address"] = ip_address
            if entrypoint_port is not None:
                robot_data_with_defaults["entrypoint_port"] = entrypoint_port
            if position_mode is not None:
                robot_data_with_defaults["position_mode"] = position_mode
            if current_model is not None:
                robot_data_with_defaults["current_model"] = current_model
            robot = RobotObjectV1(**robot_data_with_defaults)
            hook = None
            if robot.telemetry_recording is not None:
                hook = recording.change_hook(recording.RecordingScope.ROBOT, robot.name,
                                             recording.request_actor())
            await service.database.create_object(robot, publisher_id, **_hook_kwargs(hook))
            return robot.dict()
    except HTTPException:
        raise
    except Exception as e:
        logging.exception(f"Failed to register robot: {str(e)}")
        raise HTTPException(status_code=400, detail=f"Failed to register robot: {str(e)}")


@app.put("/api/v1/robots/{robot_name}")
async def update_robot(robot_name: str, robot_data: dict):
    """
    Update a robot (proxy to Mission Dispatcher database).

    Updates the robot's spec or status based on provided data.
    """
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    try:
        recording.check_level(robot_data)
        # Get existing robot
        robot = await service.database.get_object(RobotObjectV1, robot_name)
        # Maps U6: robots have no current_map (their map is the open session); an old
        # caller's field is ignored rather than failing the whole update.
        robot_data = {k: v for k, v in robot_data.items() if k != "current_map"}

        publisher_id = uuid.uuid4()

        # Update spec if provided
        if "status" not in robot_data or len(robot_data) > 1:
            # This is a spec update
            for key, value in robot_data.items():
                if key != "status" and key != "name" and key != "lifecycle":
                    setattr(robot, key, value)
            hook = None
            if recording.SPEC_FIELD in robot_data:
                # TELEMETRY.RECORDING_CHANGED in the same transaction (packages/api/recording.py)
                hook = recording.change_hook(recording.RecordingScope.ROBOT, robot.name,
                                             recording.request_actor())
            await service.database.update_spec(RobotObjectV1, robot.name, robot.spec,
                                               publisher_id, **_hook_kwargs(hook))

        # Update status if provided
        if "status" in robot_data:
            robot.status = robot.get_status_class()(**robot_data["status"])
            await service.database.update_status(RobotObjectV1, robot.name, robot.status, publisher_id)

        # Return updated robot
        updated_robot = await service.database.get_object(RobotObjectV1, robot_name)
        return updated_robot.dict()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to update robot: {str(e)}")


@app.delete("/api/v1/robots/{robot_name}")
async def delete_robot(robot_name: str, delete_telemetry: bool = False,
                       delete_rosbags: bool = False):
    """
    Delete a robot.

    Always: closes the robot's open map session and removes its site assignments, run epoch,
    latest-state row and the robot itself. History (telemetry tables, events, mission runs)
    and rosbags are kept unless `delete_telemetry` / `delete_rosbags` are true.

    200 `{success, message, deleted: {telemetry, rosbags, sessions_closed}, robot_actions}`
    (the mapping services stopped on the robot, best effort); 404 unknown robot;
    409 `ROBOT_HAS_ACTIVE_MISSION` (ON_TASK or a pending/running mission; nothing is changed);
    500 any other failure. The robot can be registered again afterwards like a new one.
    """
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    try:
        return await RobotDeleter(service.database, service.mapping_switch).delete(
            robot_name, delete_telemetry=delete_telemetry, delete_rosbags=delete_rosbags,
            actor=recording.request_actor(), rosbag_deleter=_delete_rosbags_of)
    except HTTPException:
        raise
    except Exception as e:
        logging.exception("Failed to delete robot %s", robot_name)
        raise HTTPException(status_code=500, detail=f"Failed to delete robot: {str(e)}")


async def _delete_rosbags_of(robot_name: str) -> Dict[str, Any]:
    return await service.delete_robot_bags(robot_name=robot_name)


# Maps U6: the deprecated "assign map" is gone. 410 for one release so an old client gets a
# clear answer instead of a 404/405; remove the route in the release after U6.
ROBOT_MAP_GONE = ("PUT /api/v1/robots/{robot}/map was removed (maps U6): a robot's map is its "
                  "open session. Use POST /api/v1/maps/{id}/sessions (purpose mapping or "
                  "operate) and POST /api/v1/maps/{id}/sessions/{sid}/finish.")


@app.put("/api/v1/robots/{robot_name}/map", status_code=410)
async def update_robot_map(robot_name: str):
    """REMOVED in maps U6 (was the deprecated M2 shim over sessions): always 410 Gone, pointing
    to POST /api/v1/maps/{id}/sessions and .../sessions/{sid}/finish. Kept for one release."""
    raise HTTPException(status_code=410, detail=ROBOT_MAP_GONE)


@app.post("/api/v1/robots/{robot_name}/cancel-order")
async def force_cancel_robot_order(robot_name: str):
    """
    Force the robot to abandon whatever VDA5050 order it currently holds, independent
    of mission tracking.

    An operator escape hatch: a robot can end up holding an order nothing tracks
    anymore (the mission that dispatched it hit a client-side error, was force-failed
    by a timeout, or was otherwise abandoned server-side) and keep reporting that
    stale orderId forever, rejecting every subsequently dispatched order as "An order
    is running". Unlike POST /missions/{name}/cancel, this doesn't require (or touch)
    any tracked mission -- it just asks the dispatcher to send a cancelOrder to the
    robot the next time it processes a robot-object change. See
    RobotSpecV1.needs_order_cancel's doc comment for the full mechanism.
    """
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    try:
        robot = await service.database.get_object(RobotObjectV1, robot_name)
        robot.needs_order_cancel = True
        await service.database.update_spec(RobotObjectV1, robot_name, robot.spec, uuid.uuid4())
        return {"success": True, "message": f"cancelOrder requested for {robot_name}"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to request cancel-order: {str(e)}")


@app.post("/api/v1/robots/{robot_name}/actions", response_model=InvokeActionResponse)
async def invoke_custom_action(robot_name: str, request: InvokeActionRequest):
    """
    Invoke a custom VDA5050 action on a robot.

    This endpoint creates a mission with an instant action that will be sent to the robot
    via the Mission Dispatcher. The action is executed immediately without navigation.
    """
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    result = await service.invoke_custom_action(
        robot_name=robot_name,
        action_type=request.action_type,
        action_parameters=request.action_parameters or {},
        blocking_type=request.blocking_type or "HARD"
    )

    return InvokeActionResponse(**result)


# ==================== Sites (Phase 0 WP9) ====================
# Transactions, NOTIFYs and RECORDING_CHANGED: packages/api/sites.py.

def _require_service():
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")


async def _site_call(what: str, coro):
    """Run a packages/api/sites.py call: HTTPExceptions pass through, anything else is a
    logged 500."""
    try:
        return await coro
    except HTTPException:
        raise
    except Exception as e:
        logging.exception(f"Failed to {what}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to {what}: {str(e)}")


@app.get("/api/v1/sites", response_model=List[dict])
async def list_sites():
    """List all sites."""
    _require_service()
    found = await _site_call("list sites", service.database.list_objects(SiteObjectV1))
    return [site.dict() for site in found]


@app.get("/api/v1/sites/{site_id}")
async def get_site(site_id: str):
    """Get one site (404 if unknown)."""
    _require_service()
    return (await _site_call("get site", service.database.get_object(SiteObjectV1,
                                                                     site_id))).dict()


@app.post("/api/v1/sites", status_code=201)
async def create_site(site_data: dict):
    """Create a site. Body: `name` (the site_id) plus any spec field (customer, display_name,
    sector, geofence, gps_datum, rtk_base, timezone, telemetry_recording). 409 if the id is
    taken, 422 on unknown fields or invalid values."""
    _require_service()
    site = await _site_call("create site", sites.create_site(
        service.database, site_data, uuid.uuid4(), recording.request_actor()))
    return site.dict()


@app.put("/api/v1/sites/{site_id}")
async def update_site(site_id: str, site_data: dict):
    """Partial update: only the spec fields in the body change (null clears one). 404 if
    unknown, 422 on unknown fields or invalid values."""
    _require_service()
    site = await _site_call("update site", sites.update_site(
        service.database, site_id, site_data, uuid.uuid4(), recording.request_actor()))
    return site.dict()


@app.delete("/api/v1/sites/{site_id}")
async def delete_site(site_id: str):
    """Delete a site. 409 while robots are assigned to it; its assignment history stays."""
    _require_service()
    await _site_call("delete site", sites.delete_site(
        service.database, site_id, uuid.uuid4(), recording.request_actor()))
    return {"success": True, "message": f"Site {site_id} deleted"}


class AssignRobotSiteRequest(BaseModel):
    """Body of PUT /api/v1/robots/{robot_name}/site."""
    site_id: Optional[str] = Field(..., description="Site to assign the robot to from now "
                                                    "on; null unassigns it")

    class Config:
        extra = "forbid"


@app.put("/api/v1/robots/{robot_name}/site")
async def assign_robot_site(robot_name: str, request: AssignRobotSiteRequest):
    """Assign a robot to a site from now on: closes its current assignment and opens a new
    one in one transaction (a no-op if it is already there). `{"site_id": null}` unassigns.
    404 for an unknown robot or site."""
    _require_service()
    return await _site_call("assign site", sites.assign_robot(
        service.database, robot_name, request.site_id, recording.request_actor()))


@app.get("/api/v1/robots/{robot_name}/site-assignments", response_model=List[dict])
async def list_robot_site_assignments(robot_name: str):
    """The robot's site assignment history, newest first."""
    _require_service()
    return await _site_call("list site assignments",
                            sites.list_assignments(service.database, robot_name))


# ==================== Runs, events, timeline (Phase 0 WP10) ====================
# Read-only; response shapes, pagination and `not_recorded`: packages/api/fleet_reads.py.

@app.get("/api/v1/runs")
async def list_runs(
    robot: Optional[str] = Query(None, description="Robot name"),
    site: Optional[str] = Query(None, description="Site id (the site the run started at)"),
    state: Optional[str] = Query(None, description="RUNNING, COMPLETED, FAILED, CANCELED, "
                                                   "ABORTED or TIMEOUT"),
    sw_version: Optional[str] = Query(None, description="Robot build id at run start"),
    mission: Optional[str] = Query(None, description="Mission name: runs whose mission_name "
                                                     "is exactly this, or this followed by "
                                                     "one or more `-rerun-<digits>` (reruns)"),
    archived: str = Query("exclude", description="Archived runs: exclude (default), include "
                                                 "or only"),
    from_: Optional[str] = Query(None, alias="from",
                                 description="started_at >= this (ISO-8601 with time zone)"),
    to: Optional[str] = Query(None, description="started_at < this (ISO-8601 with time zone)"),
    cursor: Optional[str] = Query(None, description="`next_cursor` of the previous page"),
    limit: int = Query(fleet_reads.DEFAULT_LIMIT, ge=1, le=fleet_reads.MAX_LIMIT),
):
    """Mission runs, newest first: `{"items": [run...], "next_cursor": str | null}`."""
    _require_service()
    start, end = fleet_reads.parse_ts(from_, "from"), fleet_reads.parse_ts(to, "to")
    return await _site_call("list runs", fleet_reads.list_runs(
        service.database, robot=robot, site=site, state=state, sw_version=sw_version,
        mission=mission, archived=archived, start=start, end=end, cursor=cursor,
        limit=limit))


@app.post("/api/v1/runs/archive")
async def archive_runs(request: run_admin.ArchiveRequest):
    """Archive (`archived`: true, the default) or restore (false) runs, selected by exactly one
    of `run_ids` (1-500 run ids) or `mission` (the mission and its reruns, the same rule as
    GET /api/v1/runs?mission=). Open runs are never archived (`skipped_running`). Returns
    `{"updated": n, "skipped_running": m}`, n = runs whose archive state changed."""
    _require_service()
    run_admin.check_archive_request(request)   # 422 before any database work
    return await _site_call("archive runs", run_admin.archive_runs(service.database, request))


@app.get("/api/v1/runs/{run_id}")
async def get_run(run_id: uuid.UUID):
    """One run (with mission_tree) and its events, oldest first. 404 if unknown."""
    _require_service()
    return await _site_call("get run", fleet_reads.get_run(service.database, run_id))


@app.get("/api/v1/runs/{run_id}/timeline")
async def get_run_timeline(run_id: uuid.UUID):
    """Events, coarse robot_state/diagnostics tracks, trajectory and the recording level
    history (`recording.not_recorded` intervals) over the run's window. 404 if unknown."""
    _require_service()
    return await _site_call("get run timeline",
                            fleet_reads.run_timeline(service.database, run_id))


@app.get("/api/v1/events")
async def list_events(
    robot: Optional[str] = Query(None, description="Robot name"),
    site: Optional[str] = Query(None, description="Site id"),
    code: Optional[List[str]] = Query(None, description="Event code or category prefix "
                                                        "(NAV.*); may be repeated"),
    severity: Optional[List[str]] = Query(None, description="info, warning, error or "
                                                            "critical; may be repeated"),
    run: Optional[str] = Query(None, description="Run id"),
    from_: Optional[str] = Query(None, alias="from",
                                 description="ts >= this (ISO-8601 with time zone)"),
    to: Optional[str] = Query(None, description="ts < this (ISO-8601 with time zone)"),
    cursor: Optional[str] = Query(None, description="`next_cursor` of the previous page"),
    limit: int = Query(fleet_reads.DEFAULT_LIMIT, ge=1, le=fleet_reads.MAX_LIMIT),
):
    """Fleet events, newest first: `{"items": [event...], "next_cursor": str | null}`."""
    _require_service()
    start, end = fleet_reads.parse_ts(from_, "from"), fleet_reads.parse_ts(to, "to")
    return await _site_call("list events", fleet_reads.list_events(
        service.database, robot=robot, site=site, codes=fleet_reads.expand_codes(code),
        severities=severity, run=fleet_reads.parse_uuid(run, "run"), start=start, end=end,
        cursor=cursor, limit=limit))


@app.get("/api/v1/robots/{robot_name}/recording")
async def get_robot_recording(robot_name: str):
    """The robot's effective telemetry recording level now and where it comes from:
    `{"level", "source": "robot"|"site"|"global"|"default", "site_id", "configured": {...}}`."""
    _require_service()
    return await _site_call("get recording level",
                            fleet_reads.effective_recording(service.database, robot_name))


# Not /api/v1/robots/recording: GET /api/v1/robots/{robot_name} (declared earlier) would take
# "recording" as a robot name.
@app.get("/api/v1/recording", response_model=List[dict])
async def list_recording_levels():
    """Every robot's effective recording level (the same rule as
    /api/v1/robots/{name}/recording), by name: `[{"robot_name", "level", "source", "site_id",
    "site_name"}]`."""
    _require_service()
    return await _site_call("list recording levels",
                            fleet_reads.effective_recording_all(service.database))


@app.get("/api/v1/health/recording")
async def get_recording_health():
    """Recorder health (Phase 0 WP13), read-only, always 200: per recording process
    (`dispatch` = mission-dispatch's fleet_recorder, `api` = the elected telemetry writer)
    queue depth/capacity/%, dropped rows, spilled events pending + oldest age, last flush
    ages, heartbeat sweep lag (dispatch) and election role (api), plus the active `alerts`
    (writer_queue_high, spill_pending, heartbeat_sweep_lag, report_stale). `status` is "ok"
    when there is no alert. Details: packages/api/recorder_health.py."""
    _require_service()
    return await _site_call("read recorder health", recorder_health.recording_health(
        service.database, getattr(service, "telemetry", None)))


# ==================== Mission Operations ====================

@app.get("/api/v1/missions", response_model=List[dict])
async def list_missions():
    """
    List all missions (proxy to Mission Dispatcher database).

    Returns all mission objects in the database.
    """
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    try:
        missions = await service.database.list_objects(MissionObjectV1)
        return [mission.dict() for mission in missions]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list missions: {str(e)}")


@app.get("/api/v1/missions/{mission_name}")
async def get_mission(mission_name: str):
    """
    Get a specific mission by name (proxy to Mission Dispatcher database).

    Returns the complete mission object including spec and status.
    """
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    try:
        mission = await service.database.get_object(MissionObjectV1, mission_name)
        return mission.dict()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Mission not found: {str(e)}")


@app.get("/api/v1/missions/{mission_name}/status")
async def get_mission_status(mission_name: str):
    """Get just the status field of a mission."""
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    try:
        mission = await service.database.get_object(MissionObjectV1, mission_name)
        return mission.status.dict()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Mission not found: {str(e)}")


@app.post("/api/v1/missions", response_model=dict)
async def create_mission(mission_data: dict):
    """
    Create a new mission (proxy to Mission Dispatcher database).

    Accepts mission specification data and creates a new mission object.
    """
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    try:
        # Validate required fields
        if "name" not in mission_data:
            raise HTTPException(status_code=400, detail="Missing required field: name")
        if "robot" not in mission_data:
            raise HTTPException(status_code=400, detail="Missing required field: robot")
        if "mission_tree" not in mission_data:
            raise HTTPException(status_code=400, detail="Missing required field: mission_tree")

        # Create mission with auto-initialized status
        # Note: status and lifecycle must be set before **mission_data to avoid being overridden
        mission_data_with_defaults = {"status": MissionStatusV1(), "lifecycle": ObjectLifecycleV1.ALIVE, **mission_data}
        mission = MissionObjectV1(**mission_data_with_defaults)
        # Dispatcher-owned (see PUT below): a new mission is never dispatched yet, so a
        # caller-supplied run_id could only make its order ids collide with another run's.
        mission.status.run_id = None
        mission.status.order_rev = 0
        publisher_id = uuid.uuid4()
        await service.database.create_object(mission, publisher_id)
        return mission.dict()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to create mission: {str(e)}")


@app.put("/api/v1/missions/{mission_name}")
async def update_mission(mission_name: str, mission_data: dict):
    """
    Update a mission (proxy to Mission Dispatcher database).

    Updates the mission's spec or status based on provided data.
    """
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    try:
        # Get existing mission
        mission = await service.database.get_object(MissionObjectV1, mission_name)

        publisher_id = uuid.uuid4()

        # Update spec if provided
        if "status" not in mission_data or len(mission_data) > 1:
            # This is a spec update
            edits = {}
            for key, value in mission_data.items():
                if key in ("status", "name", "lifecycle"):
                    continue
                if key in EDITABLE_SPEC_FIELDS:
                    edits[key] = value
                else:
                    # e.g. update_nodes (a reroute), which is meant for a running mission
                    setattr(mission, key, value)
            if edits:
                # Only a mission that has not started can be edited: once its orders are
                # with the robot the operator has to start a new mission instead.
                if mission.status.state != MissionStateV1.PENDING:
                    raise HTTPException(
                        status_code=409,
                        detail=f"Mission {mission_name} is {mission.status.state.value}; "
                               "only a PENDING mission can be edited")
                try:
                    edited = MissionSpecV1(**{**mission.spec.dict(), **edits})
                except Exception as e:
                    raise HTTPException(status_code=400,
                                        detail=f"Invalid mission spec: {str(e)}")
                for key in edits:
                    setattr(mission, key, getattr(edited, key))
                # Every node of the tree has a status entry (the dispatcher reads them
                # by name), so a new tree needs its entries made and old ones dropped.
                if "mission_tree" in edits:
                    node_names = ["root"] + [str(node.name) for node in mission.mission_tree]
                    mission.status.node_status = {
                        name: mission.status.node_status.get(name, MissionNodeStatusV1())
                        for name in node_names}
            await service.database.update_spec(MissionObjectV1, mission.name, mission.spec, publisher_id)
            if "mission_tree" in edits:
                await service.database.update_status(
                    MissionObjectV1, mission.name, mission.status, publisher_id)

        # Update status if provided
        if "status" in mission_data:
            new_status = mission.get_status_class()(**mission_data["status"])
            # run_id / order_rev belong to the dispatcher: they name the VDA5050
            # orders it has already sent, so a caller's copy of the status (stale, or
            # simply without them) must not blank or change them.
            new_status.run_id = mission.status.run_id
            new_status.order_rev = mission.status.order_rev
            mission.status = new_status
            await service.database.update_status(MissionObjectV1, mission.name, mission.status, publisher_id)

        # Return updated mission
        updated_mission = await service.database.get_object(MissionObjectV1, mission_name)
        return updated_mission.dict()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to update mission: {str(e)}")


@app.delete("/api/v1/missions/{mission_name}")
async def delete_mission(mission_name: str,
                         with_reruns: bool = Query(False, description="Also delete every "
                                                   "rerun (`<name>-rerun-<digits>...`) and "
                                                   "all their runs")):
    """
    Delete a mission and, for good, its recorded runs (mission_runs rows, their events and
    trajectory rows; robot telemetry is kept). With `with_reruns=true`, the whole family
    (mission objects that are already gone are fine: their runs are still deleted).

    409 (nothing deleted) while a targeted mission is RUNNING or one of its runs is still open.
    404 if the mission does not exist (with_reruns: if neither a mission nor a run matches).
    Returns `{"success", "message", "deleted_runs", "deleted_events", "deleted_trajectory"}`,
    plus `"deleted_missions": [names]` with with_reruns. Rules: packages/api/run_admin.py.
    """
    _require_service()
    try:
        return await run_admin.delete_mission(service.database, mission_name,
                                              with_reruns=with_reruns,
                                              publisher_id=uuid.uuid4())
    except HTTPException:
        raise
    except Exception as e:
        logging.exception(f"Failed to delete mission {mission_name}: {e}")
        raise HTTPException(status_code=404, detail=f"Failed to delete mission: {str(e)}")


@app.get("/api/v1/missions/{mission_name}/plan")
async def get_mission_plan(mission_name: str, map_id: Optional[str] = None):
    """
    Get mission plan (proxy to Mission Planner service).

    Returns the planned path as a list of node IDs reconstructed from waypoints.

    Args:
        mission_name: Name of the mission
        map_id: Map ID to use for node lookup (uses default if not provided)
    """
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    try:
        result = await service.get_mission_plan(mission_name, map_id=map_id)
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Failed to get mission plan: {str(e)}")


@app.post("/api/v1/missions/{mission_name}/cancel")
async def cancel_mission(mission_name: str):
    """
    Cancel a mission (proxy to Mission Dispatcher database).

    Cancels an active mission.
    """
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    try:
        mission = await service.database.get_object(MissionObjectV1, mission_name)
        await mission.cancel()
        await service.database.update_spec(MissionObjectV1, mission_name, mission.spec, uuid.uuid4())
        await run_admin.record_cancel_requested(service.database, mission_name,
                                                getattr(mission.spec, "robot", None))
        return {"success": True, "message": f"Mission {mission_name} cancelled"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to cancel mission: {str(e)}")


# ==================== Detection Results Operations ====================

@app.get("/api/v1/detection_results", response_model=List[dict])
async def list_detection_results():
    """
    List all detection results (proxy to Mission Dispatcher database).

    Returns all detection result objects in the database.
    """
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    try:
        results = await service.database.list_objects(DetectionResultsObjectV1)
        return [result.dict() for result in results]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list detection results: {str(e)}")


@app.get("/api/v1/detection_results/{name}")
async def get_detection_result(name: str):
    """
    Get specific detection results by name (proxy to Mission Dispatcher database).

    Returns the complete detection results object.
    """
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    try:
        result = await service.database.get_object(DetectionResultsObjectV1, name)
        return result.dict()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Detection results not found: {str(e)}")


@app.delete("/api/v1/detection_results/{name}")
async def delete_detection_result(name: str):
    """
    Delete detection results (proxy to Mission Dispatcher database).

    Removes the detection results from the database.
    """
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    try:
        await service.database.set_lifecycle(DetectionResultsObjectV1, name, ObjectLifecycleV1.DELETED, uuid.uuid4())
        return {"success": True, "message": f"Detection results {name} deleted"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Failed to delete detection results: {str(e)}")


@app.websocket("/ws/mission/{mission_name}")
async def websocket_mission_status(websocket: WebSocket, mission_name: str):
    """
    WebSocket endpoint for real-time mission status updates.
    """
    if service is None:
        await websocket.close(code=1011, reason="Service not initialized")
        return

    await service.ws_manager.connect(websocket, "mission_status", mission_name)

    # Send current state immediately so clients that connect after state changes
    # (e.g. fast-completing missions) don't get stuck on stale PENDING state.
    try:
        mission = await service.database.get_object(MissionObjectV1, mission_name)
        st = mission.status
        snapshot = {
            "type": "mission_update",
            "mission_name": mission.name,
            "robot_name": mission.robot,
            "needs_canceled": mission.needs_canceled,
            "timestamp": datetime.now().isoformat(),
            "status": {
                "state": st.state.value if hasattr(st, "state") else "PENDING",
                "current_node": st.current_node if hasattr(st, "current_node") else 0,
                "failure_reason": st.failure_reason if hasattr(st, "failure_reason") else None,
                "failure_category": st.failure_category.value if (
                    hasattr(st, "failure_category") and st.failure_category is not None
                ) else None,
                "start_timestamp": st.start_timestamp.isoformat() if (
                    hasattr(st, "start_timestamp") and st.start_timestamp is not None
                ) else None,
                "end_timestamp": st.end_timestamp.isoformat() if (
                    hasattr(st, "end_timestamp") and st.end_timestamp is not None
                ) else None,
                "blocked": st.blocked if hasattr(st, "blocked") else False,
                "blocked_node": st.blocked_node if hasattr(st, "blocked_node") else None,
                "blocked_edge": st.blocked_edge if hasattr(st, "blocked_edge") else None,
                "blocked_waypoint_index": st.blocked_waypoint_index if hasattr(
                    st, "blocked_waypoint_index") else None,
                "block_reason": st.block_reason if hasattr(st, "block_reason") else None,
            },
        }
        await websocket.send_json(snapshot)
    except Exception:
        pass  # Mission may not exist yet; client will receive updates via watcher

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        service.ws_manager.disconnect(websocket, "mission_status", mission_name)


@app.websocket("/ws/robot/{robot_name}")
async def websocket_robot_status(websocket: WebSocket, robot_name: str):
    """
    WebSocket endpoint for real-time robot status updates.
    """
    if service is None:
        await websocket.close(code=1011, reason="Service not initialized")
        return
    
    await service.ws_manager.connect(websocket, "robot_status", robot_name)
    
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        service.ws_manager.disconnect(websocket, "robot_status", robot_name)


# ==================== Main Entry Point ====================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="API Delegation Service")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Host to bind to")
    parser.add_argument("--port", type=int, default=PORT_API_DELEGATION, help="Port to bind to")
    parser.add_argument("--log-level", default=LOG_LEVEL_DEFAULT, choices=["DEBUG", "INFO", "WARNING", "ERROR"])

    args = parser.parse_args()

    # Configure logging using shared utility
    configure_service_logging("api_delegation", args.log_level)

    # Run server
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level=args.log_level.lower()
    )

