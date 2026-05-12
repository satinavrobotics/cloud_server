#!/bin/bash

# Start Nginx Gateway for Sati Client
# This script starts the Nginx reverse proxy in the cloud_server directory

CLOUD_SERVER_DIR="/home/satiadmin/satinavrobotics/cloud_server"

echo "🌐 Starting Nginx Gateway..."

# Check if cloud_server directory exists
if [ ! -d "$CLOUD_SERVER_DIR" ]; then
    echo "❌ Error: Cloud server directory not found at $CLOUD_SERVER_DIR"
    exit 1
fi

# Check if docker-compose file exists
if [ ! -f "$CLOUD_SERVER_DIR/docker_compose/nginx-gateway.yaml" ]; then
    echo "❌ Error: Nginx gateway docker-compose file not found"
    exit 1
fi

# Check if Nginx is already running
if docker ps | grep -q "sati_nginx_gateway"; then
    echo "✅ Nginx gateway is already running"
    exit 0
fi

# Start Nginx gateway
cd "$CLOUD_SERVER_DIR"
docker compose -f docker_compose/nginx-gateway.yaml up -d

# Wait a moment for it to start
sleep 2

# Check if it started successfully
if docker ps | grep -q "sati_nginx_gateway"; then
    echo "✅ Nginx gateway started successfully"
    echo "   Access API at: http://mlflow.satinavrobotics.com/api/v1/robots"
else
    echo "⚠️  Nginx gateway may not have started correctly"
    echo "   Check logs with: docker compose -f docker_compose/nginx-gateway.yaml logs"
fi

