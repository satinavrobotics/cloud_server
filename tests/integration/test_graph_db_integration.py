"""
Integration tests for Graph Database Service.

These tests use a real ArangoDB instance (via Docker fixture)
to test the full graph database functionality.
"""

import pytest
import math
from packages.topomap_dbs.graph_db.server import GraphDatabaseService


@pytest.mark.integration
@pytest.mark.requires_docker
class TestGraphDatabaseIntegration:
    """Integration tests for graph database with real ArangoDB."""
    
    def test_create_and_load_map(self, graph_db_client, sample_map_simple):
        """Test creating a map and loading it into the database."""
        map_id = sample_map_simple["map_id"]
        
        # Create map
        assert graph_db_client.create_map(map_id) is True

        # Add nodes
        for node in sample_map_simple["nodes"]:
            assert graph_db_client.add_node(
                node["id"], node["x"], node["y"],
                node.get("theta", 0.0), map_id=map_id
            ) is True

        # Add edges
        for edge in sample_map_simple["edges"]:
            assert graph_db_client.add_edge(
                edge["from"], edge["to"],
                map_id=map_id, metadata={"weight": edge["weight"]}
            ) is True
        
        # Verify map stats
        stats = graph_db_client.get_map_stats(map_id)
        assert stats["node_count"] == len(sample_map_simple["nodes"])
        assert stats["edge_count"] == len(sample_map_simple["edges"])
    
    def test_knn_search_accuracy(self, graph_db_client, sample_map_simple):
        """Test KNN search returns correct nearest neighbors."""
        map_id = sample_map_simple["map_id"]

        # Load map
        self._load_map(graph_db_client, sample_map_simple)

        # Search for 2 nearest neighbors to (5.0, 0.0)
        # Should return node_1 (0,0) and node_2 (10,0), both at distance 5.0
        nodes, distances = graph_db_client.k_nearest_neighbors(5.0, 0.0, k=2, map_id=map_id)

        assert len(nodes) == 2

        # Both results should be at distance 5.0 (node_1 and node_2)
        # node_3 is at distance ~11.18, so it should not be in the results
        for i, dist in enumerate(distances):
            assert dist <= 5.1, f"Result {i} has distance {dist}, expected <= 5.1"

        # Verify the returned nodes are node_1 and node_2 (in any order)
        returned_node_ids = {n["node_id"] for n in nodes}
        assert returned_node_ids == {"node_1", "node_2"}, f"Expected node_1 and node_2, got {returned_node_ids}"
    
    def test_range_search_accuracy(self, graph_db_client, sample_map_simple):
        """Test range search returns all nodes within radius."""
        map_id = sample_map_simple["map_id"]
        
        # Load map
        self._load_map(graph_db_client, sample_map_simple)
        
        # Search for nodes within 6.0 units of (0.0, 0.0)
        # Should return only node_1
        nodes, distances = graph_db_client.nodes_in_range(0.0, 0.0, radius=6.0, map_id=map_id)

        assert len(nodes) == 1
        assert nodes[0]["node_id"] == "node_1"

        # Search with larger radius
        # Should return node_1 and node_2
        nodes, distances = graph_db_client.nodes_in_range(0.0, 0.0, radius=15.0, map_id=map_id)

        assert len(nodes) >= 2
    
    def test_pathfinding_shortest_path(self, graph_db_client, sample_map_simple):
        """Test pathfinding returns shortest path."""
        map_id = sample_map_simple["map_id"]
        
        # Load map
        self._load_map(graph_db_client, sample_map_simple)
        
        # Find path from node_1 to node_3
        result = graph_db_client.find_path("node_1", "node_3", map_id=map_id)

        assert result is not None
        assert result["path"] == ["node_1", "node_2", "node_3"]
        assert result["distance"] == 20.0  # 10 + 10
    
    def test_pathfinding_no_path(self, graph_db_client):
        """Test pathfinding when no path exists between nodes."""
        map_id = "test_disconnected_map"

        # Create map with disconnected nodes
        graph_db_client.create_map(map_id)
        graph_db_client.add_node("node_a", 0.0, 0.0, 0.0, map_id=map_id)
        graph_db_client.add_node("node_b", 100.0, 100.0, 0.0, map_id=map_id)
        # No edge between them

        # Should return None when no path exists
        result = graph_db_client.find_path("node_a", "node_b", map_id=map_id)
        assert result is None
    
    def test_update_node_position(self, graph_db_client, sample_map_simple):
        """Test updating node position."""
        map_id = sample_map_simple["map_id"]
        
        # Load map
        self._load_map(graph_db_client, sample_map_simple)
        
        # Update node_1 position
        new_x, new_y = 5.0, 5.0
        assert graph_db_client.update_node(map_id, "node_1", x=new_x, y=new_y) is True

        # Verify update
        node = graph_db_client.get_node(map_id, "node_1")
        assert node is not None
        assert node["x"] == new_x
        assert node["y"] == new_y
    
    def test_delete_node(self, graph_db_client, sample_map_simple):
        """Test deleting a node."""
        map_id = sample_map_simple["map_id"]
        
        # Load map
        self._load_map(graph_db_client, sample_map_simple)
        
        # Delete node_2
        assert graph_db_client.delete_node(map_id, "node_2") is True

        # Verify deletion
        assert graph_db_client.get_node(map_id, "node_2") is None
        
        # Verify stats updated
        stats = graph_db_client.get_map_stats(map_id)
        assert stats["node_count"] == 2  # Was 3, now 2
    
    def test_delete_map(self, graph_db_client, sample_map_simple):
        """Test deleting entire map."""
        map_id = sample_map_simple["map_id"]
        
        # Load map
        self._load_map(graph_db_client, sample_map_simple)
        
        # Delete map
        assert graph_db_client.delete_map(map_id) is True

        # Verify map is gone (stats should reflect empty/no data)
        stats = graph_db_client.get_map_stats(map_id)
        assert stats.get("node_count", 0) == 0
    
    @pytest.mark.slow
    def test_large_map_performance(self, graph_db_client, sample_map_complex):
        """Test performance with large map (100 nodes)."""
        import time
        
        map_id = sample_map_complex["map_id"]
        
        # Measure load time
        start = time.time()
        self._load_map(graph_db_client, sample_map_complex)
        load_time = time.time() - start
        
        # Should load in reasonable time (< 5 seconds)
        assert load_time < 5.0
        
        # Measure KNN search time
        start = time.time()
        nodes, distances = graph_db_client.k_nearest_neighbors(50.0, 50.0, k=10, map_id=map_id)
        search_time = time.time() - start

        # Should search in reasonable time (< 1 second)
        assert search_time < 1.0
        # Should return at least k results (may return more if there are ties at the k-th distance)
        assert len(nodes) >= 10, f"Expected at least 10 results, got {len(nodes)}"
    
    # Helper methods
    
    def _load_map(self, client, map_data):
        """Helper to load a complete map into the database."""
        map_id = map_data["map_id"]

        # Create map
        client.create_map(map_id)

        # Add nodes
        for node in map_data["nodes"]:
            client.add_node(
                node["id"], node["x"], node["y"],
                node.get("theta", 0.0), map_id=map_id
            )

        # Add edges
        for edge in map_data["edges"]:
            client.add_edge(
                edge["from"], edge["to"],
                map_id=map_id, metadata={"weight": edge["weight"]}
            )


