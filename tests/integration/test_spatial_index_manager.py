#!/usr/bin/env python3
"""
Integration tests for Spatial Index Manager.

These tests use a real ArangoDB instance (via Docker fixture)
to test the SpatialIndexManager with real ArangoDB geo indexes.

Tests the actual index management functionality, not mocked data.
"""

import pytest
from arango import ArangoClient
from packages.topomap_dbs.graph_db.spatial_index_manager import SpatialIndexManager


@pytest.fixture
def spatial_index_manager(arangodb_container):
    """
    Provide SpatialIndexManager connected to test ArangoDB.
    
    This fixture:
    - Creates a fresh database and collection for each test
    - Initializes the SpatialIndexManager
    - Cleans up after each test
    """
    # Connect to ArangoDB
    client = ArangoClient(hosts=f"http://{arangodb_container['host']}:{arangodb_container['port']}")
    sys_db = client.db("_system", username="root", password="openSesame")
    
    # Create test database
    db_name = "test_spatial_index_db"
    if not sys_db.has_database(db_name):
        sys_db.create_database(db_name)
    
    db = client.db(db_name, username="root", password="openSesame")
    
    # Create test collection
    collection_name = "test_nodes"
    if not db.has_collection(collection_name):
        db.create_collection(collection_name)
    
    # Create SpatialIndexManager
    config = {
        "spatial": {
            "enable_indexes": True,
            "coordinate_fields": ["pose.x", "pose.y"]
        }
    }
    manager = SpatialIndexManager(db, config)
    
    yield manager, collection_name, db
    
    # Cleanup
    try:
        if db.has_collection(collection_name):
            db.delete_collection(collection_name)
    except Exception as e:
        print(f"Warning: Failed to cleanup collection: {e}")


@pytest.mark.integration
@pytest.mark.requires_docker
class TestSpatialIndexManagerInitialization:
    """Test SpatialIndexManager initialization."""
    
    def test_initialization(self, spatial_index_manager):
        """Test manager initialization."""
        manager, collection_name, db = spatial_index_manager
        
        assert manager is not None
        assert manager.db is not None
    
    def test_initialization_with_disabled_indexes(self, arangodb_container):
        """Test initialization with indexes disabled."""
        client = ArangoClient(hosts=f"http://{arangodb_container['host']}:{arangodb_container['port']}")
        db = client.db("_system", username="root", password="openSesame")
        
        config = {
            "spatial": {
                "enable_indexes": False
            }
        }
        
        manager = SpatialIndexManager(db, config)
        
        assert manager is not None


@pytest.mark.integration
@pytest.mark.requires_docker
class TestSpatialIndexManagerIndexOperations:
    """Test index management operations."""
    
    def test_ensure_indexes(self, spatial_index_manager):
        """Test ensuring indexes exist."""
        manager, collection_name, db = spatial_index_manager
        
        # Ensure indexes
        result = manager.ensure_indexes(collection_name)
        
        assert result is True
    
    def test_ensure_indexes_idempotent(self, spatial_index_manager):
        """Test that ensure_indexes is idempotent."""
        manager, collection_name, db = spatial_index_manager
        
        # Call ensure_indexes twice
        result1 = manager.ensure_indexes(collection_name)
        result2 = manager.ensure_indexes(collection_name)
        
        assert result1 is True
        assert result2 is True
    
    def test_create_geo_index(self, spatial_index_manager):
        """Test creating a geo index."""
        manager, collection_name, db = spatial_index_manager
        
        # Create geo index on pose field
        result = manager.create_geo_index(collection_name, ["pose.x", "pose.y"])
        
        assert result is True
    
    def test_get_index_info(self, spatial_index_manager):
        """Test getting index information."""
        manager, collection_name, db = spatial_index_manager
        
        # Create index first
        manager.create_geo_index(collection_name, ["pose.x", "pose.y"])
        
        # Get index info
        indexes = manager.get_index_info(collection_name)
        
        assert indexes is not None
        assert len(indexes["geo_indexes"]) > 0
    
    def test_drop_geo_indexes(self, spatial_index_manager):
        """Test dropping geo indexes."""
        manager, collection_name, db = spatial_index_manager
        
        # Create index first
        manager.create_geo_index(collection_name, ["pose"])
        
        # Drop indexes
        result = manager.drop_geo_indexes(collection_name)
        
        assert result is True
    
    def test_rebuild_indexes(self, spatial_index_manager):
        """Test rebuilding indexes."""
        manager, collection_name, db = spatial_index_manager
        
        # Create index first
        manager.create_geo_index(collection_name, ["pose.x", "pose.y"])
        
        # Rebuild indexes
        result = manager.rebuild_indexes(collection_name)
        
        assert result is True
        
        # Verify index was rebuilt
        indexes = manager.get_index_info(collection_name)
        assert len(indexes["geo_indexes"]) > 0


