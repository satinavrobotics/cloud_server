#!/usr/bin/env python3
"""
Integration tests for Graph Database Server.

These tests use a real ArangoDB instance (via Docker fixture)
to test the GraphDatabaseService with both ArangoDB and R-tree spatial index.

Tests the SERVER directly (not via REST API).
"""

import pytest
import math
import time
from packages.topomap_dbs.graph_db.server import GraphDatabaseService


@pytest.fixture
def graph_db_server(arangodb_container):
    """
    Provide GraphDatabaseService connected to test ArangoDB.
    
    This fixture:
    - Creates a fresh server instance for each test
    - Initializes the database and collections automatically via __init__
    - Cleans up test data after each test
    """
    # Create server instance (initialization happens in __init__)
    server = GraphDatabaseService(
        arango_host=arangodb_container["host"],
        arango_port=arangodb_container["port"],
        arango_username="root",
        arango_password="openSesame",
        database_name="test_topomap_db",
    )
    
    yield server

    # Cleanup: Delete all test maps
    try:
        maps = server.list_maps()
        for map_id in maps:
            if map_id.startswith("test_") or map_id == "nonexistent_map":
                server.delete_map(map_id)
    except Exception as e:
        print(f"Warning: Failed to cleanup test maps: {e}")


@pytest.mark.integration
@pytest.mark.requires_docker
class TestGraphDatabaseServerInitialization:
    """Test GraphDatabaseService initialization."""
    
    def test_initialization(self, arangodb_container):
        """Test database initialization creates all required collections."""
        server = GraphDatabaseService(
            arango_host=arangodb_container["host"],
            arango_port=arangodb_container["port"],
            database_name="test_init_db"
        )
        
        assert server.db is not None
        assert server.graph is not None
        assert server.node_collection == "map_nodes"
        assert server.edge_collection == "map_edges"
    
    def test_get_stats(self, graph_db_server):
        """Test getting database statistics."""
        stats = graph_db_server.get_stats()
        
        assert "node_count" in stats
        assert "edge_count" in stats


@pytest.mark.integration
@pytest.mark.requires_docker
class TestGraphDatabaseServerMapOperations:
    """Test map CRUD operations."""
    
    def test_create_map(self, graph_db_server):
        """Test creating a new map."""
        result = graph_db_server.create_map("test_map_1")
        
        assert result is True
    
    def test_create_duplicate_map(self, graph_db_server):
        """Test creating a map that already exists."""
        graph_db_server.create_map("test_map_dup")
        
        # Try to create again - should return True (idempotent)
        result = graph_db_server.create_map("test_map_dup")
        
        assert result is True
    
    def test_list_maps(self, graph_db_server):
        """Test listing all maps."""
        # Create multiple maps
        graph_db_server.create_map("test_map_a")
        graph_db_server.create_map("test_map_b")
        graph_db_server.create_map("test_map_c")
        
        maps = graph_db_server.list_maps()
        
        assert "test_map_a" in maps
        assert "test_map_b" in maps
        assert "test_map_c" in maps
    
    def test_delete_map(self, graph_db_server):
        """Test deleting a map."""
        # Create and populate map
        graph_db_server.create_map("test_map_del")
        graph_db_server.add_node("node_1", 0.0, 0.0, 0.0, map_id="test_map_del")
        
        # Delete map
        result = graph_db_server.delete_map("test_map_del")
        
        assert result is True
        
        # Verify map is deleted
        maps = graph_db_server.list_maps()
        assert "test_map_del" not in maps
    
    def test_get_map_stats(self, graph_db_server):
        """Test getting map statistics."""
        map_id = "test_map_stats"
        graph_db_server.create_map(map_id)
        
        # Add nodes and edges
        graph_db_server.add_node("node_1", 0.0, 0.0, 0.0, map_id=map_id)
        graph_db_server.add_node("node_2", 10.0, 0.0, 0.0, map_id=map_id)
        graph_db_server.add_edge("node_1", "node_2", map_id=map_id)
        
        stats = graph_db_server.get_map_stats(map_id)
        
        assert stats["map_id"] == map_id
        assert stats["node_count"] == 2
        assert stats["edge_count"] == 1


