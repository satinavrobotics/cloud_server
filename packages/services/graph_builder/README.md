# Graph Builder Service

A microservice that builds topological maps by processing new node updates from robots via MQTT.

## Overview

The Graph Builder Service is a critical component of the topological mapping system. It:
- Subscribes to MQTT for new node updates from robots
- Saves images to the Image Database
- Finds nearby nodes using radius search (default 5m threshold)
- Checks traversability using the Similarity Service
- Creates bidirectional edges between traversable nodes
- Saves nodes and edges to the Graph Database

## Architecture

```
Robot (MQTT) → Graph Builder Service
                    ↓
    ┌───────────────┼───────────────┐
    ↓               ↓               ↓
Image DB      Graph DB      Similarity Service
(MinIO)      (ArangoDB)     (Traversability)
```

## Features

- **MQTT Integration**: Subscribes to robot node updates in real-time
- **Image Storage**: Saves robot camera images to MinIO-backed Image Database
- **Spatial Indexing**: Uses R-tree spatial index for fast nearby node queries
- **Traversability Checking**: Validates edges using configurable distance threshold
- **Bidirectional Edges**: Automatically creates edges in both directions
- **REST API**: Provides health checks, statistics, and manual node processing
- **WebSocket Updates**: Publishes real-time map updates to connected clients

## Dependencies

The service requires the following services to be running:
- **MQTT Broker** (e.g., Mosquitto) - for receiving node updates
- **Image Database Service** (port 6002) - for storing images
- **Graph Database Service** (port 6001) - for storing graph nodes/edges

## Installation

### 1. Install Dependencies

```bash
cd packages/services/graph_builder
pip install -r requirements.txt
```

### 2. Configure Environment

Copy the example environment file and edit as needed:

```bash
cp .env.example .env
```

Edit `.env`:
```bash
MQTT_HOST=localhost
MQTT_PORT=1883
MQTT_TOPIC=robot/node_update
IMAGE_DB_URL=http://localhost:6002
GRAPH_DB_URL=http://localhost:6001
SIMILARITY_SERVICE_URL=http://localhost:8003
RADIUS_THRESHOLD=5.0
DEFAULT_MAP_ID=default
```

## Usage

### Start the Service

```bash
python -m packages.services.graph_builder.main --port 8004
```

Or with custom configuration:

```bash
python -m packages.services.graph_builder.main \
  --port 8004 \
  --mqtt-host localhost \
  --mqtt-port 1883 \
  --mqtt-topic robot/node_update \
  --radius-threshold 5.0 \
  --log-level INFO
```

### Command Line Arguments

