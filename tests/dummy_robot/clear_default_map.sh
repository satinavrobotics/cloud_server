#!/bin/bash

# Script to clear a map from the cloud server
# This will delete all nodes, edges, and images for the specified map

# Show help if requested
if [[ "$1" == "-h" ]] || [[ "$1" == "--help" ]]; then
    cat << 'EOFHELP'
Clear Map Script
================

Deletes all nodes, edges, and images for a specified map from the cloud server.

Usage:
  ./scripts/clear_default_map.sh [MAP_ID]

Arguments:
  MAP_ID - Optional. The map ID to clear (default: "default")

Environment Variables:
  GRAPH_DB_HOST - Graph Database host (default: localhost)
  GRAPH_DB_PORT - Graph Database port (default: 6001)
  IMAGE_DB_HOST - Image Database host (default: localhost)
  IMAGE_DB_PORT - Image Database port (default: 6002)

Examples:
  ./scripts/clear_default_map.sh              # Clear the "default" map
  ./scripts/clear_default_map.sh my_map       # Clear the "my_map" map

What it does:
  1. Checks if Graph DB and Image DB services are available
  2. Shows current map statistics (node count, edge count)
  3. Asks for confirmation before deleting
  4. Deletes all nodes and edges from Graph Database
  5. Deletes all images from Image Database
  6. Recreates an empty map with the same ID
  7. Verifies the map is now empty

Note:
  After clearing the map, you may need to reload the React Native client
  and reselect the map from the dropdown to see the empty map.
EOFHELP
    exit 0