@pytest.mark.integration
@pytest.mark.requires_docker
class TestGraphDatabaseServerNodeOperations:
    """Test node CRUD operations."""
    
    def test_add_node(self, graph_db_server):
        """Test adding a node to a map."""
        map_id = "test_map_node"
        graph_db_server.create_map(map_id)
        
        result = graph_db_server.add_node(
            node_id="node_1",
            x=10.5,
            y=20.3,
            yaw=1.57,
            map_id=map_id
        )
        
        assert result is True
    
    def test_add_node_with_metadata(self, graph_db_server):
        """Test adding a node with metadata."""
        map_id = "test_map_meta"
        graph_db_server.create_map(map_id)
        
        metadata = {
            "label": "checkpoint",
            "timestamp": "2024-01-01T00:00:00Z"
        }
        
        result = graph_db_server.add_node(
            node_id="node_1",
            x=0.0,
            y=0.0,
            yaw=0.0,
            map_id=map_id,
            metadata=metadata
        )
        
        assert result is True
        
        # Verify metadata was stored
        node = graph_db_server.get_node(map_id, "node_1")
        assert node["label"] == "checkpoint"
        assert node["timestamp"] == "2024-01-01T00:00:00Z"
    
    def test_get_node(self, graph_db_server):
        """Test retrieving a node."""
        map_id = "test_map_get"
        graph_db_server.create_map(map_id)
        graph_db_server.add_node("node_1", 5.0, 10.0, 0.5, map_id=map_id)
        
        node = graph_db_server.get_node(map_id, "node_1")
        
        assert node is not None
        assert node["node_id"] == "node_1"
        assert node["x"] == 5.0
        assert node["y"] == 10.0
        assert node["theta"] == 0.5
        assert node["yaw"] == 0.5
    
    def test_update_node(self, graph_db_server):
        """Test updating a node."""
        map_id = "test_map_update"
        graph_db_server.create_map(map_id)
        graph_db_server.add_node("node_1", 0.0, 0.0, 0.0, map_id=map_id)
        
        # Update node position
        result = graph_db_server.update_node(
            map_id=map_id,
            node_id="node_1",
            x=15.0,
            y=25.0,
            yaw=3.14
        )
        
        assert result is True
        
        # Verify update
        node = graph_db_server.get_node(map_id, "node_1")
        assert node["x"] == 15.0
        assert node["y"] == 25.0
        assert node["theta"] == 3.14
    
    def test_delete_node(self, graph_db_server):
        """Test deleting a node."""
        map_id = "test_map_del_node"
        graph_db_server.create_map(map_id)
        graph_db_server.add_node("node_1", 0.0, 0.0, 0.0, map_id=map_id)
        
        result = graph_db_server.delete_node(map_id, "node_1")
        
        assert result is True
        
        # Verify node is deleted
        node = graph_db_server.get_node(map_id, "node_1")
        assert node is None
    
    def test_add_multiple_nodes(self, graph_db_server):
        """Test adding multiple nodes."""
        map_id = "test_map_multi"
        graph_db_server.create_map(map_id)
        
        # Add 5 nodes
        for i in range(5):
            result = graph_db_server.add_node(
                f"node_{i}",
                float(i * 10),
                0.0,
                0.0,
                map_id=map_id
            )
            assert result is True
        
        # Verify all nodes exist
        stats = graph_db_server.get_map_stats(map_id)
        assert stats["node_count"] == 5


@pytest.mark.integration
@pytest.mark.requires_docker
class TestGraphDatabaseServerEdgeOperations:
    """Test edge CRUD operations."""
    
    def test_add_edge(self, graph_db_server):
        """Test adding an edge between nodes."""
        map_id = "test_map_edge"
        graph_db_server.create_map(map_id)
        graph_db_server.add_node("node_1", 0.0, 0.0, 0.0, map_id=map_id)
        graph_db_server.add_node("node_2", 10.0, 0.0, 0.0, map_id=map_id)
        
        result = graph_db_server.add_edge(
            from_node_id="node_1",
            to_node_id="node_2",
            map_id=map_id
        )

        assert result is True

    def test_add_edge_with_metadata(self, graph_db_server):
        """Test adding an edge with metadata."""
        map_id = "test_map_edge_meta"
        graph_db_server.create_map(map_id)
        graph_db_server.add_node("node_1", 0.0, 0.0, 0.0, map_id=map_id)
        graph_db_server.add_node("node_2", 10.0, 0.0, 0.0, map_id=map_id)

        metadata = {"similarity": 0.95, "traversable": True}

        result = graph_db_server.add_edge(
            from_node_id="node_1",
            to_node_id="node_2",
            map_id=map_id,
            metadata=metadata
        )

        assert result is True

    def test_add_edge_missing_nodes(self, graph_db_server):
        """Test adding edge when nodes don't exist."""
        map_id = "test_map_edge_err"
        graph_db_server.create_map(map_id)

        # Try to add edge without creating nodes first
        try:
            result = graph_db_server.add_edge(
                from_node_id="nonexistent_1",
                to_node_id="nonexistent_2",
                map_id=map_id
            )
            # Should raise ValueError or return False
            assert result is False
        except ValueError:
            # Expected behavior
            pass


