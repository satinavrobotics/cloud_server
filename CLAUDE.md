# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Isaac Mission Dispatch — a cloud server for robot fleet management. Robots communicate via MQTT using the VDA5050 protocol; humans interact via a REST/WebSocket API. The stack is 6 Python services plus 4 infrastructure containers, coordinated via Docker Compose.

Known issues, deferred work and audit findings: `AUDIT_BACKLOG.md` (punch list) and `docs/BACKLOG.md` (incident write-ups).

## Environment Setup

There are two separate env files, with **different variable names**:

- **Docker Compose** reads `docker_compose/.env` (the project dir is the directory of the first `-f` file). It expects `ARANGO_ROOT_PASSWORD`, `MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD`, `POSTGRES_PASSWORD`, `POSTGRES_DATABASE_*`, `MQTT_PORT_TCP`, `MQTT_PORT_WEBSOCKET`, `MQTT_TRANSPORT`, and maps them into each service as `ARANGO_PASSWORD` / `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY`. Compose falls back to `openSesame` / `minioadmin` when the root variables are unset — set them.
- **Running a service directly** (`python -m ...`, and the unit tests) reads the process environment: `packages/config.py` raises `EnvironmentError` at import if `ARANGO_PASSWORD`, `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY` or `POSTGRES_PASSWORD` is missing. The repo-root `.env.example` documents these names.

Never commit real credentials to either file.

## Running the Stack

```bash
# Start all services (full stack)
docker compose -f docker_compose/mission_dispatch_services.yaml up

# Start with dev overrides
docker compose -f docker_compose/mission_dispatch_services.yaml \
               -f docker_compose/mission_dispatch_services_dev_override.yaml up

# Restart all services
./restart_services.sh

# Check health of running services
./scripts/check_health.sh
```

## Running Tests

```bash
# Unit tests only (no Docker required)
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
| mission-dispatch | — (no HTTP) | VDA5050 mission controller (MQTT↔PostgreSQL); a worker, not a web service |
| graph-builder-service | 8004 | Subscribes to `robot/node_update` MQTT, builds the topomap |
| mission-planner-service | 8005 | Path planning on the topological graph |
| livekit-service | 8006 | Teleoperation video token service |
| agent-orchestrator-service | 8007 | LLM agent: watches fleet events, produces insights (`/api/agent/*`, `/ws/agent/*` via the client's nginx) |
| mosquitto | 1883/9001 | MQTT broker (TCP/WebSocket) |
| postgres | 5432 | Mission/robot/map/settings objects (mission-dispatch, mission-planner, api, graph-builder) |
| arangodb | 8529 | Graph database backend |
| minio | 9000 | Object storage: node images, rosbags, base models |

There is **no** standalone graph-db, image-db or similarity service. `packages/topomap_dbs/{graph_db,image_db,model_db,rosbag_db}/server.py` are in-process libraries: services reach ArangoDB and MinIO directly through `TopomapDatabaseClient` (`packages/topomap_dbs/client.py`).

### Data Flow

1. **Robot → Cloud**: Robot publishes pose/sensor data to MQTT topic `robot/node_update`; `graph-builder` consumes it and writes nodes/edges to ArangoDB and images to MinIO (via `TopomapDatabaseClient`) to build a topological map. The map used is the robot's `current_map` from Postgres, not the MQTT payload.
2. **Human → Cloud**: REST/WebSocket calls to `api-delegation-service` (port 8000), which calls `mission-planner` and `livekit-service` over HTTP, reads/writes ArangoDB + MinIO directly, reads/writes mission/robot objects in PostgreSQL, and reverse-proxies `/api/v1/orchestration/{robot}/*` to the orchestrator running on that robot.
3. **Mission execution**: the API writes a mission object to PostgreSQL; `mission-dispatch` watches PostgreSQL (LISTEN/NOTIFY), publishes VDA5050 orders to MQTT, and writes the robot's reported state back. There is no HTTP hop between the API and `mission-dispatch` — PostgreSQL is the interface.

### Key Packages

- `packages/config.py` — intended single source of truth for default ports, URLs, thresholds, and env-var reads. Import from here in all new code (existing violators are listed in `AUDIT_BACKLOG.md` C2).
- `packages/api/main.py` — the FastAPI `app` and **all** REST + WebSocket routes (~65K).
- `packages/api/server.py` — `ApiDelegationService` (the logic the routes call), plus `WebSocketManager` / `WebSocketProxyManager`. No routes here.
- `packages/api/orchestrator_proxy.py` — reverse proxy to the per-robot orchestrator.
- `packages/controllers/mission/server.py` — VDA5050 mission dispatcher; watches PostgreSQL, publishes to MQTT.
- `packages/controllers/mission/behavior_tree.py` — py_trees behavior tree that drives mission step execution.
- `packages/controllers/mission/vda5050_types/` — VDA5050 protocol type definitions (Pydantic v1).
- `packages/database/postgres.py` — PostgreSQL client (psycopg3): object CRUD + the LISTEN/NOTIFY watcher. Raises `fastapi.HTTPException` (404/400) itself — don't re-wrap those.
- `packages/topomap_dbs/` — ArangoDB (`graph_db`) and MinIO (`image_db`, `rosbag_db`, `model_db`) libraries + `TopomapDatabaseClient`.
- `packages/utils/mqtt_client.py` — shared MQTT pub/sub wrapper (paho-mqtt).
- `packages/utils/service_utils.py`, `fastapi_helpers.py` — health-check and FastAPI boilerplate shared by the services. Service-to-service HTTP clients (`services/*/client.py`) use `httpx.AsyncClient`.
- `cloud_common/objects/` — shared Pydantic data models: `Robot`, `Mission`, `Map`, `Settings`, `DetectionResults`.

### Pydantic Version

All services pin **Pydantic v1** (`==1.9.0`). Use v1 idioms (`@validator`, `class Config`, etc.) throughout. Do not introduce v2 syntax. Caveat: `tests/requirements-test.txt` currently installs Pydantic 2.x, so a green local unit run does not prove v1 compatibility (`AUDIT_BACKLOG.md` C1).

## Individual Service Entry Points

Each service follows the same pattern: `packages/<service>/main.py` parses CLI args → instantiates the server class from `packages/<service>/server.py` → starts uvicorn. Run a service standalone:

```bash
python -m packages.api.main --port 8000 --host 0.0.0.0
python -m packages.services.mission_planner.main --port 8005 --host 0.0.0.0
python -m packages.services.graph_builder.main --port 8004 --host 0.0.0.0
```
