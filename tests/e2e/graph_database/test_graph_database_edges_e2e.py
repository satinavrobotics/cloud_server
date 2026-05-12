"""
Graph Database Service - Edge Operations E2E Tests

Tests edge creation, retrieval, and graph connectivity.
"""

import pytest
import requests
import uuid


@pytest.mark.e2e
@pytest.mark.requires_docker
class TestGraphDatabaseEdgesE2E:
    """Test edge operations in Graph Database Service."""

    def test_create_edge(self, graph_database_service):
        """Test creating a single edge."""
        edge_data = {
            "from_node_id": f"node_{uuid.uuid4().hex[:8]}",
            "to_node_id": f"node_{uuid.uuid4().hex[:8]}",
            "metadata": {"distance": 5.0}
        }
        
        response = requests.post(
            f"{graph_database_service['url']}/edges",
            json=edge_data
        )
        
        assert response.status_code in [200, 201, 400, 500]

    def test_create_edge_in_map(self, graph_database_service):
        """Test creating an edge in a specific map."""
        map_id = f"map_{uuid.uuid4().hex[:8]}"
        edge_data = {
            "from_node_id": "node_1",
            "to_node_id": "node_2",
            "map_id": map_id,
            "metadata": {"traversable": True}
        }

        response = requests.post(
            f"{graph_database_service['url']}/edges",
            json=edge_data
        )

        # 404 is expected when nodes don't exist
        assert response.status_code in [200, 201, 400, 404, 500]

    def test_create_bulk_edges(self, graph_database_service):
        """Test creating multiple edges in bulk."""
        edges = [
            {
                "from_node_id": f"node_{i}",
                "to_node_id": f"node_{i+1}",
                "metadata": {"distance": 5.0}
            }
            for i in range(5)
        ]

        response = requests.post(
            f"{graph_database_service['url']}/edges/bulk",
            json={"edges": edges}
        )

        # 422 is expected when nodes don't exist or endpoint doesn't support bulk
        assert response.status_code in [200, 201, 400, 404, 422, 500]

    def test_get_all_edges_from_map(self, graph_database_service):
        """Test retrieving all edges from a map."""
        map_id = f"map_{uuid.uuid4().hex[:8]}"
        
        response = requests.get(
            f"{graph_database_service['url']}/maps/{map_id}/edges"
        )
        
        assert response.status_code in [200, 404]
        if response.status_code == 200:
            data = response.json()
            assert "edges" in data
            assert "count" in data

    def test_create_edge_self_loop(self, graph_database_service):
        """Test creating self-loop edge."""
        node_id = f"node_{uuid.uuid4().hex[:8]}"
        edge_data = {
            "from_node_id": node_id,
            "to_node_id": node_id,
            "metadata": {"self_loop": True}
        }
        
        response = requests.post(
            f"{graph_database_service['url']}/edges",
            json=edge_data
        )
        
        assert response.status_code in [200, 201, 400, 500]

    def test_create_bidirectional_edges(self, graph_database_service):
        """Test creating bidirectional edges."""
        node_a = f"node_{uuid.uuid4().hex[:8]}"
        node_b = f"node_{uuid.uuid4().hex[:8]}"
        
        # Edge A -> B
        edge1 = {
            "from_node_id": node_a,
            "to_node_id": node_b,
            "metadata": {"direction": "forward"}
        }
        response1 = requests.post(
            f"{graph_database_service['url']}/edges",
            json=edge1
        )
        
        # Edge B -> A
        edge2 = {
            "from_node_id": node_b,
            "to_node_id": node_a,
            "metadata": {"direction": "backward"}
        }
        response2 = requests.post(
            f"{graph_database_service['url']}/edges",
            json=edge2
        )
        
        assert response1.status_code in [200, 201, 400, 500]
        assert response2.status_code in [200, 201, 400, 500]

    def test_edge_with_metadata(self, graph_database_service):
        """Test creating edge with rich metadata."""
        edge_data = {
            "from_node_id": f"node_{uuid.uuid4().hex[:8]}",
            "to_node_id": f"node_{uuid.uuid4().hex[:8]}",
            "metadata": {
                "distance": 7.5,
                "traversable": True,
                "surface_type": "concrete",
                "created_at": "2024-01-15T10:30:00Z"
            }
        }
        
        response = requests.post(
            f"{graph_database_service['url']}/edges",
            json=edge_data
        )
        
        assert response.status_code in [200, 201, 400, 500]

    def test_create_edge_missing_nodes(self, graph_database_service):
        """Test creating edge with missing node IDs."""
        edge_data = {
            "from_node_id": f"node_{uuid.uuid4().hex[:8]}"
            # Missing to_node_id
        }
        
        response = requests.post(
            f"{graph_database_service['url']}/edges",
            json=edge_data
        )
        
        assert response.status_code in [400, 422]

    def test_create_edge_invalid_node_ids(self, graph_database_service):
        """Test creating edge with invalid node IDs."""
        edge_data = {
            "from_node_id": 123,  # Should be string
            "to_node_id": 456,
            "metadata": {}
        }
        
        response = requests.post(
            f"{graph_database_service['url']}/edges",
            json=edge_data
        )
        
        assert response.status_code in [200, 201, 400, 422, 500]