- `--host`: Host to bind to (default: 0.0.0.0)
- `--port`: Port to bind to (default: 8004)
- `--mqtt-host`: MQTT broker host (default: localhost)
- `--mqtt-port`: MQTT broker port (default: 1883)
- `--mqtt-topic`: MQTT topic for node updates (default: robot/node_update)
- `--image-db-url`: Image DB service URL (default: http://localhost:6002)
- `--graph-db-url`: Graph DB service URL (default: http://localhost:6001)
- `--distance-threshold`: Distance threshold in meters for edge creation (default: 3.0)
- `--radius-threshold`: Radius threshold in meters (default: 5.0)
- `--default-map-id`: Default map ID (default: default)
- `--log-level`: Logging level (DEBUG, INFO, WARNING, ERROR)

## API Endpoints

### Health Check
```
GET /health
```

Returns service health status and dependency status.

**Response:**
```json
{
  "status": "healthy",
  "service": "graph_builder",
  "mqtt_connected": true,
  "dependencies": {
    "image_db": true,
    "graph_db": true
  }
}
```

### Get Statistics
```
GET /stats
```

Returns service statistics.

**Response:**
```json
{
  "nodes_processed": 42,
  "images_saved": 84,
  "edges_created": 156,
  "errors": 0,
  "mqtt_connected": true,
  "radius_threshold": 5.0
}
```

### Manual Node Processing (Testing)
```
POST /node
```

Manually process a node update (for testing purposes).

**Request Body:**
```json
{
  "node_id": 1001,
  "x": 10.5,
  "y": 20.3,
  "yaw": 1.57,
  "map_id": "warehouse",
  "images": [
    {
      "image_id": "front_camera",
      "data": "base64_encoded_image_data",
      "content_type": "image/jpeg"
    }
  ],
  "metadata": {
    "robot_id": "robot_01",
    "timestamp": "2024-01-15T10:30:00Z"
  }
}
```

**Response:**
```json
{
  "success": true,
  "node_id": 1001,
  "message": "Node processed successfully"
}
```

### WebSocket Real-Time Updates
```
WS /ws/updates/{map_id}
```

Subscribe to real-time map updates for a specific map. This endpoint is typically consumed by the API Delegation Service to proxy updates to client applications.

**Connection:**
```javascript
const ws = new WebSocket('ws://localhost:8004/ws/updates/warehouse');

ws.onmessage = (event) => {
  const update = JSON.parse(event.data);
  console.log('Map update:', update);
};
```

**Update Message Format:**
```json
{
  "type": "node_added",
  "map_id": "warehouse",
  "node": {
    "node_id": 1001,
    "x": 10.5,
    "y": 20.3,
    "yaw": 1.57
  },
  "edges": [
    {
      "from_node_id": 1001,
      "to_node_id": 1000
    },
    {
      "from_node_id": 1000,
      "to_node_id": 1001
    }
  ],
  "timestamp": "2024-01-15T10:30:00Z"
}
```

**Note:** In production, clients should connect through the API Delegation Service at `ws://api-delegation:8000/ws/map/{map_id}`, which proxies to this endpoint.

## MQTT Message Format

The service expects node update messages on the configured MQTT topic with the following format:

```json
{
  "node_id": 1001,
  "x": 10.5,
  "y": 20.3,
  "yaw": 1.57,
  "map_id": "warehouse",
  "images": [
    {
      "image_id": "front_camera",
      "data": "base64_encoded_image_data",
      "content_type": "image/jpeg",
      "metadata": {
        "camera_name": "front_camera",
        "timestamp": "2024-01-15T10:30:00Z"
      }
    },
    {
      "image_id": "back_camera",
      "data": "base64_encoded_image_data",
      "content_type": "image/jpeg",
      "metadata": {
        "camera_name": "back_camera",
        "timestamp": "2024-01-15T10:30:00Z"
      }
    }
  ],
  "metadata": {
    "robot_id": "robot_01",
    "timestamp": "2024-01-15T10:30:00Z"
  }
}
```

### Required Fields
- `node_id`: Unique integer identifier for the node
- `x`: X coordinate in meters
- `y`: Y coordinate in meters

### Optional Fields
- `yaw`: Orientation in radians (default: 0.0)
- `map_id`: Map identifier (default: from config)
- `images`: List of image data (base64 encoded)
- `metadata`: Additional node metadata

## How It Works

### Node Processing Pipeline

1. **Receive MQTT Message**: Robot publishes node update to MQTT topic
2. **Save Images**: Store all images to Image Database (MinIO)
3. **Find Nearby Nodes**: Query Graph Database for nodes within radius threshold (default 5m)
4. **Check Traversability**: For each nearby node, use Similarity Service to check if traversable
5. **Create Edges**: Create bidirectional edges to all traversable nodes
6. **Save Node**: Store node with metadata to Graph Database
7. **Save Edges**: Store all edges to Graph Database

### Example Flow

```
Robot publishes node at (10.5, 20.3)
    ↓
Graph Builder receives MQTT message
    ↓
Save 2 images to Image DB
    ↓
Find nodes within 5m radius → Found 3 nodes
    ↓
Check traversability:
  - Node 100 at (12.0, 21.0): distance=1.8m → traversable ✓
  - Node 101 at (8.5, 19.5): distance=2.4m → traversable ✓
  - Node 102 at (15.0, 25.0): distance=6.5m → not traversable ✗
    ↓
Create 4 edges:
  - 1001 → 100
  - 100 → 1001
  - 1001 → 101
  - 101 → 1001
    ↓
Save node 1001 to Graph DB
Save 4 edges to Graph DB
    ↓
Done! ✅
```

## Docker Deployment

### Build Image

```bash
cd packages/services/graph_builder
docker build -t graph-builder-service:latest .
```

### Run Container

```bash
docker run -d \
  --name graph-builder-service \
  -p 8004:8004 \
  -e MQTT_HOST=mosquitto \
  -e MQTT_PORT=1883 \
  -e IMAGE_DB_URL=http://image-db-service:6002 \
  -e GRAPH_DB_URL=http://graph-db-service:6001 \
  -e DISTANCE_THRESHOLD=5.0 \
  --network mission-dispatch-network \
  graph-builder-service:latest
```

## Monitoring

### Check Service Health

```bash
curl http://localhost:8004/health
```

### View Statistics

```bash
curl http://localhost:8004/stats
```

### Check Logs

```bash
# If running with Docker
docker logs -f graph-builder-service

# If running directly
# Logs are output to stdout
```

## Troubleshooting

### MQTT Not Connected

- Check MQTT broker is running: `docker ps | grep mosquitto`
- Verify MQTT host/port configuration
- Check network connectivity

### Images Not Saving

- Verify Image DB service is running: `curl http://localhost:6002/health`
- Check image data is base64 encoded
- Verify MinIO is accessible

### No Edges Created

- Check Similarity Service is running: `curl http://localhost:8003/health`
- Verify radius threshold is appropriate for your use case
- Check that nearby nodes exist in the database

### Nodes Not Saving

- Verify Graph DB service is running: `curl http://localhost:6001/health`
- Check ArangoDB is accessible
- Ensure node_id is unique

## Configuration

### Radius Threshold

The radius threshold determines how far to search for nearby nodes. Default is 5.0 meters.

- **Too small**: Nodes won't connect, graph will be fragmented
- **Too large**: Too many edges, slower queries, less meaningful connections

Recommended values:
- Indoor environments: 3-5 meters
- Outdoor environments: 5-10 meters
- Warehouse: 5-7 meters

### MQTT Topic

Default topic is `robot/node_update`. You can customize this based on your robot fleet:

- Single robot: `robot/node_update`
- Multiple robots: `robot/+/node_update` (wildcard)
- Specific robot: `robot/robot_01/node_update`

## Development

### Running Tests

```bash
# TODO: Add tests
pytest packages/services/graph_builder/tests/
```

### Code Structure

```
packages/services/graph_builder/
├── __init__.py           # Package initialization
├── server.py             # Core GraphBuilderService logic
├── main.py               # FastAPI application
├── requirements.txt      # Python dependencies
├── .env.example          # Example configuration
├── Dockerfile            # Docker build configuration
└── README.md             # This file
```

