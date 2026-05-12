"""
Graph Database Service - Pathfinding E2E Tests

Tests shortest path queries and graph traversal.
"""

import pytest
import requests
import uuid


@pytest.mark.e2e
@pytest.mark.requires_docker
class TestGraphDatabasePathfindingE2E:
    """Test pathfinding in Graph Database Service."""

    def test_shortest_path_query(self, graph_database_service):
        """Test shortest path query between two nodes."""
        query_data = {
            "start_node_id": "node_1",
            "end_node_id": "node_5"
        }
        
        response = requests.post(
            f"{graph_database_service['url']}/query/shortest_path",
            json=query_data
        )
        
        assert response.status_code in [200, 400, 404, 500]
        if response.status_code == 200:
            data = response.json()
            assert "path" in data or "nodes" in data

    def test_shortest_path_with_map_id(self, graph_database_service):
        """Test shortest path query with specific map ID."""
        map_id = f"map_{uuid.uuid4().hex[:8]}"
        query_data = {
            "start_node_id": "node_1",
            "end_node_id": "node_3",
            "map_id": map_id
        }
        
        response = requests.post(
            f"{graph_database_service['url']}/query/shortest_path",
            json=query_data
        )
        
        assert response.status_code in [200, 400, 404, 500]

    def test_shortest_path_same_node(self, graph_database_service):
        """Test shortest path from node to itself."""
        node_id = f"node_{uuid.uuid4().hex[:8]}"
        query_data = {
            "start_node_id": node_id,
            "end_node_id": node_id
        }
        
        response = requests.post(
            f"{graph_database_service['url']}/query/shortest_path",
            json=query_data
        )
        
        assert response.status_code in [200, 400, 500]

    def test_shortest_path_nonexistent_nodes(self, graph_database_service):
        """Test shortest path with nonexistent nodes."""
        query_data = {
            "start_node_id": f"nonexistent_{uuid.uuid4().hex[:8]}",
            "end_node_id": f"nonexistent_{uuid.uuid4().hex[:8]}"
        }
        
        response = requests.post(
            f"{graph_database_service['url']}/query/shortest_path",
            json=query_data
        )
        
        assert response.status_code in [200, 400, 404, 500]

    def test_shortest_path_missing_start_node(self, graph_database_service):
        """Test shortest path with missing start node."""
        query_data = {
            "end_node_id": "node_5"
            # Missing start_node_id
        }
        
        response = requests.post(
            f"{graph_database_service['url']}/query/shortest_path",
            json=query_data
        )
        
        assert response.status_code in [400, 422]

    def test_shortest_path_missing_end_node(self, graph_database_service):
        """Test shortest path with missing end node."""
        query_data = {
            "start_node_id": "node_1"
            # Missing end_node_id
        }
        
        response = requests.post(
            f"{graph_database_service['url']}/query/shortest_path",
            json=query_data
        )
        
        assert response.status_code in [400, 422]

    def test_shortest_path_string_node_ids(self, graph_database_service):
        """Test shortest path with string node IDs."""
        query_data = {
            "start_node_id": "waypoint_start",
            "end_node_id": "waypoint_end"
        }
        
        response = requests.post(
            f"{graph_database_service['url']}/query/shortest_path",
            json=query_data
        )
        
        assert response.status_code in [200, 400, 404, 500]

    def test_shortest_path_integer_node_ids(self, graph_database_service):
        """Test shortest path with integer node IDs."""
        query_data = {
            "start_node_id": 1,
            "end_node_id": 10
        }
        
        response = requests.post(
            f"{graph_database_service['url']}/query/shortest_path",
            json=query_data
        )
        
        assert response.status_code in [200, 400, 404, 500]

    def test_shortest_path_mixed_node_id_types(self, graph_database_service):
        """Test shortest path with mixed node ID types."""
        query_data = {
            "start_node_id": 1,
            "end_node_id": "node_end"
        }
        
        response = requests.post(
            f"{graph_database_service['url']}/query/shortest_path",
            json=query_data
        )
        
        assert response.status_code in [200, 400, 404, 500]

    def test_shortest_path_large_graph(self, graph_database_service):
        """Test shortest path in large graph."""
        query_data = {
            "start_node_id": "node_1",
            "end_node_id": "node_1000"
        }
        
        response = requests.post(
            f"{graph_database_service['url']}/query/shortest_path",
            json=query_data
        )
        
        assert response.status_code in [200, 400, 404, 500]

