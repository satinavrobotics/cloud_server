# API Quick Start Guide

This guide provides a quick introduction to the Robot Fleet Management System API for frontend developers.

## Getting Started

### Base URL
```
http://localhost:8000
```

### WebSocket URL
```
ws://localhost:8000
```

---

## 5-Minute Quick Start

### 1. Check Service Health

```bash
curl http://localhost:8000/health
```

### 2. List Available Robots

```bash
curl http://localhost:8000/api/v1/robots
```

### 3. Navigate a Robot

```bash
curl -X POST http://localhost:8000/api/v1/navigate \
  -H "Content-Type: application/json" \
  -d '{
    "robot_name": "carter01",
    "target_x": 10.5,
    "target_y": 20.3
  }'
```

### 4. Monitor Mission Progress (WebSocket)

```javascript
const ws = new WebSocket('ws://localhost:8000/ws/mission/delivery_mission_001');

ws.onmessage = (event) => {
  const update = JSON.parse(event.data);
  console.log(`Progress: ${(update.status.progress * 100).toFixed(1)}%`);
};
```

---

## Common Use Cases

### Use Case 1: Send Robot to Location

```javascript
async function sendRobotToLocation(robotName, x, y) {
  const response = await fetch('http://localhost:8000/api/v1/navigate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      robot_name: robotName,
      target_x: x,
      target_y: y
    })
  });
  
  const result = await response.json();
  return result.mission_name;
}

// Usage
const missionName = await sendRobotToLocation('carter01', 10.5, 20.3);
console.log('Mission started:', missionName);
```

### Use Case 2: Monitor Robot Status

```javascript
const robotWs = new WebSocket('ws://localhost:8000/ws/robot/carter01');

robotWs.onmessage = (event) => {
  const update = JSON.parse(event.data);
  
  console.log('Position:', update.status.pose);
  console.log('Battery:', update.status.battery_level);
  console.log('State:', update.status.state);
};
```

### Use Case 3: Display Map Updates

```javascript
const mapWs = new WebSocket('ws://localhost:8000/ws/map/warehouse_floor_1');

mapWs.onmessage = (event) => {
  const update = JSON.parse(event.data);
  
  if (update.type === 'node_added') {
    console.log('New node:', update.node);
    console.log('New edges:', update.edges);
    
    // Update your map visualization
    addNodeToMap(update.node);
    update.edges.forEach(edge => addEdgeToMap(edge));
  }
};
```

### Use Case 4: Get Robot Image

```javascript
async function getRobotImage(mapId, nodeId) {
  const response = await fetch(
    `http://localhost:8000/api/v1/images/${mapId}/${nodeId}`
  );
  
  const blob = await response.blob();
  const imageUrl = URL.createObjectURL(blob);
  
  // Display in img element
  document.getElementById('robot-view').src = imageUrl;
}

// Usage
getRobotImage('warehouse_floor_1', '1001');
```

---

## Key Endpoints Reference

### Robots
- `GET /api/v1/robots` - List all robots
- `GET /api/v1/robots/{robot_name}` - Get robot details
- `POST /api/v1/robots` - Create robot
- `PUT /api/v1/robots/{robot_name}` - Update robot
- `DELETE /api/v1/robots/{robot_name}` - Delete robot

### Missions
- `GET /api/v1/missions` - List all missions
- `GET /api/v1/missions/{mission_name}` - Get mission details
- `POST /api/v1/missions` - Create mission
- `POST /api/v1/missions/{mission_name}/cancel` - Cancel mission

### Navigation
- `POST /api/v1/navigate` - Navigate robot to target
- `POST /api/v1/explore` - Start exploration

### Maps
- `POST /api/v1/map/load` - Load map

### Images
- `GET /api/v1/images/{map_id}/{node_id}` - Get node image

### WebSockets
- `WS /ws/map/{map_id}` - Real-time map updates
- `WS /ws/mission/{mission_name}` - Mission status updates
- `WS /ws/robot/{robot_name}` - Robot status updates

---

## Data Types

### Robot States
- `IDLE` - Robot is idle and available
- `ON_TASK` - Robot is executing a mission
- `CHARGING` - Robot is charging
- `MAP_DEPLOYMENT` - Robot is deploying a map
- `TELEOP` - Robot is in teleoperation mode

### Mission States
- `PENDING` - Mission created but not started
- `RUNNING` - Mission in progress
- `COMPLETED` - Mission completed successfully
- `FAILED` - Mission failed
- `CANCELED` - Mission was cancelled

---

## Error Handling

All errors follow this format:

```json
{
  "detail": "Error message"
}
```

Common HTTP status codes:
- `200` - Success
- `400` - Bad request (validation error)
- `404` - Not found
- `500` - Server error
- `503` - Service unavailable

Example error handling:

```javascript
try {
  const response = await fetch('http://localhost:8000/api/v1/robots/carter01');
  
  if (!response.ok) {
    const error = await response.json();
    console.error('Error:', error.detail);
    return;
  }
  
  const robot = await response.json();
  console.log('Robot:', robot);
  
} catch (error) {
  console.error('Network error:', error);
}
```

---

## Complete Example: Navigation Workflow

```javascript
class RobotController {
  constructor(baseUrl = 'http://localhost:8000') {
    this.baseUrl = baseUrl;
  }
  