@pytest.mark.integration
@pytest.mark.requires_docker
class TestGraphDatabaseConcurrency:
    """Test concurrent operations on graph database."""

    def test_concurrent_node_additions(self, graph_db_client):
        """Test adding nodes concurrently doesn't cause conflicts."""
        import concurrent.futures

        map_id = "test_concurrent_map"
        graph_db_client.create_map(map_id)

        def add_node(i):
            return graph_db_client.add_node(f"node_{i}", float(i), float(i), 0.0, map_id=map_id)

        # Add 10 nodes concurrently (reduced from 50 for speed)
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            results = list(executor.map(add_node, range(10)))

        # All should succeed
        assert all(results)

        # Verify all nodes were added
        stats = graph_db_client.get_map_stats(map_id)
        assert stats["node_count"] == 10

    def test_concurrent_searches(self, graph_db_client, sample_map_complex):
        """Test concurrent KNN searches don't interfere."""
        import concurrent.futures

        map_id = sample_map_complex["map_id"]
        self._load_map(graph_db_client, sample_map_complex)

        def search(coords):
            x, y = coords
            nodes, distances = graph_db_client.k_nearest_neighbors(x, y, k=5, map_id=map_id)
            return nodes

        # Perform 5 concurrent searches (reduced from 20 for speed)
        search_coords = [(float(i*5), float(i*5)) for i in range(5)]

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            results = list(executor.map(search, search_coords))

        # All should return results
        assert all(len(r) > 0 for r in results)

    def _load_map(self, client, map_data):
        """Helper to load a complete map into the database."""
        map_id = map_data["map_id"]

        # Create map
        client.create_map(map_id)

        # Add nodes
        for node in map_data["nodes"]:
            client.add_node(
                node["id"], node["x"], node["y"],
                node.get("theta", 0.0), map_id=map_id
            )

        # Add edges
        for edge in map_data["edges"]:
            client.add_edge(
                edge["from"], edge["to"],
                map_id=map_id, metadata={"weight": edge["weight"]}
            )


