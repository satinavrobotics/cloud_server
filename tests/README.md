# Cloud Server Test Suite

Professional testing infrastructure for the cloud_server microservices with Docker-based isolated test execution.

## 🚀 Quick Start

### Option 1: Docker-Based Testing (Recommended)

**Run unit tests (fast, isolated):**
```bash
./scripts/test_docker.sh unit
```

**Run integration tests (with real services):**
```bash
./scripts/test_docker.sh integration
```

**Run all tests with coverage:**
```bash
./scripts/test_docker.sh coverage
```

### Option 2: Local Testing

**Install dependencies:**
```bash
pip install -r tests/requirements-test.txt
```

**Run tests:**
```bash
# Run all tests
./scripts/run_all_tests.sh

# Run only unit tests (fast, no Docker)
./scripts/run_unit_tests.sh

# Run only integration tests (requires Docker)
./scripts/run_integration_tests.sh

# Run specific test file
pytest tests/unit/test_graph_db_client.py -v

# Run tests matching pattern
pytest -k "graph_db" -v
```

## 🎯 Why Docker for Testing?

- **Consistent Environment**: Same Python version and dependencies everywhere
- **Isolation**: Tests don't pollute your system
- **CI/CD Ready**: Same container runs locally and in CI
- **No System Pollution**: No `--break-system-packages` needed

## 📋 Docker Test Commands

```bash
./scripts/test_docker.sh [COMMAND]
```

| Command | Description |
|---------|-------------|
| `unit` | Run unit tests only (fast, no external dependencies) |
| `integration` | Run integration tests (with real databases) |
| `scenario` | Run scenario tests (business workflows) |
| `e2e` | Run end-to-end tests (full system) |
| `all` | Run all tests |
| `parallel` | Run tests in parallel (faster) |
| `coverage` | Generate coverage report with badge |
| `build` | Build test Docker image |
| `build-services` | Build microservice Docker images |
| `rebuild` | Rebuild all test images |
| `clean` | Clean up containers and reports |
| `shell` | Open shell in test container for debugging |

## 📁 Test Structure

```
tests/
├── conftest.py              # Shared pytest fixtures
├── pytest.ini               # Pytest configuration
├── requirements-test.txt    # Test dependencies
├── Dockerfile               # Test container definition
│
├── unit/                    # Unit tests (fast, mocked)
│   ├── test_graph_db_client.py
│   ├── test_image_db_client.py
│   └── ...
│
├── integration/             # Integration tests (real services)
│   ├── test_graph_db_integration.py
│   ├── test_image_db_integration.py
│   ├── test_graph_builder_integration.py
│   └── ...
│
├── scenarios/               # Scenario tests (business workflows) ⭐ NEW
│   ├── conftest.py          # Scenario fixtures
│   ├── README.md            # Scenario documentation
│   ├── test_map_loading_scenario.py
│   ├── test_navigation_scenario.py
│   ├── test_multi_service_scenario.py
│   └── ...
│
├── e2e/                     # End-to-end tests (full system)
│   ├── test_services_e2e.py
│   ├── test_websocket_proxy_e2e.py
│   └── ...
│
├── performance/             # Performance tests
│   └── ...
│
└── fixtures/                # Test data
    └── __init__.py
```

## Test Categories

### Unit Tests
- **Purpose**: Test individual components in isolation
- **Speed**: Fast (< 1 second per test)
- **Dependencies**: Mocked (no external services)
- **Run with**: `pytest tests/unit -m unit` or `./scripts/test_docker.sh unit`

**Example:**
```python
@pytest.mark.unit
def test_graph_db_client_get_node_returns_node_data():
    """Test that GraphDatabaseClient.get_node() returns correct data."""
    # Uses mocked HTTP responses
    pass
```

### Integration Tests
- **Purpose**: Test services with real dependencies
- **Speed**: Medium (1-10 seconds per test)
- **Dependencies**: Real (Docker containers)
- **Run with**: `pytest tests/integration -m integration` or `./scripts/test_docker.sh integration`

**Example:**
```python
@pytest.mark.integration
@pytest.mark.requires_docker
def test_graph_db_with_real_arangodb(graph_db_client):
    """Test graph database with real ArangoDB instance."""
    # Uses real ArangoDB via Docker fixture
    pass
```

### Scenario Tests ⭐ NEW
- **Purpose**: Test complete business workflows and use cases
- **Speed**: Medium (5-30 seconds per test)
- **Dependencies**: Real (Docker containers, multiple services)
- **Run with**: `pytest tests/scenarios -m scenario` or `./scripts/test_docker.sh scenario`

**Example:**
```python
@pytest.mark.scenario
@pytest.mark.requires_docker
def test_map_loading_workflow(graph_db_client, scenario_context, scenario_cleanup):
    """Test complete map loading workflow."""
    # Tests real-world workflow across multiple services
    pass
```

**Scenarios Included:**
- Map Loading: Create, load, and verify maps
- Navigation: Path planning and navigation workflows
- Multi-Service: Complex workflows involving multiple services

See [Scenario Tests README](scenarios/README.md) for details.

### End-to-End Tests
- **Purpose**: Test complete user workflows with full system
- **Speed**: Slow (10-60 seconds per test)
- **Dependencies**: Full system (docker-compose)
- **Run with**: `pytest tests/e2e -m e2e` or `./scripts/test_docker.sh e2e`

**Example:**
```python
@pytest.mark.e2e
def test_complete_navigation_workflow(docker_compose_services):
    """Test complete navigation from map load to mission execution."""
    # All services running via docker-compose
    pass
```