@pytest.mark.integration
@pytest.mark.requires_docker
class TestGraphDatabaseServerSpatialQueries:
    """Test spatial query operations."""

    def test_nodes_in_range_basic(self, graph_db_server):
        """Test range search for nodes."""
        map_id = "test_map_range"
        graph_db_server.create_map(map_id)

        # Add nodes in a grid pattern
        graph_db_server.add_node("node_1", 0.0, 0.0, 0.0, map_id=map_id)
        graph_db_server.add_node("node_2", 5.0, 0.0, 0.0, map_id=map_id)
        graph_db_server.add_node("node_3", 0.0, 5.0, 0.0, map_id=map_id)
        graph_db_server.add_node("node_4", 20.0, 20.0, 0.0, map_id=map_id)

        # Search for nodes within 6 units of origin
        nodes, distances = graph_db_server.nodes_in_range(
            x=0.0,
            y=0.0,
            radius=6.0,
            map_id=map_id
        )

        # Should find node_1, node_2, and node_3 (not node_4)
        assert len(nodes) == 3
        assert len(distances) == 3

        # Verify distances are within radius
        for dist in distances:
            assert dist <= 6.0

    def test_nodes_in_range_empty(self, graph_db_server):
        """Test range search on empty map."""
        map_id = "test_map_empty"
        graph_db_server.create_map(map_id)

        nodes, distances = graph_db_server.nodes_in_range(
            x=0.0,
            y=0.0,
            radius=10.0,
            map_id=map_id
        )

        assert len(nodes) == 0
        assert len(distances) == 0

    def test_k_nearest_neighbors_basic(self, graph_db_server):
        """Test k-NN search."""
        map_id = "test_map_knn"
        graph_db_server.create_map(map_id)

        # Add nodes
        graph_db_server.add_node("node_1", 0.0, 0.0, 0.0, map_id=map_id)
        graph_db_server.add_node("node_2", 10.0, 0.0, 0.0, map_id=map_id)
        graph_db_server.add_node("node_3", 0.0, 10.0, 0.0, map_id=map_id)
        graph_db_server.add_node("node_4", 20.0, 20.0, 0.0, map_id=map_id)

        # Find 2 nearest neighbors to (5, 0)
        nodes, distances = graph_db_server.k_nearest_neighbors(
            x=5.0,
            y=0.0,
            k=2,
            map_id=map_id
        )

        assert len(nodes) == 2
        assert len(distances) == 2

        # Verify results are sorted by distance
        assert distances[0] <= distances[1]

    def test_k_nearest_neighbors_k_larger_than_nodes(self, graph_db_server):
        """Test k-NN when k > number of nodes."""
        map_id = "test_map_knn_large"
        graph_db_server.create_map(map_id)

        # Add only 2 nodes
        graph_db_server.add_node("node_1", 0.0, 0.0, 0.0, map_id=map_id)
        graph_db_server.add_node("node_2", 10.0, 0.0, 0.0, map_id=map_id)

        # Request 5 nearest neighbors (more than available)
        nodes, distances = graph_db_server.k_nearest_neighbors(
            x=0.0,
            y=0.0,
            k=5,
            map_id=map_id
        )

        # Should return all available nodes (2)
        assert len(nodes) == 2
        assert len(distances) == 2


@pytest.mark.integration
@pytest.mark.requires_docker
class TestGraphDatabaseServerPathfinding:
    """Test pathfinding operations."""

    def test_shortest_path_basic(self, graph_db_server):
        """Test finding shortest path between nodes."""
        map_id = "test_map_path"
        graph_db_server.create_map(map_id)

        # Create a simple path: node_1 -> node_2 -> node_3
        graph_db_server.add_node("node_1", 0.0, 0.0, 0.0, map_id=map_id)
        graph_db_server.add_node("node_2", 10.0, 0.0, 0.0, map_id=map_id)
        graph_db_server.add_node("node_3", 20.0, 0.0, 0.0, map_id=map_id)
        graph_db_server.add_edge("node_1", "node_2", map_id=map_id)
        graph_db_server.add_edge("node_2", "node_3", map_id=map_id)

        # Find path from node_1 to node_3
        path = graph_db_server.shortest_path(
            start_node_id="node_1",
            end_node_id="node_3",
            map_id=map_id
        )

        assert path is not None
        assert len(path) == 3
        assert path[0] == "node_1"
        assert path[-1] == "node_3"

    def test_shortest_path_no_connection(self, graph_db_server):
        """Test pathfinding when no path exists."""
        map_id = "test_map_no_path"
        graph_db_server.create_map(map_id)

        # Create disconnected nodes
        graph_db_server.add_node("node_1", 0.0, 0.0, 0.0, map_id=map_id)
        graph_db_server.add_node("node_2", 10.0, 0.0, 0.0, map_id=map_id)
        # No edge between them

        path = graph_db_server.shortest_path(
            start_node_id="node_1",
            end_node_id="node_2",
            map_id=map_id
        )

        # Should return None when no path exists
        assert path is None


