#!/usr/bin/env python3
"""
Graph Builder Service - FastAPI Application

Exposes the GraphBuilderService via REST API and runs MQTT listener.
"""

import argparse
import logging
import os
import asyncio
from contextlib import asynccontextmanager
from typing import Optional, Dict, Any, Union

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, validator

from .server import GraphBuilderService
from packages.utils.service_utils import (
    HealthResponse, create_health_response, create_root_response, configure_service_logging
)
from packages.utils.fastapi_helpers import add_error_handlers
from packages.config import (
    ARANGO_HOST, ARANGO_PORT, ARANGO_USERNAME, DATA_BASE_NAME,
    MINIO_HOST, MINIO_PORT, MINIO_SECURE,
    MQTT_HOST, MQTT_PORT, MQTT_TOPIC_NODE_UPDATE, MQTT_IMAGE_TOPIC, MQTT_DEPTH_TOPIC,
    MQTT_COSTMAP_TOPIC,
    POSTGRES_DATABASE_NAME, POSTGRES_DATABASE_USERNAME, postgres_database_password, require_secret,
    POSTGRES_DATABASE_HOST, POSTGRES_DATABASE_PORT,
    RADIUS_THRESHOLD, DISTANCE_THRESHOLD, IMAGE_BUFFER_TIMEOUT,
    PORT_GRAPH_BUILDER, DEFAULT_HOST, LOG_LEVEL_DEFAULT,
    SCENE_GRAPH_WORKER_ENABLED,
)


# ==================== Request/Response Models ====================

class NodeUpdate(BaseModel):
    """Model for manual node update (for testing). `map_id` is required since maps M2 (no
    default map) and must name a map in the `mapping` state; x/y/yaw are map-frame."""
    node_id: Union[int, str]
    x: float
    y: float
    yaw: float = 0.0
    map_id: str
    images: Optional[list] = []
    metadata: Optional[Dict[str, Any]] = None

    @validator('node_id', pre=True)
    @classmethod
    def validate_node_id(cls, v):
        """Convert node_id to string if it's an int."""
        if isinstance(v, int):
            return str(v)
        return v


class StatsResponse(BaseModel):
    """Response model for statistics."""
    nodes_processed: int
    images_saved: int
    edges_created: int
    errors: int
    mqtt_connected: bool
    radius_threshold: float
    # Robot ingest contract (acks.py): per robot, the retained high-water mark we publish and the
    # robot's own spool_state; the count of bundles waiting for parts; the ack counters.
    ingest_state: Dict[str, Any] = {}
    spool_state: Dict[str, Any] = {}
    ack_trackers: int = 0
    acks_sent: int = 0
    acks_failed: int = 0
    acks_dropped: int = 0
    run_id_mismatch: int = 0
    # process RSS, cgroup memory.current / max, oom counters, and (worker on) per engine nbytes + canvas
    memory: Dict[str, Any] = {}


# HealthResponse is now imported from packages.utils.service_utils


# ==================== FastAPI Application ====================