## Using Fixtures

### Docker Fixtures

```python
def test_with_arangodb(arangodb_container):
    """Test using ArangoDB container."""
    # arangodb_container provides: host, port, url
    url = arangodb_container["url"]
    # ... test code ...
```

### Service Client Fixtures

```python
def test_with_graph_db_client(graph_db_client):
    """Test using GraphDatabaseClient."""
    # Client is pre-configured and connected
    result = graph_db_client.create_map("test_map")
    # ... test code ...
    # Cleanup is automatic
```

### Test Data Fixtures

```python
def test_with_sample_map(sample_map_simple):
    """Test using sample map data."""
    # sample_map_simple provides a 3-node test map
    map_id = sample_map_simple["map_id"]
    nodes = sample_map_simple["nodes"]
    # ... test code ...
```

## Writing New Tests

### 1. Choose Test Category

- **Unit test**: Testing a single function/class with mocked dependencies
- **Integration test**: Testing service with real database/dependencies
- **E2E test**: Testing complete workflow with all services

### 2. Create Test File

```python
# tests/unit/test_my_service.py

import pytest
from packages.my_service.client import MyServiceClient


@pytest.mark.unit
class TestMyServiceClient:
    """Unit tests for MyServiceClient."""
    
    def test_method_returns_expected_value(self):
        """Test that method() returns expected value."""
        client = MyServiceClient()
        result = client.method()
        assert result == "expected"
```

### 3. Use Appropriate Markers

```python
@pytest.mark.unit           # Unit test
@pytest.mark.integration    # Integration test
@pytest.mark.e2e            # End-to-end test
@pytest.mark.slow           # Slow test (> 10 seconds)
@pytest.mark.requires_docker # Requires Docker
```

### 4. Follow Naming Convention

```python
# Format: test_<component>_<scenario>_<expected_result>

def test_graph_db_client_get_node_returns_node_data():
    pass

def test_mission_planner_no_path_found_returns_error():
    pass
```

## Coverage Reports

### Generate Coverage Report

```bash
pytest --cov=packages --cov-report=html
```

### View Coverage Report

```bash
# Open in browser
open tests/reports/coverage/index.html
```

### Coverage Requirements

- **Overall**: 80% minimum
- **Critical paths**: 95% (navigation, mission planning)
- **Utilities**: 70%

## Continuous Integration

Tests run automatically on:
- Every push to main branch
- Every pull request
- Nightly builds

See `.github/workflows/` for CI configuration.

## Troubleshooting

### Tests Fail with "Docker not running"

**Solution**: Start Docker daemon
```bash
sudo systemctl start docker
```

### Tests Fail with "Port already in use"

**Solution**: Stop conflicting services
```bash
docker-compose -f docker_compose/mission_dispatch_services.yaml down
```

### Tests Fail with "Module not found"

**Solution**: Install test dependencies
```bash
pip install -r tests/requirements-test.txt
```

### Slow Test Execution

**Solution**: Run tests in parallel
```bash
pytest -n auto  # Uses all CPU cores
```

## Best Practices

### 1. Keep Tests Independent

```python
# ✅ Good: Each test is independent
def test_create_map():
    client.create_map("test_map_1")
    # ... test ...

def test_delete_map():
    client.create_map("test_map_2")
    client.delete_map("test_map_2")
    # ... test ...
```

```python
# ❌ Bad: Tests depend on each other
def test_create_map():
    client.create_map("shared_map")

def test_delete_map():
    # Assumes test_create_map ran first
    client.delete_map("shared_map")
```

### 2. Use Descriptive Test Names

```python
# ✅ Good: Clear what is being tested
def test_knn_search_returns_k_nearest_neighbors():
    pass

# ❌ Bad: Unclear what is being tested
def test_search():
    pass
```

### 3. Test One Thing Per Test

```python
# ✅ Good: Tests one specific behavior
def test_create_map_with_valid_name_succeeds():
    result = client.create_map("valid_name")
    assert result["success"] is True

def test_create_map_with_empty_name_fails():
    with pytest.raises(ValueError):
        client.create_map("")
```

```python
# ❌ Bad: Tests multiple behaviors
def test_create_map():
    # Tests success case
    result = client.create_map("valid")
    assert result["success"] is True
    
    # Tests failure case
    with pytest.raises(ValueError):
        client.create_map("")
```

### 4. Use Fixtures for Setup

```python
# ✅ Good: Use fixtures
@pytest.fixture
def loaded_map(graph_db_client, sample_map_simple):
    # Setup
    load_map(graph_db_client, sample_map_simple)
    yield sample_map_simple
    # Cleanup happens automatically

def test_search(loaded_map):
    # Test uses pre-loaded map
    pass
```

```python
# ❌ Bad: Duplicate setup in each test
def test_search_1():
    # Duplicate setup
    client.create_map("test")
    client.add_node(...)
    # ... test ...

def test_search_2():
    # Duplicate setup
    client.create_map("test")
    client.add_node(...)
    # ... test ...
```

## Resources

- [Pytest Documentation](https://docs.pytest.org/)
- [Testing Best Practices](https://docs.python-guide.org/writing/tests/)
- [Test Pyramid](https://martinfowler.com/articles/practical-test-pyramid.html)
- [TESTING_STRATEGY.md](../docs/TESTING_STRATEGY.md) - Detailed testing strategy

## Support

For questions or issues with tests:
1. Check this README
2. Check [TESTING_STRATEGY.md](../docs/TESTING_STRATEGY.md)
3. Ask in team chat
4. Create an issue

