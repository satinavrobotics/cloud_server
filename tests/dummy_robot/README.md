# Dummy Robot Service

A simulated robot for testing Mission Dispatch and Graph Builder services.

## 📚 Documentation

- **[QUICK_START.md](QUICK_START.md)** - Get started in 5 minutes
- **[ARCHITECTURE.md](ARCHITECTURE.md)** - System design and data flow
- **[README.md](README.md)** - This file (overview and usage)

## Features

- ✅ **VDA5050 Compliance**: Publishes state messages and receives orders
- ✅ **Node Publishing**: Publishes node updates for topological map building
- ✅ **Autonomous Movement**: Simulates circular movement pattern
- ✅ **Battery Simulation**: Simulates battery drain and recharge
- ✅ **MQTT Communication**: Full bidirectional MQTT communication
- ✅ **Docker Support**: Ready for containerized deployment
- ✅ **Integration Tests**: Comprehensive test suite included

## What It Does

1. **Publishes VDA5050 State**: Sends robot state to Mission Dispatch every second
2. **Listens for Orders**: Receives and acknowledges VDA5050 orders from Mission Dispatch
3. **Publishes Nodes**: Sends node updates to Graph Builder every 5 seconds
4. **Simulates Movement**: Moves in a circular pattern continuously

## MQTT Topics

### Publishes To:
- `uagv/v2/RobotCompany/{robot_name}/state` - Robot state (VDA5050)
- `uagv/v2/RobotCompany/{robot_name}/factsheet` - Robot capabilities (VDA5050)
- `robot/node_update` - Node updates for graph building

### Subscribes To:
- `uagv/v2/RobotCompany/{robot_name}/order` - Mission orders (VDA5050)
- `uagv/v2/RobotCompany/{robot_name}/instantActions` - Instant actions (VDA5050)

## Usage

### Run Standalone

```bash
cd /home/satiadmin/satinavrobotics/cloud_server
python tests/dummy_robot/dummy_robot.py \
  --robot_name dummy_robot_01 \
  --mqtt_host localhost \
  --mqtt_port 1883 \
  --map_id default \
  --loop_radius 10.0 \
  --speed 1.0
```

### Run with Docker

```bash
# Build image
docker build -f tests/dummy_robot/Dockerfile -t dummy_robot:latest .

# Run container
docker run --network host \
  dummy_robot:latest \
  python tests/dummy_robot/dummy_robot.py \
  --robot_name dummy_robot_01 \
  --mqtt_host localhost
```

### Run with Docker Compose

The dummy robot is included in `docker_compose/mission_dispatch_services_dev.yaml`:

```bash
cd docker_compose
docker-compose -f mission_dispatch_services_dev.yaml up dummy-robot
```

## Configuration Options

| Argument | Default | Description |
|----------|---------|-------------|
| `--robot_name` | `dummy_robot_01` | Name of the robot |
| `--manufacturer` | `DummyManufacturer` | Manufacturer name |
| `--serial_number` | `DUMMY001` | Serial number |
| `--mqtt_host` | `localhost` | MQTT broker host |
| `--mqtt_port` | `1883` | MQTT broker port |
| `--mqtt_prefix` | `uagv/v2/RobotCompany` | VDA5050 MQTT topic prefix |
| `--node_topic` | `robot/node_update` | Topic for node updates |
| `--map_id` | `default` | Map ID for nodes |
| `--loop_radius` | `10.0` | Radius of circular movement (meters) |
| `--speed` | `1.0` | Movement speed (m/s) |
| `--tick_period` | `1.0` | Update period (seconds) |
| `--no_nodes` | `false` | Disable node publishing |
| `--mode` | `patrol` (or `$DUMMY_ROBOT_MODE`) | `patrol`: circle, orders ignored. `goal`: follow VDA5050 orders |
| `--action_duration` | `1.0` | `goal` mode: seconds a node action stays RUNNING |
| `--goal_tolerance` | `0.05` | `goal` mode: distance (m) at which a node counts as reached |

## Goal-following mode (`--mode goal`)

The default `patrol` mode free-runs a circle and never reports progress on an
order. `--mode goal` (or `DUMMY_ROBOT_MODE=goal`) makes the robot execute the
orders mission-dispatch sends, so missions can reach `COMPLETED`:

- The first node of a new order is taken as reached on acceptance; the robot then
  drives to each released node's `nodePosition` in sequence at `--speed`, one node
  per tick at most, and stops at the end of the released base.
