#!/bin/bash
# Verify integration test setup
# Checks that all required files and configurations are in place

set -e

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}Integration Test Setup Verification${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

ERRORS=0

# Check Docker is installed
echo -e "${YELLOW}Checking Docker installation...${NC}"
if command -v docker &> /dev/null; then
    echo -e "${GREEN}✓ Docker is installed: $(docker --version)${NC}"
else
    echo -e "${RED}✗ Docker is not installed${NC}"
    ERRORS=$((ERRORS + 1))
fi

# Check Docker Compose is installed
echo -e "${YELLOW}Checking Docker Compose installation...${NC}"
if docker compose version &> /dev/null; then
    echo -e "${GREEN}✓ Docker Compose is installed: $(docker compose version)${NC}"
else
    echo -e "${RED}✗ Docker Compose is not installed${NC}"
    ERRORS=$((ERRORS + 1))
fi

echo ""

# Check required Dockerfiles exist
echo -e "${YELLOW}Checking Dockerfiles...${NC}"
DOCKERFILES=(
    "packages/topomap_dbs/graph_db/Dockerfile"
    "packages/topomap_dbs/image_db/Dockerfile"
    "packages/services/similarity_service/Dockerfile"
    "packages/services/graph_builder/Dockerfile"
    "packages/services/mission_planner/Dockerfile"
    "packages/api/Dockerfile"
    "tests/Dockerfile"
)

for dockerfile in "${DOCKERFILES[@]}"; do
    if [ -f "$dockerfile" ]; then
        echo -e "${GREEN}✓ $dockerfile${NC}"
    else
        echo -e "${RED}✗ $dockerfile not found${NC}"
        ERRORS=$((ERRORS + 1))
    fi
done

echo ""

# Check docker-compose.test.yaml
echo -e "${YELLOW}Checking Docker Compose configuration...${NC}"
if [ -f "docker_compose/docker-compose.test.yaml" ]; then
    echo -e "${GREEN}✓ docker_compose/docker-compose.test.yaml exists${NC}"
    
    # Check for required services
    SERVICES=(
        "arangodb-test"
        "minio-test"
        "postgres-test"
        "mosquitto-test"
        "graph-db-service-test"
        "similarity-service-test"
        "graph-builder-service-test"
        "mission-planner-service-test"
        "api-delegation-service-test"
        "test-integration"
    )
    
    for service in "${SERVICES[@]}"; do
        if grep -q "$service:" docker_compose/docker-compose.test.yaml; then
            echo -e "${GREEN}  ✓ Service defined: $service${NC}"
        else
            echo -e "${RED}  ✗ Service missing: $service${NC}"
            ERRORS=$((ERRORS + 1))
        fi
    done
else
    echo -e "${RED}✗ docker_compose/docker-compose.test.yaml not found${NC}"
    ERRORS=$((ERRORS + 1))
fi

echo ""

# Check test files
echo -e "${YELLOW}Checking test files...${NC}"
TEST_FILES=(
    "tests/conftest.py"
    "tests/integration/test_services_e2e.py"
    "tests/integration/test_mission_planner_integration.py"
    "tests/integration/test_graph_db_integration.py"
)

for test_file in "${TEST_FILES[@]}"; do
    if [ -f "$test_file" ]; then
        echo -e "${GREEN}✓ $test_file${NC}"
    else
        echo -e "${RED}✗ $test_file not found${NC}"
        ERRORS=$((ERRORS + 1))
    fi
done

echo ""

# Check mosquitto.sh
echo -e "${YELLOW}Checking MQTT broker script...${NC}"
if [ -f "packages/utils/test_utils/mosquitto.sh" ]; then
    echo -e "${GREEN}✓ packages/utils/test_utils/mosquitto.sh exists${NC}"
else
    echo -e "${RED}✗ packages/utils/test_utils/mosquitto.sh not found${NC}"
    ERRORS=$((ERRORS + 1))
fi

echo ""

# Check if ports are available
echo -e "${YELLOW}Checking if required ports are available...${NC}"
PORTS=(6001 6002 8003 8004 8005 8000 8529 9090 5432 1893 9091 9011)

for port in "${PORTS[@]}"; do
    if lsof -Pi :$port -sTCP:LISTEN -t >/dev/null 2>&1; then
        echo -e "${YELLOW}⚠ Port $port is in use (may need to stop other services)${NC}"
    else
        echo -e "${GREEN}✓ Port $port is available${NC}"
    fi
done

echo ""

# Summary
echo -e "${BLUE}========================================${NC}"
if [ $ERRORS -eq 0 ]; then
    echo -e "${GREEN}✓ All checks passed!${NC}"
    echo -e "${GREEN}You can now run integration tests with:${NC}"
    echo -e "${GREEN}  ./scripts/test_docker.sh integration${NC}"
    exit 0
else
    echo -e "${RED}✗ Found $ERRORS error(s)${NC}"
    echo -e "${RED}Please fix the errors above before running integration tests${NC}"
    exit 1
fi

