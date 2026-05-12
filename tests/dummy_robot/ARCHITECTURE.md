# Dummy Robot Architecture

## Overview

The Dummy Robot is a simulated robot service designed for testing and development of the Mission Dispatch and Graph Builder systems. It implements the full VDA5050 protocol and publishes topological map nodes.

## System Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         MQTT Broker                              │
│                        (Mosquitto)                               │
└────────┬────────────────────────────┬────────────────────────────┘
         │                            │
         │ VDA5050 Topics             │ Node Updates
         │                            │
    ┌────▼─────────┐            ┌─────▼──────────┐
    │   Mission    │            │     Graph      │
    │   Dispatch   │            │    Builder     │
    └────┬─────────┘            └─────┬──────────┘
         │                            │
         │ Orders                     │ Nodes/Edges
         │                            │
    ┌────▼─────────┐            ┌─────▼──────────┐
    │   Mission    │            │     Graph      │
    │   Database   │            │    Database    │
    └──────────────┘            └────────────────┘
         ▲                            ▲
         │                            │
         │ REST API                   │ REST API
         │                            │
    ┌────┴─────────────────────────────┴──────────┐
    │           Dummy Robot Service                │
    │  - VDA5050 State Publishing                  │
    │  - Order Reception & Processing              │
    │  - Node Publishing for Graph Building        │
    │  - Simulated Movement                        │
    └──────────────────────────────────────────────┘
```

## Components

### 1. Dummy Robot Service

**File**: `tests/dummy_robot/dummy_robot.py`

**Responsibilities**:
- Publish VDA5050 state messages
- Listen for VDA5050 orders
- Publish node updates for graph building
- Simulate robot movement

**Key Classes**:
- `DummyRobot`: Main robot controller

**Key Methods**:
- `_publish_state()`: Publishes VDA5050 state
- `_publish_factsheet()`: Publishes robot capabilities
- `_publish_node_update()`: Publishes topological nodes
- `_handle_order()`: Processes incoming missions
- `_update_position()`: Simulates movement

### 2. VDA5050 Communication

**MQTT Topics**:

| Topic | Direction | Message Type | Purpose |
|-------|-----------|--------------|---------|
| `uagv/v2/RobotCompany/{robot}/state` | Publish | VDA5050State | Robot status updates |
| `uagv/v2/RobotCompany/{robot}/factsheet` | Publish | VDA5050Factsheet | Robot capabilities |
| `uagv/v2/RobotCompany/{robot}/order` | Subscribe | VDA5050Order | Mission orders |
| `uagv/v2/RobotCompany/{robot}/instantActions` | Subscribe | VDA5050InstantActions | Instant commands |

**Message Structures**:

#### VDA5050State
```json
{
  "headerId": 123,
  "timestamp": "2024-01-01T12:00:00Z",
  "version": "2.0.0",
  "manufacturer": "DummyManufacturer",
  "serialNumber": "DUMMY001",
  "orderId": "mission-123",
  "orderUpdateId": 0,
  "agvPosition": {
    "x": 10.0,
    "y": 5.0,
    "theta": 1.57,
    "mapId": "default"
  },
  "batteryState": {
    "batteryCharge": 85.5,
    "charging": false
  },
  "driving": true,
  "safetyState": {
    "eStop": "NONE",
    "fieldViolation": false
  }
}
```

#### VDA5050Order
```json
{
  "headerId": 456,
  "timestamp": "2024-01-01T12:00:00Z",
  "orderId": "mission-123",
  "orderUpdateId": 0,
  "nodes": [
    {
      "nodeId": "mission-123-n0-s0",
      "sequenceId": 0,
      "nodePosition": {
        "x": 5.0,
        "y": 5.0,
        "theta": 0.0,
        "mapId": "default"
      }
    }
  ],
  "edges": []
}
```

### 3. Node Publishing

**MQTT Topic**: `robot/node_update`

**Message Structure**:
```json
{
  "node_id": 1000,
  "x": 10.5,
  "y": 20.3,
  "yaw": 1.57,
  "map_id": "default",
  "images": [],
  "metadata": {
    "robot_name": "dummy_robot_01",
    "timestamp": "2024-01-01T12:00:00Z",
    "battery": 85.5,
    "node_type": "auto_generated"
  }
}
```

**Publishing Frequency**: Every 5 seconds (configurable)

### 4. Movement Simulation

**Pattern**: Circular movement

**Algorithm**:
```python
angle += (speed * tick_period) / loop_radius
x = loop_radius * cos(angle)
y = loop_radius * sin(angle)
theta = angle + π/2  # Tangent to circle
```

**Parameters**:
- `loop_radius`: Radius of circular path (default: 10.0 meters)
- `speed`: Linear velocity (default: 1.0 m/s)
- `tick_period`: Update interval (default: 1.0 seconds)

## Data Flow

### Mission Execution Flow

```
1. User creates mission via REST API
   ↓
