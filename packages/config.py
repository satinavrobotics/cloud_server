#!/usr/bin/env python3
"""
Centralized Configuration Constants

This module defines all default configuration values used across the cloud server services.
All services should import from this module to ensure consistency.

Credentials (passwords, keys) are read exclusively from environment variables so that
plaintext secrets never live in source code.  Set them via a root-level .env file
(see .env.example) or via Docker / Kubernetes secrets.
"""

import os

# ==================== Service Ports ====================
# Default ports for all microservices
PORT_GRAPH_BUILDER = 8004
PORT_MISSION_PLANNER = 8005
PORT_LIVEKIT = 8006
PORT_API_DELEGATION = 8000

# ==================== Infrastructure Ports ====================
PORT_ARANGODB = 8529
PORT_MINIO = 9000
PORT_POSTGRES = 5432
PORT_MQTT = 1883

# ==================== Service URLs ====================
# Default URLs for service-to-service communication
URL_GRAPH_BUILDER = f"http://localhost:{PORT_GRAPH_BUILDER}"
URL_MISSION_PLANNER = f"http://localhost:{PORT_MISSION_PLANNER}"
URL_LIVEKIT = f"http://localhost:{PORT_LIVEKIT}"
URL_API_DELEGATION = f"http://localhost:{PORT_API_DELEGATION}"
URL_MISSION_DISPATCH = f"http://localhost:5000"

# ==================== Spatial & Distance Thresholds ====================
# All spatial thresholds in meters
DISTANCE_THRESHOLD = 3.0  # Default distance threshold for traversability
RADIUS_THRESHOLD = 2.0    # Default radius for spatial search
RANGE_SEARCH_RADIUS = 2.0 # Default radius for range search in mission planning

# ==================== Timeouts ====================
# All timeouts in seconds
TIMEOUT_HTTP_REQUEST = 30      # Default HTTP request timeout
TIMEOUT_HTTP_REQUEST_SHORT = 10  # Short timeout for health checks
# ==================== Database Configuration ====================
# ArangoDB
ARANGO_HOST     = os.getenv("ARANGO_HOST", "localhost")
ARANGO_PORT     = int(os.getenv("ARANGO_PORT", str(PORT_ARANGODB)))
ARANGO_USERNAME = os.getenv("ARANGO_USERNAME", "root")
DATA_BASE_NAME  = os.getenv("DATABASE_NAME", "topomap_db")
GRAPH_NAME      = os.getenv("GRAPH_NAME", "topological_map")
NODE_COLLECTION = os.getenv("NODE_COLLECTION", "map_nodes")
EDGE_COLLECTION = os.getenv("EDGE_COLLECTION", "map_edges")

ARANGO_PASSWORD = os.getenv("ARANGO_PASSWORD")

# MinIO
MINIO_HOST   = os.getenv("MINIO_HOST", "localhost")
MINIO_PORT   = int(os.getenv("MINIO_PORT", str(PORT_MINIO)))
MINIO_SECURE = os.getenv("MINIO_SECURE", "false").lower() in ("true", "1", "yes")

MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY")

# PostgreSQL (container / base settings)
POSTGRES_HOST = os.getenv("POSTGRES_HOST", "localhost")
POSTGRES_PORT = int(os.getenv("POSTGRES_PORT", str(PORT_POSTGRES)))
POSTGRES_USER = os.getenv("POSTGRES_USER", "postgres")
POSTGRES_DB   = os.getenv("POSTGRES_DB", "mission_dispatch")

POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD")

# PostgreSQL (service connection settings — Docker Compose injects POSTGRES_DATABASE_*)
# Falls back to the base POSTGRES_* names for local development via .env.
POSTGRES_DATABASE_NAME     = os.getenv("POSTGRES_DATABASE_NAME", "mission")
POSTGRES_DATABASE_USERNAME = os.getenv("POSTGRES_DATABASE_USERNAME", POSTGRES_USER)
POSTGRES_DATABASE_PASSWORD = os.getenv("POSTGRES_DATABASE_PASSWORD", POSTGRES_PASSWORD)
POSTGRES_DATABASE_HOST     = os.getenv("POSTGRES_DATABASE_HOST", POSTGRES_HOST)
POSTGRES_DATABASE_PORT     = int(os.getenv("POSTGRES_DATABASE_PORT", str(POSTGRES_PORT)))

# Graph Builder specific
IMAGE_BUFFER_TIMEOUT = float(os.getenv("IMAGE_BUFFER_TIMEOUT", "30.0"))

# ==================== MQTT Configuration ====================
MQTT_HOST      = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT      = int(os.getenv("MQTT_PORT", str(PORT_MQTT)))
MQTT_KEEPALIVE = int(os.getenv("MQTT_KEEPALIVE", "60"))
MQTT_TOPIC_NODE_UPDATE = "robot/node_update"
# Some services use MQTT_BROKER; normalise to MQTT_HOST as fallback
MQTT_BROKER    = os.getenv("MQTT_BROKER", MQTT_HOST)
MQTT_IMAGE_TOPIC = os.getenv("MQTT_IMAGE_TOPIC", "robot/image_upload")

# ==================== Map Configuration ====================
DEFAULT_MAP_ID = "default"
GPS_MAP_SENTINEL = 'GEO'    # map_id sentinel for GPS-mode robots with no assigned map
LOCAL_MAP_SENTINEL = 'LOCAL'  # map_id sentinel for local-odometry robots with no assigned map

# ==================== Camera Configuration ====================
# Camera yaw offsets in radians relative to robot's forward direction (0 radians)
# These are used for topomap creation and factsheet generation
CAMERA_YAW_OFFSETS = {
    "front_camera": 0.0,           # 0 degrees - forward
    "right_camera": -1.5708,       # -90 degrees - right side
    "back_camera": 3.14159,        # 180 degrees - backward
    "left_camera": 1.5708,         # 90 degrees - left side
}

# ==================== Mission Planning ====================
KNN_K = 1  # Number of nearest neighbors to find

# ==================== Logging ====================
LOG_LEVEL_DEFAULT = "INFO"
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

# ==================== Host Binding ====================
DEFAULT_HOST = "0.0.0.0"  # Bind to all interfaces by default

# ==================== ROS Bag Storage ====================
ROSBAG_PRESIGN_EXPIRY = int(os.getenv("ROSBAG_PRESIGN_EXPIRY", "3600"))  # seconds

# ==================== LiveKit Configuration ====================
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET")
LIVEKIT_SERVER_URL = os.getenv("LIVEKIT_SERVER_URL", "ws://localhost:7880")
LIVEKIT_TTL = int(os.getenv("LIVEKIT_TTL", "36000"))

# Validate required credentials at import time
for _required_secret in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    if not os.getenv(_required_secret):
        raise EnvironmentError(
            f"Required environment variable {_required_secret} is not set. "
            "Check your .env file or deployment secrets."
        )

