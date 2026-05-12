#!/bin/bash
# Run tests in Docker containers
# Provides isolated, reproducible test environment
#
# Features:
# - Automatic cleanup of containers on exit or interruption (Ctrl+C)
# - Proper signal handling to ensure no orphaned containers
# - Volume cleanup to prevent disk space issues

set -e

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Flag to track if integration test services are running
INTEGRATION_SERVICES_RUNNING=false

# Cleanup function for integration tests
cleanup_integration_services() {
    if [ "$INTEGRATION_SERVICES_RUNNING" = true ]; then
        echo ""
        echo -e "${YELLOW}Cleaning up integration test services...${NC}"
        docker compose -f docker_compose/docker-compose.test.yaml down -v
        INTEGRATION_SERVICES_RUNNING=false
        echo -e "${GREEN}✓ Integration test services stopped and removed${NC}"
    fi
}

# Set up trap to cleanup on script exit or interruption
trap cleanup_integration_services EXIT INT TERM

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}Docker-Based Test Runner${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""

# Function to show usage
show_usage() {
    echo "Usage: $0 [COMMAND]"
    echo ""
    echo "Commands:"
    echo "  unit          Run unit tests only (fast, no external dependencies)"
    echo "  integration   Run integration tests (with real Docker containers)"
    echo "  scenario      Run scenario tests (business workflows and use cases)"
    echo "  e2e           Run end-to-end tests (full system integration)"
    echo "  all           Run all tests"
    echo "  parallel      Run tests in parallel (faster)"
    echo "  coverage      Generate coverage report with badge"
    echo "  build         Build test Docker image"
    echo "  build-services Build microservice Docker images for integration tests"
    echo "  rebuild       Rebuild all test images (test-runner + all microservices)"
    echo "  clean         Clean up test containers and reports"
    echo "  shell         Open shell in test container for debugging"
    echo ""
    echo "Examples:"
    echo "  $0 unit                    # Run unit tests"
    echo "  $0 integration             # Run integration tests with all microservices"
    echo "  $0 scenario                # Run scenario tests"
    echo "  $0 e2e                     # Run end-to-end tests"
    echo "  $0 build-services          # Build microservice images"
    echo "  $0 rebuild                 # Rebuild all test images from scratch"
    echo "  $0 coverage                # Generate coverage report"
    echo ""
}

# Function to build test image
build_test_image() {
    echo -e "${YELLOW}Building all test runner images...${NC}"
    docker compose -f docker_compose/docker-compose.test.yaml build test-runner test-unit test-integration test-scenario test-e2e
    echo -e "${GREEN}✓ Test images built successfully${NC}"
    echo ""
}

# Function to build microservice images for integration tests
build_microservice_images() {
    echo -e "${YELLOW}Building microservice Docker images for integration tests...${NC}"

    echo -e "${BLUE}Building Graph DB Service...${NC}"
    docker build -t graph_db_service:latest -f packages/topomap_dbs/graph_db/Dockerfile .

    echo -e "${BLUE}Building Graph Builder Service...${NC}"
    docker build -t graph_builder_service:latest -f packages/services/graph_builder/Dockerfile .

    echo -e "${BLUE}Building Mission Planner Service...${NC}"
    docker build -t mission_planner_service:latest -f packages/services/mission_planner/Dockerfile .

    echo -e "${BLUE}Building API Delegation Service...${NC}"
    docker build -t api_delegation_service:latest -f packages/api/Dockerfile .

    echo -e "${GREEN}✓ All microservice images built successfully${NC}"
    echo ""
}

# Function to rebuild all test images (test-runner + all microservices)
rebuild_all_images() {
    echo -e "${YELLOW}Rebuilding all test images from scratch...${NC}"
    echo ""

    # Build test-runner image with --no-cache
    echo -e "${BLUE}Rebuilding test-runner image...${NC}"
    docker compose -f docker_compose/docker-compose.test.yaml build --no-cache test-runner
    echo -e "${GREEN}✓ Test-runner image rebuilt${NC}"
    echo ""

    # Rebuild all microservice images with --no-cache
    echo -e "${YELLOW}Rebuilding microservice Docker images...${NC}"

    echo -e "${BLUE}Rebuilding Graph DB Service...${NC}"
    docker build --no-cache -t graph_db_service:latest -f packages/topomap_dbs/graph_db/Dockerfile .

    echo -e "${BLUE}Rebuilding Graph Builder Service...${NC}"
    docker build --no-cache -t graph_builder_service:latest -f packages/services/graph_builder/Dockerfile .

    echo -e "${BLUE}Rebuilding Mission Planner Service...${NC}"
    docker build --no-cache -t mission_planner_service:latest -f packages/services/mission_planner/Dockerfile .

    echo -e "${BLUE}Rebuilding API Delegation Service...${NC}"
    docker build --no-cache -t api_delegation_service:latest -f packages/api/Dockerfile .

    echo -e "${GREEN}✓ All images rebuilt successfully${NC}"
    echo ""
}

# Function to run unit tests
run_unit_tests() {
    echo -e "${BLUE}Running unit tests in Docker...${NC}"
    echo ""

    echo -e "${YELLOW}Building test unit image...${NC}"
    docker compose -f docker_compose/docker-compose.test.yaml build test-unit

    docker compose -f docker_compose/docker-compose.test.yaml run --rm test-unit

    EXIT_CODE=$?

    echo ""
    if [ $EXIT_CODE -eq 0 ]; then
        echo -e "${GREEN}✓ Unit tests passed!${NC}"
    else
        echo -e "${RED}✗ Unit tests failed${NC}"
    fi

    return $EXIT_CODE
}

# Function to run integration tests
run_integration_tests() {
    echo -e "${YELLOW}Building test runner image...${NC}"
    build_test_image

    echo -e "${YELLOW}Building microservice images...${NC}"
    build_microservice_images

    echo -e "${BLUE}Starting all test services (databases + microservices)...${NC}"
    docker compose -f docker_compose/docker-compose.test.yaml up -d --build \
        arangodb-test \
        minio-test \
        postgres-test \
        mosquitto-test \
        graph-db-service-test \
        mission-dispatch-test \
        graph-builder-service-test \
        mission-planner-service-test \
        api-delegation-service-test

    # Mark services as running so trap will clean them up
    INTEGRATION_SERVICES_RUNNING=true

    echo -e "${YELLOW}Waiting for all services to be healthy...${NC}"
    echo -e "${YELLOW}This may take 30-60 seconds...${NC}"
    sleep 10

    # Wait for health checks to pass
    echo -e "${YELLOW}Checking service health...${NC}"
    for i in {1..30}; do
        # Get list of unhealthy services (only from test compose file, exclude sati_nginx_gateway)
        UNHEALTHY=$(docker compose -f docker_compose/docker-compose.test.yaml ps --format json 2>/dev/null | jq -r 'select(.Health == "unhealthy" or .Health == "starting") | .Name' | grep -v "sati_nginx_gateway" || true)

        # Fallback if jq is not available
        if [ -z "$UNHEALTHY" ] && ! command -v jq &> /dev/null; then
            UNHEALTHY=$(docker compose -f docker_compose/docker-compose.test.yaml ps 2>/dev/null | grep -E "unhealthy|starting" | awk '{print $1}' | grep -v "sati_nginx_gateway" || true)
        fi

        if [ -n "$UNHEALTHY" ]; then
            echo -e "${YELLOW}Waiting for services to become healthy... ($i/30)${NC}"
            echo -e "${YELLOW}Unhealthy/Starting services:${NC}"
            echo "$UNHEALTHY" | while read service; do
                if [ -n "$service" ]; then
                    echo -e "${YELLOW}  - $service${NC}"
                fi
            done
            sleep 2
        else
            echo -e "${GREEN}All services are healthy!${NC}"
            break
        fi
    done

    echo -e "${BLUE}Running integration tests in Docker...${NC}"
    echo ""

    # Run tests and capture exit code
    # Don't use 'set -e' here so we can cleanup even if tests fail
    set +e
    echo -e "${YELLOW}Building test integration image...${NC}"
    docker compose -f docker_compose/docker-compose.test.yaml build test-integration

    docker compose -f docker_compose/docker-compose.test.yaml run --rm test-integration
    EXIT_CODE=$?
    set -e

    echo ""
    echo -e "${YELLOW}Stopping all test services...${NC}"
    docker compose -f docker_compose/docker-compose.test.yaml down -v
    INTEGRATION_SERVICES_RUNNING=false

    if [ $EXIT_CODE -eq 0 ]; then
        echo -e "${GREEN}✓ Integration tests passed!${NC}"
    else
        echo -e "${RED}✗ Integration tests failed${NC}"
    fi

    return $EXIT_CODE
}

# Function to run scenario tests
run_scenario_tests() {
    echo -e "${YELLOW}Building test runner image...${NC}"
    build_test_image

    echo -e "${YELLOW}Building microservice images...${NC}"
    build_microservice_images

    echo -e "${BLUE}Starting all test services (databases + microservices)...${NC}"
    docker compose -f docker_compose/docker-compose.test.yaml up -d --build \
        arangodb-test \
        minio-test \
        postgres-test \
        mosquitto-test \
        graph-db-service-test \
        mission-dispatch-test \
        graph-builder-service-test \
        mission-planner-service-test \
        api-delegation-service-test

    # Mark services as running so trap will clean them up
    INTEGRATION_SERVICES_RUNNING=true

    echo -e "${YELLOW}Waiting for all services to be healthy...${NC}"
    echo -e "${YELLOW}This may take 30-60 seconds...${NC}"
    sleep 10

    # Wait for health checks to pass
    echo -e "${YELLOW}Checking service health...${NC}"
    for i in {1..30}; do
        # Get list of unhealthy services (only from test compose file, exclude sati_nginx_gateway)
        UNHEALTHY=$(docker compose -f docker_compose/docker-compose.test.yaml ps --format json 2>/dev/null | jq -r 'select(.Health == "unhealthy" or .Health == "starting") | .Name' | grep -v "sati_nginx_gateway" || true)

        # Fallback if jq is not available
        if [ -z "$UNHEALTHY" ] && ! command -v jq &> /dev/null; then
            UNHEALTHY=$(docker compose -f docker_compose/docker-compose.test.yaml ps 2>/dev/null | grep -E "unhealthy|starting" | awk '{print $1}' | grep -v "sati_nginx_gateway" || true)
        fi

        if [ -n "$UNHEALTHY" ]; then
            echo -e "${YELLOW}Waiting for services to become healthy... ($i/30)${NC}"
            echo -e "${YELLOW}Unhealthy/Starting services:${NC}"
            echo "$UNHEALTHY" | while read service; do
                if [ -n "$service" ]; then
                    echo -e "${YELLOW}  - $service${NC}"
                fi
            done
            sleep 2
        else
            echo -e "${GREEN}All services are healthy!${NC}"
            break
        fi
    done

    echo -e "${BLUE}Running scenario tests in Docker...${NC}"
    echo ""

    # Run tests and capture exit code
    # Don't use 'set -e' here so we can cleanup even if tests fail
    set +e
    echo -e "${YELLOW}Building test scenario image...${NC}"
    docker compose -f docker_compose/docker-compose.test.yaml build test-scenario

    docker compose -f docker_compose/docker-compose.test.yaml run --rm test-scenario
    EXIT_CODE=$?
    set -e

    echo ""
    echo -e "${YELLOW}Stopping all test services...${NC}"
    docker compose -f docker_compose/docker-compose.test.yaml down -v
    INTEGRATION_SERVICES_RUNNING=false

    if [ $EXIT_CODE -eq 0 ]; then
        echo -e "${GREEN}✓ Scenario tests passed!${NC}"
    else
        echo -e "${RED}✗ Scenario tests failed${NC}"
    fi

    return $EXIT_CODE
}

# Function to run e2e tests
run_e2e_tests() {
    echo -e "${YELLOW}Building test runner image...${NC}"
    build_test_image

    echo -e "${YELLOW}Building microservice images...${NC}"
    build_microservice_images

    echo -e "${BLUE}Starting all test services (databases + microservices)...${NC}"
    docker compose -f docker_compose/docker-compose.test.yaml up -d --build \
        arangodb-test \
        minio-test \
        postgres-test \
        mosquitto-test \
        graph-db-service-test \
        mission-database-test \
        mission-dispatch-test \
        graph-builder-service-test \
        mission-planner-service-test \
        api-delegation-service-test

    # Mark services as running so trap will clean them up
    INTEGRATION_SERVICES_RUNNING=true

    echo -e "${YELLOW}Waiting for all services to be healthy...${NC}"
    echo -e "${YELLOW}This may take 30-60 seconds...${NC}"
    sleep 10

    # Wait for health checks to pass
    echo -e "${YELLOW}Checking service health...${NC}"
    for i in {1..30}; do
        # Get list of unhealthy services (only from test compose file, exclude sati_nginx_gateway)
        UNHEALTHY=$(docker compose -f docker_compose/docker-compose.test.yaml ps --format json 2>/dev/null | jq -r 'select(.Health == "unhealthy" or .Health == "starting") | .Name' | grep -v "sati_nginx_gateway" || true)

        # Fallback if jq is not available
        if [ -z "$UNHEALTHY" ] && ! command -v jq &> /dev/null; then
            UNHEALTHY=$(docker compose -f docker_compose/docker-compose.test.yaml ps 2>/dev/null | grep -E "unhealthy|starting" | awk '{print $1}' | grep -v "sati_nginx_gateway" || true)
        fi

        if [ -n "$UNHEALTHY" ]; then
            echo -e "${YELLOW}Waiting for services to become healthy... ($i/30)${NC}"
            echo -e "${YELLOW}Unhealthy/Starting services:${NC}"
            echo "$UNHEALTHY" | while read service; do
                if [ -n "$service" ]; then
                    echo -e "${YELLOW}  - $service${NC}"
                fi
            done
            sleep 2
        else
            echo -e "${GREEN}All services are healthy!${NC}"
            break
        fi
    done

    echo -e "${BLUE}Running end-to-end tests in Docker...${NC}"
    echo ""

    # Run tests and capture exit code
    # Don't use 'set -e' here so we can cleanup even if tests fail
    set +e
    echo -e "${YELLOW}Building test e2e image...${NC}"
    docker compose -f docker_compose/docker-compose.test.yaml build test-e2e

    docker compose -f docker_compose/docker-compose.test.yaml run --rm test-e2e
    EXIT_CODE=$?
    set -e

    echo ""
    echo -e "${YELLOW}Stopping all test services...${NC}"
    docker compose -f docker_compose/docker-compose.test.yaml down -v
    INTEGRATION_SERVICES_RUNNING=false

    if [ $EXIT_CODE -eq 0 ]; then
        echo -e "${GREEN}✓ End-to-end tests passed!${NC}"
    else
        echo -e "${RED}✗ End-to-end tests failed${NC}"
    fi

    return $EXIT_CODE
}

# Function to run all tests
run_all_tests() {
    echo -e "${BLUE}Running all tests in Docker...${NC}"
    echo ""

    docker compose -f docker_compose/docker-compose.test.yaml run --rm test-runner

    EXIT_CODE=$?

    echo ""
    if [ $EXIT_CODE -eq 0 ]; then
        echo -e "${GREEN}✓ All tests passed!${NC}"
    else
        echo -e "${RED}✗ Some tests failed${NC}"
    fi

    return $EXIT_CODE
}

# Function to run tests in parallel
run_parallel_tests() {
    echo -e "${BLUE}Running tests in parallel in Docker...${NC}"
    echo ""

    docker compose -f docker_compose/docker-compose.test.yaml run --rm test-parallel

    EXIT_CODE=$?

    echo ""
    if [ $EXIT_CODE -eq 0 ]; then
        echo -e "${GREEN}✓ Parallel tests passed!${NC}"
    else
        echo -e "${RED}✗ Some tests failed${NC}"
    fi

    return $EXIT_CODE
}

# Function to generate coverage report
generate_coverage() {
    echo -e "${BLUE}Generating coverage report in Docker...${NC}"
    echo ""

    docker compose -f docker_compose/docker-compose.test.yaml run --rm test-coverage

    EXIT_CODE=$?

    echo ""
    if [ $EXIT_CODE -eq 0 ]; then
        echo -e "${GREEN}✓ Coverage report generated!${NC}"
        echo -e "${GREEN}  HTML Report: tests/reports/coverage/index.html${NC}"
        echo -e "${GREEN}  XML Report:  tests/reports/coverage.xml${NC}"
        echo -e "${GREEN}  Badge:       tests/reports/coverage.svg${NC}"
    else
        echo -e "${RED}✗ Coverage generation failed${NC}"
    fi

    return $EXIT_CODE
}

# Function to clean up
clean_up() {
    echo -e "${YELLOW}Cleaning up test containers and reports...${NC}"

    # Stop and remove containers
    docker compose -f docker_compose/docker-compose.test.yaml down -v

    # Remove test reports (optional)
    read -p "Remove test reports? (y/N) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        rm -rf tests/reports/*
        echo -e "${GREEN}✓ Test reports removed${NC}"
    fi

    echo -e "${GREEN}✓ Cleanup complete${NC}"
}

# Function to open shell in test container
open_shell() {
    echo -e "${BLUE}Opening shell in test container...${NC}"
    echo ""

    docker compose -f docker_compose/docker-compose.test.yaml run --rm test-runner /bin/bash
}

# Change to project root
cd "$PROJECT_ROOT"

# Parse command
COMMAND=${1:-unit}

case "$COMMAND" in
    unit)
        run_unit_tests
        ;;
    integration)
        run_integration_tests
        ;;
    scenario)
        run_scenario_tests
        ;;
    e2e)
        run_e2e_tests
        ;;
    all)
        run_all_tests
        ;;
    parallel)
        run_parallel_tests
        ;;
    coverage)
        generate_coverage
        ;;
    build)
        build_test_image
        ;;
    build-services)
        build_microservice_images
        ;;
    rebuild)
        rebuild_all_images
        ;;
    clean)
        clean_up
        ;;
    shell)
        open_shell
        ;;
    help|--help|-h)
        show_usage
        ;;
    *)
        echo -e "${RED}Error: Unknown command '$COMMAND'${NC}"
        echo ""
        show_usage
        exit 1
        ;;
esac

exit $?

