# Scenario-Based Testing

Comprehensive scenario tests for business workflows and use cases.

## Overview

Scenario tests validate complete user workflows and business logic flows across multiple services. Unlike unit tests (isolated components) or integration tests (service + dependencies), scenario tests focus on **real-world use cases** and **end-to-end workflows**.

## Test Scenarios

### 1. Map Loading Scenario (`test_map_loading_scenario.py`)

Tests the complete workflow of loading a map into the system.

#### Tests Included

| Test | Description | Duration |
|------|-------------|----------|
| `test_simple_map_loading` | Load 3-node map | ~5s |
| `test_grid_map_loading` | Load 5x5 grid map (25 nodes) | ~8s |
| `test_map_loading_with_images` | Load map with associated images | ~10s |
| `test_map_loading_error_handling` | Error handling during map loading | ~5s |

#### Workflow Steps

**Simple Map Loading**:
1. Create map in Graph DB
2. Add 3 nodes with coordinates
3. Add bidirectional edges
4. Verify map structure
5. Query and verify statistics

**Grid Map Loading**:
1. Create 5x5 grid map (25 nodes)
2. Verify all nodes created
3. Verify grid connectivity (40 edges)
4. Query map statistics

**Map with Images**:
1. Create map
2. Add nodes
3. Upload images for each node
4. Verify images stored
5. Retrieve and verify images

**Error Handling**:
1. Try to create duplicate map (should fail)
2. Try to add node to non-existent map (should fail)
3. Try to add invalid node (should fail)
4. Verify error messages

#### Run Tests

```bash
# Run all map loading scenario tests
pytest tests/scenarios/test_map_loading_scenario.py -v

# Run specific test
pytest tests/scenarios/test_map_loading_scenario.py::TestMapLoadingScenario::test_simple_map_loading -v

# Run with Docker
./scripts/test_docker.sh scenario
```

### 2. Navigation Scenario (`test_navigation_scenario.py`)

Tests navigation workflows on different map types.

#### Tests Included

| Test | Description | Duration |
|------|-------------|----------|
| `test_simple_navigation` | Navigate on 3-node map | ~5s |
| `test_grid_navigation` | Navigate on 5x5 grid | ~8s |
| `test_navigation_no_path` | Handle no path case | ~5s |
| `test_navigation_with_obstacles` | Navigate around obstacles | ~8s |

#### Workflow Steps

**Simple Navigation**:
1. Create 3-node map
2. Query path from node_1 to node_3
3. Verify path is correct
4. Verify path length (3 nodes)

**Grid Navigation**:
1. Create 5x5 grid map
2. Query path from corner to opposite corner
3. Verify path exists
4. Verify path is optimal (9 nodes)

**No Path Case**:
1. Create map with disconnected nodes
2. Try to find path between disconnected nodes
3. Verify error handling

**Navigation with Obstacles**:
1. Create 3x3 grid with center node disconnected (obstacle)
2. Query path from top-left to bottom-right
3. Verify path avoids obstacle

#### Run Tests

```bash
# Run all navigation scenario tests
pytest tests/scenarios/test_navigation_scenario.py -v

# Run specific test
pytest tests/scenarios/test_navigation_scenario.py::TestNavigationScenario::test_simple_navigation -v
```

### 3. Multi-Service Scenario (`test_multi_service_scenario.py`)

Tests complex workflows involving multiple services.

#### Tests Included

| Test | Description | Duration |
|------|-------------|----------|
| `test_map_load_and_navigate` | Load map and navigate | ~12s |
| `test_node_processing_workflow` | Node processing workflow | ~10s |
| `test_mission_lifecycle` | Complete mission lifecycle | ~10s |
| `test_error_recovery_workflow` | Error handling and recovery | ~8s |

#### Workflow Steps

**Map Load and Navigate**:
1. Create 4x4 grid map
2. Load map into system
3. Query robot position
4. Find closest node to robot
5. Find closest node to goal
6. Compute path
7. Prepare mission data

**Node Processing**:
1. Create map
2. Add first node with image
3. Add second node with image
4. Create edge between nodes
5. Verify nodes are connected
6. Verify images are stored

**Mission Lifecycle**:
1. Create map
2. Plan mission (compute path)
3. Create mission object
4. Verify mission state
5. Simulate mission execution
6. Verify mission completion

**Error Recovery**:
1. Attempt invalid operation
2. Verify error is caught
3. Recover and retry
4. Verify recovery succeeds

#### Run Tests

```bash
# Run all multi-service scenario tests
pytest tests/scenarios/test_multi_service_scenario.py -v

# Run specific test
pytest tests/scenarios/test_multi_service_scenario.py::TestMultiServiceScenario::test_map_load_and_navigate -v
```

## Writing Scenario Tests

### Template

