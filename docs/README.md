# Robot Fleet Management System - Documentation

Welcome to the Robot Fleet Management System documentation for frontend developers!

## 📚 Documentation Overview

This directory contains comprehensive API documentation for building client-side applications that interact with the robot fleet management system.

### Available Documentation

1. **[API Quick Start Guide](./API_QUICK_START.md)** ⚡
   - Get started in 5 minutes
   - Common use cases with code examples
   - Quick reference for key endpoints
   - **Start here if you're new!**

2. **[API Reference](./API_REFERENCE.md)** 📖
   - Complete API documentation
   - All REST endpoints with request/response examples
   - WebSocket endpoints and message formats
   - Data schemas and TypeScript interfaces
   - Integration patterns and best practices
   - Error handling strategies
   - Backend service details
   - **Your comprehensive reference guide**

---

## 🚀 Quick Start

### 1. Check if the API is running

```bash
curl http://localhost:8000/health
```

Expected response:
```json
{
  "status": "healthy",
  "service": "api_delegation"
}
```

### 2. Explore available endpoints

```bash
curl http://localhost:8000/
```

This returns a list of all available endpoints.

### 3. Try a simple request

```bash
# List all robots
curl http://localhost:8000/api/v1/robots
```

### 4. Read the Quick Start Guide

Open [API_QUICK_START.md](./API_QUICK_START.md) for code examples and common use cases.

---

## 📋 What You'll Find

### REST API Endpoints

The API provides endpoints for:

- **Robot Management**: Create, read, update, delete robots; monitor status
- **Mission Management**: Create missions, monitor progress, cancel missions
- **Navigation**: Send robots to target locations, start exploration
- **Map Operations**: Load maps, query spatial data
- **Image Retrieval**: Get images captured by robots
- **Detection Results**: Access object detection data

### WebSocket Endpoints

Real-time updates via WebSocket:

- **Map Updates**: Real-time topological map building
- **Mission Status**: Live mission progress updates
- **Robot Status**: Real-time robot position, battery, state

### Data Schemas

Complete TypeScript-style interfaces for:

- Robot objects (spec + status)
- Mission objects (spec + status + mission tree)
- Map nodes and edges
- Detection results
- WebSocket message formats

### Integration Patterns

Production-ready code examples:

- Complete navigation workflow
- Real-time map visualization
- Fleet management dashboard
- Image retrieval and display
- Error handling and retry logic
- WebSocket reconnection strategies

---

## 🏗️ System Architecture

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

**Key Points:**
- All client requests go through the **API Delegation Service** (port 8000)
- Backend services are abstracted away - you don't need to know their details
- WebSocket connections are proxied through the API Delegation Service
- MQTT communication is internal - not exposed to clients

---

## 🎯 Common Use Cases

### 1. Send a Robot to a Location

```javascript
const response = await fetch('http://localhost:8000/api/v1/navigate', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    robot_name: 'carter01',
    target_x: 10.5,
    target_y: 20.3
  })
});

const result = await response.json();
console.log('Mission started:', result.mission_name);
```

### 2. Monitor Mission Progress

```javascript
const ws = new WebSocket('ws://localhost:8000/ws/mission/delivery_mission_001');

ws.onmessage = (event) => {
  const update = JSON.parse(event.data);
  console.log(`Progress: ${(update.status.progress * 100).toFixed(1)}%`);
  
  if (update.status.state === 'COMPLETED') {
    console.log('Mission completed!');
    ws.close();
  }
};
```

### 3. Monitor Robot Status

```javascript
const ws = new WebSocket('ws://localhost:8000/ws/robot/carter01');

ws.onmessage = (event) => {
  const update = JSON.parse(event.data);
  console.log('Battery:', update.status.battery_level);
  console.log('Position:', update.status.pose);
};
```

### 4. Watch Map Building in Real-Time

```javascript
const ws = new WebSocket('ws://localhost:8000/ws/map/warehouse_floor_1');

ws.onmessage = (event) => {
  const update = JSON.parse(event.data);
  
  if (update.type === 'node_added') {
    console.log('New node added:', update.node);
    console.log('New edges:', update.edges);
  }
};
```

---

## 🔑 Key Concepts

### Robots

Robots are autonomous mobile robots (AMRs) that can navigate, explore, and perform tasks. Each robot has:

- **Specification**: Name, labels, battery config, heartbeat timeout
- **Status**: Position, battery level, state (IDLE/ON_TASK/CHARGING), online status
- **Lifecycle**: ALIVE, DELETED, PENDING_DELETE

### Missions

Missions are tasks assigned to robots. Each mission has:

- **Mission Tree**: Hierarchical structure of tasks (nodes)
- **Status**: State (PENDING/RUNNING/COMPLETED/FAILED), progress, timestamps
- **Node Types**: Route, Move, Action, Notify, Selector, Sequence

### Maps

Topological maps represent the environment as a graph:

- **Nodes**: Locations with (x, y, yaw) coordinates
- **Edges**: Connections between nodes (bidirectional)
- **Spatial Index**: R-tree for ultra-fast spatial queries

### Real-Time Updates