2. Mission Database stores mission
   ↓
3. Mission Dispatch detects new mission
   ↓
4. Mission Dispatch converts to VDA5050 Order
   ↓
5. Order published to MQTT: uagv/v2/RobotCompany/{robot}/order
   ↓
6. Dummy Robot receives order
   ↓
7. Dummy Robot acknowledges in state message
   ↓
8. Dummy Robot simulates execution
   ↓
9. State updates published to MQTT
   ↓
10. Mission Dispatch updates mission status
```

### Graph Building Flow

```
1. Dummy Robot moves to new position
   ↓
2. Node update published to MQTT: robot/node_update
   ↓
3. Graph Builder receives node update
   ↓
4. Graph Builder saves node to Graph Database
   ↓
5. Graph Builder finds nearby nodes
   ↓
6. Graph Builder creates edges
   ↓
7. Edges saved to Graph Database
   ↓
8. WebSocket update sent to clients
```

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DUMMY_ROBOT_NAME` | `dummy_robot_01` | Robot identifier |
| `DUMMY_ROBOT_LOOP_RADIUS` | `10.0` | Movement radius (meters) |
| `DUMMY_ROBOT_SPEED` | `1.0` | Movement speed (m/s) |
| `DUMMY_ROBOT_TICK_PERIOD` | `1.0` | Update interval (seconds) |
| `MQTT_HOST` | `localhost` | MQTT broker host |
| `MQTT_PORT_TCP` | `1883` | MQTT broker port |
| `MQTT_PREFIX` | `uagv/v2/RobotCompany` | VDA5050 topic prefix |
| `GRAPH_BUILDER_MQTT_TOPIC` | `robot/node_update` | Node update topic |
| `IMAGE_DB_DEFAULT_MAP` | `default` | Map identifier |

### Command Line Arguments

All environment variables can be overridden via command line:

```bash
python dummy_robot.py \
  --robot_name custom_robot \
  --mqtt_host mqtt.example.com \
  --loop_radius 20.0 \
  --speed 2.0
```

## Integration Points

### Mission Database
- **Endpoint**: `http://localhost:5000`
- **Purpose**: Robot registration, mission storage
- **API**: REST API

### Mission Dispatch
- **Protocol**: MQTT (VDA5050)
- **Purpose**: Mission orchestration
- **Topics**: See VDA5050 Communication section

### Graph Builder
- **Protocol**: MQTT (custom)
- **Purpose**: Topological map building
- **Topic**: `robot/node_update`

### Graph Database
- **Endpoint**: `http://localhost:6001`
- **Purpose**: Store nodes and edges
- **API**: REST API

## Testing

### Unit Testing
```bash
# Run dummy robot in test mode
python dummy_robot.py --tick_period 0.1 --no_nodes
```

### Integration Testing
```bash
# Run full integration test
./test_integration.sh
```

### Manual Testing
```bash
# Monitor MQTT
mosquitto_sub -h localhost -p 1883 -t "#" -v

# Check robot status
curl http://localhost:5000/robot/dummy_robot_01

# Check graph nodes
curl http://localhost:6001/maps/default/nodes
```

## Performance Characteristics

### Resource Usage
- **CPU**: < 1% (idle), ~5% (active)
- **Memory**: ~50 MB
- **Network**: ~1 KB/s (state updates)

### Message Rates
- **VDA5050 State**: 1 Hz (1 message/second)
- **Node Updates**: 0.2 Hz (1 message/5 seconds)
- **Total MQTT**: ~1.2 messages/second

### Scalability
- **Single Robot**: Negligible load
- **10 Robots**: ~12 messages/second
- **100 Robots**: ~120 messages/second

## Limitations

1. **No Real Sensors**: No camera images or lidar data
2. **Simplified Movement**: Only circular pattern
3. **No Collision Detection**: Ignores obstacles
4. **No Action Execution**: Actions are acknowledged but not executed
5. **Fixed Battery Model**: Simple linear drain

## Future Enhancements

1. **Configurable Paths**: Support waypoint-based movement
2. **Image Generation**: Generate synthetic camera images
3. **Action Simulation**: Simulate pick/place actions
4. **Multiple Patterns**: Support different movement patterns
5. **Realistic Physics**: Add acceleration, deceleration
6. **Error Injection**: Simulate failures for testing

## Troubleshooting

See [QUICK_START.md](QUICK_START.md) for common issues and solutions.