# Global service instance
service: Optional[GraphBuilderService] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global service

    mqtt_topic = os.getenv("MQTT_TOPIC", MQTT_TOPIC_NODE_UPDATE)
    service = GraphBuilderService(
        mqtt_host=MQTT_HOST,
        mqtt_port=MQTT_PORT,
        mqtt_topic=mqtt_topic,
        mqtt_image_topic=MQTT_IMAGE_TOPIC,
        mqtt_depth_topic=MQTT_DEPTH_TOPIC,
        mqtt_costmap_topic=MQTT_COSTMAP_TOPIC,
        arango_host=ARANGO_HOST,
        arango_port=ARANGO_PORT,
        arango_username=ARANGO_USERNAME,
        arango_password=require_secret("ARANGO_PASSWORD"),
        arango_database=DATA_BASE_NAME,
        minio_host=MINIO_HOST,
        minio_port=MINIO_PORT,
        minio_access_key=require_secret("MINIO_ACCESS_KEY"),
        minio_secret_key=require_secret("MINIO_SECRET_KEY"),
        minio_secure=MINIO_SECURE,
        distance_threshold=DISTANCE_THRESHOLD,
        postgres_db=POSTGRES_DATABASE_NAME,
        postgres_user=POSTGRES_DATABASE_USERNAME,
        postgres_password=postgres_database_password(),
        postgres_host=POSTGRES_DATABASE_HOST,
        postgres_port=POSTGRES_DATABASE_PORT,
        radius_threshold=RADIUS_THRESHOLD,
        image_buffer_timeout=IMAGE_BUFFER_TIMEOUT,
    )

    await service.database.async_init()
    service.connect_mqtt()
    service.set_event_loop(asyncio.get_event_loop())

    async def _periodic_cleanup():
        while True:
            await asyncio.sleep(60)
            service._cleanup_old_mappings()

    async def _periodic_reject_flush():
        # MAP.INGEST_REJECTED for drops that no later drop has reported (maps M2).
        while True:
            await asyncio.sleep(10)
            try:
                await service.flush_rejects()
            except Exception as e:  # noqa: BLE001
                logging.warning(f"Rejection flush failed: {e}")

    async def _periodic_ack_flush():
        # Timeout acks and held-back ingest_state values (robot ingest contract).
        while True:
            await asyncio.sleep(1)
            await service.flush_acks()

    async def _periodic_ingest_mode():
        # The active mission's register_map on the retained ingest_mode (robot ingest contract).
        while True:
            await asyncio.sleep(2)
            await service.refresh_ingest_mode()

    # Off by default: the robot team does not subscribe to ingest_mode (they asked for per-node
    # rejection instead), so a retained value nobody reads could only go stale.
    if os.getenv("INGEST_MODE_PUBLISH", "false").lower() in ("1", "true", "yes"):
        asyncio.create_task(_periodic_ingest_mode())
    asyncio.create_task(_periodic_cleanup())
    asyncio.create_task(_periodic_reject_flush())
    asyncio.create_task(_periodic_ack_flush())
    global _scene_host
    scene_host = None
    if SCENE_GRAPH_WORKER_ENABLED:
        try:
            from .scene_host import start_scene_worker
            scene_host = _scene_host = start_scene_worker(service, asyncio.get_running_loop())
        except Exception as e:  # noqa: BLE001
            logging.error(f"Scene graph worker not started: {type(e).__name__}: {e}")
    logging.info("🚀 Graph Builder Service started")

    yield

    if scene_host is not None:
        _scene_host = None
        try:
            await asyncio.to_thread(scene_host.stop)
        except Exception as e:  # noqa: BLE001
            logging.error(f"Scene graph worker shutdown failed: {e}")
    if service:
        service.disconnect_mqtt()
        service.close()
    logging.info("👋 Graph Builder Service shutting down")


_scene_host = None

app = FastAPI(
    title="Graph Builder Service",
    description="Builds topological maps from robot node updates via MQTT",
    version="1.0.0",
    lifespan=lifespan,
)

# Add standardized error handlers
add_error_handlers(app)


# ==================== API Endpoints ====================

@app.get("/")
async def root():
    """Root endpoint with service information."""
    return create_root_response(
        service_name="Graph Builder Service",
        version="1.0.0",
        description="Builds topological maps from robot node updates via MQTT",
        endpoints={
            "health": "GET /health",
            "stats": "GET /stats",
            "process_node": "POST /node"
        }
    )


def _scene_graph_health() -> dict:
    """Runner state of the scene-graph worker (running, per-map state / last step age), or {}."""
    host = _scene_host
    if host is None:
        return {"scene_graph_worker": {"enabled": bool(SCENE_GRAPH_WORKER_ENABLED), "running": False}}
    try:
        return {"scene_graph_worker": {"enabled": True, **host.state()}}
    except Exception as exc:  # noqa: BLE001
        return {"scene_graph_worker": {"enabled": True, "error": f"{type(exc).__name__}: {exc}"}}


