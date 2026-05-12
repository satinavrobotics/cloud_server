# Dummy Robot Quick Start Guide

Get the dummy robot running in 5 minutes!

## Prerequisites

- Docker and Docker Compose installed
- Mission Dispatch services running
- MQTT broker running (mosquitto)

## Quick Start

### Option 1: Using Docker Compose (Recommended)

```bash
# 1. Start all services including dummy robot
cd /home/satiadmin/satinavrobotics/cloud_server/docker_compose
docker-compose -f mission_dispatch_services_dev.yaml up -d

# 2. Wait for services to start (30 seconds)
sleep 30

# 3. Register the robot
cd ../tests/dummy_robot
./register_robot.sh

# 4. Send a test mission
./send_test_mission.sh

# 5. Monitor the robot
docker-compose -f ../../docker_compose/mission_dispatch_services_dev.yaml logs -f dummy-robot
```

### Option 2: Run Standalone (Development)

```bash
# 1. Start Mission Dispatch services (without dummy robot)
cd /home/satiadmin/satinavrobotics/cloud_server/docker_compose
docker-compose -f mission_dispatch_services_dev.yaml up -d mosquitto mission-database mission-dispatch graph-builder-service

# 2. Wait for services to start
sleep 30

# 3. Run dummy robot locally
cd ../tests/dummy_robot
python dummy_robot.py \
  --robot_name dummy_robot_01 \
  --mqtt_host localhost \
  --mqtt_port 1883

# 4. In another terminal, register the robot
./register_robot.sh

# 5. Send a test mission
./send_test_mission.sh
```

## Verify It's Working

### Check Robot State

```bash
# Via REST API
curl http://localhost:5000/robot/dummy_robot_01 | jq

# Via MQTT
mosquitto_sub -h localhost -p 1883 -t "uagv/v2/RobotCompany/dummy_robot_01/state" -C 1
```

### Check Node Publishing

```bash
# Monitor node updates
mosquitto_sub -h localhost -p 1883 -t "robot/node_update" -v
```

You should see messages like:
```json
{
  "node_id": 1000,
  "x": 10.0,
  "y": 0.0,
  "yaw": 1.57,
  "map_id": "default",
  "images": [],
  "metadata": {
    "robot_name": "dummy_robot_01",
    "timestamp": "2024-01-01T12:00:00",
    "battery": 99.5,
    "node_type": "auto_generated"
  }
}
```

### Check Graph Building

```bash
# Check nodes in graph
curl http://localhost:6001/maps/default/nodes | jq

# Check Graph Builder logs
docker-compose -f docker_compose/mission_dispatch_services_dev.yaml logs -f graph-builder-service
```

You should see:
```
📨 Received node update: 1000
✅ Successfully processed node 1000
```

### Check Mission Dispatch

```bash
# Check Mission Dispatch logs
docker-compose -f docker_compose/mission_dispatch_services_dev.yaml logs -f mission-dispatch
```

You should see the robot connecting and receiving orders.

## Run Integration Test

```bash
cd /home/satiadmin/satinavrobotics/cloud_server/tests/dummy_robot
./test_integration.sh
```

This will:
1. ✅ Register the robot
2. ✅ Wait for connection
3. ✅ Check robot status
4. ✅ Send a test mission
5. ✅ Verify mission processing
6. ✅ Check graph nodes
7. ✅ Monitor MQTT messages

## Troubleshooting

### Robot not connecting

**Check MQTT broker:**
```bash
docker-compose -f docker_compose/mission_dispatch_services_dev.yaml ps mosquitto
```

**Check robot logs:**
```bash
docker-compose -f docker_compose/mission_dispatch_services_dev.yaml logs dummy-robot
```

### Robot not receiving orders

**Check robot is registered:**
```bash
curl http://localhost:5000/robot/dummy_robot_01
```

**Check MQTT topics:**
```bash
mosquitto_sub -h localhost -p 1883 -t "uagv/v2/RobotCompany/dummy_robot_01/#" -v
```

### Nodes not appearing in graph

**Check Graph Builder is running:**
```bash
docker-compose -f docker_compose/mission_dispatch_services_dev.yaml ps graph-builder-service
```

**Check Graph Builder logs:**
```bash
docker-compose -f docker_compose/mission_dispatch_services_dev.yaml logs graph-builder-service
```

**Check MQTT topic:**
```bash
mosquitto_sub -h localhost -p 1883 -t "robot/node_update" -v
```

## Configuration

Edit environment variables in `docker_compose/mission_dispatch_services_dev.yaml`:

```yaml
dummy-robot:
  command: ["python", "tests/dummy_robot/dummy_robot.py",
            "--robot_name", "my_custom_robot",  # Change robot name
            "--loop_radius", "20.0",             # Larger circle
            "--speed", "2.0",                    # Faster movement
            "--tick_period", "0.5"]              # More frequent updates
```

Or use environment variables:
```bash
export DUMMY_ROBOT_NAME=my_custom_robot
export DUMMY_ROBOT_LOOP_RADIUS=20.0
export DUMMY_ROBOT_SPEED=2.0
export DUMMY_ROBOT_TICK_PERIOD=0.5

docker-compose -f mission_dispatch_services_dev.yaml up dummy-robot
```

## Multiple Robots

Run multiple dummy robots:

```bash
# Terminal 1
python dummy_robot.py --robot_name robot_01 --loop_radius 10.0

# Terminal 2
python dummy_robot.py --robot_name robot_02 --loop_radius 15.0

# Terminal 3
python dummy_robot.py --robot_name robot_03 --loop_radius 20.0
```

Register each robot:
```bash
./register_robot.sh robot_01
./register_robot.sh robot_02
./register_robot.sh robot_03
```

## Next Steps

- **Customize Movement**: Edit `_update_position()` in `dummy_robot.py`
- **Add Actions**: Implement custom actions in `_handle_instant_actions()`
- **Add Images**: Modify `_publish_node_update()` to include camera images
- **Test Missions**: Create complex mission trees with multiple waypoints
- **Monitor Performance**: Use the metrics and telemetry features

## Useful Commands

```bash
# View all MQTT topics
mosquitto_sub -h localhost -p 1883 -t "#" -v

# View robot state continuously
watch -n 1 'curl -s http://localhost:5000/robot/dummy_robot_01 | jq'

# View graph nodes continuously
watch -n 1 'curl -s http://localhost:6001/maps/default/nodes | jq ".nodes | length"'

# Restart just the dummy robot
docker-compose -f docker_compose/mission_dispatch_services_dev.yaml restart dummy-robot

# View dummy robot logs in real-time
docker-compose -f docker_compose/mission_dispatch_services_dev.yaml logs -f dummy-robot
```

