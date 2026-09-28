# API Delegation Service

## Overview

The API Delegation Service is a central API gateway that provides a unified REST and WebSocket interface for clients to interact with the robot fleet management system. It acts as a single entry point that delegates requests to the appropriate microservices.

## Features

- **REST API** for synchronous operations:
  - Load maps from graph database
  - Retrieve images from image database
  - Submit navigation requests (proxy to mission planner)
  - Query robot and mission status (proxy to Mission Dispatcher)

- **WebSocket API** for real-time updates:
  - Map updates from graph builder
  - Mission status updates
  - Robot status updates

- **Service Delegation**:
  - Proxies requests to graph database, image database, mission planner, and Mission Dispatcher
  - Manages WebSocket connections for real-time notifications
  - Provides unified error handling and logging

## Architecture

### REST API Flow
```
┌─────────────────────────────────────────────────────────────┐
│                    API Delegation Service                    │
│                    (Single Entry Point)                      │
├─────────────────────────────────────────────────────────────┤
│  REST Endpoints          │  WebSocket Endpoints             │
│  - /api/v1/map/load      │  - /ws/map/{map_id}             │
│  - /api/v1/images/{id}   │  - /ws/mission/{mission_name}   │
│  - /api/v1/navigate      │  - /ws/robot/{robot_name}       │
│  - /api/v1/robots/{name} │                                  │
│  - /api/v1/missions/{id} │                                  │
└─────────────────────────────────────────────────────────────┘
           │              │              │              │
           ▼              ▼              ▼              ▼
    ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐
    │ Graph DB │  │ Image DB │  │ Mission  │  │ Mission  │
    │          │  │          │  │ Planner  │  │Dispatcher│
    └──────────┘  └──────────┘  └──────────┘  └──────────┘
```

### WebSocket Proxy Architecture

The API Delegation Service implements a **WebSocket proxy pattern** for real-time updates:

```
┌─────────┐
│ Client  │
└────┬────┘
     │ ws://api-delegation:8000/ws/map/{map_id}
     ▼
┌────────────────────────────────────┐
│  API Delegation Service            │
│  ┌──────────────────────────────┐ │
│  │  WebSocket Proxy Manager     │ │
│  │  - Accepts client connections│ │
│  │  - Connects to backend       │ │
│  │  - Forwards messages bi-dir  │ │
│  └──────────────────────────────┘ │
└────────────────────────────────────┘
     │
     │ ws://graph-builder:8004/ws/updates/{map_id}
     ▼
┌────────────────────────────────────┐
│  Graph Builder Service             │
│  - Processes MQTT node updates     │
│  - Publishes to WebSocket clients  │
│  - Sends real-time map changes     │
└────────────────────────────────────┘
```

**Key Features:**
- **Transparent Proxying**: Clients connect to API Delegation, which proxies to backend services
- **Connection Pooling**: Multiple clients can share a single backend connection
- **Automatic Cleanup**: Backend connections close when no clients remain
- **Service Ownership**: Each backend service owns its update logic

## API Endpoints

### Health & Stats

#### `GET /health`
Health check endpoint.

**Response:**
```json
{
  "status": "healthy",
  "service": "api_delegation",
  "dependencies": {
    "graph_db": true,
    "image_db": true,
    "database": true
  }
}
```

#### `GET /stats`
Get service statistics.

**Response:**
```json
{
  "service": "api_delegation",
  "graph_db_url": "http://localhost:6001",
  "image_db_url": "http://localhost:6002",
  "mission_planner_url": "http://localhost:8005",
  "database_url": "http://localhost:5000",
  "default_map_id": "default",
  "websocket_connections": {
    "map_updates": 2,
    "mission_status": 1,
    "robot_status": 3
  }
}
```

### Map Operations

#### `POST /api/v1/map/load`
Load a map from the graph database.

**Request:**
```json
{
  "map_id": "warehouse_floor_1"
}
```

**Response:**
```json
{
  "success": true,
  "map_id": "warehouse_floor_1",
  "stats": {
    "node_count": 150,
    "edge_count": 300
  },
  "message": "Map warehouse_floor_1 loaded successfully"
}
```

