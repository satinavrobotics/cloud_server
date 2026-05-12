#!/bin/bash
# Script to run integration tests with Docker services

set -e

echo "=========================================="
echo "Mission Planner Integration Tests"
echo "=========================================="
echo ""

# Check if Docker is available
if ! command -v docker &> /dev/null; then
    echo "❌ Docker is not installed or not in PATH"
    echo "   Integration tests require Docker services"
    exit 1
fi

# Check if docker-compose is available
if ! command -v docker-compose &> /dev/null; then
    echo "❌ docker-compose is not installed or not in PATH"
    echo "   Integration tests require docker-compose"
    exit 1
fi

echo "✅ Docker and docker-compose are available"
echo ""

# Start required services
echo "Starting Docker services..."
echo "  - graph_db (Graph Database)"
echo "  - mission_database (Mission Database)"
echo ""

docker-compose up -d graph_db mission_database

# Wait for services to be ready
echo "Waiting for services to be ready..."
sleep 10

# Check if graph_db is healthy
echo "Checking Graph DB health..."
for i in {1..30}; do
    if curl -s http://localhost:6001/health > /dev/null 2>&1; then
        echo "✅ Graph DB is healthy"
        break
    fi
    if [ $i -eq 30 ]; then
        echo "❌ Graph DB failed to start"
        docker-compose logs graph_db
        docker-compose down
        exit 1
    fi
    sleep 1
done

# Check if mission_database is healthy
echo "Checking Mission Database health..."
for i in {1..30}; do
    if curl -s http://localhost:5000/health > /dev/null 2>&1; then
        echo "✅ Mission Database is healthy"
        break
    fi
    if [ $i -eq 30 ]; then
        echo "❌ Mission Database failed to start"
        docker-compose logs mission_database
        docker-compose down
        exit 1
    fi
    sleep 1
done

echo ""
echo "=========================================="
echo "Running Integration Tests"
echo "=========================================="
echo ""

# Run integration tests
python3 -m pytest \
    tests/integration/test_mission_planner_integration.py::TestMissionPlannerGetMissionPlanIntegration \
    -v \
    --tb=short \
    --color=yes

TEST_EXIT_CODE=$?

echo ""
echo "=========================================="
echo "Cleaning Up"
echo "=========================================="
echo ""

# Stop services
echo "Stopping Docker services..."
docker-compose down

echo ""
if [ $TEST_EXIT_CODE -eq 0 ]; then
    echo "✅ All integration tests passed!"
else
    echo "❌ Some integration tests failed (exit code: $TEST_EXIT_CODE)"
fi

exit $TEST_EXIT_CODE