@pytest.mark.integration
@pytest.mark.requires_docker
class TestSpatialIndexManagerWithData:
    """Test index manager with actual data."""
    
    def test_index_with_data(self, spatial_index_manager):
        """Test creating index on collection with data."""
        manager, collection_name, db = spatial_index_manager
        
        # Insert some nodes
        collection = db.collection(collection_name)
        for i in range(10):
            collection.insert({
                "_key": f"node_{i}",
                "node_id": f"node_{i}",
                "pose": {"x": float(i), "y": 0.0, "yaw": 0.0}
            })
        
        # Create index
        result = manager.ensure_indexes(collection_name)
        
        assert result is True
    
    def test_index_performance_with_data(self, spatial_index_manager):
        """Test index creation performance with larger dataset."""
        manager, collection_name, db = spatial_index_manager
        
        # Insert 1000 nodes
        collection = db.collection(collection_name)
        nodes = []
        for i in range(1000):
            nodes.append({
                "_key": f"node_{i}",
                "node_id": f"node_{i}",
                "pose": {"x": float(i % 100), "y": float(i // 100), "yaw": 0.0}
            })
        
        # Bulk insert
        collection.insert_many(nodes)
        
        # Create index
        import time
        start = time.time()
        result = manager.ensure_indexes(collection_name)
        elapsed = time.time() - start
        
        assert result is True
        # Index creation should complete in < 5 seconds
        assert elapsed < 5.0
    
    def test_drop_and_recreate_index(self, spatial_index_manager):
        """Test dropping and recreating indexes."""
        manager, collection_name, db = spatial_index_manager
        
        # Create index
        manager.create_geo_index(collection_name, ["pose.x", "pose.y"])
        
        # Insert data
        collection = db.collection(collection_name)
        collection.insert({
            "_key": "node_1",
            "pose": {"x": 0.0, "y": 0.0, "yaw": 0.0}
        })
        
        # Drop index
        manager.drop_geo_indexes(collection_name)
        
        # Recreate index
        result = manager.create_geo_index(collection_name, ["pose.x", "pose.y"])
        
        assert result is True


@pytest.mark.integration
@pytest.mark.requires_docker
class TestSpatialIndexManagerPerformance:
    """Test index manager performance."""
    
    def test_multiple_index_operations(self, spatial_index_manager):
        """Test performance of multiple index operations."""
        manager, collection_name, db = spatial_index_manager
        
        import time
        start = time.time()
        
        # Perform multiple operations
        for i in range(10):
            manager.ensure_indexes(collection_name)
        
        elapsed = time.time() - start
        
        # Should complete in < 1 second
        assert elapsed < 1.0
    
    def test_index_rebuild_performance(self, spatial_index_manager):
        """Test index rebuild performance."""
        manager, collection_name, db = spatial_index_manager
        
        # Insert data
        collection = db.collection(collection_name)
        nodes = []
        for i in range(100):
            nodes.append({
                "_key": f"node_{i}",
                "pose": {"x": float(i), "y": 0.0, "yaw": 0.0}
            })
        collection.insert_many(nodes)
        
        # Create index
        manager.create_geo_index(collection_name, ["pose.x", "pose.y"])
        
        # Rebuild index
        import time
        start = time.time()
        result = manager.rebuild_indexes(collection_name)
        elapsed = time.time() - start
        
        assert result is True
        # Rebuild should complete in < 2 seconds
        assert elapsed < 2.0


@pytest.mark.integration
@pytest.mark.requires_docker
class TestSpatialIndexManagerErrorHandling:
    """Test error handling in index manager."""
    
    def test_ensure_indexes_nonexistent_collection(self, spatial_index_manager):
        """Test ensuring indexes on non-existent collection."""
        manager, collection_name, db = spatial_index_manager
        
        # Try to ensure indexes on non-existent collection
        result = manager.ensure_indexes("nonexistent_collection")
        
        # Should handle gracefully
        assert isinstance(result, bool)
    
    def test_drop_indexes_nonexistent_collection(self, spatial_index_manager):
        """Test dropping indexes on non-existent collection."""
        manager, collection_name, db = spatial_index_manager
        
        # Try to drop indexes on non-existent collection
        result = manager.drop_geo_indexes("nonexistent_collection")
        
        # Should handle gracefully
        assert isinstance(result, bool)
    
    def test_get_index_info_nonexistent_collection(self, spatial_index_manager):
        """Test getting index info for non-existent collection."""
        manager, collection_name, db = spatial_index_manager

        # Try to get index info for non-existent collection
        indexes = manager.get_index_info("nonexistent_collection")

        # Should return empty list, None, or error dict
        assert indexes is None or len(indexes) == 0 or (isinstance(indexes, dict) and "error" in indexes)