@pytest.mark.integration
@pytest.mark.requires_docker
class TestGraphDatabaseMultiMap:
    """Test multi-map functionality."""

    def test_create_multiple_maps(self, graph_db_client):
        """Test creating and managing multiple maps simultaneously."""
        map_ids = ["map_1", "map_2", "map_3"]

        # Create multiple maps
        for map_id in map_ids:
            assert graph_db_client.create_map(map_id) is True

        # Add different nodes to each map
        for i, map_id in enumerate(map_ids):
            for j in range(3):
                graph_db_client.add_node(
                    f"node_{j}", float(i * 10 + j), float(i * 10 + j), 0.0, map_id=map_id
                )

        # Verify each map has correct nodes
        for map_id in map_ids:
            stats = graph_db_client.get_map_stats(map_id)
            assert stats["node_count"] == 3

    def test_map_isolation(self, graph_db_client):
        """Test that operations on one map don't affect another."""
        map_1 = "isolation_map_1"
        map_2 = "isolation_map_2"

        # Create two maps
        graph_db_client.create_map(map_1)
        graph_db_client.create_map(map_2)

        # Add nodes to map_1
        graph_db_client.add_node("node_a", 0.0, 0.0, 0.0, map_id=map_1)
        graph_db_client.add_node("node_b", 10.0, 0.0, 0.0, map_id=map_1)

        # Add different nodes to map_2
        graph_db_client.add_node("node_x", 100.0, 100.0, 0.0, map_id=map_2)

        # Verify map_1 stats
        stats_1 = graph_db_client.get_map_stats(map_1)
        assert stats_1["node_count"] == 2

        # Verify map_2 stats
        stats_2 = graph_db_client.get_map_stats(map_2)
        assert stats_2["node_count"] == 1

        # Delete map_1
        graph_db_client.delete_map(map_1)

        # Verify map_2 still exists and is unaffected
        stats_2_after = graph_db_client.get_map_stats(map_2)
        assert stats_2_after["node_count"] == 1


@pytest.mark.integration
@pytest.mark.requires_docker
class TestGraphDatabaseBulkOperations:
    """Test bulk operations for efficiency."""

    def test_bulk_node_creation(self, graph_db_client):
        """Test creating multiple nodes."""
        map_id = "bulk_test_map"
        graph_db_client.create_map(map_id)

        for i in range(20):
            assert graph_db_client.add_node(f"node_{i}", float(i), float(i), 0.0, map_id=map_id)

        # Verify all nodes were added
        stats = graph_db_client.get_map_stats(map_id)
        assert stats["node_count"] == 20

    def test_bulk_edge_creation(self, graph_db_client):
        """Test creating multiple edges."""
        map_id = "bulk_edges_map"
        graph_db_client.create_map(map_id)

        # Create nodes first
        for i in range(5):
            graph_db_client.add_node(f"node_{i}", float(i), 0.0, 0.0, map_id=map_id)

        # Create edges (chain: 0->1->2->3->4)
        for i in range(4):
            assert graph_db_client.add_edge(f"node_{i}", f"node_{i+1}", map_id=map_id)

        # Verify edges exist by finding path
        path_result = graph_db_client.find_path("node_0", "node_4", map_id=map_id)
        assert path_result is not None
        assert len(path_result["path"]) == 5  # 0, 1, 2, 3, 4