  async navigate(robotName, targetX, targetY) {
    // 1. Check robot is available
    const robot = await this.getRobot(robotName);
    if (robot.status.state !== 'IDLE') {
      throw new Error(`Robot is ${robot.status.state}`);
    }
    
    // 2. Start navigation
    const response = await fetch(`${this.baseUrl}/api/v1/navigate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        robot_name: robotName,
        target_x: targetX,
        target_y: targetY
      })
    });
    
    const result = await response.json();
    
    // 3. Monitor progress
    return new Promise((resolve, reject) => {
      const ws = new WebSocket(
        `ws://localhost:8000/ws/mission/${result.mission_name}`
      );
      
      ws.onmessage = (event) => {
        const update = JSON.parse(event.data);
        console.log(`Progress: ${(update.status.progress * 100).toFixed(1)}%`);
        
        if (update.status.state === 'COMPLETED') {
          ws.close();
          resolve(update);
        } else if (update.status.state === 'FAILED') {
          ws.close();
          reject(new Error('Mission failed'));
        }
      };
    });
  }
  
  async getRobot(robotName) {
    const response = await fetch(`${this.baseUrl}/api/v1/robots/${robotName}`);
    return await response.json();
  }
}

// Usage
const controller = new RobotController();

controller.navigate('carter01', 10.5, 20.3)
  .then(() => console.log('Navigation completed!'))
  .catch(error => console.error('Navigation failed:', error));
```

---

## Next Steps

1. **Read the Full API Reference**: See [API_REFERENCE.md](./API_REFERENCE.md) for complete documentation
2. **Explore Data Schemas**: Understand the structure of robots, missions, and map data
3. **Review Integration Patterns**: Learn best practices for common workflows
4. **Check Backend Services**: Understand what each backend service does

---

## Architecture Overview

```
Your Frontend Application
         │
         ▼
API Delegation Service (Port 8000)
         │
    ┌────┼────┬────────────┬──────────────┐
    ▼    ▼    ▼            ▼              ▼
Graph  Image  Mission   Mission      Graph
 DB     DB    Planner  Dispatcher   Builder
```

**Key Points:**
- All client requests go through the API Delegation Service
- WebSocket connections are proxied to backend services
- MQTT communication is handled internally (not exposed to clients)
- Spatial queries use R-tree for ultra-fast performance

---

## Support

For detailed information, see:
- **Full API Reference**: [API_REFERENCE.md](./API_REFERENCE.md)
- **Service Health**: `GET http://localhost:8000/health`
- **Service Info**: `GET http://localhost:8000/`

---

## Tips for Frontend Developers

1. **Always check response status** before parsing JSON
2. **Implement WebSocket reconnection logic** for production
3. **Handle errors gracefully** with user-friendly messages
4. **Use TypeScript** for better type safety (see data schemas in API_REFERENCE.md)
5. **Monitor robot battery levels** and warn users when low
6. **Implement loading states** for async operations
7. **Cache robot and mission data** to reduce API calls
8. **Use WebSockets for real-time updates** instead of polling

---

## Common Pitfalls

❌ **Don't poll REST endpoints for real-time data** - Use WebSockets instead

❌ **Don't forget to close WebSocket connections** - Clean up when components unmount

❌ **Don't ignore error responses** - Always check `response.ok`

❌ **Don't hardcode robot names** - Fetch from API dynamically

✅ **Do use WebSockets for real-time updates**

✅ **Do implement proper error handling**

✅ **Do clean up resources (WebSockets, object URLs)**

✅ **Do validate user input before sending to API**

---

Happy coding! 🤖