A map this route registers is typed like the M1 migration types old maps: `geo` if the datum is
real (not null, not (0, 0)), else `local`; state `ready`. It never retypes an existing map.
Since M2 `POST /map/load` with `GEO` / `LOCAL` (the old client's mapless views) registers
nothing: it returns no nodes and `transform: null` (the client then uses the robot's datum).

#### Typed maps and mapping sessions (maps redesign M1)

See `docs/satinav-maps-redesign.md` §2-§4, §7, §13.1-§13.2. Code: `packages/api/maps.py`. The
routes above and `PUT /api/v1/maps/{id}/datum` are unchanged; `PUT /api/v1/robots/{r}/map` is a
deprecated shim over sessions since M2 (below).

Every map has `type` (`local` | `geo`), `geo` (`{utm_zone, utm_north, origin_e, origin_n}` for a
geo map once its origin is known, else null) and `status.state` (`draft` | `mapping` | `paused`
| `ready` | `archived`). The object `lifecycle` (`ALIVE` / `DELETING`) is still the delete
bookkeeping. All additions are new keys; nothing was removed or renamed.

| Method | Path | Does |
|---|---|---|
| POST | `/api/v1/maps` | `{name, type, description?}` → 201, a `draft` map. 409 if the name exists, collides with another map's image bucket (case/`_` vs `-`), or ArangoDB already has nodes under it. 422 on a bad name (1-59 of `A-Za-z0-9_-`, alphanumeric at both ends; not `GEO`/`LOCAL`) or type. |
| GET | `/api/v1/maps?type=&state=&include_archived=` | Same `{maps, count}` body as before. Archived maps only with `include_archived=true` or `state=archived`. |
| GET | `/api/v1/maps/{id}` | As before plus `type`, `geo`, `state`, `open_session_id`, `grid_version` and `sessions: {count, open, unaligned, items}` (newest first, at most 50). |
| PATCH | `/api/v1/maps/{id}` | `{description}`. Renaming is not supported (422): the name keys the map in Postgres, ArangoDB and MinIO. |
| GET | `/api/v1/maps/{id}/graph` | `{map_id, type, geo, state, node_count, edge_count, nodes, edges, transform}`; nodes/edges as in `POST /map/load`, without its side effects. |
| POST | `/api/v1/maps/{id}/sessions` | `{robot}` → 201 `{map_id, map_state, changed, session}`. The robot must exist (404) and be online (409), and have no other open session (409). One open session per map for now (409). A geo map needs the robot's datum (409); its first session sets the map origin from it. The map goes to `mapping`. |
| POST | `/api/v1/maps/{id}/sessions/{sid}/pause` · `resume` · `finish` | Map → `paused` / `mapping` / `ready`. Repeating an action already in effect returns `changed: false`; pause/resume of a finished session is 409. |
| POST | `/api/v1/maps/{id}/archive` · `restore` | `archived` (409 while a session is open) / back to `ready` (`draft` if it never had a session). |
| DELETE | `/api/v1/maps/{id}` | As before (202, background delete); now 409 while a session is open. The map's sessions go with it. |

A session: `{session_id, map_name, robot_name, kind ('live' | 'legacy'), state ('mapping' |
'paused' | 'finished'), started_at, paused_at, ended_at, datum, map_T_session: {tx, ty, yaw},
aligned, node_count}`. `map_T_session` takes a robot-frame point into the map frame
(`packages/utils/map_geo.py`): a pure translation for a UTM datum in the map's zone, with the
grid-convergence yaw for an ENU datum; identity for a local map (aligned only for its first
session). Every pre-M1 map has one ended `legacy` session (robot `legacy`, identity).

Since M2 a session is what routes robot data: graph-builder stores a robot's nodes and images
in the map of its open, unpaused session (node `pose` in the map frame, plus `robot_pose` and
`session_id`) and drops everything else, reported as `MAP.INGEST_REJECTED` (at most one per robot
and reason a minute, with the drop counts). A pause or finish takes effect within ~1 s. Not yet:
alignment (M6), grid (M7). Events: `MAP.CREATED`, `MAP.ARCHIVED`,
`MAP.RESTORED`, `MAP.SESSION_STARTED/PAUSED/RESUMED/FINISHED`, `MAP.DELETED` (background delete
finished), `MAP.INGEST_REJECTED` (source `graph_builder`) in `fleet_events`. `POST /api/v1/maps`
and `POST .../sessions` accept `Idempotency-Key`.

#### `PUT /api/v1/robots/{robot}/map` — DEPRECATED (maps M2; removed with the client's Maps page, M4)

The old client's "assign map", kept working on top of sessions (`maps.assign_robot_map`), in
one transaction:

| `map_id` | Does |
|---|---|
| a real map name | Finishes the robot's open session if it is on another map; creates the map if it does not exist (`draft`, typed from the robot's datum: `geo` with a real datum, else `local`; `MAP.CREATED`; the name rules and 409s of `POST /api/v1/maps`); starts a session on it with the rules and errors of `POST /api/v1/maps/{id}/sessions` (404 robot, 409 offline / geo map without a robot datum / map busy / archived / being deleted). The map the robot already maps: nothing changes. |
| `GEO`, `LOCAL`, null, `""` | Finishes the robot's open session, if any. |

`robot.current_map` is still written (the value sent; null to clear): the old client's mission
modes and map selection, the run recorder's `map_id` and the bag metadata read it
(`docs/satinav-maps-redesign.md` §13.2). A refused call changes nothing. Body:
`{success, robot_name, current_map, deprecated, map_created, finished_session, session}`.
`POST /api/v1/maps/{id}/sessions` does not write `current_map`.

#### Robot mapping switch over MQTT (maps M3)

After every committed session change (`POST .../sessions`, `.../pause|resume|finish`, the shim)
the API publishes the robot's **retained** `{prefix}/{robot}/mapping/set` (`prefix` =
`MQTT_VDA5050_PREFIX`, `uagv/v2/RobotCompany`) from its open session, and re-publishes every
robot's on each broker (re)connect. It caches the robots' retained `{prefix}/+/mapping/state`.
Topic and payload contract: `packages/api/mapping_control.py` (docstring); robot side:
`sati_topo_mapping` in sati_ros_navstack. Additive response fields:

