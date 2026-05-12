#!/bin/bash
# Run only unit tests (fast, no Docker required)

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}Running Unit Tests${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""

pytest tests/unit \
    -v \
    -m unit \
    --cov=packages \
    --cov-report=term-missing

echo ""
echo -e "${GREEN}✓ Unit tests complete${NC}"

