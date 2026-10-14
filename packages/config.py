#!/usr/bin/env python3
"""
Centralized Configuration Constants

This module defines all default configuration values used across the cloud server services.
All services should import from this module to ensure consistency.

Credentials (passwords, keys) are read exclusively from environment variables so that
plaintext secrets never live in source code.  Set them via a root-level .env file
(see .env.example) or via Docker / Kubernetes secrets.

Importing this module never fails: a secret that is not set is None here, and the code that
needs one asks for it with `require_secret(name)` (EnvironmentError naming the variable), so a
service that never touches Arango/MinIO/Postgres does not have to be given their passwords.
Every service image ships this file, mission-dispatch included.
"""

import logging
import os
from typing import Optional


def require_secret(name: str) -> str:
    """The value of the credential environment variable `name`; EnvironmentError if unset/empty."""
    value = os.getenv(name)
    if not value:
        raise EnvironmentError(
            f"Required environment variable {name} is not set. "
            "Check your .env file or deployment secrets."
        )
    return value


def _env_bool(name: str, default: str) -> bool:
    return os.getenv(name, default).lower() in ("true", "1", "yes")


def _env_floats(name: str, default: str) -> tuple:
    """A comma-separated list of floats (e.g. back-off steps) as a tuple."""
    return tuple(float(v) for v in os.getenv(name, default).split(",") if v.strip())


# ==================== Service Ports ====================
# Default ports for all microservices
PORT_GRAPH_BUILDER = 8004
PORT_MISSION_PLANNER = 8005
PORT_LIVEKIT = 8006
PORT_AGENT_ORCHESTRATOR = 8007
PORT_API_DELEGATION = 8000

# ==================== Infrastructure Ports ====================
PORT_ARANGODB = 8529
PORT_MINIO = 9000
PORT_POSTGRES = 5432
PORT_MQTT = 1883

# ==================== Service URLs ====================
# Default URLs for service-to-service communication; each is overridable by the env var
# named here (docker_compose sets MISSION_PLANNER_URL and LIVEKIT_URL for the API). There is no
# HTTP hop to mission-dispatch (PostgreSQL is the interface), hence no URL for it.
URL_GRAPH_BUILDER = os.getenv("GRAPH_BUILDER_URL", f"http://localhost:{PORT_GRAPH_BUILDER}")
URL_MISSION_PLANNER = os.getenv("MISSION_PLANNER_URL", f"http://localhost:{PORT_MISSION_PLANNER}")
URL_LIVEKIT = os.getenv("LIVEKIT_URL", f"http://localhost:{PORT_LIVEKIT}")
URL_AGENT_ORCHESTRATOR = os.getenv("AGENT_ORCHESTRATOR_URL",
                                   f"http://localhost:{PORT_AGENT_ORCHESTRATOR}")
URL_API_DELEGATION = os.getenv("API_DELEGATION_URL", f"http://localhost:{PORT_API_DELEGATION}")
# WebSocket backends the API proxies /ws/... to (packages/api/main.py). The dispatcher one is
# unset unless given: mission-dispatch has no WebSocket endpoint.
GRAPH_BUILDER_WS_URL = os.getenv("GRAPH_BUILDER_WS_URL", f"ws://localhost:{PORT_GRAPH_BUILDER}")
MISSION_DISPATCHER_WS_URL = os.getenv("MISSION_DISPATCHER_WS_URL") or None
# The API's MQTT bridge; "false" turns it off.
MQTT_ENABLED = os.getenv("MQTT_ENABLED", "true").lower() == "true"

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

# Secrets: None when unset (importing never fails); use require_secret("...") where one is needed.
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
POSTGRES_DATABASE_HOST     = os.getenv("POSTGRES_DATABASE_HOST", POSTGRES_HOST)
POSTGRES_DATABASE_PORT     = int(os.getenv("POSTGRES_DATABASE_PORT", str(POSTGRES_PORT)))
# None when neither is set; a service that connects uses postgres_database_password() instead.
POSTGRES_DATABASE_PASSWORD = os.getenv("POSTGRES_DATABASE_PASSWORD", POSTGRES_PASSWORD)


def postgres_database_password() -> str:
    """POSTGRES_DATABASE_PASSWORD, else POSTGRES_PASSWORD; EnvironmentError if neither is set.
    Read when a connection is built, not at import."""
    value = os.getenv("POSTGRES_DATABASE_PASSWORD")
    return value if value is not None else require_secret("POSTGRES_PASSWORD")


# Graph Builder specific
IMAGE_BUFFER_TIMEOUT = float(os.getenv("IMAGE_BUFFER_TIMEOUT", "30.0"))
# Uploads that arrive before their node wait in memory (graph-builder): each of the image, depth
# and costmap buffers holds at most this many bytes of base64 data / entries; a further upload
# is dropped and reported as MAP.INGEST_REJECTED (reason buffer_full).
UPLOAD_BUFFER_MAX_BYTES = int(os.getenv("UPLOAD_BUFFER_MAX_BYTES", str(256 * 1024 * 1024)))
UPLOAD_BUFFER_MAX_ENTRIES = int(os.getenv("UPLOAD_BUFFER_MAX_ENTRIES", "5000"))

# ==================== MQTT Configuration ====================
MQTT_HOST      = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT      = int(os.getenv("MQTT_PORT", str(PORT_MQTT)))
MQTT_KEEPALIVE = int(os.getenv("MQTT_KEEPALIVE", "60"))
MQTT_TOPIC_NODE_UPDATE = "robot/node_update"
# Some services use MQTT_BROKER; normalise to MQTT_HOST as fallback
MQTT_BROKER    = os.getenv("MQTT_BROKER", MQTT_HOST)
MQTT_IMAGE_TOPIC = os.getenv("MQTT_IMAGE_TOPIC", "robot/image_upload")
# 3D reconstruction R2 (docs/reconstruction/design.md §5): one u16-mm depth PNG + camera
# parameters per node and camera (graph-builder); empty = not subscribed.
MQTT_DEPTH_TOPIC = os.getenv("MQTT_DEPTH_TOPIC", "robot/depth_upload")
# One occupancy-costmap PNG per node and layer (graph-builder); empty = not subscribed.
MQTT_COSTMAP_TOPIC = os.getenv("MQTT_COSTMAP_TOPIC", "robot/costmap_upload")
# VDA5050 topic prefix — must match the mission controller's prefix so the agent
# orchestrator subscribes to the same robot state stream (`{prefix}/+/state`).
MQTT_VDA5050_PREFIX = os.getenv("MQTT_VDA5050_PREFIX", "uagv/v2/RobotCompany")

# ==================== Mapping switch through the robot's orchestrator ====================
# A mapping session's services (packages/utils/map_sessions.py::ORCHESTRATOR_SERVICES) are started and
# stopped on the robot's satibot_orchestrator (packages/api/mapping_switch.py). The orchestrator
# names them differently on the real robot and in the sim: per session service, an ordered list
# of candidate orchestrator service names; the first one the robot's orchestrator lists is used.
# Override with MAPPING_SERVICE_<NAME> (comma-separated), e.g. MAPPING_SERVICE_TOPO=topomap.
def _candidates(env: str, default: str) -> list:
    return [n.strip() for n in os.getenv(env, default).split(",") if n.strip()]


