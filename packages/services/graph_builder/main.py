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
    ARANGO_HOST, ARANGO_PORT, ARANGO_USERNAME, ARANGO_PASSWORD, DATA_BASE_NAME,
    MINIO_HOST, MINIO_PORT, MINIO_ACCESS_KEY, MINIO_SECRET_KEY, MINIO_SECURE,
    MQTT_HOST, MQTT_PORT, MQTT_TOPIC_NODE_UPDATE, MQTT_IMAGE_TOPIC,
    POSTGRES_DATABASE_NAME, POSTGRES_DATABASE_USERNAME, POSTGRES_DATABASE_PASSWORD,
    POSTGRES_DATABASE_HOST, POSTGRES_DATABASE_PORT,
    RADIUS_THRESHOLD, DISTANCE_THRESHOLD, DEFAULT_MAP_ID, IMAGE_BUFFER_TIMEOUT,
    PORT_GRAPH_BUILDER, DEFAULT_HOST, LOG_LEVEL_DEFAULT,
)


# ==================== Request/Response Models ====================

class NodeUpdate(BaseModel):
    """Model for manual node update (for testing)."""
    node_id: Union[int, str]
    x: float
    y: float
    yaw: float = 0.0
    map_id: Optional[str] = None
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
        distance_threshold=DISTANCE_THRESHOLD,
        postgres_db=POSTGRES_DATABASE_NAME,
        postgres_user=POSTGRES_DATABASE_USERNAME,
        postgres_password=POSTGRES_DATABASE_PASSWORD,
        postgres_host=POSTGRES_DATABASE_HOST,
        postgres_port=POSTGRES_DATABASE_PORT,
        radius_threshold=RADIUS_THRESHOLD,
        default_map_id=DEFAULT_MAP_ID,
        image_buffer_timeout=IMAGE_BUFFER_TIMEOUT,
    )

    await service.database.async_init()
    service.connect_mqtt()
    service.set_event_loop(asyncio.get_event_loop())

    async def _periodic_cleanup():
        while True:
            await asyncio.sleep(60)
            service._cleanup_old_mappings()

    asyncio.create_task(_periodic_cleanup())
    logging.info("🚀 Graph Builder Service started")

    yield

    if service:
        service.disconnect_mqtt()
    logging.info("👋 Graph Builder Service shutting down")


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
            "mqtt_connected": health_details["mqtt_connected"]
        }
    )


@app.get("/stats", response_model=StatsResponse)
async def get_stats():
    """Get service statistics."""
    stats = service.get_stats()
    return StatsResponse(**stats)


@app.post("/node")
async def process_node(node: NodeUpdate):
    """
    Manually process a node update (for testing).
    
    In production, nodes are processed automatically from MQTT.
    """
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
        
        success = service.process_node_update(node_data)
        
        if not success:
            raise HTTPException(status_code=500, detail="Failed to process node")
        
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