```python
# tests/scenarios/test_my_scenario.py

import pytest
import logging

logger = logging.getLogger(__name__)

@pytest.mark.scenario
@pytest.mark.requires_docker
class TestMyScenario:
    """Test complete business workflow."""
    
    def test_my_workflow(self, graph_db_client, scenario_context, scenario_cleanup):
        """
        Scenario: Description of the workflow
        
        Steps:
        1. Step 1 description
        2. Step 2 description
        3. Step 3 description
        """
        scenario_context.scenario_name = "my_workflow"
        
        # Step 1: Description
        result = graph_db_client.create_map("test_map")
        assert result["success"] is True
        scenario_context.add_map("test_map")
        logger.info(f"✓ Step 1: Map created")
        
        # Step 2: Description
        # ... implementation ...
        logger.info(f"✓ Step 2: Completed")
        
        # Step 3: Verification
        assert scenario_context.get_state("key") == "expected_value"
        logger.info(f"✓ Scenario completed successfully")
```

### Key Components

#### 1. Markers

```python
@pytest.mark.scenario          # Marks as scenario test
@pytest.mark.requires_docker   # Requires Docker containers
```

#### 2. Fixtures

```python
# graph_db_client: GraphDatabaseClient connected to test service
# image_db_client: ImageDatabaseClient connected to test service
# scenario_context: ScenarioContext for tracking state
# scenario_cleanup: Automatic cleanup of created resources
# scenario_builder: ScenarioBuilder for constructing test scenarios
```

#### 3. Scenario Context

```python
# Track created resources
scenario_context.add_map("map_id")
scenario_context.add_mission("mission_id")
scenario_context.add_robot("robot_name")

# Store and retrieve state
scenario_context.set_state("key", value)
value = scenario_context.get_state("key")

# Get elapsed time
elapsed = scenario_context.elapsed_time()
```

#### 4. Scenario Builder

```python
# Create simple map
scenario_builder.create_map(
    map_id="test_map",
    nodes=[...],
    edges=[...]
)

# Create grid map
scenario_builder.create_grid_map(
    map_id="test_map",
    width=5,
    height=5,
    spacing=10.0
)
```

### Best Practices

1. **Clear Step Documentation**
   ```python
   """
   Scenario: Load map and navigate
   
   Steps:
   1. Create map
   2. Add nodes
   3. Query path
   4. Verify result
   """
   ```

2. **Log Progress**
   ```python
   logger.info(f"✓ Step 1: Map created")
   logger.info(f"✓ Step 2: Nodes added")
   ```

3. **Track Resources**
   ```python
   scenario_context.add_map("test_map")
   scenario_context.add_mission("mission_1")
   ```

4. **Use Scenario Builder**
   ```python
   scenario_builder.create_grid_map("test_map", width=5, height=5)
   ```

5. **Verify State**
   ```python
   assert scenario_context.get_state("key") == "expected"
   ```

## Running Scenario Tests

### Quick Start

```bash
# Run all scenario tests
./scripts/test_docker.sh scenario

# Run specific scenario file
pytest tests/scenarios/test_map_loading_scenario.py -v

# Run specific test
pytest tests/scenarios/test_map_loading_scenario.py::TestMapLoadingScenario::test_simple_map_loading -v

# Run with verbose output
pytest tests/scenarios -vv

# Run with print statements
pytest tests/scenarios -s
```

### Advanced Usage

```bash
# Run with specific marker
pytest -m scenario -v

# Run with coverage
pytest tests/scenarios --cov=packages --cov-report=html

# Run in parallel
pytest tests/scenarios -n auto

# Run with timeout (30 seconds per test)
pytest tests/scenarios --timeout=30
```

## Scenario Test Coverage

### Current Scenarios

| Scenario | Tests | Coverage |
|----------|-------|----------|
| Map Loading | 4 | Graph DB, Image DB |
| Navigation | 4 | Graph DB pathfinding |
| Multi-Service | 4 | Graph DB, Image DB, Mission Planner |
| **Total** | **12** | **Core workflows** |

### Planned Scenarios

- [ ] Robot Telemetry Scenario
- [ ] Mission Execution Scenario
- [ ] Concurrent Operations Scenario
- [ ] System Recovery Scenario
- [ ] Performance Scenario

## Troubleshooting

### Tests Fail with "Service not available"

**Solution**: Wait for services to start
```bash
# Services need 30-60 seconds to start
sleep 30
./scripts/test_docker.sh scenario
```

### Tests Fail with "Port already in use"

**Solution**: Clean up and restart
```bash
./scripts/test_docker.sh clean
docker system prune -f
./scripts/test_docker.sh scenario
```

### Tests Timeout

**Solution**: Increase timeout
```bash
pytest tests/scenarios --timeout=60
```

### Cleanup Issues

**Solution**: Manual cleanup
```bash
docker compose -f docker_compose/docker-compose.test.yaml down -v
docker volume prune -f
```

## Resources

- [Comprehensive Testing Guide](../../COMPREHENSIVE_TESTING_GUIDE.md)
- [Test README](../README.md)
- [Pytest Documentation](https://docs.pytest.org/)
- [Test Pyramid](https://martinfowler.com/articles/practical-test-pyramid.html)