MAPPING_SERVICE_CANDIDATES = {
    "topo": _candidates("MAPPING_SERVICE_TOPO", "topomap,sim_topomap"),
    "grid": _candidates("MAPPING_SERVICE_GRID", "grid"),
}
# Orchestrator HTTP timeouts (seconds): status/list are polled for the robot views, start can
# take a while (the orchestrator launches the service's entrypoint), stop is a process kill.
ORCHESTRATOR_QUERY_TIMEOUT_S = float(os.getenv("ORCHESTRATOR_QUERY_TIMEOUT_S", "3.0"))
ORCHESTRATOR_START_TIMEOUT_S = float(os.getenv("ORCHESTRATOR_START_TIMEOUT_S", "30.0"))
ORCHESTRATOR_STOP_TIMEOUT_S = float(os.getenv("ORCHESTRATOR_STOP_TIMEOUT_S", "15.0"))
# A synchronous POST /localization/save (the orchestrator proxy) waits for the driver's save_map:
# the orchestrator gives up after 600 s (mapping.save_timeout_sec), so a little longer.
ORCHESTRATOR_SAVE_TIMEOUT_S = float(os.getenv("ORCHESTRATOR_SAVE_TIMEOUT_S", "660.0"))
# A background save (POST /localization/save?background=true) is polled (GET /localization/save)
# every SAVE_POLL_S for at most SAVE_POLL_TOTAL_S (600 s timeout plus margin) per attempt. A save
# that did not finish in time is retried once, SAVE_RETRY_S later.
ORCHESTRATOR_SAVE_POLL_S = float(os.getenv("ORCHESTRATOR_SAVE_POLL_S", "3.0"))
ORCHESTRATOR_SAVE_POLL_TOTAL_S = float(os.getenv("ORCHESTRATOR_SAVE_POLL_TOTAL_S", "660.0"))
ORCHESTRATOR_SAVE_RETRY_S = float(os.getenv("ORCHESTRATOR_SAVE_RETRY_S", "30.0"))
# A save with `then=stay` stays `saving` until the orchestrator's switch onto the saved map is over
# (about 8-10 s): the poll waits this much longer than ORCHESTRATOR_SAVE_POLL_TOTAL_S.
ORCHESTRATOR_SAVE_THEN_S = float(os.getenv("ORCHESTRATOR_SAVE_THEN_S", "45.0"))
# Frame witness (packages/utils/map_sessions.py, run_change.FrameWatcher): a placed session is
# unplaced (reason frame_changed) only after the robot's definitive reading disagreed with the
# placement's frame for this long AND in this many consecutive definitive states.
FRAME_CONFIRM_S = float(os.getenv("FRAME_CONFIRM_S", "30.0"))
FRAME_CONFIRM_STATES = int(os.getenv("FRAME_CONFIRM_STATES", "3"))
# After a robot run change (mission-dispatch NOTIFY) the API starts the services of the robot's
# non-paused mapping session again (packages/api/maps.py restart_after_run_change); a robot that
# is still coming up refuses, so a failed restart is tried again RUN_CHANGE_RESTART_RETRY_S later,
# RUN_CHANGE_RESTART_TRIES times in all (only the last failure is reported).
RUN_CHANGE_RESTART_TRIES = int(os.getenv("RUN_CHANGE_RESTART_TRIES", "3"))
RUN_CHANGE_RESTART_RETRY_S = float(os.getenv("RUN_CHANGE_RESTART_RETRY_S", "10.0"))
# How long a fetched mapping service state is reused for the robot views (seconds).
MAPPING_STATE_TTL_S = float(os.getenv("MAPPING_STATE_TTL_S", "5.0"))
# How long "does this robot's orchestrator hold a stored map for cloud map X" is reused
# (packages/api/orchestrator_maps.py; relocalization, docs/satinav-maps-redesign.md ## 16).
RELOC_MAP_HELD_TTL_S = float(os.getenv("RELOC_MAP_HELD_TTL_S", "15.0"))
# A placed `reloc` session is flagged (robot view `localization_warning`) when the robot's
# localizationScore is below this. A warning only: it never refuses a placement.
RELOC_DEGRADED_SCORE = float(os.getenv("RELOC_DEGRADED_SCORE", "0.3"))
# Starting relocalization from the API (packages/api/reloc_job.py, docs/satinav-maps-redesign.md
# ## 16): `RELOC_JOB_TIMEOUT_S`: how long a job waits for the robot to report itself localized on
# the map; `RELOC_JOB_POLL_S`: how often the stored robot status is read.
RELOC_JOB_TIMEOUT_S = float(os.getenv("RELOC_JOB_TIMEOUT_S", "90.0"))
RELOC_JOB_POLL_S = float(os.getenv("RELOC_JOB_POLL_S", "1.0"))
# After the robot reports itself localized the job proposes the placement and waits this long for
# the user to confirm or edit it; then the server confirms by itself (RELOC_CONFIRM_TIMEOUT_S).
RELOC_CONFIRM_TIMEOUT_S = float(os.getenv("RELOC_CONFIRM_TIMEOUT_S", "30.0"))

# ==================== Phase 0 telemetry ingest (API) ====================
# docs/satinav-fleet-agent-phase0-v2.md §5.3 "api" items 2-4 (packages/api/telemetry.py).
# Kill switch: "false" leaves the API exactly as before (in-memory caches only).
TELEMETRY_INGEST_ENABLED = os.getenv("TELEMETRY_INGEST_ENABLED", "true").lower() in ("true", "1", "yes")
# Directory of the per-worker event spill files (api-<pid>.jsonl). Mount a volume here to keep
# spilled events across container restarts; the default is lost with the container.
TELEMETRY_SPILL_DIR = os.getenv("TELEMETRY_SPILL_DIR", "/tmp/satinav_telemetry_spill")
# How often a non-writer worker retries pg_try_advisory_lock('telemetry_writer'), and how often
# the writer re-checks its lock connection (seconds).
TELEMETRY_ELECTION_RETRY_S = float(os.getenv("TELEMETRY_ELECTION_RETRY_S", "5.0"))
TELEMETRY_ELECTION_CHECK_S = float(os.getenv("TELEMETRY_ELECTION_CHECK_S", "2.0"))
# SYSTEM.THERMAL_HIGH / THERMAL_OK hysteresis (°C, max of the jtop cpu/gpu/soc temperatures).
THERMAL_HIGH_C = float(os.getenv("THERMAL_HIGH_C", "85.0"))
THERMAL_OK_C = float(os.getenv("THERMAL_OK_C", "78.0"))

# ==================== Phase 0 recorder health + alerts (API, WP13) ====================
# packages/api/recorder_health.py: GET /api/v1/health/recording and the alert rules. The API's
# elected telemetry writer evaluates them every RECORDER_HEALTH_EVAL_S, writes its own
# `recorder_health` row, and emits SYSTEM.RECORDER_ALERT_RAISED / _CLEARED once per transition.
# mission-dispatch reports every RECORDER_HEALTH_REPORT_PERIOD_S (10 s).
RECORDER_HEALTH_EVAL_S = float(os.getenv("RECORDER_HEALTH_EVAL_S", "5.0"))
# report_stale: a process's row is older than this (seconds). Also applied when reading.
RECORDER_HEALTH_STALE_S = float(os.getenv("RECORDER_HEALTH_STALE_S", "60.0"))
# writer_queue_high: queue depth above this % of capacity; clears below the clear %.
RECORDER_ALERT_QUEUE_PCT = float(os.getenv("RECORDER_ALERT_QUEUE_PCT", "80.0"))
RECORDER_ALERT_QUEUE_CLEAR_PCT = float(os.getenv("RECORDER_ALERT_QUEUE_CLEAR_PCT", "60.0"))
# spill_pending: spilled events have been waiting continuously for longer than this (seconds).
RECORDER_ALERT_SPILL_S = float(os.getenv("RECORDER_ALERT_SPILL_S", "300.0"))
# heartbeat_sweep_lag: dispatch's sweep lag above FACTOR x its period (1 s -> 3 s); clears at
# or below CLEAR_FACTOR x period.
RECORDER_ALERT_SWEEP_LAG_FACTOR = float(os.getenv("RECORDER_ALERT_SWEEP_LAG_FACTOR", "3.0"))
RECORDER_ALERT_SWEEP_LAG_CLEAR_FACTOR = float(
    os.getenv("RECORDER_ALERT_SWEEP_LAG_CLEAR_FACTOR", "1.5"))
# Anti-flap: a raise condition must hold this long before the alert starts, and the clear
# condition this long before it ends (seconds).
RECORDER_ALERT_RAISE_S = float(os.getenv("RECORDER_ALERT_RAISE_S", "10.0"))
RECORDER_ALERT_CLEAR_S = float(os.getenv("RECORDER_ALERT_CLEAR_S", "30.0"))

# ==================== Phase 0 read endpoints (API) ====================
# docs/satinav-fleet-agent-phase0-v2.md §5.5, WP10 (packages/api/fleet_reads.py): /runs,
# /runs/{id}, /runs/{id}/timeline, /events. Each request runs in one READ ONLY transaction whose
# statements are cancelled after this many milliseconds (503), so a heavy query can't hold a
# pooled connection for long.
FLEET_READ_STATEMENT_TIMEOUT_MS = int(os.getenv("FLEET_READ_STATEMENT_TIMEOUT_MS", "5000"))
# Timeline caps: points per track (robot_state / diagnostics / trajectory; longer series are
# downsampled) and events per run (the rest is cut, with a `*_truncated` flag).
FLEET_TIMELINE_MAX_POINTS = int(os.getenv("FLEET_TIMELINE_MAX_POINTS", "2000"))
# GET /api/v1/maps/{map}/run-stats (packages/api/run_stats.py): default window in days, and the
# run length above which a run is left out of the operating hours (a hung run, e.g. a cancel
# 14 h after its start, is not operating time; docs/map_run_stats_2026-10-14.md).
RUN_STATS_DEFAULT_DAYS = int(os.getenv("RUN_STATS_DEFAULT_DAYS", "30"))
RUN_STATS_MAX_RUN_S = int(os.getenv("RUN_STATS_MAX_RUN_S", "7200"))
# GET /api/v1/maps/{map}/events-summary and /events (packages/api/map_events.py): answers are
# cached this many seconds per (map, window, filters); an episode (a RAISED/ENTERED event) counts
# as a repeat of an earlier one when the earlier one lies within MAP_EVENTS_EPISODE_LOOKBACK_DAYS
# before the window start and was not cleared in between.
MAP_EVENTS_CACHE_TTL_S = float(os.getenv("MAP_EVENTS_CACHE_TTL_S", "30"))
MAP_EVENTS_CACHE_ENTRIES = int(os.getenv("MAP_EVENTS_CACHE_ENTRIES", "64"))
MAP_EVENTS_EPISODE_LOOKBACK_DAYS = int(os.getenv("MAP_EVENTS_EPISODE_LOOKBACK_DAYS", "7"))
# TELEOP.SESSION_STARTED (packages/api/teleop.py): a robot's room token within this many seconds
# of its previous one (same robot and room) belongs to the same session and records nothing.
TELEOP_SESSION_GAP_S = int(os.getenv("TELEOP_SESSION_GAP_S", "1800"))
FLEET_TIMELINE_MAX_EVENTS = int(os.getenv("FLEET_TIMELINE_MAX_EVENTS", "5000"))
# GET /api/v1/runs/{id}/track: at most this many points (1 Hz = a 5.5 h run); more are strided.
FLEET_TRACK_MAX_POINTS = int(os.getenv("FLEET_TRACK_MAX_POINTS", "20000"))