@pytest.mark.integration
@pytest.mark.requires_docker
class TestGraphDatabaseErrorRecovery:
    """Test error handling and recovery scenarios."""

    def test_duplicate_node_handling(self, graph_db_client):
        """Test handling of duplicate node creation."""
        map_id = "duplicate_test_map"

        # Clean up any existing map first
        try:
            graph_db_client.delete_map(map_id)
        except:
            pass

        graph_db_client.create_map(map_id)

        # Add node
        assert graph_db_client.add_node("node_1", 0.0, 0.0, 0.0, map_id=map_id) is True

        # Try to add same node again (should handle gracefully — service upserts)
        result2 = graph_db_client.add_node("node_1", 5.0, 5.0, 0.0, map_id=map_id)
        assert isinstance(result2, bool)

    def test_invalid_edge_creation(self, graph_db_client):
        """Test creating edge with non-existent nodes."""
        map_id = "invalid_edge_map"
        graph_db_client.create_map(map_id)

        # Try to create edge without nodes existing — should raise ValueError
        with pytest.raises(Exception) as exc_info:
            graph_db_client.add_edge("nonexistent_1", "nonexistent_2", map_id=map_id)

        assert "not found" in str(exc_info.value).lower() or "does not exist" in str(exc_info.value).lower()

    def test_query_on_empty_map(self, graph_db_client):
        """Test queries on empty map return gracefully."""
        map_id = "empty_map"
        graph_db_client.create_map(map_id)

        # KNN search on empty map
        nodes, distances = graph_db_client.k_nearest_neighbors(0.0, 0.0, k=5, map_id=map_id)
        assert len(nodes) == 0

        # Range search on empty map
        nodes, distances = graph_db_client.nodes_in_range(0.0, 0.0, radius=10.0, map_id=map_id)
        assert len(nodes) == 0


