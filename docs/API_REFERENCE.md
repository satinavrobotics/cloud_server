# Robot Fleet Management System - API Reference for Frontend Developers

**Version:** 1.0.0  
**Base URL:** `http://localhost:8000` (API Delegation Service)

## Table of Contents

1. [Overview](#overview)
2. [Authentication](#authentication)
3. [REST API Endpoints](#rest-api-endpoints)
   - [Map Operations](#map-operations)
   - [Image Operations](#image-operations)
   - [Navigation Operations](#navigation-operations)
   - [Robot Management](#robot-management)
   - [Fleet Settings](#fleet-settings)
   - [Mission Management](#mission-management)
   - [Detection Results](#detection-results)
4. [WebSocket Endpoints](#websocket-endpoints)
5. [Data Schemas](#data-schemas)
6. [Error Handling](#error-handling)
7. [Integration Patterns](#integration-patterns)

---

## Overview

The Robot Fleet Management System provides a unified API through the **API Delegation Service**, which acts as a central gateway for all client interactions. This service proxies requests to specialized backend microservices while providing a consistent interface.

### Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              API Delegation Service (Port 8000)              │
│                    (Single Entry Point)                      │
├─────────────────────────────────────────────────────────────┤
│  REST Endpoints          │  WebSocket Endpoints             │
│  - Map loading           │  - Real-time map updates         │
│  - Image retrieval       │  - Mission status updates        │
│  - Navigation requests   │  - Robot status updates          │
│  - Robot management      │                                  │
│  - Mission management    │                                  │
└─────────────────────────────────────────────────────────────┘
         │                │                │
         ▼                ▼                ▼
┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│  Graph DB    │  │  Mission     │  │  Graph       │
│  Service     │  │  Dispatcher  │  │  Builder     │
│  (ArangoDB + │  │  (VDA5050)   │  │  Service     │
│   R-tree)    │  │              │  │              │
└──────────────┘  └──────────────┘  └──────────────┘
```

### Backend Services

The API Delegation Service integrates with the following backend services:

- **Graph Database Service**: Manages topological maps with ArangoDB (persistent storage) and R-tree (in-memory spatial index)
- **Image Database Service**: Stores robot camera images in MinIO object storage
- **Mission Planner Service**: Plans navigation paths and creates missions
- **Mission Dispatcher Service**: Executes missions using VDA5050 protocol via MQTT
- **Graph Builder Service**: Builds topological maps from robot sensor data
- **Similarity Service**: Validates edge traversability between nodes

---

## Authentication

**Current Status:** No authentication required (development mode)

**Production Recommendations:**
- Implement API key authentication
- Use OAuth 2.0 / OIDC for user authentication
- Enable TLS/SSL for all connections
- Implement rate limiting

---

## REST API Endpoints

### Service Information

#### `GET /`

Get service information and available endpoints.

**Response:**
```json
{
  "service": "API Delegation Service",
  "version": "1.0.0",
  "description": "Central API gateway for robot fleet management",
  "endpoints": {
    "health": "GET /health",
    "stats": "GET /stats",
    "load_map": "POST /api/v1/map/load",
    "get_image": "GET /api/v1/images/{image_id}",
    "navigate": "POST /api/v1/navigate",
    "explore": "POST /api/v1/explore",
    "robots": { ... },
    "missions": { ... },
    "websockets": { ... }
  }
}
```

#### `GET /health`

Health check endpoint.

**Response:**
```json
{
  "status": "healthy",
  "service": "api_delegation",
  "dependencies": {},
  "details": {}
}
```

#### `GET /stats`

Get service statistics including WebSocket connection counts.

**Response:**
```json
{
  "service": "API Delegation Service",
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

---

### Map Operations

#### `POST /api/v1/map/load`

Load a topological map from the graph database.

**Request Body:**
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
    "edge_count": 420
  },
  "message": "Map loaded successfully"
}
```

**Error Response:**
```json
{
  "success": false,
  "map_id": "warehouse_floor_1",
  "error": "Map not found"
}
```

**Backend Service:** Graph Database Service

---

### Map Location and Relocalization

Details: `docs/satinav-maps-redesign.md` §12 and §16.

#### `PUT /api/v1/maps/{map_id}/approx_location`

Set the approximate location of a `local` map (map pins, distance sorting). A hint only: placement, alignment and sessions never read it.

**Request Body:**
```json
{
  "latitude": 47.4979,
  "longitude": 19.0402,
  "accuracy_m": 25,
  "source": "manual"
}
```

`accuracy_m` is optional; `source` is `manual` (default) or `robot`. The server sets `set_at`.

**Response:**
```json
{
  "success": true,
  "map_id": "warehouse_floor_1",
  "approx_location": {"latitude": 47.4979, "longitude": 19.0402, "accuracy_m": 25.0, "source": "manual", "set_at": "2026-10-01T12:00:00+00:00"}
}
```

**Errors:** 404 unknown map; 409 geo map (its location is its datum, see `transform` in the map summary); 422 for (0, 0) or out-of-range values. `GET /api/v1/maps` and `GET /api/v1/maps/{map_id}` return the stored `approx_location` unchanged.

#### `GET /api/v1/maps/{map_id}/reloc?robot={robot_name}`

Does the robot's orchestrator hold a stored map for this map, so it can relocalize itself without a manual initial position? Works before any session exists. The answer is cached for `RELOC_MAP_HELD_TTL_S` (15 s).

**Response:**
```json
{"available": true, "known": true, "source": "orchestrator", "can_start": true, "can_start_reason": null}
```

- `can_start` / `can_start_reason` (additive): `true` when the API can START relocalization on the robot: it is online, its orchestrator answers and lists a relocalization service (`RELOC_SERVICE_CANDIDATES`, default `odin_reloc`; the sim has none) and holds the map. Otherwise `false` with a readable reason. See "Starting relocalization" below.
- `available: true`: no manual initial position needed; place with `POST /api/v1/maps/{map_id}/sessions/{session_id}/place` and `{"source": "reloc"}`.
- `known: false`: the orchestrator could not be asked (unknown or offline robot, no orchestrator address, unreachable or error); `available` is then `false`.
- A geo map is placed by its datum: `{"available": false, "known": true, "source": "orchestrator", "can_start": false, "can_start_reason": "..."}`.
- 404 unknown map.

`GET /api/v1/maps/{map_id}/sessions/{session_id}/placement-suggestions` carries the same object as `reloc` (`null` for a geo map or a placed or finished session).

#### `POST /api/v1/maps/{map_id}/sessions/{session_id}/place` with `source: "reloc"`

`source` is `last_position` (accepting a suggestion, with `pose` and `robot_pose`) or `reloc`. With `reloc` the body is just:
```json
{"source": "reloc"}
```
The robot relocalizes itself on the stored map its orchestrator holds. The server places the session with the identity `map_T_session` (assumption D0: the session frame equals the robot's map frame for a map built in that session) and records the robot's current pose. The robot-still check is skipped.

**Errors:** 404 unknown map, session or robot; 409 session finished or already placed, geo map, robot offline, robot reports `position_initialized: false`, or the orchestrator does not hold the map (a fresh check is made, an unknown answer counts as not held); 422 on a bad body. `reloc` is not accepted when starting a session (422): start the session, then place it. A low localization score never refuses a placement; it shows as `localization_warning` (below).

This is the **check-only** behaviour, and it is exactly what happens while `reloc.can_start` is `false` and no `init_pose` is sent (200, identity placement, no call to the robot).

#### Starting relocalization (three modes)

1. **Manual placement**: unchanged (`pose` + `robot_pose`, or `source: "last_position"`).
2. **Odin alone**: `{"source": "reloc"}` while `reloc.can_start` is `true`.
3. **Odin assisted by a pose**: `{"source": "reloc", "reloc": {"init_pose": {"x": 2.0, "y": -1.0, "yaw": 0.5}}}` (cloud map frame, metres / radians CCW). Needs `can_start`: **409** otherwise (no robot call).

With `can_start` the answer is **202** and a background job:
```json
{"map_id": "shed", "map_state": "ready", "changed": false, "session": {"...": "unplaced"},
 "reloc_job": {"id": "…", "state": "preparing", "step": "checking", "mode": "odin",
               "started_at": "2026-10-04T10:00:00+00:00", "deadline": "2026-10-04T10:01:30+00:00"}}
```
`mode` is `odin` or `assisted`. The job: re-checks, finds the stored map on the robot, `PATCH /maps/{name}` `init_pos` (`null` for `odin`: this also clears a stale seed and drops a hand-set value; `[x, y, 0, qx, qy, qz, qw]` from the yaw for `assisted`), `PUT /robot/config/map`, stops (if running) and starts the relocalization service, then waits for the robot to report `position_initialized: true` (`RELOC_JOB_TIMEOUT_S`, default 90 s, from the start of the job), and finally places the session (identity `map_T_session`, `placement.source: "reloc"`, plus `placement.init_pose` for `assisted`; `MAP.SESSION_PLACED`). The robot's pose is read when it reports itself initialized.

- `GET /api/v1/maps/{map_id}/sessions/{session_id}/reloc-job` returns `{id, state, step, mode, started_at, deadline, error?, position_initialized?, localization_score?}`; `state` is `preparing`, `starting`, `waiting`, `placed`, `failed` or `cancelled`. 404 when there is no job (jobs are kept **in memory** and are lost when the API restarts; a lost job's robot-side service keeps running and nothing is placed).
- `POST /api/v1/maps/{map_id}/sessions/{session_id}/unplace` is a **developer/test hook** (also usable as "redo my placement"): it marks a placed operate session on a local map as unplaced (`placement.unplaced_reason: "manual"`, the old `map_T_session` is kept) and emits `MAP.SESSION_UNPLACED`. The system unplaces by itself when the robot's run changes. 409 on a finished or mapping session, a geo map, or while a relocalization job runs; an already unplaced session returns `changed: false`.
- `DELETE` on the same URL cancels a running job: the previous `init_pos` and current map are restored on the robot; the relocalization service is **not** stopped. 409 when it has finished.
- **409 (at the POST)**: robot offline, **driving**, has an open **mapping session**, another reloc job or a pending SLAM save for the robot, the map not held, or `init_pose` without `can_start`. A robot that reports `position_initialized: false` is *not* refused (the job is about to fix that).
- **`failed`** (`error` says why): the orchestrator's 409 / 502 / 504 are spelled out (for example "another service is probably holding the Odin USB device"); a failed PATCH / PUT / start restores the previous `init_pos` and current map (best effort); the robot going offline, or the session finished, replaced or placed meanwhile, fails the job with no placement; on **timeout** the relocalization service is left running and nothing is restored.
- The frame is the D0 assumption (identity), kept in `map_sessions.reloc_map_t_session()` / `reloc_bin_pose()`; see docs/satinav-maps-redesign.md section 16 for the open questions (a map saved by a later session, the robot's frame after a reloc-mode driver start).

#### Robot view additions

`GET /api/v1/robots[/{robot_name}]` and the WebSocket `robot_update`:

- `status.position_initialized` (boolean or `null`) and `status.localization_score` (0..1 or `null`): VDA5050 `agvPosition` as last reported.
- `status.approx_position`: `{latitude, longitude, accuracy_m?, fix_quality?, source, stamp?, stored_at}` or `null`, from the robot's MQTT `approx_position` topic. Display only, never used for placement.
- `session.placement_source`: how a placed session was placed (`user`, `last_position`, `reloc`, `session` or `datum`), else `null`.
- `localization_warning`: a reason string, or `null`. Set only for a placed `reloc` session when the robot reports `position_initialized: false` or a `localization_score` below `RELOC_DEGRADED_SCORE` (default 0.3). Informational: the session stays placed. The score is provisional: the robot's current value is a GNSS-sigma stopgap, not a map-matching score.

The robot spec also has `datum_changed_at` and `datum_stamp` (see `docs/MQTT_MISSION_INTEGRATION.md`).

#### Orchestrator proxy and map links

`POST /api/v1/orchestration/{robot}/maps/{name}/save` adds `cloud_map_id` (the open mapping session's map) and `cloud_session_id` to the body when the robot has an open mapping session and the caller did not send them (an explicit `null` counts as not sent). This links the saved map to the cloud map for `reloc`. After any proxied non-GET `maps/*` call the cached held-map answer for that robot is dropped.

**Environment variables:** `RELOC_MAP_HELD_TTL_S` (default 15), `RELOC_DEGRADED_SCORE` (default 0.3), `RELOC_SERVICE_CANDIDATES` (default `odin_reloc`), `RELOC_JOB_TIMEOUT_S` (default 90), `RELOC_JOB_POLL_S` (default 1), `RELOC_JOB_SETTLE_S` (default 5).

---

### Image Operations

#### `GET /api/v1/images/{map_id}/{node_id}`

Retrieve an image associated with a specific node.

**Path Parameters:**
- `map_id` (string): Map identifier
- `node_id` (string): Node identifier

**Query Parameters:**
- `image_id` (string, optional): Specific image ID. If not provided, returns the first available image for the node.

**Response:**
- Content-Type: `image/jpeg`
- Binary image data

**Example:**
```javascript
// Fetch image for node 1001 in warehouse map
fetch('http://localhost:8000/api/v1/images/warehouse_floor_1/1001')
  .then(response => response.blob())
  .then(blob => {
    const imageUrl = URL.createObjectURL(blob);
    document.getElementById('robot-view').src = imageUrl;
  });
```

**Error Responses:**
- `404 Not Found`: Image not found
- `503 Service Unavailable`: Service not initialized

**Backend Service:** Image Database Service (MinIO)

---

### Navigation Operations

#### `POST /api/v1/navigate`

Request navigation for a robot to a target location.

**Request Body:**
```json
{
  "robot_name": "carter01",
  "target_x": 10.5,
  "target_y": 20.3,
  "mission_name": "delivery_mission_001",
  "timeout_seconds": 300
}
```

**Request Fields:**
- `robot_name` (string, required): Name of the robot
- `target_x` (float, required): Target X coordinate in meters
- `target_y` (float, required): Target Y coordinate in meters
- `mission_name` (string, optional): Custom mission name (auto-generated if not provided)
- `timeout_seconds` (integer, optional): Mission timeout in seconds (default: 300)

**Response:**
```json
{
  "success": true,
  "mission_name": "delivery_mission_001"
}
```

**Error Response:**
```json
{
  "success": false,
  "error": "Robot 'carter01' not found"
}
```

**Workflow:**
1. API validates robot exists
2. Request forwarded to Mission Planner Service
3. Mission Planner finds path using Graph Database
4. Mission submitted to Mission Dispatcher
5. Mission Dispatcher sends VDA5050 commands to robot via MQTT
6. Client monitors progress via WebSocket (`/ws/mission/{mission_name}`)

**Backend Services:** Mission Planner → Mission Dispatcher → Robot (MQTT)

---

#### `POST /api/v1/explore`

Request exploration action for a robot.

**Request Body:**
```json
{
  "robot_name": "carter01",
  "timeout_seconds": 600
}
```

**Response:**
```json
{
  "success": true,
  "mission_name": "explore_carter01_20240115_103000",
  "robot_name": "carter01",
  "timeout_seconds": 600
}
```

**Use Case:** Robot explores the environment, building the topological map by capturing images and creating nodes.

**Backend Services:** Mission Dispatcher

---

### Robot Management

#### `GET /api/v1/robots`

List all robots with optional filtering.

**Query Parameters:**
- `min_battery` (float, optional): Minimum battery level (0-100)
- `max_battery` (float, optional): Maximum battery level (0-100)
- `state` (string, optional): Robot state filter (`IDLE`, `ON_TASK`, `CHARGING`, `MAP_DEPLOYMENT`, `TELEOP`)
- `online` (boolean, optional): Online status filter
- `robot_type` (string, optional): Robot type filter (`FORKLIFT`, `CARRIER`)

**Response:**
```json
[
  {
    "name": "carter01",
    "labels": ["warehouse", "floor1"],
    "battery": {
      "critical_level": 10.0,
      "recommended_minimum": 20.0,
      "recommended_maximum": 80.0
    },
    "heartbeat_timeout": 30.0,
    "switch_teleop": false,
    "status": {
      "pose": {
        "x": 10.5,
        "y": 20.3,
        "theta": 1.57
      },
      "online": true,
      "battery_level": 85.5,
      "state": "IDLE",
      "software_version": {
        "os": "Ubuntu 22.04",
        "app": "1.2.3"
      },
      "hardware_version": {
        "manufacturer": "NVIDIA",
        "serial_number": "SN12345"
      },
      "errors": {}
    },
    "lifecycle": "ALIVE"
  }
]
```

**Example:**
```javascript
// Get all robots with battery > 50% that are online
fetch('http://localhost:8000/api/v1/robots?min_battery=50&online=true')
  .then(response => response.json())
  .then(robots => console.log(robots));
```

**Backend Service:** Mission Dispatcher Database

---

#### `GET /api/v1/robots/{robot_name}`

Get detailed information about a specific robot.

**Path Parameters:**
- `robot_name` (string): Robot identifier

**Response:**
```json
{
  "name": "carter01",
  "labels": ["warehouse", "floor1"],
  "battery": {
    "critical_level": 10.0
  },
  "status": {
    "pose": {
      "x": 10.5,
      "y": 20.3,
      "theta": 1.57
    },
    "online": true,
    "battery_level": 85.5,
    "state": "IDLE"
  }
}
```

**Error Response:**
- `404 Not Found`: Robot not found

---

#### `GET /api/v1/robots/{robot_name}/status`

Get the current status of a robot.

**Response:** the robot's `status` object only (the `status` field of
`GET /api/v1/robots/{robot_name}`), mirroring `GET /api/v1/missions/{name}/status`.
It can be PUT back as `{"status": ...}` without clobbering anything.

---

#### `POST /api/v1/robots`

Create a new robot.

**Request Body:**
```json
{
  "name": "carter02",
  "labels": ["warehouse", "floor2"],
  "battery": {
    "critical_level": 10.0,
    "recommended_minimum": 20.0,
    "recommended_maximum": 80.0
  },
  "heartbeat_timeout": 30.0
}
```

**Required Fields:**
- `name` (string): Unique robot identifier

**Optional Fields:**
- `labels` (array of strings): Robot labels for grouping
- `battery` (object): Battery specifications
- `heartbeat_timeout` (number): Timeout in seconds for robot heartbeat

**Response:**
```json
{
  "name": "carter02",
  "labels": ["warehouse", "floor2"],
  "status": {
    "pose": { "x": 0, "y": 0, "theta": 0 },
    "online": false,
    "battery_level": 0,
    "state": "IDLE"
  },
  "lifecycle": "ALIVE"
}
```

**Error Response:**
- `400 Bad Request`: Missing required field or validation error

---

#### `PUT /api/v1/robots/{robot_name}`

Update robot specification.

**Request Body:**
```json
{
  "labels": ["warehouse", "floor2", "updated"]
}
```

**Response:**
```json
{
  "name": "carter02",
  "labels": ["warehouse", "floor2", "updated"],
  "status": { ... }
}
```

**Note:** This endpoint updates the robot's **specification** (labels, battery config, etc.), not the status. Robot status is updated by the Mission Dispatcher based on MQTT messages from the robot.

---

#### `DELETE /api/v1/robots/{robot_name}`

Delete a robot.

**Response:**
```json
{
  "success": true,
  "message": "Robot carter02 deleted"
}
```

**Error Response:**
- `404 Not Found`: Robot not found

---

### Fleet Settings

A single, fleet-wide, operator-editable settings object — not tied to any
one robot/mission/map. Always stored under the fixed name `"global"`
(`SettingsObjectV1`, `cloud_common/objects/settings.py`): a singleton
simulated by convention on top of the normal name-keyed Postgres storage,
since there's no separate keyless/singleton storage mode. Auto-created
with defaults on first `GET` or `PUT` if it doesn't exist yet.

#### `GET /api/v1/settings`

Fetch the fleet-wide settings.

**Response:**
```json
{
  "name": "global",
  "lifecycle": "ALIVE",
  "fault_error_types": [],
  "status": {}
}
```

**Backend Service:** Mission Dispatcher Database (Postgres)

---

#### `PUT /api/v1/settings`

Update the fleet-wide settings. Unknown fields are ignored; `name`,
`status`, and `lifecycle` in the request body are ignored too (this is a
spec-only update, same convention as `PUT /api/v1/robots/{robot_name}`).

**Request Body:**
```json
{
  "fault_error_types": ["motorStalledError"]
}
```

**`fault_error_types`:** VDA5050 `errorType` strings severe enough that a
robot reporting one should be badged FAULT by clients (see
`sati-client`'s `utils/robotStatus.ts`). Any other `errorType` a robot
reports is treated as a non-fault warning by clients. Empty by default —
nothing is FAULT until an operator opts specific error types in here.

Separate from FAULT classification, mission-dispatch holds a PENDING mission
(`held`/`held_reason`) while the robot reports one of these standing readiness
`errorType`s: `robotBaseNotReadyError` ("Robot base is not responding");
`navigationNotReadyError`, `poseHealthNotReadyError`, `tfChainNotReadyError`
("Robot navigation is not ready"). The reason is a fixed string, and the hold
releases when the error disappears from the robot's state.

**Response:** Same shape as `GET /api/v1/settings`, reflecting the update.

**Backend Service:** Mission Dispatcher Database (Postgres)

---

### Mission Management

#### `GET /api/v1/missions`

List all missions.

**Response:**
```json
[
  {
    "name": "delivery_mission_001",
    "robot": "carter01",
    "mission_tree": [
      {
        "name": "navigate_to_target",
        "parent": "root",
        "route": {
          "waypoints": [
            { "x": 5.0, "y": 10.0, "theta": 0.0 },
            { "x": 10.5, "y": 20.3, "theta": 1.57 }
          ]
        }
      }
    ],
    "timeout": 300.0,
    "status": {
      "state": "RUNNING",
      "progress": 0.45,
      "node_status": {
        "root": {
          "state": "RUNNING",
          "progress": 0.45
        },
        "navigate_to_target": {
          "state": "RUNNING",
          "progress": 0.45
        }
      }
    },
    "lifecycle": "ALIVE"
  }
]
```

---

#### `GET /api/v1/missions/{mission_name}`

Get detailed information about a specific mission.

**Path Parameters:**
- `mission_name` (string): Mission identifier

**Response:**
```json
{
  "name": "delivery_mission_001",
  "robot": "carter01",
  "mission_tree": [ ... ],
  "timeout": 300.0,
  "status": {
    "state": "RUNNING",
    "progress": 0.45,
    "start_time": "2024-01-15T10:30:00Z",
    "node_status": { ... }
  }
}
```

**Error Response:**
- `404 Not Found`: Mission not found

---

#### `GET /api/v1/missions/{mission_name}/status`

Get current status of a mission (alias for GET /api/v1/missions/{mission_name}).

---

#### `POST /api/v1/missions`

Create a new mission.

**Request Body:**
```json
{
  "name": "custom_mission_001",
  "robot": "carter01",
  "mission_tree": [
    {
      "name": "move_forward",
      "parent": "root",
      "move": {
        "distance": 5.0,
        "direction": "forward"
      }
    },
    {
      "name": "capture_image",
      "parent": "root",
      "action": {
        "action_type": "capture_image",
        "action_parameters": {
          "camera": "front"
        }
      }
    }
  ],
  "timeout": 300
}
```

**Required Fields:**
- `name` (string): Unique mission identifier
- `robot` (string): Robot name
- `mission_tree` (array): List of mission nodes (tasks)

**Mission Node Types:**
- `route`: Navigate through waypoints
- `move`: Relative movement (distance/rotation)
- `action`: Execute robot action
- `notify`: API callback
- `selector`: Choose first successful child
- `sequence`: Execute children in order

**Response:**
```json
{
  "name": "custom_mission_001",
  "robot": "carter01",
  "mission_tree": [ ... ],
  "status": {
    "state": "PENDING",
    "progress": 0.0
  },
  "lifecycle": "ALIVE"
}
```

**Error Response:**
- `400 Bad Request`: Missing required fields or validation error

---

#### `PUT /api/v1/missions/{mission_name}`

Update a mission.

Editing the spec (`robot`, `mission_tree`, `timeout`, `deadline`, `repeat`, `then_run`,
`register_map`, `mode`, `planned_path`) is only allowed while the mission is `PENDING`;
any other state answers `409`, and an invalid spec answers `400` with the validation
message. `name` cannot be changed (it is the mission's identity), so a rename or an edit
of a mission that has already run is a new mission. A new `mission_tree` gets its
`status.node_status` entries created and the dropped ones removed. The dispatcher applies
the edit to a queued (or held, not yet dispatched) mission; once the mission has been
dispatched a late edit is ignored. `update_nodes` (a reroute of a running mission) is not
an edit and is not restricted this way.

**Request Body:**
```json
{
  "timeout": 600,
  "repeat": 3,
  "then_run": "dock_check"
}
```

**Response:**
```json
{
  "name": "custom_mission_001",
  "timeout": 600,
  "status": { ... }
}
```

---

#### `DELETE /api/v1/missions/{mission_name}`

Delete a mission.

**Response:**
```json
{
  "success": true,
  "message": "Mission custom_mission_001 deleted"
}
```

---

#### `POST /api/v1/missions/{mission_name}/cancel`

Cancel an active mission.

**Response:**
```json
{
  "success": true,
  "message": "Mission custom_mission_001 cancelled"
}
```

**Error Response:**
- `400 Bad Request`: Mission cannot be cancelled (already completed/failed)

---

### Detection Results

#### `GET /api/v1/detection_results`

List all detection results from robot object detectors.

**Response:**
```json
[
  {
    "name": "detection_carter01_20240115",
    "status": {
      "detected_objects": [
        {
          "object_id": 1,
          "class_id": "person",
          "bbox2d": {
            "center": { "x": 320, "y": 240, "theta": 0 },
            "size_x": 100,
            "size_y": 200
          }
        }
      ]
    }
  }
]
```

---

#### `GET /api/v1/detection_results/{name}`

Get specific detection results.

**Response:**
```json
{
  "name": "detection_carter01_20240115",
  "status": {
    "detected_objects": [ ... ]
  }
}
```

---

#### `DELETE /api/v1/detection_results/{name}`

Delete detection results.

**Response:**
```json
{
  "success": true,
  "message": "Detection results deleted"
}
```

---

## WebSocket Endpoints

WebSocket connections provide real-time updates for maps, missions, and robots. All WebSocket endpoints are proxied through the API Delegation Service.

### Connection Pattern

```javascript
const ws = new WebSocket('ws://localhost:8000/ws/{endpoint}');

ws.onopen = () => {
  console.log('Connected');
};

ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  console.log('Update received:', data);
};

ws.onerror = (error) => {
  console.error('WebSocket error:', error);
};

ws.onclose = () => {
  console.log('Connection closed');
};
```

---

### `WS /ws/map/{map_id}`

Subscribe to real-time map updates for a specific map.

**Path Parameters:**
- `map_id` (string): Map identifier

**Message Types:**

#### Node Added
```json
{
  "type": "node_added",
  "map_id": "warehouse_floor_1",
  "node": {
    "node_id": 1001,
    "x": 10.5,
    "y": 20.3,
    "yaw": 1.57,
    "metadata": {
      "robot_id": "carter01",
      "timestamp": "2024-01-15T10:30:00Z"
    }
  },
  "edges": [
    {
      "from_node_id": 1001,
      "to_node_id": 1000,
      "metadata": {
        "distance": 5.2,
        "created_at": "2024-01-15T10:30:00Z"
      }
    }
  ],
  "timestamp": "2024-01-15T10:30:00Z"
}
```

**Use Cases:**
- Display real-time map building progress
- Update map visualization as robots explore
- Show new nodes and edges as they are created

**Example:**
```javascript
const mapWs = new WebSocket('ws://localhost:8000/ws/map/warehouse_floor_1');

mapWs.onmessage = (event) => {
  const update = JSON.parse(event.data);

  if (update.type === 'node_added') {
    // Add node to map visualization
    addNodeToMap(update.node);

    // Add edges to map visualization
    update.edges.forEach(edge => addEdgeToMap(edge));
  }
};
```

**Backend Service:** Graph Builder Service (proxied through API Delegation)

---

### `WS /ws/mission/{mission_name}`

Subscribe to real-time mission status updates.

**Path Parameters:**
- `mission_name` (string): Mission identifier

**Message Format:**
```json
{
  "type": "mission_update",
  "mission_name": "delivery_mission_001",
  "status": {
    "state": "RUNNING",
    "progress": 0.65,
    "current_node": "navigate_to_target",
    "node_status": {
      "root": {
        "state": "RUNNING",
        "progress": 0.65
      },
      "navigate_to_target": {
        "state": "RUNNING",
        "progress": 0.65
      }
    }
  },
  "timestamp": "2024-01-15T10:35:00Z"
}
```

**Mission States:**
- `PENDING`: Mission created but not started
- `RUNNING`: Mission in progress
- `COMPLETED`: Mission completed successfully
- `FAILED`: Mission failed
- `CANCELED`: Mission was cancelled

**Example:**
```javascript
const missionWs = new WebSocket('ws://localhost:8000/ws/mission/delivery_mission_001');

missionWs.onmessage = (event) => {
  const update = JSON.parse(event.data);

  // Update progress bar
  document.getElementById('progress').value = update.status.progress;

  // Update status text
  document.getElementById('status').textContent = update.status.state;

  // Check if mission completed
  if (update.status.state === 'COMPLETED') {
    console.log('Mission completed successfully!');
    missionWs.close();
  }
};
```

**Backend Service:** Mission Dispatcher (proxied through API Delegation)

---

### `WS /ws/robot/{robot_name}`

Subscribe to real-time robot status updates.

**Path Parameters:**
- `robot_name` (string): Robot identifier

**Message Format:**
```json
{
  "type": "robot_update",
  "robot_name": "carter01",
  "status": {
    "pose": {
      "x": 12.3,
      "y": 21.5,
      "theta": 1.60
    },
    "online": true,
    "battery_level": 82.3,
    "state": "ON_TASK",
    "errors": {}
  },
  "timestamp": "2024-01-15T10:35:00Z"
}
```

**Robot States:**
- `IDLE`: Robot is idle and available
- `ON_TASK`: Robot is executing a mission
- `CHARGING`: Robot is charging
- `MAP_DEPLOYMENT`: Robot is deploying a map
- `TELEOP`: Robot is in teleoperation mode

**Example:**
```javascript
const robotWs = new WebSocket('ws://localhost:8000/ws/robot/carter01');

robotWs.onmessage = (event) => {
  const update = JSON.parse(event.data);

  // Update robot position on map
  updateRobotPosition(update.status.pose);

  // Update battery indicator
  document.getElementById('battery').textContent =
    `${update.status.battery_level.toFixed(1)}%`;

  // Check for errors
  if (Object.keys(update.status.errors).length > 0) {
    console.error('Robot errors:', update.status.errors);
  }
};
```

**Backend Service:** Mission Dispatcher (proxied through API Delegation)

---

## Data Schemas

### Robot Object

```typescript
interface RobotObject {
  name: string;
  labels: string[];
  battery: {
    critical_level: number;
    recommended_minimum?: number;
    recommended_maximum?: number;
  };
  heartbeat_timeout: number;  // seconds
  switch_teleop: boolean;
  status: {
    pose: {
      x: number;
      y: number;
      theta: number;  // radians
    };
    software_version: {
      os: string;
      app: string;
    };
    hardware_version: {
      manufacturer: string;
      serial_number: string;
    };
    factsheet: {
      agv_class: string;
      speed_max: number;
      length: number;   // footprint, metres (physicalParameters.length); -1 until reported
      width: number;    // footprint, metres (physicalParameters.width); -1 until reported
      height: number;   // metres (physicalParameters.heightMax); -1 until reported
    };
    online: boolean;
    battery_level: number;  // 0-100
    position_initialized?: boolean | null;  // VDA5050 agvPosition.positionInitialized
    localization_score?: number | null;     // 0..1, informational (provisional)
    approx_position?: {                      // from MQTT approx_position, display only
      latitude: number;
      longitude: number;
      accuracy_m?: number;
      fix_quality?: string;
      source: string;       // 'gnss'
      stamp?: string;
      stored_at?: string;   // server time of the last stored write
    } | null;
    state: 'IDLE' | 'ON_TASK' | 'CHARGING' | 'MAP_DEPLOYMENT' | 'TELEOP';
    info_messages?: object;
    errors: object;
  };
  lifecycle: 'ALIVE' | 'DELETED' | 'PENDING_DELETE';
}
```

---

### Mission Object

```typescript
interface MissionObject {
  name: string;
  robot: string;
  mission_tree: MissionNode[];
  timeout: number;  // seconds
  deadline?: string;  // ISO 8601 timestamp
  repeat: number;  // total runs of the whole mission_tree; 1 = once (default), 0 = until cancelled
  then_run?: string | null;  // mission to start (as a copy) after this one and all repeats complete
  needs_canceled: boolean;
  status: {
    passes_completed: number;  // finished passes so far (dispatcher-owned): "lap 2 of 3"
    state: 'PENDING' | 'RUNNING' | 'COMPLETED' | 'FAILED' | 'CANCELED';
    progress: number;  // 0.0 - 1.0
    start_time?: string;  // ISO 8601 timestamp
    end_time?: string;  // ISO 8601 timestamp
    failure_category?: 'ROBOT_APP' | 'TIMEOUT' | 'DEADLINE' | 'CANCELED';
    node_status: {
      [nodeName: string]: {
        state: string;
        progress: number;
        error_msg?: string;
      };
    };
  };
  lifecycle: 'ALIVE' | 'DELETED' | 'PENDING_DELETE';
}
```

---

### Repeating, chaining and waiting

- **`repeat`**: `N` runs the mission N times in total, `0` until it is cancelled. When a
  pass completes and more remain, the dispatcher runs the *same* mission object again
  in place: node statuses reset to `PENDING`, `status.run_id` renewed (so the VDA5050
  order/node ids of each pass are unique), `status.passes_completed` incremented, and
  the timeout re-armed per pass. The robot is not idled between passes. A cancel, a
  failure or a timeout in any pass ends the whole repeat.
- **`then_run`**: when the mission has completed (last pass included) the dispatcher
  creates a new mission named `{then_run}-run-{unix_ms}` as a copy of the named mission
  (same `robot`, `mission_tree`, `timeout`, `mode`, `register_map`, `repeat` and
  `then_run`) and it is queued behind whatever is already waiting. A missing mission or
  one for another robot is logged and skipped; the finished mission is never failed by
  it. Cycles (A then B then A) are allowed; cancelling the running mission breaks them.
- **`wait` action**: `{"action": {"action_type": "wait", "action_parameters":
  {"seconds": 5}}}` (`0 < seconds <= 3600`). The dispatcher runs it as a timer between
  the surrounding nodes; nothing is sent to the robot. Cancelling during a wait cancels
  the mission at once (there is no robot order to cancel). The mission timeout keeps
  running through it.
- **Several route nodes in one mission** (needed to put a wait mid-route: route, wait,
  route). The robot's `missionStatus: "completed"` now completes only the node of the
  order it came with, and the mission completes when the whole tree does.

### Mission Node Types

```typescript
interface MissionNode {
  name?: string;
  parent: string;  // Parent node name (use "root" for top-level)

  // Node type (only one should be specified)
  route?: {
    waypoints: Array<{
      x: number;
      y: number;
      theta: number;
    }>;
  };

  move?: {
    distance?: number;
    rotation?: number;
    direction?: 'forward' | 'backward';
  };

  action?: {
    action_type: string;
    action_parameters: object;
  };

  notify?: {
    url: string;
    method: 'GET' | 'POST';
    payload?: object;
  };

  selector?: object;  // Chooses first successful child
  sequence?: object;  // Executes children in order
  constant?: {
    success: boolean;
  };
}
```

**Common Action Types:**
- `capture_image`: Capture image from camera
- `wait`: Wait for specified duration
- `explore`: Explore environment
- `dock`: Dock at charging station
- `undock`: Undock from charging station

---

### Detection Results Object

```typescript
interface DetectionResultsObject {
  name: string;
  status: {
    detected_objects: Array<{
      object_id: number;
      class_id: string;
      bbox2d?: {
        center: {
          x: number;
          y: number;
          theta: number;
        };
        size_x: number;
        size_y: number;
      };
      bbox3d?: {
        center: {
          position: { x: number; y: number; z: number };
          orientation: { w: number; x: number; y: number; z: number };
        };
        size_x: number;
        size_y: number;
        size_z: number;
      };
    }>;
  };
}
```

---

### Map Node

```typescript
interface MapNode {
  node_id: number | string;
  x: number;  // meters
  y: number;  // meters
  yaw: number;  // radians (also called theta)
  metadata?: {
    map_id?: string;
    timestamp?: string;
    robot_id?: string;
    [key: string]: any;
  };
}
```

---

### Map Edge

```typescript
interface MapEdge {
  from_node_id: number | string;
  to_node_id: number | string;
  metadata?: {
    distance?: number;  // meters
    weight?: number;
    created_at?: string;
    [key: string]: any;
  };
}
```

---

## Error Handling

### HTTP Error Codes

The API uses standard HTTP status codes:

| Code | Meaning | Description |
|------|---------|-------------|
| 200 | OK | Request succeeded |
| 201 | Created | Resource created successfully |
| 204 | No Content | Request succeeded with no response body |
| 400 | Bad Request | Invalid request (missing fields, validation error) |
| 404 | Not Found | Resource not found |
| 500 | Internal Server Error | Server error |
| 503 | Service Unavailable | Service not initialized or backend unavailable |

---

### Error Response Format

All error responses follow this format:

```json
{
  "detail": "Error message describing what went wrong"
}
```

**Examples:**

```json
// 404 Not Found
{
  "detail": "Robot 'carter99' not found"
}

// 400 Bad Request
{
  "detail": "Missing required field: name"
}

// 503 Service Unavailable
{
  "detail": "Service not initialized"
}
```

---

### Error Handling Best Practices

#### 1. Always Check Response Status

```javascript
async function getRobot(robotName) {
  const response = await fetch(`http://localhost:8000/api/v1/robots/${robotName}`);

  if (!response.ok) {
    const error = await response.json();
    throw new Error(`Failed to get robot: ${error.detail}`);
  }

  return await response.json();
}
```

#### 2. Handle Network Errors

```javascript
async function navigateRobot(robotName, targetX, targetY) {
  try {
    const response = await fetch('http://localhost:8000/api/v1/navigate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        robot_name: robotName,
        target_x: targetX,
        target_y: targetY
      })
    });

    if (!response.ok) {
      const error = await response.json();
      console.error('Navigation failed:', error.detail);
      return null;
    }

    return await response.json();
  } catch (error) {
    console.error('Network error:', error);
    return null;
  }
}
```

#### 3. WebSocket Error Handling

```javascript
function connectToMissionUpdates(missionName) {
  const ws = new WebSocket(`ws://localhost:8000/ws/mission/${missionName}`);

  ws.onerror = (error) => {
    console.error('WebSocket error:', error);
  };

  ws.onclose = (event) => {
    if (event.code !== 1000) {  // 1000 = normal closure
      console.error('WebSocket closed unexpectedly:', event.code, event.reason);

      // Implement reconnection logic
      setTimeout(() => {
        console.log('Attempting to reconnect...');
        connectToMissionUpdates(missionName);
      }, 5000);
    }
  };

  return ws;
}
```

#### 4. Retry Logic for Transient Failures

```javascript
async function fetchWithRetry(url, options = {}, maxRetries = 3) {
  for (let i = 0; i < maxRetries; i++) {
    try {
      const response = await fetch(url, options);

      if (response.ok) {
        return response;
      }

      // Don't retry client errors (4xx)
      if (response.status >= 400 && response.status < 500) {
        return response;
      }

      // Retry server errors (5xx)
      console.log(`Attempt ${i + 1} failed, retrying...`);
      await new Promise(resolve => setTimeout(resolve, 1000 * (i + 1)));

    } catch (error) {
      if (i === maxRetries - 1) throw error;
      console.log(`Attempt ${i + 1} failed, retrying...`);
      await new Promise(resolve => setTimeout(resolve, 1000 * (i + 1)));
    }
  }
}
```

---

## Integration Patterns

### Pattern 1: Complete Navigation Workflow

This pattern demonstrates a complete navigation workflow from start to finish.

```javascript
class RobotNavigationClient {
  constructor(baseUrl = 'http://localhost:8000') {
    this.baseUrl = baseUrl;
    this.missionWs = null;
  }

  async navigateToTarget(robotName, targetX, targetY) {
    // Step 1: Verify robot exists and is available
    const robot = await this.getRobot(robotName);
    if (!robot) {
      throw new Error(`Robot ${robotName} not found`);
    }

    if (robot.status.state !== 'IDLE') {
      throw new Error(`Robot ${robotName} is not idle (current state: ${robot.status.state})`);
    }

    // Step 2: Request navigation
    const response = await fetch(`${this.baseUrl}/api/v1/navigate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        robot_name: robotName,
        target_x: targetX,
        target_y: targetY,
        timeout_seconds: 300
      })
    });

    if (!response.ok) {
      const error = await response.json();
      throw new Error(`Navigation failed: ${error.detail}`);
    }

    const result = await response.json();
    const missionName = result.mission_name;

    // Step 3: Monitor mission progress via WebSocket
    return new Promise((resolve, reject) => {
      this.missionWs = new WebSocket(`ws://localhost:8000/ws/mission/${missionName}`);

      this.missionWs.onmessage = (event) => {
        const update = JSON.parse(event.data);

        console.log(`Mission progress: ${(update.status.progress * 100).toFixed(1)}%`);

        if (update.status.state === 'COMPLETED') {
          console.log('Navigation completed successfully!');
          this.missionWs.close();
          resolve(update);
        } else if (update.status.state === 'FAILED') {
          console.error('Navigation failed:', update.status.failure_category);
          this.missionWs.close();
          reject(new Error(`Mission failed: ${update.status.failure_category}`));
        } else if (update.status.state === 'CANCELED') {
          console.log('Navigation was canceled');
          this.missionWs.close();
          reject(new Error('Mission canceled'));
        }
      };

      this.missionWs.onerror = (error) => {
        console.error('WebSocket error:', error);
        reject(error);
      };
    });
  }

  async getRobot(robotName) {
    const response = await fetch(`${this.baseUrl}/api/v1/robots/${robotName}`);
    if (!response.ok) return null;
    return await response.json();
  }

  async cancelNavigation(missionName) {
    const response = await fetch(`${this.baseUrl}/api/v1/missions/${missionName}/cancel`, {
      method: 'POST'
    });
    return response.ok;
  }
}

// Usage
const client = new RobotNavigationClient();

client.navigateToTarget('carter01', 10.5, 20.3)
  .then(result => console.log('Navigation completed:', result))
  .catch(error => console.error('Navigation error:', error));
```

---

### Pattern 2: Real-Time Map Visualization

This pattern shows how to build a real-time map visualization that updates as robots explore.

```javascript
class MapVisualization {
  constructor(mapId, baseUrl = 'http://localhost:8000') {
    this.mapId = mapId;
    this.baseUrl = baseUrl;
    this.nodes = new Map();
    this.edges = new Set();
    this.mapWs = null;
  }

  async initialize() {
    // Step 1: Load existing map data
    const response = await fetch(`${this.baseUrl}/api/v1/map/load`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ map_id: this.mapId })
    });

    if (!response.ok) {
      throw new Error('Failed to load map');
    }

    const mapData = await response.json();
    console.log(`Loaded map with ${mapData.stats.node_count} nodes`);

    // Step 2: Connect to real-time updates
    this.connectToUpdates();
  }

  connectToUpdates() {
    this.mapWs = new WebSocket(`ws://localhost:8000/ws/map/${this.mapId}`);

    this.mapWs.onmessage = (event) => {
      const update = JSON.parse(event.data);

      if (update.type === 'node_added') {
        this.addNode(update.node);

        // Add edges
        update.edges.forEach(edge => {
          this.addEdge(edge);
        });

        // Trigger visualization update
        this.render();
      }
    };

    this.mapWs.onerror = (error) => {
      console.error('Map WebSocket error:', error);
    };

    this.mapWs.onclose = () => {
      console.log('Map WebSocket closed, reconnecting...');
      setTimeout(() => this.connectToUpdates(), 5000);
    };
  }

  addNode(node) {
    this.nodes.set(node.node_id, node);
    console.log(`Added node ${node.node_id} at (${node.x}, ${node.y})`);
  }

  addEdge(edge) {
    const edgeKey = `${edge.from_node_id}-${edge.to_node_id}`;
    this.edges.add(edgeKey);
    console.log(`Added edge: ${edge.from_node_id} -> ${edge.to_node_id}`);
  }

  render() {
    // Implement your visualization logic here
    // This could use Canvas, SVG, WebGL, or a library like D3.js
    console.log(`Rendering map with ${this.nodes.size} nodes and ${this.edges.size} edges`);
  }

  destroy() {
    if (this.mapWs) {
      this.mapWs.close();
    }
  }
}

// Usage
const mapViz = new MapVisualization('warehouse_floor_1');
mapViz.initialize()
  .then(() => console.log('Map visualization initialized'))
  .catch(error => console.error('Initialization error:', error));
```

---

### Pattern 3: Fleet Management Dashboard

This pattern demonstrates monitoring multiple robots simultaneously.

```javascript
class FleetDashboard {
  constructor(baseUrl = 'http://localhost:8000') {
    this.baseUrl = baseUrl;
    this.robots = new Map();
    this.robotConnections = new Map();
  }

  async initialize() {
    // Load all robots
    const response = await fetch(`${this.baseUrl}/api/v1/robots`);
    const robots = await response.json();

    // Subscribe to each robot's updates
    robots.forEach(robot => {
      this.addRobot(robot);
      this.subscribeToRobot(robot.name);
    });
  }

  addRobot(robot) {
    this.robots.set(robot.name, robot);
    this.updateDashboard();
  }

  subscribeToRobot(robotName) {
    const ws = new WebSocket(`ws://localhost:8000/ws/robot/${robotName}`);

    ws.onmessage = (event) => {
      const update = JSON.parse(event.data);

      // Update robot data
      const robot = this.robots.get(robotName);
      if (robot) {
        robot.status = update.status;
        this.updateDashboard();
      }
    };

    ws.onerror = (error) => {
      console.error(`WebSocket error for ${robotName}:`, error);
    };

    this.robotConnections.set(robotName, ws);
  }

  updateDashboard() {
    const stats = {
      total: this.robots.size,
      online: 0,
      idle: 0,
      on_task: 0,
      charging: 0,
      low_battery: 0
    };

    this.robots.forEach(robot => {
      if (robot.status.online) stats.online++;
      if (robot.status.state === 'IDLE') stats.idle++;
      if (robot.status.state === 'ON_TASK') stats.on_task++;
      if (robot.status.state === 'CHARGING') stats.charging++;
      if (robot.status.battery_level < 20) stats.low_battery++;
    });

    console.log('Fleet Status:', stats);

    // Update UI here
    this.renderDashboard(stats);
  }

  renderDashboard(stats) {
    // Implement your dashboard UI update logic
    console.log(`Dashboard: ${stats.online}/${stats.total} robots online`);
  }

  async sendRobotToLocation(robotName, x, y) {
    const response = await fetch(`${this.baseUrl}/api/v1/navigate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        robot_name: robotName,
        target_x: x,
        target_y: y
      })
    });

    if (!response.ok) {
      const error = await response.json();
      console.error('Navigation failed:', error.detail);
      return null;
    }

    return await response.json();
  }

  destroy() {
    // Close all WebSocket connections
    this.robotConnections.forEach(ws => ws.close());
    this.robotConnections.clear();
  }
}

// Usage
const dashboard = new FleetDashboard();
dashboard.initialize()
  .then(() => console.log('Fleet dashboard initialized'))
  .catch(error => console.error('Initialization error:', error));
```

---

### Pattern 4: Image Retrieval and Display

This pattern shows how to retrieve and display images from nodes.

```javascript
async function displayNodeImage(mapId, nodeId, imageId = null) {
  const baseUrl = 'http://localhost:8000';

  // Build URL
  let url = `${baseUrl}/api/v1/images/${mapId}/${nodeId}`;
  if (imageId) {
    url += `?image_id=${imageId}`;
  }

  try {
    const response = await fetch(url);

    if (!response.ok) {
      console.error('Image not found');
      return null;
    }

    // Get image as blob
    const blob = await response.blob();

    // Create object URL
    const imageUrl = URL.createObjectURL(blob);

    // Display in img element
    const imgElement = document.getElementById('node-image');
    imgElement.src = imageUrl;

    // Clean up object URL when done
    imgElement.onload = () => {
      URL.revokeObjectURL(imageUrl);
    };

    return imageUrl;

  } catch (error) {
    console.error('Failed to load image:', error);
    return null;
  }
}

// Usage
displayNodeImage('warehouse_floor_1', '1001', 'front_camera');
```

---

## Backend Service Details

### Graph Database Service

**Purpose:** Manages topological maps with persistent storage (ArangoDB) and fast spatial queries (R-tree)

**Key Features:**
- **Dual Storage:** ArangoDB for persistence + R-tree for ultra-fast spatial queries (10-100 μs)
- **Spatial Queries:** k-NN search, radius search
- **Multi-Map Support:** Manage multiple maps simultaneously
- **Automatic Synchronization:** R-tree automatically rebuilds when nodes are added

**Performance:**
- k-NN query: ~2-3ms total (including HTTP overhead)
- Radius search: ~2-3ms total
- Node insertion: ~10-20ms (includes ArangoDB write + R-tree update)

---

### Image Database Service

**Purpose:** Stores robot camera images using MinIO object storage

**Key Features:**
- **Object Storage:** MinIO-backed storage for scalability
- **Multi-Map Support:** Organize images by map and node
- **Metadata:** Associate images with nodes and timestamps

**Storage Structure:**
```
bucket: {map_id}
  └── {node_id}/
      ├── front_camera.jpg
      ├── back_camera.jpg
      └── ...
```

---

### Mission Planner Service

**Purpose:** Plans navigation paths and creates missions

**Key Features:**
- **Path Planning:** Uses graph database to find optimal paths
- **Mission Creation:** Generates VDA5050-compatible mission trees
- **Automatic Submission:** Submits missions to Mission Dispatcher

**Workflow:**
1. Receive navigation request (target coordinates)
2. Find nearest node to robot's current position
3. Find nearest node to target position
4. Compute path using graph database
5. Create mission with waypoints
6. Submit to Mission Dispatcher

---

### Mission Dispatcher Service

**Purpose:** Executes missions using VDA5050 protocol via MQTT

**Key Features:**
- **VDA5050 Protocol:** Industry-standard AGV communication protocol
- **MQTT Communication:** Real-time bidirectional communication with robots
- **Mission Lifecycle:** Manages mission states (PENDING → RUNNING → COMPLETED/FAILED)
- **Robot Status Tracking:** Monitors robot position, battery, errors

**MQTT Topics:**
- `uagv/v2/RobotCompany/{robot_name}/order`: Send missions to robot
- `uagv/v2/RobotCompany/{robot_name}/state`: Receive robot status updates
- `uagv/v2/RobotCompany/{robot_name}/factsheet`: Receive robot capabilities

**Note:** MQTT is not directly exposed to client applications. Clients interact via REST API and WebSockets.

---

### Graph Builder Service

**Purpose:** Builds topological maps from robot sensor data

**Key Features:**
- **MQTT Integration:** Subscribes to robot node updates
- **Image Storage:** Saves robot camera images
- **Spatial Indexing:** Uses R-tree for fast nearby node queries (5m radius default)
- **Traversability Checking:** Validates edges using Similarity Service
- **Bidirectional Edges:** Automatically creates edges in both directions

**Workflow:**
1. Receive MQTT node update from robot
2. Save images to Image Database
3. Find nearby nodes (radius search, default 5m)
4. Check traversability with Similarity Service
5. Create bidirectional edges to traversable nodes
6. Save node to Graph Database
7. Broadcast update via WebSocket

---

### Similarity Service

**Purpose:** Validates edge traversability between nodes

**Key Features:**
- **Distance Calculation:** Computes Euclidean distance between nodes
- **Traversability Threshold:** Configurable distance threshold (default: 10m)
- **Relative Pose:** Computes relative position and orientation

**Algorithm:**
```python
distance = sqrt((x2 - x1)^2 + (y2 - y1)^2)
traversable = distance <= threshold
```

---

## Rate Limiting and Performance

**Current Status:** No rate limiting implemented (development mode)

**Recommended Limits for Production:**
- REST API: 100 requests/minute per client
- WebSocket connections: 10 concurrent connections per client
- Image downloads: 50 requests/minute per client

**Performance Characteristics:**
- REST API latency: 10-50ms (typical)
- WebSocket message latency: <10ms
- Image download: 100-500ms (depends on image size)
- Map loading: 100-1000ms (depends on map size)

---

## Security Considerations

**Current Implementation:** Development mode with no authentication

**Production Recommendations:**

1. **Authentication:**
   - Implement API key authentication for REST endpoints
   - Use JWT tokens for WebSocket connections
   - Integrate with OAuth 2.0 / OIDC providers

2. **Authorization:**
   - Role-based access control (RBAC)
   - Separate read/write permissions
   - Robot-specific access controls

3. **Transport Security:**
   - Enable TLS/SSL for all connections
   - Use WSS (WebSocket Secure) for WebSocket connections
   - Implement certificate pinning for mobile clients

4. **Data Validation:**
   - Validate all input data
   - Sanitize user-provided strings
   - Implement request size limits

5. **Network Security:**
   - Deploy behind reverse proxy (nginx, Traefik)
   - Implement firewall rules
   - Use VPN for robot-to-cloud communication

---

## Appendix: Complete Example Application

Here's a complete example of a simple web application that demonstrates the key integration patterns:

```html
<!DOCTYPE html>
<html>
<head>
  <title>Robot Fleet Manager</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 20px; }
    .robot-card { border: 1px solid #ccc; padding: 10px; margin: 10px 0; }
    .online { color: green; }
    .offline { color: red; }
    button { margin: 5px; padding: 5px 10px; }
  </style>
</head>
<body>
  <h1>Robot Fleet Manager</h1>

  <div id="robots"></div>

  <h2>Send Robot to Location</h2>
  <select id="robot-select"></select>
  <input type="number" id="target-x" placeholder="X" step="0.1">
  <input type="number" id="target-y" placeholder="Y" step="0.1">
  <button onclick="navigate()">Navigate</button>

  <h2>Mission Status</h2>
  <div id="mission-status"></div>

  <script>
    const BASE_URL = 'http://localhost:8000';
    let robots = new Map();
    let robotConnections = new Map();
    let missionWs = null;

    // Initialize
    async function init() {
      await loadRobots();
      subscribeToRobots();
    }

    // Load all robots
    async function loadRobots() {
      const response = await fetch(`${BASE_URL}/api/v1/robots`);
      const robotList = await response.json();

      robotList.forEach(robot => {
        robots.set(robot.name, robot);
      });

      updateUI();
    }

    // Subscribe to robot updates
    function subscribeToRobots() {
      robots.forEach((robot, name) => {
        const ws = new WebSocket(`ws://localhost:8000/ws/robot/${name}`);

        ws.onmessage = (event) => {
          const update = JSON.parse(event.data);
          const robot = robots.get(name);
          if (robot) {
            robot.status = update.status;
            updateUI();
          }
        };

        robotConnections.set(name, ws);
      });
    }

    // Update UI
    function updateUI() {
      const robotsDiv = document.getElementById('robots');
      const select = document.getElementById('robot-select');

      robotsDiv.innerHTML = '';
      select.innerHTML = '';

      robots.forEach((robot, name) => {
        // Robot card
        const card = document.createElement('div');
        card.className = 'robot-card';
        card.innerHTML = `
          <h3>${name}</h3>
          <p>Status: <span class="${robot.status.online ? 'online' : 'offline'}">
            ${robot.status.online ? 'Online' : 'Offline'}
          </span></p>
          <p>State: ${robot.status.state}</p>
          <p>Battery: ${robot.status.battery_level.toFixed(1)}%</p>
          <p>Position: (${robot.status.pose.x.toFixed(2)}, ${robot.status.pose.y.toFixed(2)})</p>
        `;
        robotsDiv.appendChild(card);

        // Select option
        const option = document.createElement('option');
        option.value = name;
        option.textContent = name;
        select.appendChild(option);
      });
    }

    // Navigate robot
    async function navigate() {
      const robotName = document.getElementById('robot-select').value;
      const targetX = parseFloat(document.getElementById('target-x').value);
      const targetY = parseFloat(document.getElementById('target-y').value);

      if (!robotName || isNaN(targetX) || isNaN(targetY)) {
        alert('Please fill all fields');
        return;
      }

      const response = await fetch(`${BASE_URL}/api/v1/navigate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          robot_name: robotName,
          target_x: targetX,
          target_y: targetY
        })
      });

      if (!response.ok) {
        const error = await response.json();
        alert(`Navigation failed: ${error.detail}`);
        return;
      }

      const result = await response.json();
      subscribeToMission(result.mission_name);
    }

    // Subscribe to mission updates
    function subscribeToMission(missionName) {
      if (missionWs) {
        missionWs.close();
      }

      missionWs = new WebSocket(`ws://localhost:8000/ws/mission/${missionName}`);

      missionWs.onmessage = (event) => {
        const update = JSON.parse(event.data);
        const statusDiv = document.getElementById('mission-status');

        statusDiv.innerHTML = `
          <h3>Mission: ${missionName}</h3>
          <p>State: ${update.status.state}</p>
          <p>Progress: ${(update.status.progress * 100).toFixed(1)}%</p>
        `;

        if (update.status.state === 'COMPLETED') {
          statusDiv.innerHTML += '<p style="color: green;">✓ Mission completed!</p>';
          missionWs.close();
        } else if (update.status.state === 'FAILED') {
          statusDiv.innerHTML += '<p style="color: red;">✗ Mission failed</p>';
          missionWs.close();
        }
      };
    }

    // Start application
    init();
  </script>
</body>
</html>
```

Save this as `index.html` and open in a browser to see a working fleet management interface!

---

## Support and Resources

- **API Base URL:** `http://localhost:8000`
- **WebSocket Base URL:** `ws://localhost:8000`
- **Health Check:** `GET /health`
- **Service Info:** `GET /`

For questions or issues, please refer to the service logs or contact the backend development team.



#### SLAM maps: `slam_map` on `POST /api/v1/maps`

`POST /api/v1/maps` accepts `slam_map: bool` (default `false`); `true` only with `type: "local"` (else 422, `loc` `["body","slam_map"]`). Map views and lists return `slam_map`. It cannot be changed afterwards (not in `PATCH`), and `POST /api/v1/maps/{id}/type` to `geo` clears it (a note is added to `warnings`).

A mapping session on such a map also records a SLAM map on the robot's orchestrator under the name `cloud-<map>` (`POST /maps/cloud-<map>/mapping/start`, saved in the background after `.../finish`). The session responses (`POST .../sessions`, `.../finish`) carry `slam_warning` (string) only when something went wrong; the session itself is never failed or rolled back for it. `mapping_warning` stays the topomap's.