# ==================== Phase 0 fixes (API, WP11) ====================
# F1 map delete (packages/api/map_delete.py): cleanup attempts per round before
# MAP.DELETE_FAILED, and the backoff between them (base * 2^(n-1), capped), in seconds.
# A map that exhausts its round stays DELETING until the next API start or another DELETE.
MAP_DELETE_MAX_ATTEMPTS = int(os.getenv("MAP_DELETE_MAX_ATTEMPTS", "5"))
MAP_DELETE_BACKOFF_S = float(os.getenv("MAP_DELETE_BACKOFF_S", "2.0"))
MAP_DELETE_BACKOFF_MAX_S = float(os.getenv("MAP_DELETE_BACKOFF_MAX_S", "60.0"))
# Scene graph: a delete first takes the worker's advisory lock `scene_graph:<map>`, polling up to
# SCENE_GRAPH_DELETE_LOCK_TIMEOUT_S per attempt. A fold holding it does NOT consume
# MAP_DELETE_MAX_ATTEMPTS: the delete keeps retrying (backoff) until the lock has been busy for
# SCENE_GRAPH_DELETE_LOCK_DEADLINE_S in total; only then does a busy lock count as a failed attempt.
SCENE_GRAPH_DELETE_LOCK_TIMEOUT_S = float(os.getenv("SCENE_GRAPH_DELETE_LOCK_TIMEOUT_S", "30.0"))
SCENE_GRAPH_DELETE_LOCK_DEADLINE_S = float(os.getenv("SCENE_GRAPH_DELETE_LOCK_DEADLINE_S", "3600.0"))
# F3 Idempotency-Key (packages/api/idempotency.py): how long a key is remembered, how long an
# unfinished request holds its key before a retry may take it over (longer than any guarded
# route can take), and how often a worker purges expired keys (seconds).
IDEMPOTENCY_TTL_S = int(os.getenv("IDEMPOTENCY_TTL_S", str(24 * 3600)))
IDEMPOTENCY_LEASE_S = int(os.getenv("IDEMPOTENCY_LEASE_S", "120"))
IDEMPOTENCY_PURGE_INTERVAL_S = float(os.getenv("IDEMPOTENCY_PURGE_INTERVAL_S", "600"))

# ==================== 3D reconstruction gateway (API, R3) ====================
# docs/reconstruction/design.md §6.9 (packages/api/reconstruction.py). The reconstruction runs in
# an external service (its own repo, any host); the API owns the job and talks to it over HTTP.
# Unset (or empty) service URL, key or callback secret = feature off: POST .../reconstruction
# answers 503 not_configured. The secrets are deliberately NOT in the import-time required list.
def _env_or_none(name: str):
    value = os.getenv(name)
    return value if value else None


RECONSTRUCTION_SERVICE_URL = _env_or_none("RECONSTRUCTION_SERVICE_URL")
RECONSTRUCTION_SERVICE_KEY = _env_or_none("RECONSTRUCTION_SERVICE_KEY")
RECONSTRUCTION_CALLBACK_SECRET = _env_or_none("RECONSTRUCTION_CALLBACK_SECRET")
# How the service reaches the API's /internal/reconstruction/... callbacks.
RECONSTRUCTION_CALLBACK_BASE_URL = os.getenv("RECONSTRUCTION_CALLBACK_BASE_URL",
                                             "http://localhost:8000")
# MinIO host:port as the SERVICE sees it: presigned URLs are signed for this host (SigV4 signs
# the Host header; they cannot be rewritten afterwards). localhost:9000 while the service runs
# on this host (MinIO listens on 127.0.0.1 and the Tailscale IP only).
RECONSTRUCTION_MINIO_ENDPOINT = os.getenv("RECONSTRUCTION_MINIO_ENDPOINT", "localhost:9000")
RECONSTRUCTION_MINIO_SECURE = os.getenv("RECONSTRUCTION_MINIO_SECURE",
                                        "false").lower() in ("true", "1", "yes")
RECONSTRUCTION_STAGING_BUCKET = os.getenv("RECONSTRUCTION_STAGING_BUCKET", "recon-staging")
RECONSTRUCTION_URL_EXPIRY_S = int(os.getenv("RECONSTRUCTION_URL_EXPIRY_S", "14400"))
RECONSTRUCTION_JOB_TIMEOUT_S = int(os.getenv("RECONSTRUCTION_JOB_TIMEOUT_S", "3600"))
RECONSTRUCTION_QUEUE_TIMEOUT_S = int(os.getenv("RECONSTRUCTION_QUEUE_TIMEOUT_S", "1800"))
RECONSTRUCTION_MAX_INFLIGHT = int(os.getenv("RECONSTRUCTION_MAX_INFLIGHT", "1"))
RECONSTRUCTION_VOXEL_M = float(os.getenv("RECONSTRUCTION_VOXEL_M", "0.05"))
RECONSTRUCTION_MAX_DEPTH_M = float(os.getenv("RECONSTRUCTION_MAX_DEPTH_M", "10.0"))
RECONSTRUCTION_CLIP_Z = float(os.getenv("RECONSTRUCTION_CLIP_Z", "2.3"))
# The costmap-like relief grid next to the top view: cell size (doubled until it fits) and cap.
RECONSTRUCTION_RELIEF_RES_M = float(os.getenv("RECONSTRUCTION_RELIEF_RES_M", "0.10"))
RECONSTRUCTION_RELIEF_MAX_CELLS = int(os.getenv("RECONSTRUCTION_RELIEF_MAX_CELLS", "4000000"))
# The top view (ortho.png, height.png) is derived by the API from cloud.ply in a child process
# (packages/api/reconstruction_topview.py): its address-space cap, its time limit, and where the
# PLY is downloaded to (empty = the system temp dir; needs ~200 MB free for a 10 M-point cloud).
RECONSTRUCTION_TOPVIEW_MEM_MB = int(os.getenv("RECONSTRUCTION_TOPVIEW_MEM_MB", "1024"))
RECONSTRUCTION_TOPVIEW_TIMEOUT_S = int(os.getenv("RECONSTRUCTION_TOPVIEW_TIMEOUT_S", "600"))
RECONSTRUCTION_WORK_DIR = _env_or_none("RECONSTRUCTION_WORK_DIR")

# ==================== Map Configuration ====================
DEFAULT_MAP_ID = "default"

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
# Self-hosted SFU (docker_compose/livekit_sfu.env). The API service uses this pair only to
# remove a deleted robot's participant (packages/api/livekit_admin.py); unset = that step is
# skipped. The admin URL is the SFU's HTTP port as seen from the API service (host network).
LIVEKIT_SFU_API_KEY = os.getenv("LIVEKIT_SFU_API_KEY") or None
LIVEKIT_SFU_API_SECRET = os.getenv("LIVEKIT_SFU_API_SECRET") or None
LIVEKIT_SFU_ADMIN_URL = os.getenv("LIVEKIT_SFU_ADMIN_URL", "http://localhost:7880")
LIVEKIT_ADMIN_TIMEOUT = float(os.getenv("LIVEKIT_ADMIN_TIMEOUT", "3"))  # seconds per request

# ==================== Offline missions (VDA5050 orders) ====================
# What mission-dispatch decides in advance for each node of an order (allowedDeviationXY/Theta,
# nodePolicy, timeout paused while offline, reroute-cancel dwell, blocked-node exclusion time)
# is read by packages/controllers/mission/order_policy.py (its own env keys, listed there).
# The planner and the API read BLOCKED_NODE_MATCH_RADIUS_M in packages/utils/blocked_nodes.py.

# ==================== Agent Orchestrator ====================
# LLM-based fleet triage service. ANTHROPIC_API_KEY is intentionally optional:
# when unset the orchestrator runs in degraded (non-LLM) mode and emits
# deterministic summaries, so the service still boots in dev without a key.
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
AGENT_MODEL = os.getenv("AGENT_MODEL", "claude-haiku-4-5")
# Optional: point the Anthropic client at an Anthropic-compatible proxy (e.g. a
# local LiteLLM proxy in front of a free model like Gemini) for development.
# Leave unset to talk to the real Anthropic API. See docker_compose/llm_proxy.yaml.
ANTHROPIC_BASE_URL = os.getenv("ANTHROPIC_BASE_URL") or None
# Battery state-of-charge (%) at or below which a low-battery event fires. One setting for the
# fleet recorder (BATTERY.LOW, see DISPATCH_RECORDER below) and the agent; the agent's own env
# name still overrides it for the agent alone.
BATTERY_LOW_PCT = float(os.getenv("BATTERY_LOW_PCT", "20.0"))
AGENT_BATTERY_LOW_THRESHOLD = float(os.getenv("AGENT_BATTERY_LOW_THRESHOLD", str(BATTERY_LOW_PCT)))

