#!/usr/bin/env python3
"""
API Delegation Service - Main Entry Point

FastAPI application that provides REST and WebSocket endpoints for clients.
"""

import logging
import uuid
import argparse
from contextlib import asynccontextmanager
from typing import Optional, Dict, Any, List
import os
from datetime import datetime

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field, ValidationError
import uvicorn

from packages.api.server import ApiDelegationService
from packages.api import fleet_reads, recording, run_admin, sites
from packages.api.idempotency import IdempotencyMiddleware, IdempotencyStore
from packages.utils.service_utils import (
    HealthResponse, create_health_response, create_root_response,
    configure_service_logging, DependencyHealthChecker
)
from packages.utils.fastapi_helpers import add_error_handlers
from packages.config import (
    ARANGO_HOST, ARANGO_PORT, ARANGO_USERNAME, ARANGO_PASSWORD, DATA_BASE_NAME,
    URL_MISSION_PLANNER, URL_LIVEKIT,
    MINIO_HOST, MINIO_PORT, MINIO_ACCESS_KEY, MINIO_SECRET_KEY, MINIO_SECURE,
    MQTT_BROKER, MQTT_PORT, MQTT_KEEPALIVE,
    POSTGRES_DATABASE_NAME, POSTGRES_DATABASE_USERNAME, POSTGRES_DATABASE_PASSWORD,
    POSTGRES_DATABASE_HOST, POSTGRES_DATABASE_PORT,
    DEFAULT_MAP_ID, PORT_API_DELEGATION, DEFAULT_HOST, LOG_LEVEL_DEFAULT,
    IDEMPOTENCY_TTL_S, IDEMPOTENCY_LEASE_S, IDEMPOTENCY_PURGE_INTERVAL_S,
)
from cloud_common.objects.robot import RobotObjectV1, RobotStatusV1, CustomActionV1
from cloud_common.objects.mission import (
    EDITABLE_SPEC_FIELDS, MissionNodeStatusV1, MissionObjectV1, MissionSpecV1, MissionStateV1,
    MissionStatusV1)
from cloud_common.objects.detection_results import DetectionResultsObjectV1
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
    datum_bearing_deg: float = Field(0.0, description="Angle from map +X axis to true north in degrees")


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
                "set_map": "PUT /api/v1/robots/{robot_name}/map",
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
async def list_maps():
    """List all maps registered in Postgres (includes datum and metadata)."""
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")
    try:
        maps = await service.database.list_objects(MapObjectV1)
        # A DELETING map is on its way out (packages/api/map_delete.py): hidden.
        maps = [m for m in maps if m.lifecycle != ObjectLifecycleV1.DELETING]
        return {"maps": [m.dict() for m in maps], "count": len(maps)}
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Failed to list maps: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to list maps: {str(e)}")



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
    return result


class UpdateDatumRequest(BaseModel):
    """Request model for registering or updating a map's GPS datum."""
    datum_latitude: float = Field(..., description="WGS84 origin latitude in degrees")
    datum_longitude: float = Field(..., description="WGS84 origin longitude in degrees")
    datum_bearing_deg: float = Field(0.0, description="Angle from map +X axis to true north in degrees")



@app.put("/api/v1/maps/{map_id}/datum")
async def update_map_datum(map_id: str, request: UpdateDatumRequest):
    """Register or update the GPS datum for an existing map."""
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")
    await service.ensure_map_not_deleting(map_id)
    result = await service.update_map_datum(
        map_id,
        request.datum_latitude,
        request.datum_longitude,
        request.datum_bearing_deg,
    )
    if not result.get("success"):
        raise HTTPException(status_code=404, detail=result.get("error"))
    return result


@app.delete("/api/v1/maps/{map_id}", status_code=202)
async def delete_map(map_id: str):
    """
    Delete a map and all its data from the graph and image databases.

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
        return [robot.dict() for robot in robots]
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
        return robot.dict()
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
                robot.status.factsheet.speed_max = factsheet_data.get("speed_max", robot.status.factsheet.speed_max)
                robot.status.factsheet.length = factsheet_data.get("length", robot.status.factsheet.length)
                robot.status.factsheet.width = factsheet_data.get("width", robot.status.factsheet.width)
                robot.status.factsheet.height = factsheet_data.get("height", robot.status.factsheet.height)
                robot.status.factsheet.custom_actions = [
                    CustomActionV1(**a) for a in factsheet_data.get("actions", [])
                ]
                await service.database.update_status(RobotObjectV1, robot.name, robot.status, publisher_id)
            return (await service.database.get_object(RobotObjectV1, robot_data["name"])).dict()
        else:
            # Robot doesn't exist — create it
            if robot_data.get("current_map"):
                await service.ensure_map_not_deleting(robot_data["current_map"])
            status = RobotStatusV1()
            if factsheet_data:
                status.factsheet.agv_class = factsheet_data.get("agv_class", "")
                status.factsheet.speed_max = factsheet_data.get("speed_max", -1)
                status.factsheet.length = factsheet_data.get("length", -1)
                status.factsheet.width = factsheet_data.get("width", -1)
                status.factsheet.height = factsheet_data.get("height", -1)
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
        if robot_data.get("current_map") and robot_data["current_map"] != robot.current_map:
            await service.ensure_map_not_deleting(robot_data["current_map"])

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
async def delete_robot(robot_name: str):
    """
    Delete a robot (proxy to Mission Dispatcher database).

    Removes the robot from the database.
    """
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    try:
        await service.database.set_lifecycle(RobotObjectV1, robot_name, ObjectLifecycleV1.DELETED, uuid.uuid4())
        return {"success": True, "message": f"Robot {robot_name} deleted"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Failed to delete robot: {str(e)}")


class UpdateRobotMapRequest(BaseModel):
    """Request model for setting a robot's current map."""
    map_id: str = Field(..., description="Map ID the robot is currently operating on")


@app.put("/api/v1/robots/{robot_name}/map")
async def update_robot_map(robot_name: str, request: UpdateRobotMapRequest):
    """
    Set the current map for a robot.

    Updates robot.current_map in the database. The graph_builder service reads
    this field when processing node_update MQTT messages so that map_id is
    authoritative from the server side rather than trusted from the robot payload.
    """
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    try:
        publisher_id = uuid.uuid4()
        robot = await service.database.get_object(RobotObjectV1, robot_name)
        await service.ensure_map_not_deleting(request.map_id)
        robot.current_map = request.map_id
        await service.database.update_spec(RobotObjectV1, robot_name, robot.spec, publisher_id)
        updated_robot = await service.database.get_object(RobotObjectV1, robot_name)
        return {"success": True, "robot_name": robot_name, "current_map": updated_robot.current_map}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Failed to update robot map: {str(e)}")


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

