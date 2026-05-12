# MQTT Mission Integration

## Overview

This document describes the MQTT-based mission integration between the Mission Planner service and the VisNav Nav2 node.

## Architecture

```
Mission Planner Service ←→ MQTT Broker ←→ VisNav Nav2 Node
     (subscribes to           (pub/sub)      (publishes progress,
      progress updates)                       subscribes to images)
```

### Components

1. **Mission Planner Service** (`packages/services/mission_planner/server.py`)
   - Publishes mission images via MQTT when missions are created
   - Fetches images from Image Database for each waypoint
   - Sends images one by one with order metadata and coordinates
   - **Subscribes to mission progress updates from robots**
   - **Tracks mission state via MQTT (not database queries)**

2. **VisNav Nav2 Node** (`src/sati_ros_navstack/sati_pogany_nav2/sati_pogany_nav2/visnav_nav2.py`)
   - Subscribes to robot-specific mission topic
   - Stores mission images with coordinates
   - Matches goal poses to closest mission image
   - Sets matched image as visual navigation goal
   - **Publishes mission progress updates when waypoints change**

## MQTT Topic Structure

### Mission Images Topic

**Topic**: `robot/{robot_name}/mission/images`

**Direction**: Mission Planner → Robot

**Message Format** (JSON):
```json
{
  "mission_id": "nav_carter01_20231107_143022",
  "image": "base64_encoded_image_data",
  "x": 1.5,
  "y": 2.3,
  "order": 0,
  "metadata": {
    "node_id": "node_123",
    "map_id": "warehouse",
    "theta": 0.0
  }
}
```

### Mission Progress Topic

**Topic**: `robot/{robot_name}/mission/progress`

**Direction**: Robot → Mission Planner

**QoS**: 1 (at least once delivery)

**Message Format** (JSON):
```json
{
  "mission_id": "nav_carter01_20231107_143022",
  "robot_name": "carter01",
  "next_waypoint_index": 3,
  "current_waypoint_index": 2,
  "timestamp": "2025-11-07T10:30:00Z",
  "status": "navigating"
}
```

**Status Values**:
- `navigating`: Robot is actively navigating to a waypoint
- `completed`: Mission completed successfully
- `failed`: Mission failed due to error or obstacle

**Progress Tracking**:
- `next_waypoint_index`: Index of the next waypoint to be reached (fully identifies mission state)
- `current_waypoint_index`: Index of the waypoint currently being navigated to
- Updates are published when:
  - Robot starts navigating to a new waypoint
  - Waypoint is completed
  - Error or obstacle is encountered

## Configuration

### Docker Compose (Recommended)

The MQTT integration is automatically configured when using docker-compose. Environment variables are defined in `docker_compose/.env`:

```bash
# MQTT Configuration
MQTT_ENABLED=true
MQTT_BROKER=localhost
MQTT_PORT_TCP=1883
MQTT_KEEPALIVE=60
```

Start services with:
```bash
cd docker_compose
docker-compose -f mission_dispatch_services.yaml up
```

The mission planner service will automatically:
- Connect to MQTT broker at `localhost:1883`
- Publish mission images when missions are created
- Use the image database at `http://localhost:6002`

### Mission Planner Service (Standalone)

For standalone deployment:

```python
service = MissionPlannerService(
    graph_db_url="http://localhost:6001",
    database_url="http://localhost:5000",
    image_db_url="http://localhost:6002",
    mqtt_enabled=True,
    mqtt_broker="localhost",
    mqtt_port=1883
)
```

Or via command line:
```bash
python -m packages.services.mission_planner.main \
    --mqtt-enabled true \
    --mqtt-broker localhost \
    --mqtt-port 1883 \
    --image-db-url http://localhost:6002
```

### VisNav Nav2 Node

ROS2 parameters:
```yaml
visnav_nav2_coordinator:
  ros__parameters:
    mqtt:
      enabled: true
      broker: "localhost"
      port: 1883
      robot_name: "carter01"
      mission_topic: "robot/{robot_name}/mission/images"
      progress_topic: "robot/{robot_name}/mission/progress"
    mission:
      distance_threshold: 1.0  # meters
```

## Workflow

1. **Mission Creation**:
   - Mission Planner creates mission with waypoints
   - For each waypoint, finds closest graph node
   - Fetches image for that node from Image Database
   - Publishes image via MQTT with coordinates and order

2. **Mission Reception**:
   - VisNav Nav2 receives mission images via MQTT
   - Stores images with coordinates in mission storage
   - If new mission arrives, clears old mission

3. **Goal Image Selection**:
   - When Nav2 requests path to pose (ComputePathToPose action)
   - VisNav Nav2 finds closest mission image to goal coordinates
   - If no image within 1m threshold, logs warning
   - Sets closest image as visual navigation goal
   - Returns path from VisNav planner

4. **Progress Tracking**:
   - When waypoint changes (new goal image selected)
   - VisNav Nav2 updates current and next waypoint indices
   - Publishes progress update via MQTT
   - Mission Planner receives and stores progress
   - Progress can be queried via REST API

## API Endpoints

### Get Mission Progress

**Endpoint**: `GET /api/v1/missions/{mission_id}/progress`

**Description**: Get latest mission progress from MQTT updates (not from database)

**Response**:
```json
{
  "mission_id": "nav_carter01_20231107_143022",
  "robot_name": "carter01",
  "next_waypoint_index": 3,
  "current_waypoint_index": 2,
  "timestamp": "2025-11-07T10:30:00Z",
  "status": "navigating"
}
```

**Example**:
```bash
curl http://localhost:8005/api/v1/missions/nav_carter01_20231107_143022/progress
```

## Error Handling

- **No image within threshold**: Warning logged, navigation continues with existing goal
- **MQTT disconnection**: Automatic reconnection via paho-mqtt
- **Invalid image data**: Error logged, image skipped
- **Missing waypoint images**: Warning logged, waypoint skipped
- **No progress updates**: API returns 404 if no progress received for mission
- **Progress message parsing errors**: Error logged, message skipped

## Testing

See test script: `tests/test_mqtt_mission_integration.py` (to be created)

## Dependencies

- `paho-mqtt`: MQTT client library
- `cv2` (OpenCV): Image encoding/decoding
- `numpy`: Image data handling
- `base64`: Image encoding for MQTT transmission