# ==================== API: orchestrator proxy, mapping switch, caches ====================
# packages/api/orchestrator_proxy.py: timeout of a proxied orchestrator request that is not a save.
ORCHESTRATOR_PROXY_TIMEOUT_S = float(os.getenv("ORCHESTRATOR_PROXY_TIMEOUT_S", "60.0"))
# packages/api/orchestrator_maps.py: how long an "unknown" (None) answer is reused: just enough
# that a burst of reads of an unreachable robot does not each wait for the timeout. A real
# answer lives RELOC_MAP_HELD_TTL_S.
ORCHESTRATOR_MAPS_UNKNOWN_TTL_S = float(os.getenv("ORCHESTRATOR_MAPS_UNKNOWN_TTL_S", "2.0"))
# packages/api/mapping_switch.py save_slam(): a 503 (driver not up yet) is retried this often;
# consecutive failed status reads that end a poll; saves started per save_slam() (one retry of a
# save that ran out of time).
SLAM_SAVE_START_TRIES = int(os.getenv("SLAM_SAVE_START_TRIES", "5"))
SLAM_SAVE_POLL_ERRORS = int(os.getenv("SLAM_SAVE_POLL_ERRORS", "5"))
SLAM_SAVE_MAX_ATTEMPTS = int(os.getenv("SLAM_SAVE_MAX_ATTEMPTS", "2"))
# packages/api/reloc_job.py: finished relocalization jobs kept in memory (oldest dropped first).
RELOC_MAX_FINISHED_JOBS = int(os.getenv("RELOC_MAX_FINISHED_JOBS", "50"))
# packages/api/maps.py OpenSessionCache: robot_sessions() is reused this long (seconds). Changes
# through the API invalidate it; changes by mission-dispatch show within this time.
OPEN_SESSION_CACHE_TTL_S = float(os.getenv("OPEN_SESSION_CACHE_TTL_S", "1.0"))

# ==================== PostgreSQL pool (packages/database/postgres.py) ====================
POSTGRES_POOL_MIN_SIZE = int(os.getenv("POSTGRES_POOL_MIN_SIZE", "2"))
POSTGRES_POOL_MAX_SIZE = int(os.getenv("POSTGRES_POOL_MAX_SIZE", "10"))
# Seconds before trying to reconnect to the database: pool open, and the LISTEN/NOTIFY watcher.
POSTGRES_RECONNECT_PERIOD = float(os.getenv("POSTGRES_RECONNECT_PERIOD", "0.5"))
WATCHER_POSTGRES_RECONNECT_PERIOD = float(os.getenv("WATCHER_POSTGRES_RECONNECT_PERIOD", "0.1"))

# ==================== mission-dispatch (packages/controllers/mission) ====================
# Process defaults (its command line in docker_compose sets the MQTT and PostgreSQL ones).
DISPATCH_DISABLE_FLEET_RECORDING = _env_bool("DISABLE_FLEET_RECORDING", "false")
DISPATCH_FLEET_SPILL_PATH = os.getenv("FLEET_SPILL_PATH", "/tmp/mission_dispatch/fleet_events_spill.jsonl")

# server.py: webhook calls (notify nodes, the charging hook) run in a worker thread; a notify
# node's spec timeout is capped, its retries wait these seconds before the 2nd, 3rd and 4th
# attempt. The charging hook (--mission_ctrl_url): per-request timeout and the least time
# between two attempts for one robot.
DISPATCH_NOTIFY_MAX_TIMEOUT_S = float(os.getenv("DISPATCH_NOTIFY_MAX_TIMEOUT_S", "30.0"))
DISPATCH_NOTIFY_RETRY_BACKOFF_S = _env_floats("DISPATCH_NOTIFY_RETRY_BACKOFF_S", "1.0,2.0,4.0")
DISPATCH_CHARGING_HOOK_TIMEOUT_S = float(os.getenv("DISPATCH_CHARGING_HOOK_TIMEOUT_S", "5.0"))
DISPATCH_CHARGING_HOOK_RETRY_S = float(os.getenv("DISPATCH_CHARGING_HOOK_RETRY_S", "60.0"))
# A handler that fails the same way on every state message logs its first occurrence, then at
# most one count summary per interval; at most MAX_KINDS distinct kinds are tracked.
DISPATCH_LOOP_ERROR_SUMMARY_INTERVAL_S = float(
    os.getenv("DISPATCH_LOOP_ERROR_SUMMARY_INTERVAL_S", "60.0"))
DISPATCH_LOOP_ERROR_MAX_KINDS = int(os.getenv("DISPATCH_LOOP_ERROR_MAX_KINDS", "32"))
# The recording-only settings watcher's wait before re-watching after a failure; _watch_changes
# (missions, robots) retries after the minimum, doubling up to the maximum.
DISPATCH_SETTINGS_WATCH_RETRY_S = float(os.getenv("DISPATCH_SETTINGS_WATCH_RETRY_S", "5.0"))
DISPATCH_WATCH_CHANGES_RETRY_MIN_S = float(os.getenv("DISPATCH_WATCH_CHANGES_RETRY_MIN_S", "1.0"))
DISPATCH_WATCH_CHANGES_RETRY_MAX_S = float(os.getenv("DISPATCH_WATCH_CHANGES_RETRY_MAX_S", "30.0"))
# An MQTT message from an unknown robot is looked up in the database once per this many seconds.
DISPATCH_UNKNOWN_ROBOT_TTL_S = float(os.getenv("DISPATCH_UNKNOWN_ROBOT_TTL_S", "60.0"))
# A robot state of ON_TASK with no mission is set back to IDLE only this long after the
# robot's controller was created.
DISPATCH_STALE_STATE_GRACE_S = float(os.getenv("DISPATCH_STALE_STATE_GRACE_S", "30.0"))
# A failed run-epoch check is retried after RETRY_S; the check is given TIMEOUT_S.
DISPATCH_RUN_CHECK_RETRY_S = float(os.getenv("DISPATCH_RUN_CHECK_RETRY_S", "30.0"))
DISPATCH_RUN_CHECK_TIMEOUT_S = float(os.getenv("DISPATCH_RUN_CHECK_TIMEOUT_S", "5.0"))
# A current mission whose start failed is started again at most every START_RETRY_S, and
# failed after MAX_START_ATTEMPTS.
DISPATCH_START_RETRY_S = float(os.getenv("DISPATCH_START_RETRY_S", "5.0"))
DISPATCH_MAX_START_ATTEMPTS = int(os.getenv("DISPATCH_MAX_START_ATTEMPTS", "5"))
# A failed status write is retried with this back-off (doubling, capped); a failure streak is
# an error from the Nth failure on; a shutdown waits FLUSH_S for the queued writes.
DISPATCH_STATUS_WRITE_RETRY_MIN_S = float(os.getenv("DISPATCH_STATUS_WRITE_RETRY_MIN_S", "0.5"))
DISPATCH_STATUS_WRITE_RETRY_MAX_S = float(os.getenv("DISPATCH_STATUS_WRITE_RETRY_MAX_S", "10.0"))
DISPATCH_STATUS_WRITE_ERROR_AFTER = int(os.getenv("DISPATCH_STATUS_WRITE_ERROR_AFTER", "5"))
DISPATCH_STATUS_WRITE_FLUSH_S = float(os.getenv("DISPATCH_STATUS_WRITE_FLUSH_S", "3.0"))
# Graceful shutdown steps (their sum must stay under lifecycle.SHUTDOWN_TIMEOUT_S).
DISPATCH_SHUTDOWN_MQTT_S = float(os.getenv("DISPATCH_SHUTDOWN_MQTT_S", "1.5"))
DISPATCH_SHUTDOWN_ROBOTS_S = float(
    os.getenv("DISPATCH_SHUTDOWN_ROBOTS_S", str(DISPATCH_STATUS_WRITE_FLUSH_S + 1.0)))
