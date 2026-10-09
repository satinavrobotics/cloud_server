# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Isaac Mission Dispatch — a cloud server for robot fleet management. Robots communicate via MQTT using the VDA5050 protocol; humans interact via a REST/WebSocket API. The stack is 6 Python services plus 4 infrastructure containers, coordinated via Docker Compose.

Known issues, deferred work and audit findings: `AUDIT_BACKLOG.md` (punch list) and `docs/BACKLOG.md` (incident write-ups).

## Environment Setup

There are two separate env files, with **different variable names**:

- **Docker Compose** reads `docker_compose/.env` (the project dir is the directory of the first `-f` file). It expects `ARANGO_ROOT_PASSWORD`, `MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD`, `POSTGRES_PASSWORD`, `POSTGRES_DATABASE_*`, `MQTT_PORT_TCP`, `MQTT_PORT_WEBSOCKET`, `MQTT_TRANSPORT`, and maps them into each service as `ARANGO_PASSWORD` / `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY`. Compose falls back to `openSesame` / `minioadmin` when the root variables are unset — set them.
- **Running a service directly** (`python -m ...`, and the unit tests) reads the process environment: `packages/config.py` never fails at import (every image ships it, mission-dispatch included); the code that needs `ARANGO_PASSWORD`, `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY` or `POSTGRES_PASSWORD` calls `config.require_secret(name)` / `postgres_database_password()` when it builds its client, and raises `EnvironmentError` naming the variable. The repo-root `.env.example` documents these names.

Never commit real credentials to either file.

## Running the Stack

```bash
# Start all services (full stack)
docker compose -f docker_compose/mission_dispatch_services.yaml up

# Start with dev overrides
docker compose -f docker_compose/mission_dispatch_services.yaml \
               -f docker_compose/mission_dispatch_services_dev_override.yaml up

# Rebuild and recreate the changed services (build first; a failed build stops nothing)
./restart_services.sh

# Check health of running services
./scripts/check_health.sh
```

## Running Tests

```bash
# Unit tests on the PRODUCTION PINS (Python 3.10, pydantic 1.9.0, psycopg 3.0.15, paho 1.6.1),
# in a throw-away container (needs Docker, no network); args go to pytest. Use this to verify.
./scripts/run_unit_tests_pinned.sh [pytest args...]

# Unit tests on the host Python (no Docker; pydantic 2.x, so NOT proof of v1 compatibility)
./scripts/run_unit_tests.sh
# Equivalent: pytest tests/unit -v -m unit --cov=packages --cov-report=term-missing

# Integration tests (requires Docker)
./scripts/run_integration_tests.sh
# Equivalent: pytest tests/integration -v -m integration

# Full suite with coverage HTML + XML reports
./scripts/run_all_tests.sh

# Single test file
pytest tests/unit/test_something.py -v

# Single test function
pytest tests/unit/test_something.py::TestClass::test_function -v
```

Test markers (enforced strictly): `unit`, `integration`, `e2e`, `performance`, `slow`, `requires_docker`, `requires_services`. `asyncio_mode = auto` so async tests work without decorators.

## Service Architecture

All services use `network_mode: host` and communicate over localhost. Port assignments (defaults live in `packages/config.py`):

