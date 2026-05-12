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

Get current status of a robot (alias for GET /api/v1/robots/{robot_name}).

**Response:** Same as `GET /api/v1/robots/{robot_name}`

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

**Request Body:**
```json
{
  "timeout": 600
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
    };
    online: boolean;
    battery_level: number;  // 0-100
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
  needs_canceled: boolean;
  status: {
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


