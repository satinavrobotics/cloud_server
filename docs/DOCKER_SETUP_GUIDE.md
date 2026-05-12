# SATI Cloud Server - Docker Setup Guide

Complete guide for setting up all microservices using Docker.

## Table of Contents
- [Overview](#overview)
- [Prerequisites](#prerequisites)
- [Architecture](#architecture)
- [Quick Start](#quick-start)
- [Service Details](#service-details)
- [Building Docker Images](#building-docker-images)
- [Running Services](#running-services)
- [Verification](#verification)
- [Troubleshooting](#troubleshooting)

---

## Overview

The SATI Cloud Server consists of the following microservices:

1. **Image Database Service** (port 6002) - Stores and retrieves robot images using MinIO
2. **Graph Database Service** (port 6001) - Manages topological maps using ArangoDB
3. **Similarity Service** (port 8003) - Computes image similarity for loop closure
4. **Graph Builder Service** (port 8004) - Builds topological maps from robot data
5. **Mission Planner Service** (port 8005) - Plans navigation missions using graph search
6. **API Delegation Service** (port 8000) - Central API gateway with REST and WebSocket interfaces

---

## Prerequisites

### Required Software
- Docker (version 20.10+)
- Docker Compose (version 1.29+)

### System Requirements
- 8GB RAM minimum (16GB recommended)
- 20GB free disk space
- Linux/macOS/Windows with WSL2

### Check Installation
```bash
docker --version
docker-compose --version
```

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    API Delegation Service                    │
│                        (Port 8000)                           │
│              REST API + WebSocket Gateway                    │
└────────┬──────────────┬──────────────┬─────────────┬────────┘
         │              │              │             │
         ▼              ▼              ▼             ▼
┌────────────┐  ┌──────────────┐  ┌─────────┐  ┌──────────────┐
│   Image    │  │    Graph     │  │ Mission │  │   Mission    │
│  Database  │  │   Database   │  │ Planner │  │  Dispatcher  │
│ (Port 6002)│  │ (Port 6001)  │  │(Port 8005)│ │  Database   │
└─────┬──────┘  └──────┬───────┘  └────┬────┘  └──────────────┘
      │                │               │
      ▼                ▼               │
┌──────────┐    ┌──────────┐          │
│  MinIO   │    │ ArangoDB │          │
│(Port 9000)│   │(Port 8529)│         │
└──────────┘    └──────────┘          │
                                      │
         ┌────────────────────────────┘
         │
         ▼
┌─────────────────┐      ┌──────────────┐
│ Graph Builder   │─────▶│ Similarity   │
│  (Port 8004)    │      │   Service    │
│                 │      │ (Port 8003)  │
└────────┬────────┘      └──────────────┘
         │
         ▼
    ┌────────┐
    │  MQTT  │
    │Mosquitto│
    └────────┘
```

---

## Quick Start

### 1. Clone and Navigate
```bash
cd /home/satiadmin/satinavrobotics/cloud_server
```

### 2. Build All Docker Images
```bash
# Build Image Database Service
docker build -t image_db_service:latest -f packages/topomap_dbs/image_db/Dockerfile packages/topomap_dbs/image_db/

# Build Graph Database Service
docker build -t graph_db_service:latest -f packages/topomap_dbs/graph_db/Dockerfile packages/topomap_dbs/graph_db/

# Build Similarity Service
docker build -t similarity_service:latest -f packages/services/similarity_service/Dockerfile packages/services/similarity_service/

# Build Graph Builder Service (from repo root)
docker build -t graph_builder_service:latest -f packages/services/graph_builder/Dockerfile .

# Build Mission Planner Service (from repo root)
docker build -t mission_planner_service:latest -f packages/services/mission_planner/Dockerfile .

# Build API Delegation Service (from repo root)
docker build -t api_delegation_service:latest -f packages/api/Dockerfile .
```

### 3. Start All Services
```bash
cd docker_compose
docker-compose -f mission_dispatch_services.yaml up -d
```

### 4. Verify Services
```bash
# Check all containers are running
docker-compose -f mission_dispatch_services.yaml ps

# Check logs
docker-compose -f mission_dispatch_services.yaml logs -f
```

---

## Service Details

### 1. Image Database Service

**Purpose:** Stores and retrieves robot camera images organized by map and node.

**Dependencies:**
- MinIO (object storage)

**Environment Variables:**
```bash
MINIO_HOST=localhost
MINIO_PORT=9000
MINIO_ACCESS_KEY=minioadmin
MINIO_SECRET_KEY=minioadmin
MINIO_SECURE=false
DEFAULT_MAP_ID=default
IMAGE_DB_PORT=6002
```

**Dockerfile Location:** `packages/topomap_dbs/image_db/Dockerfile`

**Build Command:**
```bash
docker build -t image_db_service:latest \
  -f packages/topomap_dbs/image_db/Dockerfile \
  packages/topomap_dbs/image_db/
```

**Run Standalone:**
```bash
docker run -d \
  --name image-db \
  --network host \
  -e MINIO_HOST=localhost \
  -e MINIO_PORT=9000 \
  -e MINIO_ACCESS_KEY=minioadmin \
  -e MINIO_SECRET_KEY=minioadmin \
  image_db_service:latest
```

**Health Check:**
```bash
curl http://localhost:6002/health
```

---

### 2. Graph Database Service

**Purpose:** Manages topological maps with spatial indexing for efficient pathfinding.

**Dependencies:**
- ArangoDB (graph database)

**Environment Variables:**
```bash
ARANGO_HOST=localhost
ARANGO_PORT=8529
ARANGO_USERNAME=root
ARANGO_PASSWORD=openSesame
DATABASE_NAME=topomap_db
GRAPH_NAME=topological_map
USE_SPATIAL_INDEX=true
REBUILD_THRESHOLD=100
GRAPH_DB_PORT=6001
```

**Dockerfile Location:** `packages/topomap_dbs/graph_db/Dockerfile`

**Build Command:**
```bash
docker build -t graph_db_service:latest \
  -f packages/topomap_dbs/graph_db/Dockerfile \
  packages/topomap_dbs/graph_db/
```

**Run Standalone:**
```bash
docker run -d \
  --name graph-db \
  --network host \
  -e ARANGO_HOST=localhost \
  -e ARANGO_PORT=8529 \
  -e ARANGO_USERNAME=root \
  -e ARANGO_PASSWORD=openSesame \
  graph_db_service:latest
```

**Health Check:**
```bash
curl http://localhost:6001/health
```

---

### 3. Similarity Service

**Purpose:** Computes image similarity scores for loop closure detection.

**Dependencies:** None (standalone service)

**Environment Variables:**
```bash
SIMILARITY_SERVICE_PORT=8003
```

**Dockerfile Location:** `packages/services/similarity_service/Dockerfile`

**Build Command:**
```bash
docker build -t similarity_service:latest \
  -f packages/services/similarity_service/Dockerfile \
  packages/services/similarity_service/
```

**Run Standalone:**
```bash
docker run -d \
  --name similarity-service \
  -p 8003:8003 \
  similarity_service:latest
```

**Health Check:**
```bash
curl http://localhost:8003/health
```

---

### 4. Graph Builder Service

**Purpose:** Listens to robot updates via MQTT and builds topological maps.

**Dependencies:**
- MQTT Broker (Mosquitto)
- Image Database Service
- Graph Database Service
- Similarity Service

**Environment Variables:**
```bash
MQTT_HOST=localhost
MQTT_PORT=1883
MQTT_TOPIC=robot/node_update
IMAGE_DB_URL=http://localhost:6002
GRAPH_DB_URL=http://localhost:6001
SIMILARITY_SERVICE_URL=http://localhost:8003
RADIUS_THRESHOLD=5.0
DEFAULT_MAP_ID=default
GRAPH_BUILDER_PORT=8004
```

**Dockerfile Location:** `packages/services/graph_builder/Dockerfile`

**Build Command:**
```bash
# Build from repository root
docker build -t graph_builder_service:latest \
  -f packages/services/graph_builder/Dockerfile \
  .
```

**Run Standalone:**
```bash
docker run -d \
  --name graph-builder \
  --network host \
  -e MQTT_HOST=localhost \
  -e IMAGE_DB_URL=http://localhost:6002 \
  -e GRAPH_DB_URL=http://localhost:6001 \
  -e SIMILARITY_SERVICE_URL=http://localhost:8003 \
  graph_builder_service:latest
```

**Health Check:**
```bash
curl http://localhost:8004/health
```

---

### 5. Mission Planner Service

**Purpose:** Plans navigation missions using KNN and pathfinding on the topological map.

**Dependencies:**
- Graph Database Service
- Mission Dispatcher Database

**Environment Variables:**
```bash
GRAPH_DB_URL=http://localhost:6001
DATABASE_URL=http://localhost:5000
DEFAULT_MAP_ID=default
KNN_K=1
RANGE_SEARCH_RADIUS=5.0
MISSION_PLANNER_PORT=8005
```

**Dockerfile Location:** `packages/services/mission_planner/Dockerfile`

**Build Command:**
```bash
# Build from repository root
docker build -t mission_planner_service:latest \
  -f packages/services/mission_planner/Dockerfile \
  .
```

**Run Standalone:**
```bash
docker run -d \
  --name mission-planner \
  --network host \
  -e GRAPH_DB_URL=http://localhost:6001 \
  -e DATABASE_URL=http://localhost:5000 \
  mission_planner_service:latest
```

**Health Check:**
```bash
curl http://localhost:8005/health
```

---

### 6. API Delegation Service

**Purpose:** Central API gateway providing unified REST and WebSocket interfaces for all services.

**Dependencies:**
- Graph Database Service
- Image Database Service
- Mission Planner Service
- Mission Dispatcher Database

**Environment Variables:**
```bash
GRAPH_DB_URL=http://localhost:6001
IMAGE_DB_URL=http://localhost:6002
MISSION_PLANNER_URL=http://localhost:8005
DATABASE_URL=http://localhost:5000
DEFAULT_MAP_ID=default
API_PORT=8000
```

**Dockerfile Location:** `packages/api/Dockerfile`

**Build Command:**
```bash
# Build from repository root
docker build -t api_delegation_service:latest \
  -f packages/api/Dockerfile \
  .
```

**Run Standalone:**
```bash
docker run -d \
  --name api-delegation \
  --network host \
  -e GRAPH_DB_URL=http://localhost:6001 \
  -e IMAGE_DB_URL=http://localhost:6002 \
  -e MISSION_PLANNER_URL=http://localhost:8005 \
  -e DATABASE_URL=http://localhost:5000 \
  api_delegation_service:latest
```

**Health Check:**
```bash
curl http://localhost:8000/health
```

**API Documentation:**
```bash
# Open interactive API docs
open http://localhost:8000/docs
```

---

## Building Docker Images

### Build All Images at Once

A build script `scripts/build_all.sh` is provided for convenience.

Run it from the repository root:
```bash
./scripts/build_all.sh
```

### Verify Built Images
```bash
docker images | grep -E "image_db|graph_db|similarity|graph_builder|mission_planner|api_delegation"
```

---

## Running Services

### Option 1: Using Docker Compose (Recommended)

The `docker_compose/mission_dispatch_services.yaml` file orchestrates all services.

**Start all services:**
```bash
cd docker_compose
docker-compose -f mission_dispatch_services.yaml up -d
```

**View logs:**
```bash
# All services
docker-compose -f mission_dispatch_services.yaml logs -f

# Specific service
docker-compose -f mission_dispatch_services.yaml logs -f graph-db-service
docker-compose -f mission_dispatch_services.yaml logs -f image-db-service
docker-compose -f mission_dispatch_services.yaml logs -f mission-planner
```

**Stop all services:**
```bash
docker-compose -f mission_dispatch_services.yaml down
```

**Stop and remove volumes:**
```bash
docker-compose -f mission_dispatch_services.yaml down -v
```

### Option 2: Start Services Individually

**1. Start Infrastructure Services:**
```bash
# ArangoDB
docker run -d --name arangodb \
  --network host \
  -e ARANGO_ROOT_PASSWORD=openSesame \
  arangodb/arangodb:latest

# MinIO
docker run -d --name minio \
  --network host \
  -e MINIO_ROOT_USER=minioadmin \
  -e MINIO_ROOT_PASSWORD=minioadmin \
  minio/minio server /data --console-address ":9001"

# MQTT Mosquitto
docker run -d --name mosquitto \
  --network host \
  eclipse-mosquitto:latest

# PostgreSQL
docker run -d --name postgres \
  --network host \
  -e POSTGRES_USER=postgres \
  -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=mission \
  postgres:14.5
```

**2. Start Database Services:**
```bash
# Wait for ArangoDB to be ready
sleep 10

# Graph Database Service
docker run -d --name graph-db \
  --network host \
  -e ARANGO_HOST=localhost \
  -e ARANGO_PORT=8529 \
  -e ARANGO_PASSWORD=openSesame \
  graph_db_service:latest

# Image Database Service
docker run -d --name image-db \
  --network host \
  -e MINIO_HOST=localhost \
  -e MINIO_PORT=9000 \
  image_db_service:latest
```

**3. Start Application Services:**
```bash
# Similarity Service
docker run -d --name similarity-service \
  -p 8003:8003 \
  similarity_service:latest

# Graph Builder Service
docker run -d --name graph-builder \
  --network host \
  -e MQTT_HOST=localhost \
  -e IMAGE_DB_URL=http://localhost:6002 \
  -e GRAPH_DB_URL=http://localhost:6001 \
  -e SIMILARITY_SERVICE_URL=http://localhost:8003 \
  graph_builder_service:latest

# Mission Planner Service
docker run -d --name mission-planner \
  --network host \
  -e GRAPH_DB_URL=http://localhost:6001 \
  -e DATABASE_URL=http://localhost:5000 \
  mission_planner_service:latest

# API Delegation Service
docker run -d --name api-delegation \
  --network host \
  -e GRAPH_DB_URL=http://localhost:6001 \
  -e IMAGE_DB_URL=http://localhost:6002 \
  -e MISSION_PLANNER_URL=http://localhost:8005 \
  -e DATABASE_URL=http://localhost:5000 \
  api_delegation_service:latest
```

---

## Verification

### Check Service Health

A health check script `scripts/check_health.sh` is provided for convenience.

Run it from the repository root:
```bash
./scripts/check_health.sh
```

### Test API Endpoints

```bash
# Test API Delegation Service
curl http://localhost:8000/

# Test Graph Database
curl http://localhost:6001/stats

# Test Image Database
curl http://localhost:6002/stats

# Test Mission Planner
curl http://localhost:8005/stats

# Test Similarity Service
curl http://localhost:8003/health
```

---

## Troubleshooting

### Common Issues

#### 1. Port Already in Use
```bash
# Find process using port
sudo lsof -i :6001
sudo lsof -i :8000

# Kill process
sudo kill -9 <PID>
```

#### 2. Container Won't Start
```bash
# Check logs
docker logs <container-name>

# Check if dependencies are running
docker ps

# Restart container
docker restart <container-name>
```

#### 3. Service Can't Connect to Database
```bash
# Check network connectivity
docker exec <container-name> ping localhost

# Verify environment variables
docker exec <container-name> env | grep -E "ARANGO|MINIO|GRAPH_DB"

# Check if database is ready
curl http://localhost:8529/_api/version  # ArangoDB
curl http://localhost:9000/minio/health/live  # MinIO
```

#### 4. Out of Memory
```bash
# Check Docker memory usage
docker stats

# Increase Docker memory limit (Docker Desktop)
# Settings -> Resources -> Memory -> Increase to 8GB+

# Clean up unused containers/images
docker system prune -a
```

#### 5. Build Failures
```bash
# Clear Docker build cache
docker builder prune -a

# Rebuild without cache
docker build --no-cache -t <image-name> -f <dockerfile> <context>
```

### Logs and Debugging

```bash
# View all logs
docker-compose -f docker_compose/mission_dispatch_services.yaml logs -f

# View specific service logs
docker logs -f graph-db-service
docker logs -f mission-planner

# Enter container for debugging
docker exec -it <container-name> /bin/bash

# Check container resource usage
docker stats
```

### Reset Everything

```bash
# Stop all containers
docker-compose -f docker_compose/mission_dispatch_services.yaml down -v

# Remove all SATI images
docker rmi image_db_service:latest graph_db_service:latest \
  similarity_service:latest graph_builder_service:latest \
  mission_planner_service:latest api_delegation_service:latest

# Clean up Docker system
docker system prune -a --volumes

# Rebuild and restart
./scripts/build_all.sh
cd docker_compose
docker-compose -f mission_dispatch_services.yaml up -d
```

---

## Environment Configuration

### Configuration File: `docker_compose/.env`

All services use environment variables defined in `docker_compose/.env`:

```bash
# Docker Images
GRAPH_DB_SERVICE_IMAGE=graph_db_service:latest
IMAGE_DB_SERVICE_IMAGE=image_db_service:latest

# ArangoDB
ARANGO_PORT=8529
ARANGO_ROOT_PASSWORD=openSesame

# Graph Database Service
GRAPH_DB_PORT=6001
GRAPH_DB_NAME=topomap_db
GRAPH_NAME=topological_map

# MinIO
MINIO_PORT=9000
MINIO_CONSOLE_PORT=9001
MINIO_ROOT_USER=minioadmin
MINIO_ROOT_PASSWORD=minioadmin

# Image Database Service
IMAGE_DB_PORT=6002
IMAGE_DB_DEFAULT_MAP=default

# MQTT
MQTT_PORT_TCP=1883
MQTT_PORT_WEBSOCKET=9001

# Mission Database
DATABASE_API_PORT=5000
DATABASE_CONTROLLER_PORT=5001
POSTGRES_DATABASE_PORT=5432
```

To customize, edit `docker_compose/.env` before starting services.

---

## Next Steps

1. **Build all Docker images** using the build script
2. **Start services** with Docker Compose
3. **Verify health** of all services
4. **Test API endpoints** to ensure connectivity
5. **Check logs** for any errors
6. **Integrate with robots** by configuring MQTT topics

For detailed API documentation, see:
- `packages/api/README.md` - API Delegation Service
- `packages/topomap_dbs/graph_db/README.md` - Graph Database
- `packages/topomap_dbs/image_db/README.md` - Image Database
- `packages/services/mission_planner/README.md` - Mission Planner

---

## Quick Reference

### Service Ports
| Service | Port | Protocol |
|---------|------|----------|
| API Delegation | 8000 | HTTP/WebSocket |
| Graph Database | 6001 | HTTP |
| Image Database | 6002 | HTTP |
| Similarity Service | 8003 | HTTP |
| Graph Builder | 8004 | HTTP |
| Mission Planner | 8005 | HTTP |
| ArangoDB | 8529 | HTTP |
| MinIO | 9000 | HTTP |
| MinIO Console | 9001 | HTTP |
| MQTT (TCP) | 1883 | MQTT |
| MQTT (WebSocket) | 9001 | MQTT/WS |
| PostgreSQL | 5432 | PostgreSQL |

### Docker Commands Cheat Sheet
```bash
# Build all images
./scripts/build_all.sh

# Start all services
cd docker_compose && docker-compose -f mission_dispatch_services.yaml up -d

# Stop all services
docker-compose -f mission_dispatch_services.yaml down

# View logs
docker-compose -f mission_dispatch_services.yaml logs -f

# Check health
./scripts/check_health.sh

# Restart a service
docker-compose -f mission_dispatch_services.yaml restart <service-name>

# Clean up
docker-compose -f mission_dispatch_services.yaml down -v
docker system prune -a
```