WebSocket connections provide live updates:

- **Map Updates**: New nodes and edges as robots explore
- **Mission Updates**: Progress, state changes, completion
- **Robot Updates**: Position, battery, state, errors

---

## 📊 Data Flow Examples

### Navigation Workflow

```
1. Client → API: POST /api/v1/navigate
2. API → Mission Planner: Find path
3. Mission Planner → Graph DB: Query map
4. Mission Planner → Mission Dispatcher: Submit mission
5. Mission Dispatcher → Robot: Send VDA5050 order (MQTT)
6. Robot → Mission Dispatcher: Status updates (MQTT)
7. Mission Dispatcher → Client: WebSocket updates
```

### Map Building Workflow

```
1. Robot → Graph Builder: Node update (MQTT)
2. Graph Builder → Image DB: Save images
3. Graph Builder → Graph DB: Radius search (find nearby nodes)
4. Graph Builder → Similarity Service: Check traversability
5. Graph Builder → Graph DB: Save node + edges
6. Graph Builder → Client: WebSocket update (new node/edges)
```

---

## 🛠️ Development Tips

### TypeScript Support

The API Reference includes TypeScript-style interfaces for all data types. Use these to get type safety in your application:

```typescript
interface RobotObject {
  name: string;
  status: {
    pose: { x: number; y: number; theta: number };
    battery_level: number;
    state: 'IDLE' | 'ON_TASK' | 'CHARGING' | 'MAP_DEPLOYMENT' | 'TELEOP';
    online: boolean;
  };
  // ... more fields
}
```

### Error Handling

Always check response status and handle errors:

```javascript
try {
  const response = await fetch('http://localhost:8000/api/v1/robots/carter01');
  
  if (!response.ok) {
    const error = await response.json();
    console.error('Error:', error.detail);
    return;
  }
  
  const robot = await response.json();
  // Use robot data
  
} catch (error) {
  console.error('Network error:', error);
}
```

### WebSocket Reconnection

Implement reconnection logic for production:

```javascript
function connectWithRetry(url, maxRetries = 5) {
  let retries = 0;
  
  function connect() {
    const ws = new WebSocket(url);
    
    ws.onclose = () => {
      if (retries < maxRetries) {
        retries++;
        console.log(`Reconnecting... (attempt ${retries})`);
        setTimeout(connect, 1000 * retries);
      }
    };
    
    return ws;
  }
  
  return connect();
}
```

---

## 🔒 Security Notes

**Current Status**: Development mode with no authentication

**For Production**:
- Implement API key or OAuth 2.0 authentication
- Enable TLS/SSL for all connections
- Use WSS (WebSocket Secure) for WebSocket connections
- Implement rate limiting
- Add request validation and sanitization

See the [API Reference](./API_REFERENCE.md) for detailed security recommendations.

---

## 📈 Performance Characteristics

- **REST API latency**: 10-50ms (typical)
- **WebSocket message latency**: <10ms
- **Spatial queries (k-NN, radius)**: ~2-3ms (using R-tree)
- **Image download**: 100-500ms (depends on size)
- **Map loading**: 100-1000ms (depends on map size)

---

## 🐛 Troubleshooting

### API not responding

```bash
# Check if service is running
curl http://localhost:8000/health

# Check service info
curl http://localhost:8000/
```

### WebSocket connection fails

- Verify the API Delegation Service is running
- Check that you're using `ws://` (not `wss://` in development)
- Ensure the map_id/mission_name/robot_name exists

### Robot not found

```bash
# List all robots
curl http://localhost:8000/api/v1/robots

# Check specific robot
curl http://localhost:8000/api/v1/robots/carter01
```

### Mission not starting

- Verify robot exists and is in IDLE state
- Check robot battery level (must be above critical level)
- Ensure robot is online

---

## 📞 Support

For questions or issues:

1. Check the [API Reference](./API_REFERENCE.md) for detailed documentation
2. Review the [Quick Start Guide](./API_QUICK_START.md) for examples
3. Check service health: `GET http://localhost:8000/health`
4. View service info: `GET http://localhost:8000/`

---

## 📝 Document Structure

```
docs/
├── README.md              # This file - documentation overview
├── API_QUICK_START.md     # Quick start guide with examples
└── API_REFERENCE.md       # Complete API reference
```

---

## 🎓 Learning Path

**Beginner** (30 minutes):
1. Read this README
2. Follow the Quick Start Guide
3. Try the example code snippets

**Intermediate** (2 hours):
1. Read the complete API Reference
2. Understand data schemas
3. Review integration patterns
4. Build a simple robot controller

**Advanced** (1 day):
1. Build a complete fleet management dashboard
2. Implement real-time map visualization
3. Add error handling and retry logic
4. Optimize WebSocket connections

---

## 🚀 Next Steps

1. **Start with the Quick Start**: [API_QUICK_START.md](./API_QUICK_START.md)
2. **Explore the API Reference**: [API_REFERENCE.md](./API_REFERENCE.md)
3. **Build something awesome!** 🤖

---

Happy coding! If you have questions or feedback, please reach out to the backend development team.