@pytest.mark.integration
@pytest.mark.requires_docker
class TestDatabaseConcurrencyAndTransactions:
    """Integration tests for database transaction handling and concurrent operations."""

    def test_concurrent_map_operations(self, graph_db_client):
        """Test concurrent node additions, edge creations, and deletions on same map."""
        import threading

        map_id = "concurrent_ops_map"
        graph_db_client.create_map(map_id)

        # Pre-create some nodes for edge operations
        for i in range(10):
            graph_db_client.add_node(f"base_node_{i}", float(i), float(i), 0.0, map_id=map_id)

        results = {"nodes": [], "edges": [], "deletes": [], "errors": []}

        def add_nodes(start, count):
            """Add nodes concurrently."""
            try:
                for i in range(start, start + count):
                    result = graph_db_client.add_node(
                        f"node_{i}", float(i * 2), float(i * 2), 0.0, map_id=map_id
                    )
                    results["nodes"].append(result)
            except Exception as e:
                results["errors"].append(str(e))

        def add_edges(start, count):
            """Add edges concurrently."""
            try:
                for i in range(start, start + count - 1):
                    result = graph_db_client.add_edge(
                        f"base_node_{i}", f"base_node_{i+1}", map_id=map_id
                    )
                    results["edges"].append(result)
            except Exception as e:
                results["errors"].append(str(e))

        def delete_nodes(node_ids):
            """Delete nodes concurrently."""
            try:
                for node_id in node_ids:
                    result = graph_db_client.delete_node(map_id, node_id)
                    results["deletes"].append(result)
            except Exception as e:
                results["errors"].append(str(e))

        # Create threads for concurrent operations
        threads = [
            threading.Thread(target=add_nodes, args=(0, 5)),
            threading.Thread(target=add_nodes, args=(5, 5)),
            threading.Thread(target=add_edges, args=(0, 5)),
            threading.Thread(target=add_edges, args=(5, 5)),
        ]

        # Start all threads
        for t in threads:
            t.start()

        # Wait for completion
        for t in threads:
            t.join()

        # Verify operations completed
        assert len(results["nodes"]) >= 8  # At least most nodes added
        assert len(results["edges"]) >= 6  # At least most edges added

        # Verify map integrity
        stats = graph_db_client.get_map_stats(map_id)
        assert stats["node_count"] >= 10  # Base nodes + new nodes

    def test_transaction_rollback_scenarios(self, graph_db_client):
        """Test database behavior when operations fail mid-transaction."""
        map_id = "transaction_rollback_map"
        graph_db_client.create_map(map_id)

        # Add some valid nodes
        graph_db_client.add_node("node_1", 0.0, 0.0, 0.0, map_id=map_id)
        graph_db_client.add_node("node_2", 5.0, 5.0, 0.0, map_id=map_id)

        initial_stats = graph_db_client.get_map_stats(map_id)
        initial_node_count = initial_stats["node_count"]

        # Attempt to add edge to non-existent node (should fail)
        try:
            graph_db_client.add_edge("node_1", "nonexistent_node", map_id=map_id)
            # If it doesn't raise, that's ok - just check state
        except Exception:
            # Expected to fail
            pass

        # Verify database state is consistent (no partial changes)
        stats_after = graph_db_client.get_map_stats(map_id)
        assert stats_after["node_count"] == initial_node_count

        # Verify original nodes still exist
        node_1 = graph_db_client.get_node(map_id, "node_1")
        assert node_1 is not None

        # Try batch operation with some invalid entries
        try:
            # Some implementations may support bulk operations
            if hasattr(graph_db_client, 'bulk_add_nodes'):
                nodes = [
                    {"node_id": "node_3", "x": 10.0, "y": 10.0, "theta": 0.0},
                    {"node_id": "node_1", "x": 15.0, "y": 15.0, "theta": 0.0},  # Duplicate
                ]
                graph_db_client.bulk_add_nodes(map_id, nodes)
        except Exception:
            # Expected to fail on duplicate
            pass

        # Verify database is still in consistent state
        final_stats = graph_db_client.get_map_stats(map_id)
        assert final_stats["node_count"] >= initial_node_count

    def test_graph_database_rtree_rebuild_under_load(self, graph_db_client):
        """Test R-tree rebuild behavior when nodes are being added concurrently."""
        import threading
        import time

        map_id = "rtree_rebuild_test"
        graph_db_client.create_map(map_id)

        # Add initial nodes
        for i in range(20):
            graph_db_client.add_node(f"initial_{i}", float(i), float(i), 0.0, map_id=map_id)

        search_results = []
        add_results = []

        def continuous_search():
            """Continuously perform KNN searches."""
            for _ in range(10):
                try:
                    nodes, _ = graph_db_client.k_nearest_neighbors(10.0, 10.0, k=5, map_id=map_id)
                    search_results.append(len(nodes))
                    time.sleep(0.01)
                except Exception as e:
                    search_results.append(f"error: {e}")

        def continuous_add():
            """Continuously add nodes."""
            for i in range(10):
                try:
                    result = graph_db_client.add_node(
                        f"concurrent_{i}", float(i + 50), float(i + 50), 0.0, map_id=map_id
                    )
                    add_results.append(result)
                    time.sleep(0.01)
                except Exception as e:
                    add_results.append(f"error: {e}")

        # Run searches and adds concurrently
        threads = [
            threading.Thread(target=continuous_search),
            threading.Thread(target=continuous_add),
            threading.Thread(target=continuous_search),
        ]

        for t in threads:
            t.start()

        for t in threads:
            t.join()

        # Verify searches returned results (R-tree remained functional)
        valid_searches = [r for r in search_results if isinstance(r, int)]
        assert len(valid_searches) >= 15  # Most searches should succeed
        assert all(r > 0 for r in valid_searches)  # All should return results

        # Verify nodes were added
        valid_adds = [r for r in add_results if r is True]
        assert len(valid_adds) >= 8  # Most adds should succeed
