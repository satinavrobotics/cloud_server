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

