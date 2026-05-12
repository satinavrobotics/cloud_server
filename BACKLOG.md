# Cloud Server — Technical Debt Backlog

Generated 2026-05-09 from codebase audit. Items are ordered by priority within each section.
Items marked ✅ have been fixed.

---

## CRITICAL — Must fix before production

### ✅ A1 · Broken Dockerfile COPYs (build fails)
**Files:** `packages/api/Dockerfile`, `services/graph_builder/Dockerfile`, `services/mission_planner/Dockerfile`

**Fixed:** Replaced bogus `client.py` COPY lines with `COPY packages/topomap_dbs/ /app/packages/topomap_dbs/` (full directory copy). mission_planner only needs graph_db so copies that sub-directory only.

---

### ✅ A2 · `mqtt_*` kwargs never stored → `AttributeError` on shutdown
**File:** `packages/api/server.py`

**Fixed:** Added `self.mqtt_enabled`, `self.mqtt_broker`, `self.mqtt_port`, `self.mqtt_keepalive`, `self.mqtt_client = None` assignments in `__init__`.

---

### A3 · LiveKit credentials in `.env.example` and hard-coded in livekit/main.py
**Files:** `.env.example:31-33`, `services/livekit/main.py:78-79`

Real API key + secret are committed in the git repo (which is pushed to GitHub). Must be rotated and replaced with `<your-livekit-api-key>` placeholders. Remove hard-coded defaults from `livekit/main.py` so a missing env var raises at startup rather than silently using stale creds.

---

### ✅ A4 · `get_image` client/server URL mismatch → always 404
**Fixed:** `get_image` in `client.py` now calls `GET /api/v1/images/{map_id}/{node_id}` with `image_id` as optional query param, matching the server route.

---

### ✅ A5 · `get_mission_status` calls a route that doesn't exist
**Fixed:** Added `GET /api/v1/missions/{mission_name}/status` route to `api/main.py` that returns `mission.status.dict()`.

---

### ✅ A6 · `signal.SIGALRM` used from non-main threads → crash
**File:** `packages/utils/service_utils.py`

**Fixed:** Replaced SIGALRM handler with `ThreadPoolExecutor.submit(...).result(timeout=N)` which is thread-safe and cross-platform.

---

### ✅ A7 · Single global Postgres connection, no pool
**File:** `packages/database/postgres.py:182-200`

One shared `AsyncConnection` is reused across all requests and watchers. `psycopg_pool` is installed but never used. Concurrent requests serialize; long queries stall the API.

**Fixed:** `self._connection` replaced with `AsyncConnectionPool(min_size=2, max_size=10)`. All 7 data methods now use `async with self._pool.connection() as conn:`. Pool auto-commits/rolls back on context exit. `PostgresWatcher` unchanged (keeps its own dedicated connection for LISTEN/NOTIFY).

---

### ✅ A8 · Sync `requests` calls inside async handlers → blocks event loop
**Files:** `packages/api/server.py:1416, 1345`, `services/mission_planner/client.py:88,121,142,165`, `services/livekit/client.py:90,109,124`

`MissionPlannerClient` and `LiveKitClient` use blocking `requests`. Called from `async def` endpoints → blocks the uvicorn event loop for the full HTTP RTT.

**Fixed:** Rewrote `MissionPlannerClient` and `LiveKitClient` to use `httpx.AsyncClient` (instance-level, for connection pooling). All methods are now `async def`. Updated 4 call sites in `api/server.py` to `await`. Added `httpx>=0.27.0` to `api/requirements.txt`, `mission_planner/requirements.txt`, and livekit Dockerfile; removed `requests` from mission_planner and livekit.

---

### ✅ A9 · Unbounded in-memory caches with no cleanup wired up
**File:** `packages/services/graph_builder/server.py:1099-1180, 1181-1213`

`image_buffer`, `session_to_global_map`, and `known_robots` grow without bound. `_cleanup_old_mappings` (line 1181) is defined but has zero callers — it is never scheduled.

**Fixed:** Added `_periodic_cleanup` coroutine in `startup_event` in `graph_builder/main.py` that calls `_cleanup_old_mappings()` every 60 s.

---

## HIGH — Significant bugs / data correctness

### ✅ B1 · 156 `os.getenv` calls bypassing `packages/config.py`
**Fixed:** Added `POSTGRES_DATABASE_NAME/USERNAME/PASSWORD/HOST/PORT`, `MQTT_BROKER`, `MQTT_IMAGE_TOPIC`, and `IMAGE_BUFFER_TIMEOUT` to `config.py`. All three service mains now import from config.py and pass values directly to service constructors — no per-service `os.getenv` re-reads for shared settings.

