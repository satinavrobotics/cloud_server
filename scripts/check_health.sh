#!/bin/bash

echo "Checking SATI Cloud Server Services..."
echo "========================================"
echo ""

services=(
  "Graph Builder:http://localhost:8004/health"
  "Mission Planner:http://localhost:8005/health"
  "LiveKit Service:http://localhost:8006/health"
  "Agent Orchestrator:http://localhost:8007/health"
  "API Delegation:http://localhost:8000/health"
)

for service in "${services[@]}"; do
  name="${service%%:*}"
  url="${service#*:}"
  
  printf "%-20s " "$name:"
  
  if curl -s -f "$url" > /dev/null 2>&1; then
    echo "✅ HEALTHY"
  else
    echo "❌ UNHEALTHY"
  fi
done

echo ""
echo "Infrastructure Services:"
echo "========================"

# Check ArangoDB
printf "%-20s " "ArangoDB:"
if curl -s -f "http://localhost:8529/_api/version" > /dev/null 2>&1; then
  echo "✅ RUNNING"
else
  echo "❌ DOWN"
fi

# Check MinIO
printf "%-20s " "MinIO:"
if curl -s -f "http://localhost:9000/minio/health/live" > /dev/null 2>&1; then
  echo "✅ RUNNING"
else
  echo "❌ DOWN"
fi

# Check PostgreSQL
printf "%-20s " "PostgreSQL:"
if docker exec postgres pg_isready -U postgres > /dev/null 2>&1; then
  echo "✅ RUNNING"
else
  echo "❌ DOWN"
fi

# Check MQTT
printf "%-20s " "MQTT (Mosquitto):"
if docker ps | grep -q mosquitto; then
  echo "✅ RUNNING"
else
  echo "❌ DOWN"
fi

echo ""
echo "Docker Containers:"
echo "=================="
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" | grep -E "NAME|arangodb|minio|mosquitto|postgres|mission-dispatch|graph-builder|mission-planner|livekit|agent-orchestrator|api-delegation"

