#!/bin/bash
# Run integration tests (requires Docker)

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}Running Integration Tests${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""

# Check if Docker is running
if ! docker info &> /dev/null; then
    echo -e "${RED}Error: Docker is not running${NC}"
    echo "Integration tests require Docker to be running"
    exit 1
fi

pytest tests/integration \
    -v \
    -m integration \
    --cov=packages \
    --cov-report=term-missing

echo ""
echo -e "${GREEN}✓ Integration tests complete${NC}"