| Where | Field |
|---|---|
| `POST .../sessions`, `PUT /robots/{r}/map` | `robot_notified` (bool: the broker acknowledged the set message; false never fails the call), `mapping_service` (`"running"` \| `"not_running"`: the robot's topomap is connected; the session starts either way), `mapping_state` |
| `POST .../sessions/{sid}/pause\|resume\|finish` | `robot_notified`, `mapping_state` |
| `GET /api/v1/maps/{id}` | `sessions.mapping_state`, `sessions.mapping_service` (of the open session's robot; null without an open session) |
| `GET /api/v1/robots`, `GET /api/v1/robots/{r}` | `mapping_state` per robot |
| `POST /api/v1/robots/{r}/mapping/off` | new: force a robot's capture off when it has **no** open session (404 unknown robot; 409 `finish or pause the session on map X` otherwise). Publishes the retained `{enabled: false, session_id: null, map: null, force: true, issued_at}`; `force` makes the robot apply it even if unchanged (it also ends a local `~/set_enabled` override). Returns `robot_notified`, `mapping_state` |
| `WS /ws/robot/{r}` | `{type: "mapping_state_update", robot_name, timestamp, mapping_state}` on every state message |

`mapping_state`: null (nothing received since the API started: the topomap never connected, or
runs a build without the switch) or `{status: "on"|"off"|"unreachable", online, enabled,
session_id, map, nodes_sent, since, stamp, source: "mqtt"|"local"|"startup", received_at}`.
`unreachable` = the robot's last will (or clean shutdown): its topomap service is not running.
The robot confirmed a session when `mapping_state.session_id` equals the open session's id and
`enabled` matches (`true` while mapping, `false` while paused).

#### `WS /ws/map/{map_id}`
WebSocket endpoint for real-time map updates.

Connect to this endpoint after loading a map to receive updates from the graph builder service.

**Example (JavaScript):**
```javascript
const ws = new WebSocket('ws://localhost:8000/ws/map/warehouse_floor_1');

ws.onmessage = (event) => {
  const update = JSON.parse(event.data);
  console.log('Map update:', update);
};
```

### Image Operations

#### `GET /api/v1/images/{image_id}?node_id={node_id}&map_id={map_id}`
Retrieve an image from the image database.

**Parameters:**
- `image_id` (path): Image ID
- `node_id` (query, required): Node ID
- `map_id` (query, optional): Map ID (uses default if not provided)

**Response:**
Binary image data (JPEG)

### Navigation Operations

#### `POST /api/v1/navigate`
Request navigation for a robot (proxy to mission planner).

**Request:**
```json
{
  "robot_name": "carter01",
  "target_x": 10.5,
  "target_y": 20.3,
  "mission_name": "delivery_mission_001",
  "timeout_seconds": 300
}
```

**Response:**
```json
{
  "success": true,
  "mission_name": "delivery_mission_001"
}
```

### Status Operations

#### `GET /api/v1/robots/{robot_name}/status`
Get robot status (proxy to Mission Dispatcher database).

**Response:**
```json
{
  "name": "carter01",
  "status": {
    "position": {
      "x": 5.2,
      "y": 10.1,
      "theta": 1.57
    },
    "battery": 85.5,
    "state": "IDLE"
  }
}
```

#### `GET /api/v1/missions/{mission_name}/status`
Get mission status (proxy to Mission Dispatcher database).

**Response:**
```json
{
  "name": "delivery_mission_001",
  "status": {
    "state": "RUNNING",
    "progress": 0.45,
    "current_node": 5
  }
}
```

#### `WS /ws/mission/{mission_name}`
WebSocket endpoint for real-time mission status updates.

#### `WS /ws/robot/{robot_name}`
WebSocket endpoint for real-time robot status updates.

## Usage

### Starting the Service

```bash
cd packages/api
python main.py --host 0.0.0.0 --port 8000 --log-level info
```

**Command-line arguments:**
- `--host`: Host to bind to (default: 0.0.0.0)
- `--port`: Port to bind to (default: 8000)
- `--log-level`: Logging level (default: info)

**Environment variables:**
- `GRAPH_DB_URL`: Graph database service URL (default: http://localhost:6001)
- `IMAGE_DB_URL`: Image database service URL (default: http://localhost:6002)
- `MISSION_PLANNER_URL`: Mission planner service URL (default: http://localhost:8005)
- `DATABASE_URL`: Mission database service URL (default: http://localhost:5000)
- `DEFAULT_MAP_ID`: Default map ID (default: default)

### Example Client Usage

#### Python Client

```python
import requests
import websocket
import json

# Base URL
BASE_URL = "http://localhost:8000"

# Load a map
response = requests.post(f"{BASE_URL}/api/v1/map/load", json={
    "map_id": "warehouse_floor_1"
})
print(response.json())

# Connect to map updates WebSocket
ws = websocket.WebSocket()
ws.connect("ws://localhost:8000/ws/map/warehouse_floor_1")

# Request navigation
response = requests.post(f"{BASE_URL}/api/v1/navigate", json={
    "robot_name": "carter01",
    "target_x": 10.5,
    "target_y": 20.3
})
print(response.json())

# Get robot status
response = requests.get(f"{BASE_URL}/api/v1/robots/carter01/status")
print(response.json())

# Get image
response = requests.get(f"{BASE_URL}/api/v1/images/node_001_front", params={
    "node_id": "1",
    "map_id": "warehouse_floor_1"
})
with open("image.jpg", "wb") as f:
    f.write(response.content)
```

#### JavaScript Client

```javascript
// Load a map
fetch('http://localhost:8000/api/v1/map/load', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ map_id: 'warehouse_floor_1' })
})
.then(response => response.json())
.then(data => console.log(data));

// Connect to map updates
const mapWs = new WebSocket('ws://localhost:8000/ws/map/warehouse_floor_1');
mapWs.onmessage = (event) => {
  const update = JSON.parse(event.data);
  console.log('Map update:', update);
};

// Request navigation
fetch('http://localhost:8000/api/v1/navigate', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    robot_name: 'carter01',
    target_x: 10.5,
    target_y: 20.3
  })
})
.then(response => response.json())
.then(data => console.log(data));

// Connect to mission status updates
const missionWs = new WebSocket('ws://localhost:8000/ws/mission/delivery_mission_001');
missionWs.onmessage = (event) => {
  const status = JSON.parse(event.data);
  console.log('Mission status:', status);
};
```

## Integration with Other Services

The API Delegation Service integrates with:

1. **Graph Database Service** (`packages/topomap_dbs/graph_db`)
   - Queries map data
   - Retrieves node and edge information

2. **Image Database Service** (`packages/topomap_dbs/image_db`)
   - Retrieves images associated with nodes

3. **Mission Planner Service** (`packages/services/mission_planner`)
   - Proxies navigation requests
   - Handles path planning

4. **Mission Dispatcher** (`packages/database`)
   - Queries robot status
   - Queries mission status
   - Monitors mission progress

5. **Graph Builder Service** (`packages/services/graph_builder`)
   - Receives map updates via WebSocket
   - Broadcasts to connected clients

## Development

### Running Tests

```bash
# TODO: Add tests
```

### Docker Deployment

```bash
# TODO: Add Dockerfile and docker-compose configuration
```

## License

SPDX-FileCopyrightText: NVIDIA CORPORATION & AFFILIATES
Copyright (c) 2021-2024 NVIDIA CORPORATION & AFFILIATES. All rights reserved.

Licensed under the Apache License, Version 2.0.