| Service | Port | Purpose |
|---|---|---|
| api-delegation-service | 8000 | Unified REST+WebSocket gateway (main entry point) |
| mission-dispatch | — (no HTTP) | VDA5050 mission controller (MQTT↔PostgreSQL); a worker, not a web service; liveness = a heartbeat-file docker healthcheck, not acted on by plain compose |
| graph-builder-service | 8004 | Subscribes to `robot/node_update` MQTT, builds the topomap |
| mission-planner-service | 8005 | Path planning on the topological graph |
| livekit-service | 8006 | Teleoperation video token service for **LiveKit Cloud** (caller-chosen grants; the pre-existing one) |
| livekit-sfu-tokens | 8008 | Role-scoped tokens for the **self-hosted** Tailscale-only LiveKit SFU (`livekit-sfu`, below); shares only `packages/utils/livekit_tokens.py` with `livekit-service` |
| agent-orchestrator-service | 8007 | LLM agent: watches fleet events, produces insights (`/api/agent/*`, `/ws/agent/*` via the client's nginx) |
| mosquitto | 1883/9001 | MQTT broker (TCP/WebSocket) |
| postgres | 5432 | Mission/robot/map/settings objects (mission-dispatch, mission-planner, api, graph-builder) |
| arangodb | 8529 | Graph database backend |
| minio | 9000 | Object storage: node images, rosbags, base models |

There is **no** standalone graph-db, image-db or similarity service. `packages/topomap_dbs/{graph_db,image_db,model_db,rosbag_db}/server.py` are in-process libraries: services reach ArangoDB and MinIO directly through `TopomapDatabaseClient` (`packages/topomap_dbs/client.py`).

Self-hosted LiveKit is part of the main compose file (`docker_compose/mission_dispatch_services.yaml`), so `restart_services.sh` (build first, then `up -d`: only changed containers are recreated) restarts it when its image or config changed (that drops live video for a few seconds; reconnects are automatic): `livekit-sfu` (7880/7881 TCP, 50000-60000 UDP, Prometheus 6789, container `sati_livekit_sfu`) and `livekit-sfu-tokens` (8008, role-scoped tokens, `packages/services/livekit_sfu_tokens/`; operators get a `wss://` name from `tailscale serve` on 443 so the https dashboard works, via the operator-only `/api/operator/createToken`). Tailscale-only, settings in the gitignored `docker_compose/livekit_sfu.env` (optional for compose; without it just these two fail to start). See `docs/livekit_sfu/README.md`.

### Data Flow

1. **Robot → Cloud**: Robot publishes pose/sensor data to MQTT topic `robot/node_update`; `graph-builder` consumes it and writes nodes/edges to ArangoDB and images to MinIO (via `TopomapDatabaseClient`) to build a topological map. The node goes to the map of the robot's one open session (`map_sessions` in Postgres) when that is an unpaused **mapping** session (any map state), converted into the map frame with that session's `map_T_session`; the payload's map and session ids are ignored. It is dropped, and reported as `MAP.INGEST_REJECTED`, with no open session (`no_session`), a missing or deleting map, an operate session (`operate_session`: operate adds no data), a paused one (`session_paused`), an unplaced session (`session_unplaced`) or a changed datum (`datum_changed`). The robot's mapping services are switched through its **orchestrator**, not MQTT: opening a mapping session starts each of the session's `services` after the session's transaction committed (SLAM first, then the topomap), resume starts them, pause/finish/robot delete stop them (a replace always stops the old ones before the SLAM save; a robot delete also saves the closed session's SLAM map, under the robot's lock), and SLAM recording is driven for `slam_map` maps. A start/resume while the robot's previous SLAM map is still being saved is deferred (its `robot_actions` say so) and runs when the save ended; after a robot run change mission-dispatch NOTIFYs `robot_run_changed` and the API restarts the services of the robot's unpaused mapping session (`maps.restart_session_services`, event `MAP.SESSION_SERVICES_RESTARTED`/`_RESTART_FAILED`). A finish / replace / robot delete / restore leaves the map `ready` only when it holds data (ArangoDB nodes, counted before the transaction, or a saved SLAM map `status.slam_saved_at`), else `draft`. On a robot with the orchestrator's **mapping API** (`GET /localization` reports `topomap`) the topomap is `PUT /localization {mode, map, topomap: true|false}` on the current mode, whichever it is; otherwise (older robots, the sim) it is the orchestrator service `topomap` / `sim_topomap` (candidates in `packages/config.py::MAPPING_SERVICE_CANDIDATES`) via `POST /services/{name}/start|stop`. `mapping_services` (`topo`, `grid`, `slam`) is what the robot offers, and the client's mapping choices come from it (`packages/api/mapping_switch.py`, `orchestrator_client.py`). Localization (SLAM recording, relocalization) goes through the robot orchestrator's **localization facade** (`GET/PUT /localization`, `POST/GET /localization/save`; the robot switches mode in-process): every orchestrator has it, there is no probe and no fallback to the old `/maps/{name}/mapping/start|relocalize` / `/maps/mapping/*` calls (removed). SLAM recording is the mode `slam`; the live truth is the VDA5050 state (`positionInitialized`, `agvPosition.mapId` = the map **name** once localized, never assumed to be "map"), the relocalization job watches it, and a finished SLAM save PUTs the previous intent back (the restore also sends `topomap: false` on a mapping-API robot, as the orchestrator refuses a mode change while the topomap runs; the open mapping session's topomap is then restarted). A failed save leaves the robot in slam (leaving would discard the unsaved map) until the operator retries or discards it: the robot view's `slam_save` (`{map, state: saving|failed, detail, at}`, persisted in `robot_slam_saves` with the pre-SLAM intent) and `POST /api/v1/robots/{r}/slam-save/retry|discard`. Relocalization is refused while the robot's mapping session records SLAM or a save failed; a topomap-only mapping session allows it. Deleting a map does not stop a robot that is still recording SLAM for it (the facade does not name the recorded map; there is no orphan stop or reconcile pass). A reloc job's and the proxy's PUTs invalidate the cached mapping snapshot. While the robot records SLAM for its mapping session, has a save pending or a failed one, the orchestrator proxy refuses (409 `detail` naming the server route: pause/finish the session, `slam-save/retry|discard`) `PUT /localization`, `POST /localization/save` and start/stop of the mapping services; its other localization/service mutations hold the robot lock for the one call, then call `robot_changed`. The switching **never blocks or undoes** the user's action (no 502/504/409, no closing/re-pausing the session): failures are only reported, and every robot-side action is returned in the response's `robot_actions` (`[{service, action, ok, label, detail}]`, documented in `packages/api/README.md`); the SLAM background save's end is the event `MAP.SLAM_SAVE_DONE`/`_FAILED`. `mapping_state`/`mapping_services` are read from the orchestrator (`docs/satinav-maps-redesign.md` §14.16). Since §14 (U1–U3) a robot *uses* a map through its one open session (`purpose` mapping | operate); its `map_T_session` (valid only while *placed*) is what the dispatcher, planner and client use to convert between the robot's run frame and the map frame, and mission-dispatch unplaces it when the robot's run changes (`packages/utils/map_sessions.py`). Since U6 the open session is the **only** notion of a robot's map: there is no `robot.current_map`, no `GEO`/`LOCAL` sentinel and no map/datum fallback; `PUT /api/v1/robots/{r}/map` answers 410 (one release), and route waypoints / plans on a map the robot has no placed session on are refused. See `docs/satinav-maps-redesign.md` (§14.14).
2. **Human → Cloud**: REST/WebSocket calls to `api-delegation-service` (port 8000), which calls `mission-planner` and `livekit-service` over HTTP, reads/writes ArangoDB + MinIO directly, reads/writes mission/robot objects in PostgreSQL, and reverse-proxies `/api/v1/orchestration/{robot}/*` to the orchestrator running on that robot.
3. **3D reconstruction** (`docs/reconstruction/design.md`): the robot also sends one u16-mm depth PNG + camera parameters per node and camera on `robot/depth_upload`; graph-builder stores the PNG at `map-{id}/{node}/depth/{camera}.png` and the parameters (with the map-frame `pose3d_map`) on the ArangoDB node as `depth.{camera}`. The reconstruction itself runs in an **external service** (own repo, any host, no DB/MinIO credentials; contract `docs/reconstruction/handover.md`). The API is its gateway (`packages/api/reconstruction.py`): it owns the job (`map_reconstructions`), a dispatcher (advisory lock, one per cluster) POSTs a manifest of presigned MinIO URLs (signed for `RECONSTRUCTION_MINIO_ENDPOINT`) to the service, the service PUTs only `cloud.ply` + `meta.json` into the `recon-staging` bucket and calls back on `/internal/reconstruction/jobs/{id}/…` (per-job HMAC token), and the API verifies them, copies them to `map-{id}/reconstruction/{job}/` and derives the 2.5D top view (`ortho.png`, `height.png`) from the PLY itself in a child process (`packages/api/reconstruction_topview.py`, numpy). Off until `RECONSTRUCTION_SERVICE_URL`/`_SERVICE_KEY`/`_CALLBACK_SECRET` are set. Likewise `robot/costmap_upload` (`MQTT_COSTMAP_TOPIC`, QoS 1) carries one occupancy PNG per node and layer: stored at `map-{id}/{node}/costmap/{layer}.png` and as `costmap.{layer}` on the node (with the map-frame `origin_map`), session-gated and buffered like depth, drops reported as `dropped_costmap` (`packages/services/graph_builder/README.md`).
4. **Mission execution**: the API writes a mission object to PostgreSQL; `mission-dispatch` watches PostgreSQL (LISTEN/NOTIFY), publishes VDA5050 orders to MQTT, and writes the robot's reported state back. There is no HTTP hop between the API and `mission-dispatch` — PostgreSQL is the interface.