fi

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
MAP_ID="${1:-default}"
GRAPH_DB_PORT="${GRAPH_DB_PORT:-6001}"
IMAGE_DB_PORT="${IMAGE_DB_PORT:-6002}"
GRAPH_DB_HOST="${GRAPH_DB_HOST:-localhost}"
IMAGE_DB_HOST="${IMAGE_DB_HOST:-localhost}"

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}Clear Map Script${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""
echo -e "${YELLOW}Map ID: ${MAP_ID}${NC}"
echo -e "${YELLOW}Graph DB: ${GRAPH_DB_HOST}:${GRAPH_DB_PORT}${NC}"
echo -e "${YELLOW}Image DB: ${IMAGE_DB_HOST}:${IMAGE_DB_PORT}${NC}"
echo ""

# Confirmation prompt
echo -e "${RED}WARNING: This will permanently delete all data for map '${MAP_ID}'!${NC}"
echo -e "${RED}This includes:${NC}"
echo -e "${RED}  - All nodes and their coordinates${NC}"
echo -e "${RED}  - All edges between nodes${NC}"
echo -e "${RED}  - All images associated with nodes${NC}"
echo ""
read -p "Are you sure you want to continue? (yes/no): " -r
echo ""

if [[ ! $REPLY =~ ^[Yy][Ee][Ss]$ ]]; then
    echo -e "${YELLOW}Operation cancelled.${NC}"
    exit 0
fi

# Function to check if service is available
check_service() {
    local host=$1
    local port=$2
    local service_name=$3
    
    echo -e "${BLUE}Checking ${service_name}...${NC}"
    if curl -s -f "http://${host}:${port}/health" > /dev/null 2>&1; then
        echo -e "${GREEN}✓ ${service_name} is available${NC}"
        return 0
    else
        echo -e "${RED}✗ ${service_name} is not available at ${host}:${port}${NC}"
        return 1
    fi
}

# Function to get map stats
get_map_stats() {
    local map_id=$1
    
    echo -e "${BLUE}Getting map statistics...${NC}"
    
    # Get stats from Graph DB
    local response=$(curl -s "http://${GRAPH_DB_HOST}:${GRAPH_DB_PORT}/maps/${map_id}/stats" 2>/dev/null || echo "{}")
    
    if [ -n "$response" ] && [ "$response" != "{}" ]; then
        local node_count=$(echo "$response" | python3 -c "import sys, json; data = json.load(sys.stdin); print(data.get('node_count', 0))" 2>/dev/null || echo "0")
        local edge_count=$(echo "$response" | python3 -c "import sys, json; data = json.load(sys.stdin); print(data.get('edge_count', 0))" 2>/dev/null || echo "0")
        
        echo -e "${YELLOW}Current map statistics:${NC}"
        echo -e "  Nodes: ${node_count}"
        echo -e "  Edges: ${edge_count}"
        echo ""
    else
        echo -e "${YELLOW}Could not retrieve map statistics (map may not exist)${NC}"
        echo ""
    fi
}

# Check if services are available
echo -e "${BLUE}Checking services...${NC}"
echo ""

GRAPH_DB_AVAILABLE=false
IMAGE_DB_AVAILABLE=false

if check_service "${GRAPH_DB_HOST}" "${GRAPH_DB_PORT}" "Graph Database Service"; then
    GRAPH_DB_AVAILABLE=true
fi
echo ""

if check_service "${IMAGE_DB_HOST}" "${IMAGE_DB_PORT}" "Image Database Service"; then
    IMAGE_DB_AVAILABLE=true
fi
echo ""

if [ "$GRAPH_DB_AVAILABLE" = false ] && [ "$IMAGE_DB_AVAILABLE" = false ]; then
    echo -e "${RED}Error: No services are available. Please start the services first.${NC}"
    echo -e "${YELLOW}Hint: Run 'docker compose -f docker_compose/mission_dispatch_services_dev.yaml up -d'${NC}"
    exit 1
fi

# Get current map stats
if [ "$GRAPH_DB_AVAILABLE" = true ]; then
    get_map_stats "${MAP_ID}"
fi

# Delete map from Graph Database
if [ "$GRAPH_DB_AVAILABLE" = true ]; then
    echo -e "${BLUE}Deleting map from Graph Database...${NC}"
    
    response=$(curl -s -X DELETE "http://${GRAPH_DB_HOST}:${GRAPH_DB_PORT}/maps/${MAP_ID}" 2>/dev/null || echo '{"success": false}')
    success=$(echo "$response" | python3 -c "import sys, json; data = json.load(sys.stdin); print(data.get('success', False))" 2>/dev/null || echo "False")
    
    if [ "$success" = "True" ]; then
        echo -e "${GREEN}✓ Map deleted from Graph Database${NC}"
    else
        error=$(echo "$response" | python3 -c "import sys, json; data = json.load(sys.stdin); print(data.get('detail', 'Unknown error'))" 2>/dev/null || echo "Unknown error")
        echo -e "${YELLOW}⚠ Could not delete map from Graph Database: ${error}${NC}"
    fi
    echo ""
else
    echo -e "${YELLOW}⚠ Skipping Graph Database deletion (service not available)${NC}"
    echo ""
fi

# Delete map from Image Database
if [ "$IMAGE_DB_AVAILABLE" = true ]; then
    echo -e "${BLUE}Deleting map from Image Database...${NC}"
    
    response=$(curl -s -X DELETE "http://${IMAGE_DB_HOST}:${IMAGE_DB_PORT}/maps/${MAP_ID}" 2>/dev/null || echo '{"success": false}')
    success=$(echo "$response" | python3 -c "import sys, json; data = json.load(sys.stdin); print(data.get('success', False))" 2>/dev/null || echo "False")
    
    if [ "$success" = "True" ]; then
        echo -e "${GREEN}✓ Map deleted from Image Database${NC}"
    else
        error=$(echo "$response" | python3 -c "import sys, json; data = json.load(sys.stdin); print(data.get('detail', 'Unknown error'))" 2>/dev/null || echo "Unknown error")
        echo -e "${YELLOW}⚠ Could not delete map from Image Database: ${error}${NC}"
    fi
    echo ""
else
    echo -e "${YELLOW}⚠ Skipping Image Database deletion (service not available)${NC}"
    echo ""
fi

# Recreate the map (empty)
if [ "$GRAPH_DB_AVAILABLE" = true ]; then
    echo -e "${BLUE}Recreating empty map...${NC}"
    
    response=$(curl -s -X POST "http://${GRAPH_DB_HOST}:${GRAPH_DB_PORT}/maps" \
        -H "Content-Type: application/json" \
        -d "{\"map_id\": \"${MAP_ID}\", \"description\": \"Map ${MAP_ID}\"}" \
        2>/dev/null || echo '{"success": false}')
    
    success=$(echo "$response" | python3 -c "import sys, json; data = json.load(sys.stdin); print(data.get('success', False))" 2>/dev/null || echo "False")
    
    if [ "$success" = "True" ]; then
        echo -e "${GREEN}✓ Empty map recreated${NC}"
    else
        error=$(echo "$response" | python3 -c "import sys, json; data = json.load(sys.stdin); print(data.get('detail', 'Unknown error'))" 2>/dev/null || echo "Unknown error")
        echo -e "${YELLOW}⚠ Could not recreate map: ${error}${NC}"
    fi
    echo ""
fi

# Verify deletion
if [ "$GRAPH_DB_AVAILABLE" = true ]; then
    echo -e "${BLUE}Verifying deletion...${NC}"
    get_map_stats "${MAP_ID}"
fi

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}Map '${MAP_ID}' has been cleared!${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo -e "${YELLOW}Note: If you have the React Native client open, you may need to:${NC}"
echo -e "${YELLOW}  1. Reload the page${NC}"
echo -e "${YELLOW}  2. Reselect the map from the dropdown${NC}"
echo ""