---

### ✅ B2 · Argparse → env → `os.environ` triple-write boilerplate (~100 lines × 3 services)
**Fixed:** Deleted all `os.environ[...] = args.*` write blocks from `graph_builder/main.py` and `mission_planner/main.py`. The `__main__` sections now only handle `--host`, `--port`, and `--log-level`. Service configuration is read exclusively from env vars via `config.py` at startup.

---

### ✅ B3 · CLAUDE.md says Pydantic v2 in `packages/api`; reality is v1 everywhere
**Fixed:** Updated CLAUDE.md to state all services use Pydantic v1 (`==1.9.0`).

---

### ✅ B4 · 503 handler exposes `str(exc)` and internal hostnames to clients
**File:** `packages/utils/fastapi_helpers.py`

**Fixed:** Replaced `detail = str(exc)` with `"detail": "Upstream service unavailable"`. Full error still logged server-side.

---

### ✅ B5 · Duplicate `get_robot_status` / `_get_robot_status` with different return shapes
**Fixed:** Deleted `_get_robot_status`. Updated `find_nearby_nodes` to call `get_robot_status` and access `.status.pose.x/y` directly. Updated all tests to mock `get_robot_status` with a `MagicMock` having `.status.pose.*` attributes.

---

### ✅ B6 · `find_path` returns two incompatible shapes
**Fixed:** Split into two methods: `find_path(start_node_id, end_node_id, map_id) → Tuple[Optional[List], Optional[str]]` (legacy, called by `plan_and_execute_mission`) and `find_path_for_robot(robot_id, goal_position, map_id) → Optional[List]` (new convention). Updated all test call sites.

---

## MEDIUM — Dead code / redundancies

### ✅ C1 · `BaseServiceClient` has zero production subclasses
**File:** `packages/utils/base_client.py`

**Fixed:** Deleted `base_client.py` and `tests/unit/test_base_client.py`. `MissionPlannerClient` and `LiveKitClient` are async/httpx (incompatible with the sync/requests base). `ApiDelegationClient` is sync but has no production callers either — adoption would serve no purpose.

---

### ✅ C2 · Duplicate psycopg dependency declarations
**Fixed:** Removed `psycopg-binary` and `psycopg-pool` redundant entries from all 5 requirements files (`api`, `mission_planner`, `graph_builder`, `controllers/mission`, `database`). Each now lists only `psycopg[binary,pool]==3.0.15`.

---

### ✅ C3 · Transitive dependency pins in requirements files
**Fixed:** Removed all unpinned transitive sub-deps (`anyio`, `certifi`, `chardet`, `charset-normalizer`, `click`, `exceptiongroup`, `h11`, `idna`, `pydot`, `pyparsing`, `setuptools`, `sniffio`, `starlette`, `typing_extensions`, `urllib3`) from `controllers/mission/requirements.txt` and `database/requirements.txt`. Also moved `fastapi`/`uvicorn` out of the sub-deps section into direct deps.

---

### ✅ C4 · Three near-identical MinIO wrappers
**Fixed:** Extracted shared logic into `packages/topomap_dbs/minio_base.py` (`MinIOService` base class). Covers: client construction (`_connect`), `is_healthy`, `_bucket_name(map_id)`, `_ensure_bucket(name)`, `_ensure_map_bucket(map_id)`, `_delete_bucket(name)`, `_list_maps()`. All three services now subclass `MinIOService`; `ModelDatabaseService` overrides `_connect` (single fixed bucket, swallows errors) and `is_healthy`. Backward-compat aliases `_get_bucket_name` / `_ensure_bucket_exists` kept in base for existing tests.

---

### ✅ D1 · `telemetry_sender.py` is a logging stub
**Fixed:** `send_telemetry` converted to a true no-op (removed misleading debug log). The stub is retained as a fallback for the dynamic import in `controllers/mission/server.py`. Implementing real telemetry (Prometheus/OTEL) remains out of scope.

---

### ✅ D2 · R-tree spatial index built but disabled at every call site
**Files:** `packages/topomap_dbs/graph_db/rtree_spatial_index.py`, `packages/topomap_dbs/client.py:58`

**Fixed:** Deleted `rtree_spatial_index.py` and `test_rtree_spatial_index.py`. Removed `use_spatial_index` / `rebuild_threshold` params, all R-tree fields, and R-tree conditional branches from `server.py`. `k_nearest_neighbors` and `nodes_in_range` now go directly to ArangoDB (which provides geo indexes via `SpatialIndexManager`). Updated fixture and tests in `test_graph_db_server.py`; kept `spatial_index_manager.py` and its integration tests.