### Key Packages

- `packages/config.py` — intended single source of truth for default ports, URLs, thresholds, and env-var reads. Import from here in all new code (existing violators are listed in `AUDIT_BACKLOG.md` C2).
- `packages/api/main.py` — the FastAPI `app` and **all** REST + WebSocket routes (~120K).
- `packages/api/server.py` — `ApiDelegationService` (the logic the routes call), plus `WebSocketManager` / `WebSocketProxyManager`. No routes here.
- `packages/api/orchestrator_proxy.py` — reverse proxy to the per-robot orchestrator.
- `packages/api/orchestrator_client.py`, `mapping_switch.py` — the API's own calls to a robot's orchestrator (start/stop/status of a session's mapping services).
- `packages/controllers/mission/server.py` — VDA5050 mission dispatcher; watches PostgreSQL, publishes to MQTT.
- `packages/controllers/mission/behavior_tree.py` — py_trees behavior tree that drives mission step execution.
- `packages/controllers/mission/vda5050_types/` — VDA5050 protocol type definitions (Pydantic v1).
- `packages/database/postgres.py` — PostgreSQL client (psycopg3): object CRUD + the LISTEN/NOTIFY watcher. Raises `fastapi.HTTPException` (404/400) itself — don't re-wrap those.
- `packages/topomap_dbs/` — ArangoDB (`graph_db`) and MinIO (`image_db`, `rosbag_db`, `model_db`) libraries + `TopomapDatabaseClient`.
- `packages/utils/mqtt_client.py` — shared MQTT pub/sub wrapper (paho-mqtt).
- `packages/utils/service_utils.py`, `fastapi_helpers.py` — health-check and FastAPI boilerplate shared by the services. Service-to-service HTTP clients (`services/*/client.py`) use `httpx.AsyncClient`.
- `cloud_common/objects/` — shared Pydantic data models: `Robot`, `Mission`, `Map`, `Settings`, `DetectionResults`.

### Pydantic Version

All services pin **Pydantic v1** (`==1.9.0`). Use v1 idioms (`@validator`, `class Config`, etc.) throughout. Do not introduce v2 syntax. Caveat: the host-Python test env (`tests/requirements-test.txt`, Python 3.12) installs Pydantic 2.x (1.9.0 does not install there), so a green host run does not prove v1 compatibility; use `scripts/run_unit_tests_pinned.sh` (`tests/Dockerfile.unit`, Python 3.10 + the service pins) (`AUDIT_BACKLOG.md` C1).

## Individual Service Entry Points

Each service follows the same pattern: `packages/<service>/main.py` parses CLI args → instantiates the server class from `packages/<service>/server.py` → starts uvicorn. Run a service standalone:

```bash
python -m packages.api.main --port 8000 --host 0.0.0.0
python -m packages.services.mission_planner.main --port 8005 --host 0.0.0.0
python -m packages.services.graph_builder.main --port 8004 --host 0.0.0.0
```