DISPATCH_SHUTDOWN_RECORDER_DRAIN_S = float(os.getenv("DISPATCH_SHUTDOWN_RECORDER_DRAIN_S", "2.5"))
DISPATCH_SHUTDOWN_RECORDER_STOP_S = float(os.getenv("DISPATCH_SHUTDOWN_RECORDER_STOP_S", "2.0"))
DISPATCH_SHUTDOWN_POOL_CLOSE_S = float(os.getenv("DISPATCH_SHUTDOWN_POOL_CLOSE_S", "1.5"))
DISPATCH_SHUTDOWN_LOCK_S = float(os.getenv("DISPATCH_SHUTDOWN_LOCK_S", "1.0"))
# The robot row is written at most this often for continuously changing fields.
DISPATCH_ROBOT_STATUS_MIN_WRITE_S = float(os.getenv("DISPATCH_ROBOT_STATUS_MIN_WRITE_S", "1.0"))
# A coalesced (replaced) state message is a warning the first time and then every N times.
DISPATCH_STATE_COALESCED_WARN_EVERY = int(os.getenv("DISPATCH_STATE_COALESCED_WARN_EVERY", "100"))
# Per-node notes and skipped nodes kept on a mission.
DISPATCH_MISSION_NODE_NOTES_MAX = int(os.getenv("DISPATCH_MISSION_NODE_NOTES_MAX", "50"))
DISPATCH_MISSION_SKIPPED_NODES_MAX = int(os.getenv("DISPATCH_MISSION_SKIPPED_NODES_MAX", "100"))
# Node offsets that suggest a frame error: at least MIN_N, mostly one way, on average MIN_M far.
DISPATCH_OFFSET_SUSPECT_MIN_N = int(os.getenv("DISPATCH_OFFSET_SUSPECT_MIN_N", "5"))
DISPATCH_OFFSET_SUSPECT_CONSISTENCY = float(os.getenv("DISPATCH_OFFSET_SUSPECT_CONSISTENCY", "0.8"))
DISPATCH_OFFSET_SUSPECT_MIN_M = float(os.getenv("DISPATCH_OFFSET_SUSPECT_MIN_M", "0.2"))
# A datum that moves less than this is GNSS jitter, not a change.
DISPATCH_DATUM_CHANGE_THRESHOLD_M = float(os.getenv("DISPATCH_DATUM_CHANGE_THRESHOLD_M", "1.0"))
DISPATCH_DATUM_BEARING_THRESHOLD_DEG = float(
    os.getenv("DISPATCH_DATUM_BEARING_THRESHOLD_DEG", "0.5"))
# Approximate position: a smaller move (accuracy within REL_TOL) is not stored, unless the
# stored copy is older than REFRESH_S.
DISPATCH_APPROX_POSITION_THRESHOLD_M = float(
    os.getenv("DISPATCH_APPROX_POSITION_THRESHOLD_M", "5.0"))
DISPATCH_APPROX_ACCURACY_REL_TOL = float(os.getenv("DISPATCH_APPROX_ACCURACY_REL_TOL", "0.2"))
DISPATCH_APPROX_POSITION_REFRESH_S = float(os.getenv("DISPATCH_APPROX_POSITION_REFRESH_S", "300.0"))
# Robot controller: instant-action and order resend budgets (counts per state message or
# seconds, see the comments in server.py::Robot), and how many finished missions are remembered.
DISPATCH_MAX_INSTANT_ACTION_RESENDS = int(os.getenv("DISPATCH_MAX_INSTANT_ACTION_RESENDS", "20"))
DISPATCH_MAX_ORDER_MISMATCHES = int(os.getenv("DISPATCH_MAX_ORDER_MISMATCHES", "40"))
DISPATCH_MAX_FINISHED_MISSIONS_TRACKED = int(
    os.getenv("DISPATCH_MAX_FINISHED_MISSIONS_TRACKED", "256"))

# fleet_recorder.py (Phase 0 recording). Battery: BATTERY.LOW at or below BATTERY_LOW_PCT (the
# agent's low threshold derives from the same setting), BATTERY.OK at or above BATTERY_OK_PCT.
BATTERY_OK_PCT = float(os.getenv("BATTERY_OK_PCT", "25.0"))
RECORDER_STATE_ROW_INTERVAL_S = float(os.getenv("RECORDER_STATE_ROW_INTERVAL_S", "5.0"))
RECORDER_LATEST_STATE_MSG_INTERVAL_S = float(
    os.getenv("RECORDER_LATEST_STATE_MSG_INTERVAL_S", "5.0"))
RECORDER_TRACK_ROW_INTERVAL_S = float(os.getenv("RECORDER_TRACK_ROW_INTERVAL_S", "1.0"))
RECORDER_SWEEP_PERIOD_S = float(os.getenv("RECORDER_SWEEP_PERIOD_S", "1.0"))
# How often dispatch upserts its recorder_health row (well inside RECORDER_HEALTH_STALE_S).
RECORDER_HEALTH_REPORT_PERIOD_S = float(os.getenv("RECORDER_HEALTH_REPORT_PERIOD_S", "10.0"))
RECORDER_HEALTH_CONNECT_TIMEOUT_S = float(os.getenv("RECORDER_HEALTH_CONNECT_TIMEOUT_S", "2.0"))
RECORDER_HEALTH_WRITE_TIMEOUT_S = float(os.getenv("RECORDER_HEALTH_WRITE_TIMEOUT_S", "5.0"))
# The recording policy is also reloaded this often as a safety net for a missed NOTIFY.
RECORDER_POLICY_REFRESH_MAX_S = float(os.getenv("RECORDER_POLICY_REFRESH_MAX_S", "60.0"))
RECORDER_REHYDRATE_TIMEOUT_S = float(os.getenv("RECORDER_REHYDRATE_TIMEOUT_S", "10.0"))
RECORDER_REHYDRATE_RETRY_S = float(os.getenv("RECORDER_REHYDRATE_RETRY_S", "10.0"))
RECORDER_REHYDRATE_ATTEMPTS = int(os.getenv("RECORDER_REHYDRATE_ATTEMPTS", "30"))
RECORDER_OP_CONNECT_TIMEOUT_S = float(os.getenv("RECORDER_OP_CONNECT_TIMEOUT_S", "5.0"))
RECORDER_OP_RETRY_DELAYS_S = _env_floats("RECORDER_OP_RETRY_DELAYS_S", "1.0,2.0,5.0,10.0,30.0")
RECORDER_MAX_PENDING_OPS = int(os.getenv("RECORDER_MAX_PENDING_OPS", "1000"))
# Orphaned RUNNING runs are settled this often; runs younger than MIN_AGE_S are left alone.
RECORDER_RECONCILE_PERIOD_S = float(os.getenv("RECORDER_RECONCILE_PERIOD_S", "600.0"))
RECORDER_RECONCILE_MIN_AGE_S = float(os.getenv("RECORDER_RECONCILE_MIN_AGE_S", "60.0"))
# Trajectory rows logged this long after the run ended still belong to it.
RECORDER_TRAJECTORY_GRACE_S = int(os.getenv("RECORDER_TRAJECTORY_GRACE_S", "5"))

# Per-edge traversal statistics of the topomap (packages/utils/edge_stats.py): the newest
# EDGE_STATS_WINDOW observations of each (map, edge, robot) are kept and summarised. An observation
# with a duration over EDGE_STATS_MAX_DURATION_S (a paused session, a stuck robot) or a speed over
# EDGE_STATS_MAX_SPEED_MPS (bursty MQTT delivery, a wrong clock) is dropped, as is a duration <= 0.
# A MAPPING step (two consecutive session nodes) slower than EDGE_STATS_MIN_SPEED_MPS is dropped
# too: the session was paused or the robot stood still, so its time is no traversal time (legs are
# not filtered by a minimum speed).
EDGE_STATS_WINDOW = int(os.getenv("EDGE_STATS_WINDOW", "20"))

# GET /api/v1/maps/{id}/drive-stats (docs/drive_stats_2026-10-14.md): per-tile / per-region driving
# statistics from the recorded map-frame track. A consecutive pair of track rows further apart than
# DRIVE_STATS_MAX_GAP_S, faster than DRIVE_STATS_MAX_SPEED_MS (a pose jump) or not moving forward in
# time is not a movement; a pair slower than min_speed (default DRIVE_STATS_MIN_SPEED_MS) is "stopped".
# At most DRIVE_STATS_MAX_ROWS track rows are read (the newest; the answer says `partial`).
DRIVE_STATS_MAX_ROWS = int(os.getenv("DRIVE_STATS_MAX_ROWS", "2000000"))
DRIVE_STATS_MAX_GAP_S = float(os.getenv("DRIVE_STATS_MAX_GAP_S", "5.0"))
DRIVE_STATS_MAX_SPEED_MS = float(os.getenv("DRIVE_STATS_MAX_SPEED_MS", "2.0"))
DRIVE_STATS_MIN_SPEED_MS = float(os.getenv("DRIVE_STATS_MIN_SPEED_MS", "0.05"))
DRIVE_STATS_LEG_MAX_M = float(os.getenv("DRIVE_STATS_LEG_MAX_M", "10.0"))
DRIVE_STATS_CACHE_TTL_S = float(os.getenv("DRIVE_STATS_CACHE_TTL_S", "120.0"))
DRIVE_STATS_CACHE_ENTRIES = int(os.getenv("DRIVE_STATS_CACHE_ENTRIES", "16"))
# GET /api/v1/maps/{id}/mapping-stats (docs/mapping_stats_2026-10-14.md): time between two consecutive
# nodes of a mapping session longer than MAPPING_STATS_MAX_GAP_S (after the paused seconds are taken out)
# is not attributed to space (the robot stood, the uploads stalled); it still counts in the active time.
MAPPING_STATS_MAX_GAP_S = float(os.getenv("MAPPING_STATS_MAX_GAP_S", "60.0"))
EDGE_STATS_MAX_DURATION_S = float(os.getenv("EDGE_STATS_MAX_DURATION_S", "3600.0"))
EDGE_STATS_MAX_SPEED_MPS = float(os.getenv("EDGE_STATS_MAX_SPEED_MPS", "5.0"))
EDGE_STATS_MIN_SPEED_MPS = float(os.getenv("EDGE_STATS_MIN_SPEED_MPS", "0.02"))

