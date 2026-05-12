# Integration Tests for Graph Database Components

This directory contains comprehensive integration tests for the Graph Database Server, R-tree Spatial Index, and Spatial Index Manager components.

## Overview

These integration tests verify the functionality of database-dependent components using real ArangoDB instances via Docker Compose.

### Test Files

1. **test_graph_db_server.py** - Graph Database Server integration tests (50+ tests)
2. **test_rtree_spatial_index.py** - R-tree Spatial Index integration tests (25+ tests)
3. **test_spatial_index_manager.py** - Spatial Index Manager integration tests (15+ tests)

## Prerequisites

### 1. Docker and Docker Compose

Ensure Docker and Docker Compose are installed and running:

```bash
docker --version
docker-compose --version
```

### 2. Python Dependencies

Install required Python packages:

```bash
pip install pytest pytest-cov python-arango rtree
```

### 3. Start Docker Services

Start the required services using Docker Compose:

```bash
# From the cloud_server root directory
docker-compose up -d arangodb minio postgres
```

Wait for services to be ready (usually 10-30 seconds):

```bash
# Check if ArangoDB is ready
curl http://localhost:8529/_api/version

# Check if services are running
docker-compose ps
```

## Running the Tests

### Run All Integration Tests

```bash
# From the cloud_server root directory
python3 -m pytest tests/integration -v
```

### Run Specific Test Files

```bash
# Graph DB Server tests
python3 -m pytest tests/integration/test_graph_db_server.py -v

# R-tree Spatial Index tests
python3 -m pytest tests/integration/test_rtree_spatial_index.py -v

# Spatial Index Manager tests
python3 -m pytest tests/integration/test_spatial_index_manager.py -v
```

### Run with Coverage

```bash
# All integration tests with coverage
python3 -m pytest tests/integration --cov=packages/topomap_dbs/graph_db --cov-report=term-missing

# Specific component coverage
python3 -m pytest tests/integration/test_graph_db_server.py \
    --cov=packages/topomap_dbs/graph_db/server \
    --cov-report=term-missing

python3 -m pytest tests/integration/test_rtree_spatial_index.py \
    --cov=packages/topomap_dbs/graph_db/rtree_spatial_index \
    --cov-report=term-missing

python3 -m pytest tests/integration/test_spatial_index_manager.py \
    --cov=packages/topomap_dbs/graph_db/spatial_index_manager \
    --cov-report=term-missing
```

### Run Specific Test Classes

```bash
# Test only map operations
python3 -m pytest tests/integration/test_graph_db_server.py::TestGraphDatabaseServerMapOperations -v

# Test only spatial queries
python3 -m pytest tests/integration/test_graph_db_server.py::TestGraphDatabaseServerSpatialQueries -v

# Test only R-tree KNN search
python3 -m pytest tests/integration/test_rtree_spatial_index.py::TestRTreeSpatialIndexKNNSearch -v
```

## Test Coverage

### Graph Database Server Tests (50+ tests)

**TestGraphDatabaseServerInitialization** (3 tests)
- Database initialization
- Idempotent initialization
- Health checks

**TestGraphDatabaseServerMapOperations** (6 tests)
- Create map
- Create duplicate map
- List maps
- Delete map
- Get map stats

**TestGraphDatabaseServerNodeOperations** (7 tests)
- Add node
- Add node with metadata
- Get node
- Update node
- Delete node
- List nodes

**TestGraphDatabaseServerEdgeOperations** (3 tests)
- Add edge
- Add edge with metadata
- Get edges
- Delete edge

**TestGraphDatabaseServerSpatialQueries** (6 tests)
- Range search with R-tree
- Range search with larger radius
- KNN search with R-tree
- KNN search with k > nodes
- Spatial query on empty map

**TestGraphDatabaseServerRTreeSynchronization** (4 tests)
- R-tree creation on first query
- R-tree rebuild after threshold
- R-tree update on node update
- R-tree delete on node delete

**TestGraphDatabaseServerPathfinding** (2 tests)
- Find shortest path
- Find path with no connection

