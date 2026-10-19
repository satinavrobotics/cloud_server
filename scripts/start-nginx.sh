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

# Always recreated (a few seconds): the config files are bind-mounted, and when git (or an editor) replaces a file the running
# container keeps the old one, so neither `up -d` (nothing changed in the compose file) nor `nginx -s reload` picks up the edit.
# Server diagnostics (/api/diagnostics/) would show the symptom as a failing gateway check.

cd "$CLOUD_SERVER_DIR"
docker compose -f "$COMPOSE_FILE" up -d --force-recreate

# Wait a moment for it to start
sleep 2

if docker ps | grep -q "sati_nginx_gateway"; then
    echo "✅ Nginx gateway started successfully"
    echo "   Access API at: http://mlflow.satinavrobotics.com/api/v1/robots"
else
    echo "⚠️  Nginx gateway may not have started correctly"
    echo "   Check logs with: docker compose -f $COMPOSE_FILE logs"
fi
