# Mission Planner Service

The Mission Planner Service is a microservice that plans navigation missions for robots by querying the graph database and submitting missions to the Mission Dispatcher.

## Overview

This service handles the complete workflow for planning navigation missions:

1. **Find closest node to robot** - Queries robot status from Mission Dispatcher and runs KNN on the graph service to find the nearest graph node
2. **Find closest node to target** - Uses range search (or KNN fallback) to find the nearest graph node to the target coordinates
3. **Find path between nodes** - Queries the graph database to compute the shortest path
4. **Create mission** - Constructs a mission object with waypoints from the path
5. **Submit mission** - Sends the mission to Mission Dispatcher in the correct format

## API Endpoints

### POST /api/v1/navigate

Plan and execute a navigation mission.

**Request Body:**
```json
{
  "robot_name": "robot_1",
  "target_x": 10.5,
  "target_y": 20.3,
  "mission_name": "optional_mission_name",
  "timeout_seconds": 300
}
```

**Success Response:**
```json
{
  "success": true,
  "robot_name": "robot_1",
  "target": {"x": 10.5, "y": 20.3},
  "mission_name": "nav_robot_1_20240101_120000",
  "start_node_id": 42,
  "end_node_id": 87,
  "path": [42, 43, 56, 72, 87],
  "path_length": 5,
  "waypoints_count": 5,
  "robot_position": {"x": 5.2, "y": 8.1},
  "target_node_position": {"x": 10.4, "y": 20.1},
  "message": "Mission 'nav_robot_1_20240101_120000' planned and submitted successfully"
}
```

**Failure Response:**
```json
{
  "success": false,
  "robot_name": "robot_1",
  "target": {"x": 10.5, "y": 20.3},
  "error": "Robot 'robot_1' not found in database",
  "failed_at": "find_robot_node"
}
```

**Failure Stages:**
- `find_robot_node` - Failed to find closest node to robot
- `find_target_node` - Failed to find closest node to target
- `find_path` - Failed to find path between nodes
- `get_waypoints` - Failed to retrieve waypoint poses
- `create_mission` - Failed to create mission object
- `submit_mission` - Failed to submit mission to dispatcher

### GET /health

Health check endpoint.

**Response:**
```json
{
  "status": "healthy",
  "service": "mission_planner",
  "dependencies": {
    "graph_db": true,
    "mission_database": true
  }
}
```

### GET /stats

Get service statistics and configuration.

**Response:**
```json
{
  "service": "mission_planner",
  "graph_db_url": "http://localhost:6001",
  "database_url": "http://localhost:5000",
  "default_map_id": "default",
  "knn_k": 1,
  "range_search_radius": 5.0
}
```

## Running the Service

### Using Python directly

```bash
cd packages/services/mission_planner
python -m main \
  --host 0.0.0.0 \
  --port 8005 \
  --graph-db-url http://localhost:6001 \
  --database-url http://localhost:5000 \
  --default-map-id default \
  --knn-k 1 \
  --range-search-radius 5.0 \
  --log-level INFO
```

### Using Bazel

```bash
bazel run //packages/services/mission_planner:mission_planner -- \
  --host 0.0.0.0 \
  --port 8005
```

### Environment Variables

The service can be configured using environment variables:

- `GRAPH_DB_URL` - Graph database service URL (default: `http://localhost:6001`)
- `DATABASE_URL` - Mission database service URL (default: `http://localhost:5000`)
- `DEFAULT_MAP_ID` - Default map ID (default: `default`)
- `KNN_K` - Number of nearest neighbors to find (default: `1`)
- `RANGE_SEARCH_RADIUS` - Radius for range search in meters (default: `5.0`)

## Using the Client Library

```python
from packages.services.mission_planner.client import MissionPlannerClient

# Initialize client
client = MissionPlannerClient(url="http://localhost:8005")

# Request navigation mission
result = client.navigate(
    robot_name="robot_1",
    target_x=10.5,
    target_y=20.3,
    mission_name="my_mission",  # Optional
    timeout_seconds=300
)

if result["success"]:
    print(f"Mission created: {result['mission_name']}")
    print(f"Path length: {result['path_length']} nodes")
else:
    print(f"Failed at: {result['failed_at']}")
    print(f"Error: {result['error']}")

# Check service health
if client.health_check():
    print("Service is healthy")

# Get service stats
stats = client.get_stats()
print(f"Graph DB: {stats['graph_db_url']}")
```

## Dependencies

The service depends on:

- **Graph Database Service** (port 6001) - For KNN, range search, and shortest path queries
- **Mission Database Service** (port 5000) - For robot status and mission submission
- **Mission Dispatcher** - Receives and executes the planned missions

## Architecture

```
┌─────────────────┐
│  HTTP Request   │
│  (target x, y)  │
└────────┬────────┘
         │
         v
┌─────────────────────────────────────┐
│   Mission Planner Service (8005)    │
│                                     │
│  1. Get robot status                │
│  2. Find closest node to robot      │
│  3. Find closest node to target     │
│  4. Find shortest path              │
│  5. Create mission with waypoints   │
│  6. Submit to Mission Dispatcher    │
└──────┬──────────────────────┬───────┘
       │                      │
       v                      v
┌──────────────┐      ┌──────────────┐
│  Graph DB    │      │  Mission DB  │
│  (port 6001) │      │  (port 5000) │
│              │      │              │
│  - KNN       │      │  - Robots    │
│  - Range     │      │  - Missions  │
│  - Path      │      │              │
└──────────────┘      └──────────────┘
```

## Configuration Parameters

### KNN K
Number of nearest neighbors to find when searching for closest nodes. Default is `1` (closest node only).

### Range Search Radius
Radius in meters for range search when finding the closest node to the target. If no nodes are found within this radius, the service falls back to KNN. Default is `5.0` meters.

### Default Map ID
The map ID to use for waypoints if not specified in the graph nodes. Default is `"default"`.

## Error Handling

The service provides detailed error information including:
- Which stage of the planning process failed
- Specific error messages
- Robot and target positions
- Partial results (e.g., if path finding fails, you still get the start and end node IDs)

This allows clients to diagnose issues and potentially retry with different parameters.

## Logging

The service logs all major operations:
- Robot status queries
- KNN and range search results
- Path finding results
- Mission creation and submission
- Errors and warnings

Set the log level using the `--log-level` argument or by configuring the logging module.