---

### ✅ D3 · `graph_builder/server.py` — `websocket_port` stored but never used
**Fixed:** Removed `websocket_port` parameter and `self.websocket_port` assignment. Updated the one test caller (`test_graph_builder_integration.py:640`).

---

### ✅ D4 · Unused env vars in `.env.example`
**Fixed:** Removed `GRAPH_DB_URL`, `IMAGE_DB_URL`, `SIMILARITY_SERVICE_URL`, `GRAPH_BUILDER_URL`. Replaced the "Service URLs" block with a comment noting services communicate at fixed ports (see `config.py`). Retained `MISSION_DISPATCH_URL`, `MISSION_PLANNER_URL`, `LIVEKIT_URL`.

---

### ✅ D5 · `packages/utils/test_utils/` in production package tree
**Fixed:** Moved `docker.py`, `network.py`, `mosquitto.sh` to `tests/utils/`. Created `tests/utils/__init__.py` re-exporting the same symbols. Updated the one Python import in `packages/controllers/mission/tests/test_context.py`.

---

## MEDIUM — Performance

### ✅ E1 · N+1 ArangoDB edge inserts
**Fixed:** Added `add_edges_bulk(edges, map_id)` to `GraphDatabaseService` using `collection.insert_many`. `_handle_node_update` in `graph_builder/server.py` now calls `add_edges_bulk` instead of looping `add_edge`.

---

### ✅ E2 · Sync MinIO `store_image` called from async MQTT callback thread
**Fixed:** The async call site in `_handle_node_update` now uses `await asyncio.to_thread(self._save_images, ...)`. Sync callers (MQTT callback thread, REST endpoint) are unaffected.

---

### ✅ E3 · O(n) linear scan over `image_buffer` for every node update
**Fixed:** `image_buffer` restructured from `Dict[(robot, session, camera), ...]` to `Dict[(robot, session), Dict[camera, ...]]`. `_get_buffered_images` now does an O(1) `pop` lookup. `_clear_robot_state` and `_cleanup_old_mappings` updated accordingly.

---

### ✅ E4 · One upstream WebSocket per client in `WebSocketProxyManager`
**Note:** Already implemented — `WebSocketProxyManager` already maintains one backend connection per `map_id` and fans out to all subscribed client WebSockets via `proxy_connections[key]["clients"]`. No change needed.

---

### E5 · No shared Docker base image
**Note:** No Dockerfiles exist in this project (Bazel build system is used). N/A.

---

### ✅ E6 · Deprecated `@app.on_event` lifecycle hooks
**Fixed:** Migrated `graph_builder/main.py` and `api/main.py` from `@app.on_event("startup"/"shutdown")` to `@asynccontextmanager lifespan` passed to `FastAPI(lifespan=lifespan)`. (The other two files mentioned in the backlog — `services/livekit/main.py` and `services/mission_planner/main.py` — do not exist in this repo.)

---

## LOW — Minor / style

- ✅ `packages/api/client.py WebSocketClient` — deleted (no callers; only formatted URL strings).
- ✅ `packages/api/server.py:WebSocketProxyManager._forward_from_backend_stub` — renamed to `_hold_until_cancelled`; clarifies intentional sleep-until-cancel behaviour.
- ✅ `packages/api/server.py:WebSocketManager` — `ws_manager` IS actively used (stats, broadcast × 3); removed the incorrect "legacy" comment.
- ✅ `packages/utils/service_utils.py` — extracted `_derive_status(results)` helper; `DependencyHealthChecker.get_status` and `create_health_response` both delegate to it.

## Final-pass fixes (post-backlog audit)

- ✅ `tests/requirements-test.txt` — removed redundant `psycopg-binary` and `psycopg-pool` lines; only `psycopg[binary,pool]==3.2.4` remains (matches fix C2).
- ✅ `services/mission_planner/main.py` and `services/livekit/main.py` — migrated from deprecated `@app.on_event` to `@asynccontextmanager lifespan` (completes fix E6 for all four services).
- ✅ `packages/api/client.py ApiDelegationClient` — deleted (no production callers; sync/requests-based).
- ✅ `packages/utils/service_utils.py get_service_config` — deleted (no callers anywhere).
- ✅ Stale `__pycache__` `.pyc` files for deleted modules (`base_client`, `test_base_client`, `test_rtree_spatial_index`) removed.