**TestGraphDatabaseServerErrorHandling** (5 tests)
- Add node to nonexistent map
- Add edge with missing nodes
- Get nonexistent node
- Delete nonexistent map
- Concurrent spatial queries

### R-tree Spatial Index Tests (25+ tests)

**TestRTreeSpatialIndexInitialization** (2 tests)
- Create R-tree index
- R-tree index properties

**TestRTreeSpatialIndexBuildOperations** (5 tests)
- Build single node
- Build multiple nodes
- Build with metadata
- Rebuild R-tree
- Clear R-tree

**TestRTreeSpatialIndexRangeSearch** (5 tests)
- Range search single result
- Range search multiple results
- Range search empty result
- Range search distance accuracy
- Range search large dataset

**TestRTreeSpatialIndexKNNSearch** (5 tests)
- KNN search basic
- KNN search ordering
- KNN search k > nodes
- KNN search empty index

**TestRTreeSpatialIndexRectangleQuery** (2 tests)
- Rectangle query basic
- Rectangle query empty result

**TestRTreeSpatialIndexPerformance** (3 tests)
- Large dataset build (10,000 nodes)
- Range search performance
- KNN search performance

### Spatial Index Manager Tests (15+ tests)

**TestSpatialIndexManagerInitialization** (2 tests)
- Create spatial index manager
- Ensure indexes

**TestSpatialIndexManagerIndexOperations** (5 tests)
- Create geo index
- Geo index idempotent
- Get index info
- Drop geo indexes
- Rebuild indexes

**TestSpatialIndexManagerWithData** (3 tests)
- Index with data insertion
- Index after data update
- Index after data deletion

**TestSpatialIndexManagerPerformance** (2 tests)
- Large dataset insertion with index (1,000 nodes)
- Index rebuild performance

## Expected Coverage Improvements

After running these integration tests, the coverage should improve significantly:

| Component | Before | After | Improvement |
|-----------|--------|-------|-------------|
| **Graph DB Server** | 9% | **>80%** | +71% |
| **R-tree Spatial Index** | 16% | **>80%** | +64% |
| **Spatial Index Manager** | 16% | **>80%** | +64% |

## Troubleshooting

### Docker Services Not Running

If tests fail with connection errors:

```bash
# Check service status
docker-compose ps

# Restart services
docker-compose restart arangodb

# View logs
docker-compose logs arangodb
```

### R-tree Library Not Available

If R-tree tests are skipped:

```bash
# Install rtree library
pip install rtree

# On Ubuntu/Debian, you may also need:
sudo apt-get install libspatialindex-dev
```

### ArangoDB Connection Timeout

If tests timeout waiting for ArangoDB:

```bash
# Increase wait time in conftest.py (already set to 60 seconds)
# Or manually wait for ArangoDB to be ready:
while ! curl -s http://localhost:8529/_api/version > /dev/null; do
    echo "Waiting for ArangoDB..."
    sleep 2
done
echo "ArangoDB is ready!"
```

### Test Data Cleanup

Tests automatically clean up test data, but if needed:

```bash
# Connect to ArangoDB web interface
open http://localhost:8529

# Login with root/openSesame
# Delete test databases manually if needed
```

## CI/CD Integration

These tests are designed to run in CI/CD pipelines:

```yaml
# Example GitHub Actions workflow
- name: Start Docker services
  run: docker-compose up -d arangodb minio postgres

- name: Wait for services
  run: |
    timeout 60 bash -c 'until curl -s http://localhost:8529/_api/version; do sleep 2; done'

- name: Run integration tests
  run: |
    python3 -m pytest tests/integration \
      --cov=packages/topomap_dbs/graph_db \
      --cov-report=xml \
      --cov-report=term

- name: Stop Docker services
  run: docker-compose down
```

## Notes

- All tests use the `@pytest.mark.integration` marker
- Tests requiring Docker use `@pytest.mark.requires_docker` marker
- R-tree tests are automatically skipped if rtree library is not available
- Each test uses fresh fixtures with automatic cleanup
- Tests are designed to be independent and can run in any order
- Performance tests verify operations complete within reasonable time limits

