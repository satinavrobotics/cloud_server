#!/bin/bash

# Start the Nginx gateway (the single front door for the Sati client and the cloud services).
# Config and routing live in docker_compose/nginx/ (routes.conf is shared with the robot gateway).

CLOUD_SERVER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="docker_compose/nginx-gateway.yaml"

echo "🌐 Starting Nginx Gateway..."

if [ ! -f "$CLOUD_SERVER_DIR/$COMPOSE_FILE" ]; then
    echo "❌ Error: Nginx gateway docker-compose file not found: $CLOUD_SERVER_DIR/$COMPOSE_FILE"
    exit 1
fi

# Check if Nginx is already running
if docker ps | grep -q "sati_nginx_gateway"; then
    echo "✅ Nginx gateway is already running"
    echo "   (after editing docker_compose/nginx/: docker exec sati_nginx_gateway nginx -s reload;"
    echo "    after changing the compose file or the mounted paths: docker compose -f $COMPOSE_FILE up -d)"
    exit 0
fi

cd "$CLOUD_SERVER_DIR"
docker compose -f "$COMPOSE_FILE" up -d

# Wait a moment for it to start
sleep 2

if docker ps | grep -q "sati_nginx_gateway"; then
    echo "✅ Nginx gateway started successfully"
    echo "   Access API at: http://mlflow.satinavrobotics.com/api/v1/robots"
else
    echo "⚠️  Nginx gateway may not have started correctly"
    echo "   Check logs with: docker compose -f $COMPOSE_FILE logs"
fi
