#!/bin/bash
# Run all tests with coverage reporting

set -e

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}Running Cloud Server Test Suite${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""

# Check if pytest is installed
if ! command -v pytest &> /dev/null; then
    echo -e "${RED}Error: pytest is not installed${NC}"
    echo "Install test dependencies with:"
    echo "  pip install -r tests/requirements-test.txt"
    exit 1
fi

# Check if Docker is running (for integration tests)
if ! docker info &> /dev/null; then
    echo -e "${YELLOW}Warning: Docker is not running${NC}"
    echo "Integration and E2E tests will be skipped"
    SKIP_DOCKER_TESTS=true
else
    SKIP_DOCKER_TESTS=false
fi

# Run tests with coverage
echo -e "${YELLOW}Running tests with coverage...${NC}"
echo ""

if [ "$SKIP_DOCKER_TESTS" = true ]; then
    # Run only unit tests
    pytest tests/unit \
        -v \
        --cov=packages \
        --cov-report=html \
        --cov-report=term-missing \
        --cov-report=xml \
        --junit-xml=tests/reports/junit/results.xml
else
    # Run all tests
    pytest tests \
        -v \
        --cov=packages \
        --cov-report=html \
        --cov-report=term-missing \
        --cov-report=xml \
        --junit-xml=tests/reports/junit/results.xml
fi

TEST_EXIT_CODE=$?

echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}Test Results${NC}"
echo -e "${GREEN}========================================${NC}"

if [ $TEST_EXIT_CODE -eq 0 ]; then
    echo -e "${GREEN}✓ All tests passed!${NC}"
else
    echo -e "${RED}✗ Some tests failed${NC}"
fi

echo ""
echo "Coverage report: tests/reports/coverage/index.html"
echo "JUnit report: tests/reports/junit/results.xml"
echo ""

exit $TEST_EXIT_CODE