- State reports `orderId`/`orderUpdateId`, `lastNodeId`/`lastNodeSequenceId`,
  shrinking `nodeStates`/`edgeStates`, `driving`, `agvPosition`. When the order is
  done: empty `nodeStates`/`edgeStates`, `driving: false`, same `orderId`.
- Order update (same `orderId`, higher `orderUpdateId`): nodes/edges after the last
  reached node replace the rest of the order. A resend with the same update id is
  ignored; a lower one is rejected. A new `orderId` replaces the current order.
- Node actions run one at a time and block driving: `WAITING` (node not reached)
  -> `RUNNING` (for `--action_duration`) -> `FINISHED`. Order actions come first in
  `actionStates` (dispatch reads `actionStates[0]` for an action node).
- Instant actions: `cancelOrder` stops the robot and drops the order, reported
  `FINISHED` (or `FAILED` / `noOrderToCancel` when idle); `factsheetRequest`
  republishes the factsheet; `startTeleop`/`stopTeleop` are acknowledged.
  Unknown instant actions are ignored.

```bash
python tests/dummy_robot/dummy_robot.py --mode goal --robot_name dummy_robot_01 \
    --speed 1.0 --tick_period 0.5 --action_duration 2
```

The logic lives in `goal_follower.py` (no MQTT) and is tested without a broker:

```bash
docker build -t dummy-robot-test -f - . <<'DOCKERFILE'
FROM python:3.10-slim
COPY tests/dummy_robot/requirements.txt /r.txt
RUN pip install -r /r.txt pytest pytest-asyncio requests
DOCKERFILE
docker run --rm --network none -v "$PWD":/src:ro -w /src \
    -e ARANGO_PASSWORD=x -e MINIO_ACCESS_KEY=x -e MINIO_SECRET_KEY=x -e POSTGRES_PASSWORD=x \
    dummy-robot-test python -m pytest -p no:cacheprovider tests/dummy_robot/test_goal_follower.py
```

`test_goal_follower_dispatch.py` runs the real dispatcher `Robot` against the
follower (MQTT/Postgres mocked) and needs the dispatcher's dependencies
(`packages/controllers/mission/requirements.txt`); it skips without them.

## Testing

### Test VDA5050 Communication

1. Start Mission Dispatch services:
```bash
cd docker_compose
docker-compose -f mission_dispatch_services_dev.yaml up
```

2. Register the robot in Mission Database:
```bash
curl -X POST http://localhost:5000/robot \
  -H "Content-Type: application/json" \
  -d '{
    "name": "dummy_robot_01",
    "labels": ["test", "dummy"]
  }'
```

3. Start the dummy robot:
```bash
python tests/dummy_robot/dummy_robot.py
```

4. Send a mission:
```bash
curl -X POST http://localhost:5000/mission \
  -H "Content-Type: application/json" \
  -d '{
    "robot": "dummy_robot_01",
    "name": "test_mission",
    "mission_tree": [
      {
        "name": "goto_point",
        "parent": "root",
        "route": {
          "waypoints": [
            {"x": 5.0, "y": 5.0, "theta": 0.0, "map_id": "default"}
          ]
        }
      }
    ]
  }'
```

### Test Node Publishing

Monitor MQTT for node updates:
```bash
mosquitto_sub -h localhost -p 1883 -t "robot/node_update" -v
```

You should see node updates every 5 seconds.

### Test Graph Building

Check the Graph Builder service logs:
```bash
docker-compose -f mission_dispatch_services_dev.yaml logs -f graph-builder-service
```

You should see messages like:
```
📨 Received node update: 1000
✅ Successfully processed node 1000
```

## Integration with Services

### Mission Dispatch
- Receives robot state updates
- Can send missions to the robot
- Tracks robot position and battery

### Graph Builder
- Receives node updates
- Builds topological map
- Creates edges between nodes

### Mission Database
- Stores robot information
- Stores mission history

## Troubleshooting

**Robot not connecting to MQTT:**
- Check MQTT broker is running: `docker-compose ps mosquitto`
- Check MQTT host/port settings
- Check network connectivity

**Robot not receiving orders:**
- Ensure robot is registered in Mission Database
- Check MQTT topic prefix matches Mission Dispatch configuration
- Check Mission Dispatch logs

**Nodes not appearing in graph:**
- Check Graph Builder service is running
- Check MQTT topic matches Graph Builder configuration
- Check Graph Builder logs for errors

## Development

To modify the robot behavior:

1. Edit `tests/dummy_robot/dummy_robot.py`
2. Rebuild Docker image if using Docker
3. Restart the service

Example modifications:
- Change movement pattern in `_update_position()`
- Add custom actions in `_handle_instant_actions()`
- Modify state publishing in `_publish_state()`

