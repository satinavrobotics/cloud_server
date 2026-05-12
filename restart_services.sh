#!/bin/bash

# Script to restart services with fresh code changes
# This rebuilds all custom services (not third-party like postgres, mosquitto, etc.)

COMPOSE_FILE="docker_compose/mission_dispatch_services.yaml"

echo "🔄 Restarting services to pick up code changes..."
echo ""

# Stop all services
echo "⏹️  Stopping services..."
docker compose -f $COMPOSE_FILE down

# Build all custom services
echo "🔨 Rebuilding services with code changes..."
docker compose -f $COMPOSE_FILE build \
    mission-dispatch \
    graph-builder-service \
    mission-planner-service \
    livekit-service \
    api-delegation-service

# Start all services
echo ""
echo "▶️  Starting services..."
docker compose -f $COMPOSE_FILE up -d

echo ""
echo "✅ Services restarted successfully!"
echo ""
echo "📋 To view logs:"
echo "   docker compose -f $COMPOSE_FILE logs -f mission-planner-service"
echo "   docker compose -f $COMPOSE_FILE logs -f api-delegation-service"
echo "   docker compose -f $COMPOSE_FILE logs -f graph-builder-service"
echo ""
echo "📊 To check service status:"
echo "   docker compose -f $COMPOSE_FILE ps"

