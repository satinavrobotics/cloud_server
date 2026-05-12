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

