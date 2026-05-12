# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Isaac Mission Dispatch — a cloud server for robot fleet management. Robots communicate via MQTT using the VDA5050 protocol; humans interact via a REST/WebSocket API. The stack is composed of ~10 Python microservices coordinated via Docker Compose.

## Environment Setup

Before running any service locally, copy `.env.example` to `.env` and fill in required credentials (ARANGO_PASSWORD, MINIO_ACCESS_KEY, MINIO_SECRET_KEY, POSTGRES_PASSWORD). `packages/config.py` raises `EnvironmentError` on startup if these are missing.

```bash
cp .env.example .env
# Edit .env with actual values
```

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

All services use `network_mode: host` and communicate over localhost. Port assignments:

| Service | Port | Purpose |
|---|---|---|
| api-delegation-service | 8000 | Unified REST+WebSocket gateway (main entry point) |
| mission-dispatch | 5000 | VDA5050 mission controller (MQTT↔PostgreSQL) |
| graph-builder-service | 8004 | Subscribes to `robot/node_update` MQTT, builds topomap |
| mission-planner-service | 8005 | Path planning on topological graph |
| similarity-service | 8003 | Distance/traversability metric computation |
| graph-db-service (ArangoDB wrapper) | 6001 | Topological graph CRUD + spatial index |
| image-db-service (MinIO wrapper) | 6002 | Node image storage/retrieval |
| livekit-service | 8006 | Teleoperation video token service |
| mosquitto | 1883/9001 | MQTT broker (TCP/WebSocket) |
| postgres | 5432 | Mission/robot state (used by mission-dispatch + mission-planner) |
| arangodb | 8529 | Graph database backend |

### Data Flow

1. **Robot → Cloud**: Robot publishes pose/sensor data to MQTT topic `robot/node_update`; `graph-builder` consumes this and calls `graph-db-service` + `image-db-service` + `similarity-service` to build a topological map.
2. **Human → Cloud**: REST calls to `api-delegation-service` (port 8000) which proxies to `mission-planner`, `graph-db-service`, `image-db-service`, `livekit-service`, and PostgreSQL (via `mission-dispatch`).
3. **Mission execution**: `api-delegation-service` sends a mission plan to `mission-dispatch`, which publishes VDA5050 orders to MQTT; the robot subscribes and executes, reporting state back via MQTT.

### Key Packages

- `packages/config.py` — single source of truth for all default ports, URLs, thresholds, and env-var reads. Import from here in all new code.
- `packages/api/server.py` — main FastAPI app (~64K); handles REST + WebSocket endpoints, proxies to downstream services.
- `packages/controllers/mission/server.py` — VDA5050 mission dispatcher; reads from PostgreSQL, publishes to MQTT.
- `packages/controllers/mission/behavior_tree.py` — py_trees behavior tree that drives mission step execution.
- `packages/controllers/mission/vda5050_types/` — VDA5050 protocol type definitions (Pydantic v1).
- `packages/database/postgres.py` — PostgreSQL client (psycopg3) for mission/robot state.
- `packages/utils/mqtt_client.py` — shared MQTT pub/sub wrapper (paho-mqtt).
- `packages/utils/base_client.py` — HTTP client base with retry logic (currently unused; service clients use bare `requests` calls).
- `cloud_common/objects/` — shared Pydantic data models: `Robot`, `Mission`, `DetectionResults`.

### Pydantic Version

All services pin **Pydantic v1** (`==1.9.0`). Use v1 idioms (`@validator`, `class Config`, etc.) throughout. Do not introduce v2 syntax.

## Individual Service Entry Points

Each service follows the same pattern: `packages/<service>/main.py` parses CLI args → instantiates the server class from `packages/<service>/server.py` → starts uvicorn. Run a service standalone:

```bash
python -m packages.api.main --port 8000 --host 0.0.0.0
python -m packages.services.mission_planner.main --port 8005 --host 0.0.0.0
python -m packages.topomap_dbs.graph_db.main --port 6001 --host 0.0.0.0
```
