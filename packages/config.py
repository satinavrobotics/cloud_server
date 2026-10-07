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
PORT_AGENT_ORCHESTRATOR = 8007
PORT_API_DELEGATION = 8000

# ==================== Infrastructure Ports ====================
PORT_ARANGODB = 8529
PORT_MINIO = 9000
PORT_POSTGRES = 5432
PORT_MQTT = 1883

# ==================== Service URLs ====================
# Default URLs for service-to-service communication
URL_GRAPH_BUILDER = f"http://localhost:{PORT_GRAPH_BUILDER}"
URL_MISSION_PLANNER = os.getenv("MISSION_PLANNER_URL", f"http://localhost:{PORT_MISSION_PLANNER}")
URL_LIVEKIT = f"http://localhost:{PORT_LIVEKIT}"
URL_AGENT_ORCHESTRATOR = f"http://localhost:{PORT_AGENT_ORCHESTRATOR}"
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
# 3D reconstruction R2 (docs/reconstruction/design.md §5): one u16-mm depth PNG + camera
# parameters per node and camera (graph-builder).
MQTT_DEPTH_TOPIC = os.getenv("MQTT_DEPTH_TOPIC", "robot/depth_upload")
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
# A synchronous POST /maps/{name}/save (the orchestrator proxy) waits for the driver's save_map:
# the orchestrator gives up after 600 s (mapping.save_timeout_sec), so a little longer.
ORCHESTRATOR_SAVE_TIMEOUT_S = float(os.getenv("ORCHESTRATOR_SAVE_TIMEOUT_S", "660.0"))
# A background save (POST .../save?background=true) is polled (GET /maps/mapping/save) every
# SAVE_POLL_S for at most SAVE_POLL_TOTAL_S (600 s timeout plus margin) per attempt. After a save
# that did not finish in time the driver is never stopped: the save is retried every
# SAVE_RETRY_S while the orchestrator reports late_save_sec > 0.
ORCHESTRATOR_SAVE_POLL_S = float(os.getenv("ORCHESTRATOR_SAVE_POLL_S", "3.0"))
ORCHESTRATOR_SAVE_POLL_TOTAL_S = float(os.getenv("ORCHESTRATOR_SAVE_POLL_TOTAL_S", "660.0"))
ORCHESTRATOR_SAVE_RETRY_S = float(os.getenv("ORCHESTRATOR_SAVE_RETRY_S", "30.0"))
# How long a fetched mapping service state is reused for the robot views (seconds).
MAPPING_STATE_TTL_S = float(os.getenv("MAPPING_STATE_TTL_S", "5.0"))
# How long "does this robot's orchestrator hold a stored map for cloud map X" is reused
# (packages/api/orchestrator_maps.py; relocalization, docs/satinav-maps-redesign.md ## 16).
RELOC_MAP_HELD_TTL_S = float(os.getenv("RELOC_MAP_HELD_TTL_S", "15.0"))
# A placed `reloc` session is flagged (robot view `localization_warning`) when the robot's
# localizationScore is below this. A warning only: it never refuses a placement.
RELOC_DEGRADED_SCORE = float(os.getenv("RELOC_DEGRADED_SCORE", "0.3"))
# Starting relocalization from the API (packages/api/reloc_job.py, docs/satinav-maps-redesign.md
# ## 16): the orchestrator service that relocalizes the robot on its stored map (first candidate
# the robot's orchestrator lists is used; comma-separated). `RELOC_JOB_TIMEOUT_S`: how long a job
# waits for the robot to report `position_initialized`; `RELOC_JOB_POLL_S`: how often the stored
# robot status is read; `RELOC_JOB_SETTLE_S`: when the robot already reported
# `position_initialized: true` BEFORE the restart (a stale value), the job waits this long after
# the restart before it believes a true again (unless it saw the flag drop first).
RELOC_SERVICE_CANDIDATES = _candidates("RELOC_SERVICE_CANDIDATES", "odin_reloc")
RELOC_JOB_TIMEOUT_S = float(os.getenv("RELOC_JOB_TIMEOUT_S", "90.0"))
RELOC_JOB_POLL_S = float(os.getenv("RELOC_JOB_POLL_S", "1.0"))
RELOC_JOB_SETTLE_S = float(os.getenv("RELOC_JOB_SETTLE_S", "5.0"))
# After the robot reports itself localized the job proposes the placement and waits this long for
# the user to confirm or edit it; then the server confirms by itself (RELOC_CONFIRM_TIMEOUT_S).
RELOC_CONFIRM_TIMEOUT_S = float(os.getenv("RELOC_CONFIRM_TIMEOUT_S", "30.0"))
# Relocalization normally uses the orchestrator's POST /maps/{name}/relocalize when it offers it
# (GET /maps/mapping reports `mode`/`relocalizing`). Set true to always use the reloc SERVICE
# (RELOC_SERVICE_CANDIDATES) instead, e.g. in a simulation whose relocalize endpoint is a stub.
RELOC_FORCE_SERVICE = os.getenv("RELOC_FORCE_SERVICE", "false").lower() in ("1", "true", "yes")

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
# mission-dispatch reports every fleet_recorder.HEALTH_REPORT_PERIOD_S (10 s; its image has no
# config.py).
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
FLEET_TIMELINE_MAX_EVENTS = int(os.getenv("FLEET_TIMELINE_MAX_EVENTS", "5000"))

# ==================== Phase 0 fixes (API, WP11) ====================
# F1 map delete (packages/api/map_delete.py): cleanup attempts per round before
# MAP.DELETE_FAILED, and the backoff between them (base * 2^(n-1), capped), in seconds.
# A map that exhausts its round stays DELETING until the next API start or another DELETE.
MAP_DELETE_MAX_ATTEMPTS = int(os.getenv("MAP_DELETE_MAX_ATTEMPTS", "5"))
MAP_DELETE_BACKOFF_S = float(os.getenv("MAP_DELETE_BACKOFF_S", "2.0"))
MAP_DELETE_BACKOFF_MAX_S = float(os.getenv("MAP_DELETE_BACKOFF_MAX_S", "60.0"))
# Seconds between SLAM reconcile passes (lost saves, drivers of deleted maps); 0 = startup only.
SLAM_RECONCILE_INTERVAL_S = float(os.getenv("SLAM_RECONCILE_INTERVAL_S", "300"))
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
# Battery state-of-charge (%) at or below which a low-battery event fires.
AGENT_BATTERY_LOW_THRESHOLD = float(os.getenv("AGENT_BATTERY_LOW_THRESHOLD", "20.0"))

# Validate required credentials at import time
for _required_secret in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    if not os.getenv(_required_secret):
        raise EnvironmentError(
            f"Required environment variable {_required_secret} is not set. "
            "Check your .env file or deployment secrets."
        )