# ==================== Pre-mission go-to (API) ====================
# packages/api/pre_goto.py: before a MAPPED mission is queued (POST /api/v1/missions) the server
# first queues a planner go-to to the topomap node closest to the mission's first waypoint, so
# the robot does not plan one long path through the whole map. Skipped (the mission is queued
# as before) when the robot is within PRE_GOTO_MIN_DISTANCE_M of that waypoint or node, or when no
# node lies within PRE_GOTO_NODE_RADIUS_M of the waypoint.
PRE_GOTO_ENABLED = _env_bool("PRE_GOTO_ENABLED", "true")
PRE_GOTO_MIN_DISTANCE_M = float(os.getenv("PRE_GOTO_MIN_DISTANCE_M", "2.0"))
PRE_GOTO_NODE_RADIUS_M = float(os.getenv("PRE_GOTO_NODE_RADIUS_M", "3.0"))
# The whole go-to preparation (SQL, ArangoDB, planner) may take this long; then the mission is
# queued without a go-to.
PRE_GOTO_TIMEOUT_S = float(os.getenv("PRE_GOTO_TIMEOUT_S", "5.0"))

# ==================== Scene graph (feat/scene-graph-v2) ====================
# Both default off: the planner and the ingest worker only use the scene graph when enabled.
SCENE_GRAPH_PLANNER_ENABLED = _env_bool("SCENE_GRAPH_PLANNER_ENABLED", "false")
SCENE_GRAPH_WORKER_ENABLED = _env_bool("SCENE_GRAPH_WORKER_ENABLED", "false")
# How far the planner may move a point onto navigable space: the robot's start, a tapped goal.
SCENE_GRAPH_START_SNAP_M = float(os.getenv("SCENE_GRAPH_START_SNAP_M", "0.5"))
SCENE_GRAPH_GOAL_SNAP_M = float(os.getenv("SCENE_GRAPH_GOAL_SNAP_M", "1.5"))
# A waypoint takes a leaf id only when a leaf lies within this distance of it.
SCENE_GRAPH_LEAF_SNAP_M = float(os.getenv("SCENE_GRAPH_LEAF_SNAP_M", "1.0"))
# Largest gap between two waypoints of a scene-graph route.
SCENE_GRAPH_MAX_WAYPOINT_GAP_M = float(os.getenv("SCENE_GRAPH_MAX_WAYPOINT_GAP_M", "5.0"))

# Planner budget (the dispatcher replans with a 5 s timeout, the legacy planner needs the rest):
# TOTAL_BUDGET_S is ONE monotonic deadline per request for every chart-route stage (robot, store,
# graph load, lag, blocked rows, plan); each wait is min(its own timeout, what is left), and with
# under ~0.3 s left the request falls back at once (`budget_exhausted`). LOAD_TIMEOUT_S: a cold
# graph load is not waited for longer (it goes on in the background). PLAN_BUDGET_S: snap + route +
# simplify + leaf lookup. PLAN_MAX_INFLIGHT: plans running or hung in the plan thread pool (2
# threads) before a request falls back `planner_busy`.
SCENE_GRAPH_LOAD_TIMEOUT_S = float(os.getenv("SCENE_GRAPH_LOAD_TIMEOUT_S", "2.0"))
SCENE_GRAPH_PLAN_BUDGET_S = float(os.getenv("SCENE_GRAPH_PLAN_BUDGET_S", "3.0"))
SCENE_GRAPH_TOTAL_BUDGET_S = float(os.getenv("SCENE_GRAPH_TOTAL_BUDGET_S", "3.5"))
SCENE_GRAPH_PLAN_MAX_INFLIGHT = int(os.getenv("SCENE_GRAPH_PLAN_MAX_INFLIGHT", "4"))
# Loaded graphs kept in memory: CACHE_VERSIONS in total (an LRU over maps), at most
# CACHE_VERSIONS_PER_MAP of one map (the served and the one being refreshed), CACHE_MAX_BYTES their
# summed `nbytes` when the graph reports it (0 = no byte budget; the newest graph is always kept).
SCENE_GRAPH_CACHE_VERSIONS = int(os.getenv("SCENE_GRAPH_CACHE_VERSIONS", "4"))
SCENE_GRAPH_CACHE_VERSIONS_PER_MAP = int(os.getenv("SCENE_GRAPH_CACHE_VERSIONS_PER_MAP", "2"))
SCENE_GRAPH_CACHE_MAX_BYTES = int(os.getenv("SCENE_GRAPH_CACHE_MAX_BYTES", "0"))
# Planner graph: the tile-lazy PlanGraph only (docs/scene_graph/planner_upgrade.md, Phase 0): it opens the
# manifest and reads the owner tiles of each request's corridor. A route whose corridor needs more than
# LAZY_MAX_TILES tiles falls back `corridor_too_large`; LAZY_TILE_CACHE is the number of owner tiles
# (about 35 KB each) kept per graph. The former `full_load` mode (whole engine per version) was removed
# 2026-10-13; SCENE_GRAPH_PLANNER_MODE is ignored, with a warning when it names anything but `lazy`.
if os.getenv("SCENE_GRAPH_PLANNER_MODE", "lazy").strip().lower() != "lazy":
    logging.getLogger(__name__).warning(
        "SCENE_GRAPH_PLANNER_MODE=%r ignored: the planner is always 'lazy' (full_load was removed)",
        os.getenv("SCENE_GRAPH_PLANNER_MODE"))
SCENE_GRAPH_LAZY_MAX_TILES = int(os.getenv("SCENE_GRAPH_LAZY_MAX_TILES", "400"))
SCENE_GRAPH_LAZY_TILE_CACHE = int(os.getenv("SCENE_GRAPH_LAZY_TILE_CACHE", "512"))
# A store.meta answer is reused this long per map and shared by concurrent requests (one query).
SCENE_GRAPH_META_TTL_S = float(os.getenv("SCENE_GRAPH_META_TTL_S", "0.75"))
# A chart route needs a graph at least as new as the ingest: when the oldest costmap stored after the
# version's cursor has waited longer than this, the planner falls back (`stale_graph`); the same
# bound applies to how long a newer version may have been known (in the store's meta) while an
# older loaded one is served, whatever the cursor says. 0 disables both.
SCENE_GRAPH_MAX_VERSION_LAG_S = float(os.getenv("SCENE_GRAPH_MAX_VERSION_LAG_S", "120.0"))
# A route whose narrowest passage (engine Route.min_width, lattice error +-0.05 m) is under the
# robot's footprint plus this margin is `too_narrow`: need = width + margin when the route needs no
# turn in place, hypot(length, width) + margin when it does. The margin is the lattice error:
# lattice / 2 = 0.05 m for the default 0.1 m lattice.
SCENE_GRAPH_WIDTH_MARGIN_M = float(os.getenv("SCENE_GRAPH_WIDTH_MARGIN_M", "0.05"))
# Gate of the `too_narrow` comparison above. OFF by default until it is validated on real maps.
SCENE_GRAPH_WIDTH_CHECK_ENABLED = _env_bool("SCENE_GRAPH_WIDTH_CHECK_ENABLED", "false")
# nodePolicy skip data (docs/node_policies_2026-10-13.md). SKIP_ENABLED: the planner marks go-here
# waypoints skippable (with a corridor width from the graph) and mission-dispatch sends skippable
# (it reads the same env name in order_policy.py). GAP_FILL_ENABLED: the planner inserts planned
# waypoints between an operator's waypoints that are not in the same or a neighbouring chart.
# WIDTH_BUDGET_S: time the width queries of one request may take (a leg left over is not skippable);
# ENRICH_TIMEOUT_S: how long mission-dispatch waits for the route-enrich call before starting
# the mission with the route as it is.
NODE_POLICY_SKIP_ENABLED = _env_bool("NODE_POLICY_SKIP_ENABLED", "true")
NODE_POLICY_GAP_FILL_ENABLED = _env_bool("NODE_POLICY_GAP_FILL_ENABLED", "true")
NODE_POLICY_WIDTH_BUDGET_S = float(os.getenv("NODE_POLICY_WIDTH_BUDGET_S", "1.0"))
NODE_POLICY_ENRICH_TIMEOUT_S = float(os.getenv("NODE_POLICY_ENRICH_TIMEOUT_S", "5.0"))
# Most waypoints one POST /api/v1/route/preview accepts (docs/route_preview_2026-10-14.md); over it: 413.
ROUTE_PREVIEW_MAX_WAYPOINTS = int(os.getenv("ROUTE_PREVIEW_MAX_WAYPOINTS", "100"))
# The planner's dense lattice path per leg between the waypoints (docs/plan_trajectory_2026-10-14.md), returned with a plan
# and stored on the mission's route for the client to draw. MAX_POINTS: cap on the points of one route's trajectory (after
# straight runs are collapsed); over it the trajectory is omitted, never truncated. Display data only, never sent to the robot.
PLAN_TRAJECTORY_ENABLED = _env_bool("PLAN_TRAJECTORY_ENABLED", "true")
PLAN_TRAJECTORY_MAX_POINTS = int(os.getenv("PLAN_TRAJECTORY_MAX_POINTS", "3000"))
# Clearance pricing of the scene-graph route (docs/scene_graph/route_safety.md). KAPPA is the cost multiplier added at the
# inscribed floor (a cell whose clearance is the map's robot half side costs 1 + KAPPA times its length; cells at or above
# the planning robot's circumscribed radius cost their length); 0 disables the pricing (the shortest route). Out of range:
# reset to the default with one warning. The range [0, 10] is the measured plateau (docs/scene_graph/route_safety.md S13),
# not a bound of the theory. The planner prices on the tile-lazy graph (lazy_plan.PlanGraph) with the clearance read from
# the stored v planes; pricing is off when the margin exceeds 1 m (the clearance cap) or is within the lattice quantisation of r.
# Empty / unset = the default 1.0, and "not explicit": the exponential tier below then uses 0.5 (docs/scene_graph/inflation_profile_design.md s5).
SCENE_GRAPH_SAFETY_KAPPA_EXPLICIT = bool(os.getenv("SCENE_GRAPH_SAFETY_KAPPA", "").strip())
SCENE_GRAPH_SAFETY_KAPPA = float(os.getenv("SCENE_GRAPH_SAFETY_KAPPA", "").strip() or "1.0")