@pytest.mark.integration
@pytest.mark.requires_docker
class TestGraphDatabaseServerSpatialQueries:
    """Test ArangoDB spatial queries."""

    def test_spatial_query_returns_nearby_nodes(self, graph_db_server):
        """Test that nodes_in_range returns nearby nodes via ArangoDB."""
        map_id = "test_map_spatial"
        graph_db_server.create_map(map_id)

        graph_db_server.add_node("node_1", 0.0, 0.0, 0.0, map_id=map_id)
        graph_db_server.add_node("node_2", 10.0, 0.0, 0.0, map_id=map_id)

        nodes, distances = graph_db_server.nodes_in_range(0.0, 0.0, 5.0, map_id=map_id)

        assert len(nodes) > 0

    def test_spatial_query_many_nodes(self, graph_db_server):
        """Test that spatial queries work correctly with many nodes."""
        map_id = "test_map_many_nodes"
        graph_db_server.create_map(map_id)

        for i in range(15):
            graph_db_server.add_node(f"node_{i}", float(i), 0.0, 0.0, map_id=map_id)

        nodes, distances = graph_db_server.nodes_in_range(0.0, 0.0, 100.0, map_id=map_id)

        assert len(nodes) == 15


@pytest.mark.integration
@pytest.mark.requires_docker
class TestGraphDatabaseServerErrorHandling:
    """Test error handling."""

    def test_get_node_nonexistent_map(self, graph_db_server):
        """Test getting node from non-existent map."""
        node = graph_db_server.get_node("nonexistent_map", "node_1")

        assert node is None

    def test_delete_nonexistent_map(self, graph_db_server):
        """Test deleting a map that doesn't exist."""
        result = graph_db_server.delete_map("nonexistent_map")

        # Should handle gracefully (return False or True)
        assert isinstance(result, bool)

    def test_spatial_query_nonexistent_map(self, graph_db_server):
        """Test spatial query on non-existent map."""
        nodes, distances = graph_db_server.nodes_in_range(
            x=0.0,
            y=0.0,
            radius=10.0,
            map_id="nonexistent_map"
        )

        # Should return empty results
        assert len(nodes) == 0
        assert len(distances) == 0

    def test_update_nonexistent_node(self, graph_db_server):
        """Test updating a node that doesn't exist."""
        map_id = "test_map_update_err"
        graph_db_server.create_map(map_id)

        result = graph_db_server.update_node(
            map_id=map_id,
            node_id="nonexistent_node",
            x=10.0,
            y=10.0
        )

        # Should return False for non-existent node
        assert result is False

    def test_delete_nonexistent_node(self, graph_db_server):
        """Test deleting a node that doesn't exist."""
        map_id = "test_map_del_err"
        graph_db_server.create_map(map_id)

        result = graph_db_server.delete_node(map_id, "nonexistent_node")

        # Should return False for non-existent node
        assert result is False

    def test_add_duplicate_node(self, graph_db_server):
        """Test adding a node with duplicate ID."""
        map_id = "test_map_dup_node"
        graph_db_server.create_map(map_id)

        # Add node first time
        result1 = graph_db_server.add_node("node_1", 0.0, 0.0, 0.0, map_id=map_id)
        assert result1 is True

        # Try to add same node again
        result2 = graph_db_server.add_node("node_1", 10.0, 10.0, 0.0, map_id=map_id)

        # Should handle gracefully (may return True or False depending on implementation)
        assert isinstance(result2, bool)

    def test_pathfinding_nonexistent_nodes(self, graph_db_server):
        """Test pathfinding with non-existent nodes."""
        map_id = "test_map_path_err"
        graph_db_server.create_map(map_id)

        # Try to find path between non-existent nodes
        path = graph_db_server.shortest_path(
            start_node_id="nonexistent_1",
            end_node_id="nonexistent_2",
            map_id=map_id
        )

        # Should return None
        assert path is None

    def test_get_map_stats_nonexistent_map(self, graph_db_server):
        """Test getting stats for non-existent map."""
        stats = graph_db_server.get_map_stats("nonexistent_map")

        # Should return None, empty dict, or error dict
        assert stats is None or stats == {} or (isinstance(stats, dict) and "error" in stats)


