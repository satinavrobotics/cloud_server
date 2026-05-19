#!/usr/bin/env python3
"""
API Delegation Service - Main Entry Point

FastAPI application that provides REST and WebSocket endpoints for clients.
"""

import logging
import argparse
from contextlib import asynccontextmanager
from typing import Optional, Dict, Any, List
import os
from datetime import datetime

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
import uvicorn

from packages.api.server import ApiDelegationService
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
)
from cloud_common.objects.robot import RobotObjectV1, RobotStatusV1, CustomActionV1
from cloud_common.objects.mission import MissionObjectV1, MissionStatusV1
from cloud_common.objects.detection_results import DetectionResultsObjectV1
from cloud_common.objects.map import MapObjectV1
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
        logging.info("✅ API Delegation Service stopped")


app = FastAPI(
    title="API Delegation Service",
    description="Central API gateway for robot fleet management - provides unified REST and WebSocket interfaces",
    version="1.0.0",
    lifespan=lifespan,
)

# Add standardized error handlers
add_error_handlers(app)

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
        return {"maps": [m.dict() for m in maps], "count": len(maps)}
    except Exception as e:
        logging.error(f"Failed to list maps: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to list maps: {str(e)}")



@app.post("/api/v1/map/load", response_model=LoadMapResponse)
async def load_map(request: LoadMapRequest):
    """Load a map: creates ArangoDB graph collections and registers in Postgres."""
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

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
    result = await service.update_map_datum(
        map_id,
        request.datum_latitude,
        request.datum_longitude,
        request.datum_bearing_deg,
    )
    if not result.get("success"):
        raise HTTPException(status_code=404, detail=result.get("error"))
    return result


@app.delete("/api/v1/maps/{map_id}")
async def delete_map(map_id: str):
    """
    Delete a map and all its data from both graph and image databases.

    WARNING: This will permanently delete all map data including nodes, edges, and images!
    """
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    try:
        result = await service.delete_map(map_id)

        if not result.get("success", False):
            error_msg = result.get("error", "Unknown error")
            raise HTTPException(status_code=500, detail=f"Failed to delete map: {error_msg}")

        return result
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Failed to delete map {map_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to delete map: {str(e)}")


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
        return robot.dict()
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Robot not found: {str(e)}")


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

        import uuid
        publisher_id = uuid.uuid4()
        ip_address = robot_data.pop("ip_address", None)
        entrypoint_port = robot_data.pop("entrypoint_port", None)
        position_mode = robot_data.pop("position_mode", None)
        factsheet_data = robot_data.pop("factsheet", None)

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
            if spec_changed:
                await service.database.update_spec(RobotObjectV1, robot.name, robot.spec, publisher_id)
            if factsheet_data:
                robot.status.factsheet.agv_class = factsheet_data.get("agv_class", robot.status.factsheet.agv_class)
                robot.status.factsheet.speed_max = factsheet_data.get("speed_max", robot.status.factsheet.speed_max)
                robot.status.factsheet.custom_actions = [
                    CustomActionV1(**a) for a in factsheet_data.get("actions", [])
                ]
                await service.database.update_status(RobotObjectV1, robot.name, robot.status, publisher_id)
            return (await service.database.get_object(RobotObjectV1, robot_data["name"])).dict()
        else:
            # Robot doesn't exist — create it
            status = RobotStatusV1()
            if factsheet_data:
                status.factsheet.agv_class = factsheet_data.get("agv_class", "")
                status.factsheet.speed_max = factsheet_data.get("speed_max", -1)
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
            robot = RobotObjectV1(**robot_data_with_defaults)
            await service.database.create_object(robot, publisher_id)
            return robot.dict()
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to register robot: {str(e)}")
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
        # Get existing robot
        robot = await service.database.get_object(RobotObjectV1, robot_name)

        import uuid
        publisher_id = uuid.uuid4()

        # Update spec if provided
        if "status" not in robot_data or len(robot_data) > 1:
            # This is a spec update
            for key, value in robot_data.items():
                if key != "status" and key != "name" and key != "lifecycle":
                    setattr(robot, key, value)
            await service.database.update_spec(RobotObjectV1, robot.name, robot.spec, publisher_id)

        # Update status if provided
        if "status" in robot_data:
            robot.status = robot.get_status_class()(**robot_data["status"])
            await service.database.update_status(RobotObjectV1, robot.name, robot.status, publisher_id)

        # Return updated robot
        updated_robot = await service.database.get_object(RobotObjectV1, robot_name)
        return updated_robot.dict()
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
        import uuid
        await service.database.set_lifecycle(RobotObjectV1, robot_name, ObjectLifecycleV1.DELETED, uuid.uuid4())
        return {"success": True, "message": f"Robot {robot_name} deleted"}
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
        import uuid
        publisher_id = uuid.uuid4()
        robot = await service.database.get_object(RobotObjectV1, robot_name)
        robot.current_map = request.map_id
        await service.database.update_spec(RobotObjectV1, robot_name, robot.spec, publisher_id)
        updated_robot = await service.database.get_object(RobotObjectV1, robot_name)
        return {"success": True, "robot_name": robot_name, "current_map": updated_robot.current_map}
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Failed to update robot map: {str(e)}")


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
        import uuid
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

        import uuid
        publisher_id = uuid.uuid4()

        # Update spec if provided
        if "status" not in mission_data or len(mission_data) > 1:
            # This is a spec update
            for key, value in mission_data.items():
                if key != "status" and key != "name" and key != "lifecycle":
                    setattr(mission, key, value)
            await service.database.update_spec(MissionObjectV1, mission.name, mission.spec, publisher_id)

        # Update status if provided
        if "status" in mission_data:
            mission.status = mission.get_status_class()(**mission_data["status"])
            await service.database.update_status(MissionObjectV1, mission.name, mission.status, publisher_id)

        # Return updated mission
        updated_mission = await service.database.get_object(MissionObjectV1, mission_name)
        return updated_mission.dict()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to update mission: {str(e)}")


@app.delete("/api/v1/missions/{mission_name}")
async def delete_mission(mission_name: str):
    """
    Delete a mission (proxy to Mission Dispatcher database).

    Removes the mission from the database.
    """
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    try:
        import uuid
        await service.database.set_lifecycle(MissionObjectV1, mission_name, ObjectLifecycleV1.DELETED, uuid.uuid4())
        return {"success": True, "message": f"Mission {mission_name} deleted"}
    except Exception as e:
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
        import uuid
        await service.database.update_spec(MissionObjectV1, mission_name, mission.spec, uuid.uuid4())
        return {"success": True, "message": f"Mission {mission_name} cancelled"}
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
        import uuid
        await service.database.set_lifecycle(DetectionResultsObjectV1, name, ObjectLifecycleV1.DELETED, uuid.uuid4())
        return {"success": True, "message": f"Detection results {name} deleted"}
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