def _memory_stats() -> dict:
    try:
        snap = getattr(getattr(_scene_host, "runner", None), "memory_snapshot", None)
        if snap is not None:
            return snap()
        from .scene_memory import process_stats
        return process_stats()
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


@app.get("/health", response_model=HealthResponse)
async def health_check():
    """Health check endpoint."""
    health_details = service.get_health_details()

    return create_health_response(
        service_name="graph_builder",
        dependencies={
            "image_db": health_details["image_db"],
            "graph_db": health_details["graph_db"],
        },
        details={
            "mqtt_connected": health_details["mqtt_connected"],
            **_scene_graph_health(),
        }
    )


@app.get("/stats", response_model=StatsResponse)
async def get_stats():
    """Get service statistics."""
    stats = service.get_stats()
    stats["memory"] = _memory_stats()
    return StatsResponse(**stats)


@app.get("/ingest/backlog/{robot_name}")
async def get_ingest_backlog(robot_name: str, run_id: Optional[str] = None):
    """What the robot still has to upload (its retained spool_state.depth for its current run),
    read-only: the API gates a mapping session's Finish on `pending` false. `known: false` when
    no spool_state of that run was seen (an older robot, or graph-builder just started)."""
    return service.acks.backlog(robot_name, run_id)


@app.post("/node")
async def process_node(node: NodeUpdate):
    """
    Manually process a node update (for testing).
    
    In production, nodes are processed automatically from MQTT.
    """
    state = await service.manual_target_state(node.map_id)
    if state != "mapping":
        raise HTTPException(status_code=409,
                            detail=f"Map '{node.map_id}' does not accept nodes ({state}); "
                                   "start a mapping session first")
    try:
        node_data = {
            "node_id": node.node_id,
            "x": node.x,
            "y": node.y,
            "yaw": node.yaw,
            "map_id": node.map_id,
            "images": node.images or [],
            "metadata": node.metadata or {}
        }
        
        # process_node_update returns a result dict (always truthy) and does blocking
        # ArangoDB/MinIO I/O, so check its "success" key and keep it off the event loop.
        result = await asyncio.to_thread(service.process_node_update, node_data)

        if not result.get("success"):
            raise HTTPException(status_code=500,
                                detail=result.get("error") or "Failed to process node")
        
        return {
            "success": True,
            "node_id": node.node_id,
            "message": "Node processed successfully"
        }
    
    except Exception as e:
        logging.error(f"Error processing node: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to process node: {str(e)}")


# ==================== WebSocket Endpoints ====================

@app.websocket("/ws/updates/{map_id}")
async def websocket_map_updates(websocket: WebSocket, map_id: str):
    """
    WebSocket endpoint for real-time map updates.

    This endpoint is consumed by the API Delegation Service to proxy
    updates to client applications.

    Clients connecting here will receive:
    - Node addition events
    - Edge creation events
    - Map modification events
    """
    if service is None:
        await websocket.close(code=1011, reason="Service not initialized")
        return

    await websocket.accept()

    # Create a queue for this connection
    queue = asyncio.Queue()
    service.update_publisher.subscribe(map_id, queue)

    logging.info(f"WebSocket connected for map updates: {map_id}")

    try:
        while True:
            # Wait for updates from the publisher
            update = await queue.get()

            # Send update to client
            await websocket.send_json(update)

    except WebSocketDisconnect:
        logging.info(f"WebSocket disconnected for map: {map_id}")
    except Exception as e:
        logging.error(f"WebSocket error for map {map_id}: {e}")
    finally:
        # Clean up subscription
        service.update_publisher.unsubscribe(map_id, queue)


# ==================== Main Entry Point ====================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Graph Builder Service")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Host to bind to")
    parser.add_argument("--port", type=int, default=PORT_GRAPH_BUILDER, help="Port to bind to")
    parser.add_argument("--log-level", default=LOG_LEVEL_DEFAULT,
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Logging level")
    args = parser.parse_args()

    configure_service_logging("graph_builder", args.log_level)
    uvicorn.run(app, host=args.host, port=args.port)