def _opt_float(name: str) -> Optional[float]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        logging.getLogger("config").warning("%s=%r is not a number; ignored", name, raw)
        return None


# Robot-inflation price profile (EXPERIMENTAL, docs/scene_graph/inflation_profile_design.md): the exponential tier of the
# clearance pricing, price(g) = exp(-SCALING (g - ORIGIN)) for g < RADIUS, else 0, edge multiplier 1 + kappa * mean price. Used
# only when RADIUS and SCALING are both set and valid (RADIUS in (0, 1.0] m, the lazy reader's clearance cap; SCALING > 0; ORIGIN
# > 0); otherwise (empty = unset) the linear hinge above is used. ORIGIN default: the footprint's inscribed radius, else the map's r.
SCENE_GRAPH_INFLATION_RADIUS_M = _opt_float("SCENE_GRAPH_INFLATION_RADIUS_M")
SCENE_GRAPH_INFLATION_SCALING = _opt_float("SCENE_GRAPH_INFLATION_SCALING")
SCENE_GRAPH_GLOBAL_INSCRIBED_M = _opt_float("SCENE_GRAPH_GLOBAL_INSCRIBED_M")
# Close evidence (docs/scene_graph/close_evidence_and_turn_cost_design.md part A): a cell the costmap marked lethal stays blocked
# until a free report from a capture pose within NEAR_RANGE_M of it (the robot's support.commit_radius_m, 1.5 m, is the reference).
# Empty = off (the latest report wins). Read by the graph-builder when it makes an engine; changing it is a new generation of every
# map (the params hash changes). NEAR_FOV_DEG (optional): a free report counts only inside this horizontal cone of the base yaw.
SCENE_GRAPH_NEAR_RANGE_M = _opt_float("SCENE_GRAPH_NEAR_RANGE_M")
SCENE_GRAPH_NEAR_FOV_DEG = _opt_float("SCENE_GRAPH_NEAR_FOV_DEG")
# NEAR_MIN_CLEARANCE_M (optional, needs NEAR_RANGE_M): "away from walls" - a held region stays held only when a cell of its connected
# component is farther than this (m) from every other blocked or unknown cell; wall rims are released. Empty = every held cell holds.
SCENE_GRAPH_NEAR_MIN_CLEARANCE_M = _opt_float("SCENE_GRAPH_NEAR_MIN_CLEARANCE_M")
# Turn cost of the scene-graph route (docs/scene_graph/close_evidence_and_turn_cost_design.md B.1, B.6): every change of heading of
# the lattice route costs lambda * |dtheta| (lambda in m per radian) on top of the priced length. lambda is per robot, from its
# factsheet: speed_max / angular_speed_max, clamped to [0, 5]. LAMBDA_M_PER_RAD (empty = unset) is used only for a robot whose
# factsheet lacks one of the two; neither: no turn cost for that plan. ENABLED is the switch: off (the default), no plan has a turn cost.
SCENE_GRAPH_TURN_COST_ENABLED = _env_bool("SCENE_GRAPH_TURN_COST_ENABLED", "false")
SCENE_GRAPH_TURN_LAMBDA_M_PER_RAD = _opt_float("SCENE_GRAPH_TURN_LAMBDA_M_PER_RAD")
# Waypoint budget (docs/scene_graph/close_evidence_and_turn_cost_design.md C.2): the planner's string pull is replaced by the exact
# fewest-waypoints path whose total priced-cost excess over the lattice path is at most this many metres (final cost <= lattice +
# budget). Empty / <= 0 = off (the greedy pull, unchanged); validated below: finite and at most 1.0, else off.
SCENE_GRAPH_WAYPOINT_BUDGET_M = _opt_float("SCENE_GRAPH_WAYPOINT_BUDGET_M")
# The commit the running image was built from (compose BUILD_SHA <- scripts/deploy.sh GIT_SHA); "unknown" otherwise.
BUILD_SHA = os.getenv("BUILD_SHA", "unknown").strip() or "unknown"
# Pre-warm at planner startup (only with SCENE_GRAPH_PLANNER_ENABLED): load the graph of these maps
# (comma-separated) into the cache in the background; empty = every map the graph database lists
# that has a stored scene graph. CONCURRENCY loads at a time.
SCENE_GRAPH_PREWARM_MAPS = tuple(m.strip() for m in os.getenv("SCENE_GRAPH_PREWARM_MAPS", "").split(",")
                                 if m.strip())
SCENE_GRAPH_PREWARM_CONCURRENCY = int(os.getenv("SCENE_GRAPH_PREWARM_CONCURRENCY", "2"))


def _validate_scene_graph_planner() -> None:
    """Clamp planner settings that would break it (a zero timeout, a negative size) to their
    defaults and say so once; the planner is a best-effort path, so a bad value is repaired, not fatal."""
    fixed = []
    for name, lo, default in (
            ("SCENE_GRAPH_LOAD_TIMEOUT_S", 0.0, 2.0), ("SCENE_GRAPH_PLAN_BUDGET_S", 0.0, 3.0),
            ("SCENE_GRAPH_TOTAL_BUDGET_S", 0.0, 3.5), ("SCENE_GRAPH_START_SNAP_M", 0.0, 0.5),
            ("SCENE_GRAPH_GOAL_SNAP_M", 0.0, 1.5), ("SCENE_GRAPH_LEAF_SNAP_M", 0.0, 1.0)):
        value = globals()[name]
        if not (value > lo and value < float("inf")):            # also NaN
            fixed.append(f"{name}={value} -> {default}")
            globals()[name] = default
    for name, default, lo in (
            ("SCENE_GRAPH_CACHE_VERSIONS", 4, 1), ("SCENE_GRAPH_CACHE_VERSIONS_PER_MAP", 2, 1),
            ("SCENE_GRAPH_PLAN_MAX_INFLIGHT", 4, 1), ("SCENE_GRAPH_PREWARM_CONCURRENCY", 2, 1),
            ("SCENE_GRAPH_CACHE_MAX_BYTES", 0, 0)):
        value = globals()[name]
        if value < lo:
            fixed.append(f"{name}={value} -> {default}")
            globals()[name] = default
    for name, default in (("SCENE_GRAPH_MAX_VERSION_LAG_S", 120.0), ("SCENE_GRAPH_META_TTL_S", 0.75),
                          ("SCENE_GRAPH_WIDTH_MARGIN_M", 0.05), ("SCENE_GRAPH_MAX_WAYPOINT_GAP_M", 5.0)):
        value = globals()[name]
        if not (value >= 0.0 and value < float("inf")):
            fixed.append(f"{name}={value} -> {default}")
            globals()[name] = default
    value = SCENE_GRAPH_SAFETY_KAPPA
    if not (0.0 <= value <= 10.0):                               # also NaN
        fixed.append(f"SCENE_GRAPH_SAFETY_KAPPA={value} -> 1.0")
        globals()["SCENE_GRAPH_SAFETY_KAPPA"] = 1.0
    value = SCENE_GRAPH_WAYPOINT_BUDGET_M
    if value is not None and not (0.0 < value <= 1.0):          # also NaN; <= 0 is simply off
        if value > 0.0 or value != value:
            fixed.append(f"SCENE_GRAPH_WAYPOINT_BUDGET_M={value} -> off")
        globals()["SCENE_GRAPH_WAYPOINT_BUDGET_M"] = None
    if fixed:
        logging.getLogger("config").warning("invalid scene graph planner settings reset: %s",
                                            "; ".join(fixed))


_validate_scene_graph_planner()