@pytest.mark.integration
@pytest.mark.requires_docker
class TestGraphDatabaseServerAdvancedPathfinding:
    """Test advanced pathfinding operations."""

    def test_find_path_with_distance(self, graph_db_server):
        """Test find_path method that returns path with distance."""
        map_id = "test_map_find_path"
        graph_db_server.create_map(map_id)

        # Create a simple path: node_1 -> node_2 -> node_3
        graph_db_server.add_node("node_1", 0.0, 0.0, 0.0, map_id=map_id)
        graph_db_server.add_node("node_2", 10.0, 0.0, 0.0, map_id=map_id)
        graph_db_server.add_node("node_3", 20.0, 0.0, 0.0, map_id=map_id)
        graph_db_server.add_edge("node_1", "node_2", map_id=map_id)
        graph_db_server.add_edge("node_2", "node_3", map_id=map_id)

        # Find path with distance
        result = graph_db_server.find_path(
            start_node_id="node_1",
            end_node_id="node_3",
            map_id=map_id
        )

        # Should return dict with path and distance
        if result is not None:
            assert "path" in result or isinstance(result, list)

    def test_find_path_no_connection(self, graph_db_server):
        """Test find_path when no path exists."""
        map_id = "test_map_find_no_path"
        graph_db_server.create_map(map_id)

        # Create disconnected nodes
        graph_db_server.add_node("node_1", 0.0, 0.0, 0.0, map_id=map_id)
        graph_db_server.add_node("node_2", 10.0, 0.0, 0.0, map_id=map_id)

        result = graph_db_server.find_path(
            start_node_id="node_1",
            end_node_id="node_2",
            map_id=map_id
        )

        # Should return None
        assert result is None


@pytest.mark.integration
@pytest.mark.requires_docker
class TestGraphDatabaseServerEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_add_node_with_special_characters(self, graph_db_server):
        """Test adding node with special characters in ID."""
        map_id = "test_map_special"
        graph_db_server.create_map(map_id)

        # Try adding node with special characters
        result = graph_db_server.add_node(
            node_id="node-with-dashes_123",
            x=0.0,
            y=0.0,
            yaw=0.0,
            map_id=map_id
        )

        assert result is True

        # Verify we can retrieve it
        node = graph_db_server.get_node(map_id, "node-with-dashes_123")
        assert node is not None

    def test_add_node_with_large_coordinates(self, graph_db_server):
        """Test adding node with very large coordinates."""
        map_id = "test_map_large"
        graph_db_server.create_map(map_id)

        result = graph_db_server.add_node(
            node_id="node_large",
            x=1000000.0,
            y=1000000.0,
            yaw=6.28,
            map_id=map_id
        )

        assert result is True

    def test_spatial_query_with_large_radius(self, graph_db_server):
        """Test spatial query with very large radius."""
        map_id = "test_map_large_radius"
        graph_db_server.create_map(map_id)

        graph_db_server.add_node("node_1", 0.0, 0.0, 0.0, map_id=map_id)

        # Query with huge radius
        nodes, distances = graph_db_server.nodes_in_range(
            x=0.0,
            y=0.0,
            radius=1000000.0,
            map_id=map_id
        )

        assert len(nodes) == 1

    def test_knn_with_k_zero(self, graph_db_server):
        """Test k-NN with k=0."""
        map_id = "test_map_k_zero"
        graph_db_server.create_map(map_id)

        graph_db_server.add_node("node_1", 0.0, 0.0, 0.0, map_id=map_id)

        nodes, distances = graph_db_server.k_nearest_neighbors(
            x=0.0,
            y=0.0,
            k=0,
            map_id=map_id
        )

        # Should return empty results
        assert len(nodes) == 0
        assert len(distances) == 0

    def test_update_node_partial(self, graph_db_server):
        """Test updating only some node fields."""
        map_id = "test_map_partial"
        graph_db_server.create_map(map_id)

        # Add node
        graph_db_server.add_node("node_1", 0.0, 0.0, 0.0, map_id=map_id)

        # Update only x coordinate
        result = graph_db_server.update_node(
            map_id=map_id,
            node_id="node_1",
            x=10.0
        )

        assert result is True

        # Verify update
        node = graph_db_server.get_node(map_id, "node_1")
        assert node["x"] == 10.0
        assert node["y"] == 0.0  # Should remain unchanged

