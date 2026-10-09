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

# Run from the repo root so compose resolves the project (container names are not fixed)
COMPOSE_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/docker_compose/mission_dispatch_services.yaml"
compose() { docker compose -f "$COMPOSE_FILE" "$@"; }

# Check ArangoDB (any HTTP answer, incl. 401 with auth on, means it is up; no credentials needed)
printf "%-20s " "ArangoDB:"
arango_code=$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:8529/_api/version" 2>/dev/null)
if [ "$arango_code" = "200" ] || [ "$arango_code" = "401" ]; then
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

# Check PostgreSQL (pg_isready inside the compose service; user taken from the container's own env)
printf "%-20s " "PostgreSQL:"
if compose exec -T postgres sh -c 'pg_isready -U "${POSTGRES_USER:-postgres}"' > /dev/null 2>&1; then
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
echo "Self-hosted LiveKit (livekit-sfu services, main compose file):"
echo "==========================================================================="

printf "%-20s " "LiveKit SFU:"
if curl -s -f "http://localhost:7880/" > /dev/null 2>&1; then
  echo "✅ RUNNING"
else
  echo "❌ DOWN"
fi

printf "%-20s " "LiveKit SFU tokens:"
if curl -s -f "http://localhost:8008/health" > /dev/null 2>&1; then
  echo "✅ HEALTHY"
else
  echo "❌ UNHEALTHY"
fi

printf "%-20s " "Mission Dispatch:"
md_id=$(compose ps -q mission-dispatch 2>/dev/null | head -n1)
md_status=""
[ -n "$md_id" ] && md_status=$(docker inspect -f '{{if .State.Running}}{{if .State.Health}}{{.State.Health.Status}}{{else}}running{{end}}{{else}}{{.State.Status}}{{end}}' "$md_id" 2>/dev/null)
case "$md_status" in
  healthy) echo "✅ HEALTHY" ;;
  "") echo "❌ DOWN" ;;
  *) echo "❌ UNHEALTHY ($md_status)" ;;
esac

echo ""
echo "Docker Containers:"
echo "=================="
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" | grep -E "NAME|arangodb|minio|mosquitto|postgres|mission-dispatch|graph-builder|mission-planner|livekit|agent-orchestrator|api-delegation"