# Scene-graph ingest worker batching (measured: a whole-batch fold costs seconds, and a larger
# batch amortises a fixed solve, so the worker folds in batches, never leaf by leaf).
# A batch is folded once at least BATCH_MIN_LEAVES are waiting, or when the oldest waiting leaf has
# waited PUBLISH_INTERVAL_S (a trickle). A backlog is folded BATCH_MAX_LEAVES at a time.
SCENE_GRAPH_BATCH_MIN_LEAVES = int(os.getenv("SCENE_GRAPH_BATCH_MIN_LEAVES", "6"))
SCENE_GRAPH_BATCH_MAX_LEAVES = int(os.getenv("SCENE_GRAPH_BATCH_MAX_LEAVES", "48"))
# Floor between two snapshot saves (publishes) of one map, and the longest a leaf waits for a batch.
SCENE_GRAPH_PUBLISH_INTERVAL_S = float(os.getenv("SCENE_GRAPH_PUBLISH_INTERVAL_S", "5.0"))
# A fold of one batch that takes longer than this is logged as a warning (it is not aborted).
SCENE_GRAPH_BATCH_BUDGET_S = float(os.getenv("SCENE_GRAPH_BATCH_BUDGET_S", "10.0"))
# Idle polling of a map with nothing new: first visit after IDLE_S, doubling up to IDLE_MAX_S while
# nothing changes (a new upload resets it, so a quiet map is seen up to IDLE_MAX_S late). The
# lifecycle / lock of a map is looked up (Postgres) at most every LIFECYCLE_CHECK_S, and always
# right before a save.
SCENE_GRAPH_IDLE_S = float(os.getenv("SCENE_GRAPH_IDLE_S", "2.0"))
SCENE_GRAPH_IDLE_MAX_S = float(os.getenv("SCENE_GRAPH_IDLE_MAX_S", "30.0"))
SCENE_GRAPH_LIFECYCLE_CHECK_S = float(os.getenv("SCENE_GRAPH_LIFECYCLE_CHECK_S", "5.0"))
# Worker robustness knobs (docs/scene_graph/runbook.md). MAX_MAPS: workers kept in memory at once.
# SAVE_EVERY_*: a snapshot is saved after this many folded leaves / this many seconds (still never
# faster than PUBLISH_INTERVAL_S). REPLAY_MARGIN_S: how far below the furthest node a restart
# re-reads (late visible costmaps); the whole margin is re-read again every REPLAY_RECHECK_S.
SCENE_GRAPH_MAX_MAPS = int(os.getenv("SCENE_GRAPH_MAX_MAPS", "64"))
SCENE_GRAPH_SAVE_EVERY_LEAVES = int(os.getenv("SCENE_GRAPH_SAVE_EVERY_LEAVES", "50"))
SCENE_GRAPH_SAVE_EVERY_S = float(os.getenv("SCENE_GRAPH_SAVE_EVERY_S", "30.0"))
SCENE_GRAPH_REPLAY_MARGIN_S = float(os.getenv("SCENE_GRAPH_REPLAY_MARGIN_S", "60.0"))
# `costmap_stored_ms` (DB clock of the layer write) is the receipt of a leaf's costmap only when it
# lies within this many seconds of the node's own time (its `timestamp`, else its capture_time).
# A layer written long after capture (backfilled occupancy_depth) is a bulk write, not a receipt:
# the node's own time is used, so the per-session clock estimate does not degenerate to the write
# time. <= 0 always trusts costmap_stored_ms.
SCENE_GRAPH_RECEIPT_WINDOW_S = float(os.getenv("SCENE_GRAPH_RECEIPT_WINDOW_S", "300.0"))
# Restart replay: skip leaves the stored state already holds (same key, stamp, pose, footprint, view)
# instead of folding the replay margin again (no re-solve, no new version). false = fold them again.
SCENE_GRAPH_SKIP_FOLDED = _env_bool("SCENE_GRAPH_SKIP_FOLDED", "true")
SCENE_GRAPH_REPLAY_RECHECK_S = float(os.getenv("SCENE_GRAPH_REPLAY_RECHECK_S", "60.0"))
# Rebuilds: a rebuild is committed at its next save once older than REBUILD_MAX_AGE_S even if uploads
# keep arriving; folds inside a rebuild are fixed batches of REBUILD_BATCH leaves; a node delete
# starts a rebuild only after REBUILD_DEBOUNCE_S without a newer delete.
SCENE_GRAPH_REBUILD_MAX_AGE_S = float(os.getenv("SCENE_GRAPH_REBUILD_MAX_AGE_S", "900.0"))
SCENE_GRAPH_REBUILD_BATCH = int(os.getenv("SCENE_GRAPH_REBUILD_BATCH", "48"))
SCENE_GRAPH_REBUILD_DEBOUNCE_S = float(os.getenv("SCENE_GRAPH_REBUILD_DEBOUNCE_S", "30.0"))
# Engine load that fails deterministically this many times in a row falls into a rebuild.
SCENE_GRAPH_LOAD_FAIL_LIMIT = int(os.getenv("SCENE_GRAPH_LOAD_FAIL_LIMIT", "3"))
# One worker step stops reading nodes after this long (0: unbounded); a step slower than
# SLOW_STEP_S is logged (0: off); STATS_LOG_S: one stats line per map this often (0: off).
SCENE_GRAPH_STEP_BUDGET_S = float(os.getenv("SCENE_GRAPH_STEP_BUDGET_S", "0.0"))
SCENE_GRAPH_SLOW_STEP_S = float(os.getenv("SCENE_GRAPH_SLOW_STEP_S", "30.0"))
SCENE_GRAPH_STATS_LOG_S = float(os.getenv("SCENE_GRAPH_STATS_LOG_S", "60.0"))

# Scale of the ingest worker (one process, up to MAX_MAPS maps, small and very large).
# WORKER_THREADS: size of the dedicated step pool (one step per map at a time, earliest-due map first;
# a cold engine load never takes the last thread). MAX_RESIDENT_ENGINES / MAX_RESIDENT_BYTES: at most
# this many engines (this many bytes, `ChartGraph.nbytes`; 0 = off) stay in memory; the least recently
# active IDLE engine (nothing unsaved / buffered, idle for EVICT_IDLE_S) is saved and dropped, and reloaded
# on the map's next leaves. LAZY_LOAD: a map's engine is loaded by its first fold, not at worker start.
# PREFETCH_THREADS / PREFETCH_QUEUE: PNG fetch + decode pool of the leaf pipeline (0 threads = off) and the
# most prefetched PNGs held at once. MEMORY_RETRY_LIMIT: consecutive MemoryErrors of one batch before it is
# bisected and the leaf that still fails alone is quarantined.
SCENE_GRAPH_WORKER_THREADS = int(os.getenv("SCENE_GRAPH_WORKER_THREADS", str(min(8, os.cpu_count() or 2))))
SCENE_GRAPH_MAX_RESIDENT_ENGINES = int(os.getenv("SCENE_GRAPH_MAX_RESIDENT_ENGINES", "32"))
SCENE_GRAPH_MAX_RESIDENT_BYTES = int(os.getenv("SCENE_GRAPH_MAX_RESIDENT_BYTES", "0"))
SCENE_GRAPH_EVICT_IDLE_S = float(os.getenv("SCENE_GRAPH_EVICT_IDLE_S", "60.0"))
SCENE_GRAPH_LAZY_LOAD = _env_bool("SCENE_GRAPH_LAZY_LOAD", "true")
SCENE_GRAPH_PREFETCH_THREADS = int(os.getenv("SCENE_GRAPH_PREFETCH_THREADS", "4"))
SCENE_GRAPH_PREFETCH_QUEUE = int(os.getenv("SCENE_GRAPH_PREFETCH_QUEUE", "64"))
SCENE_GRAPH_MEMORY_RETRY_LIMIT = int(os.getenv("SCENE_GRAPH_MEMORY_RETRY_LIMIT", "3"))


def _validate_scene_graph_worker_scale() -> None:
    """Reset worker scale settings that would break it (a zero-thread pool, a negative cap) to their
    defaults and say so once, the same way as the planner settings."""
    fixed = []
    for name, default, lo in (
            ("SCENE_GRAPH_WORKER_THREADS", min(8, os.cpu_count() or 2), 1),
            ("SCENE_GRAPH_PREFETCH_QUEUE", 64, 1), ("SCENE_GRAPH_MEMORY_RETRY_LIMIT", 3, 1),
            ("SCENE_GRAPH_MAX_RESIDENT_ENGINES", 32, 0), ("SCENE_GRAPH_MAX_RESIDENT_BYTES", 0, 0),
            ("SCENE_GRAPH_PREFETCH_THREADS", 4, 0)):
        value = globals()[name]
        if value < lo:
            fixed.append(f"{name}={value} -> {default}")
            globals()[name] = default
    value = globals()["SCENE_GRAPH_EVICT_IDLE_S"]
    if not (value >= 0.0 and value < float("inf")):             # also NaN
        fixed.append(f"SCENE_GRAPH_EVICT_IDLE_S={value} -> 60.0")
        globals()["SCENE_GRAPH_EVICT_IDLE_S"] = 60.0
    value = globals()["SCENE_GRAPH_RECEIPT_WINDOW_S"]
    if not (value == value and abs(value) < float("inf")):      # NaN / inf: the documented default
        fixed.append(f"SCENE_GRAPH_RECEIPT_WINDOW_S={value} -> 300.0")
        globals()["SCENE_GRAPH_RECEIPT_WINDOW_S"] = 300.0
    if fixed:
        logging.getLogger("config").warning("invalid scene graph worker settings reset: %s",
                                            "; ".join(fixed))


_validate_scene_graph_worker_scale()
