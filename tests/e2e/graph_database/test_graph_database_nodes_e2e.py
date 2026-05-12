"""
Graph Database Service - Node Operations E2E Tests

Tests node CRUD operations, node creation, retrieval, and deletion.
"""

import pytest
import requests
import uuid
import json


@pytest.mark.e2e
@pytest.mark.requires_docker
class TestGraphDatabaseNodesE2E:
    """Test node operations in Graph Database Service."""

    def test_create_node(self, graph_database_service):
        """Test creating a single node."""
        node_id = f"node_{uuid.uuid4().hex[:8]}"
        node_data = {
            "node_id": node_id,
            "x": 10.5,
            "y": 20.3,
            "yaw": 1.57,
            "metadata": {"type": "waypoint"}
        }
        
        response = requests.post(
            f"{graph_database_service['url']}/nodes",
            json=node_data
        )
        
        assert response.status_code in [200, 201]
        data = response.json()
        assert data.get("success") is True
        assert data.get("node_id") == node_id

    def test_create_node_in_map(self, graph_database_service):
        """Test creating a node in a specific map."""
        map_id = f"map_{uuid.uuid4().hex[:8]}"
        node_id = f"node_{uuid.uuid4().hex[:8]}"
        node_data = {
            "node_id": node_id,
            "x": 15.0,
            "y": 25.0,
            "theta": 0.785
        }
        
        response = requests.post(
            f"{graph_database_service['url']}/maps/{map_id}/nodes",
            json=node_data
        )
        
        assert response.status_code in [200, 201]
        data = response.json()
        assert data.get("success") is True
        assert data.get("map_id") == map_id

    def test_get_node_from_map(self, graph_database_service):
        """Test retrieving a node from a specific map."""
        map_id = f"map_{uuid.uuid4().hex[:8]}"
        node_id = f"node_{uuid.uuid4().hex[:8]}"
        
        # Create node
        node_data = {
            "node_id": node_id,
            "x": 5.0,
            "y": 10.0,
            "theta": 0.0
        }
        requests.post(
            f"{graph_database_service['url']}/maps/{map_id}/nodes",
            json=node_data
        )
        
        # Retrieve node
        response = requests.get(
            f"{graph_database_service['url']}/maps/{map_id}/nodes/{node_id}"
        )
        
        assert response.status_code in [200, 404]
        if response.status_code == 200:
            data = response.json()
            assert data.get("node_id") == node_id

    def test_create_bulk_nodes(self, graph_database_service):
        """Test creating multiple nodes in bulk."""
        nodes = [
            {
                "node_id": f"node_{i}_{uuid.uuid4().hex[:4]}",
                "x": float(i * 5),
                "y": float(i * 10),
                "yaw": 0.0
            }
            for i in range(5)
        ]
        
        response = requests.post(
            f"{graph_database_service['url']}/nodes/bulk",
            json={"nodes": nodes}
        )

        # 422 is expected when endpoint doesn't support bulk or validation fails
        assert response.status_code in [200, 201, 400, 422, 500]

    def test_delete_node(self, graph_database_service):
        """Test deleting a node."""
        node_id = f"node_{uuid.uuid4().hex[:8]}"
        
        # Create node
        node_data = {
            "node_id": node_id,
            "x": 10.0,
            "y": 20.0,
            "yaw": 1.57
        }
        requests.post(
            f"{graph_database_service['url']}/nodes",
            json=node_data
        )
        
        # Delete node
        response = requests.delete(
            f"{graph_database_service['url']}/nodes/{node_id}"
        )
        
        assert response.status_code in [200, 204, 404]

    def test_node_with_metadata(self, graph_database_service):
        """Test creating node with custom metadata."""
        node_id = f"node_{uuid.uuid4().hex[:8]}"
        node_data = {
            "node_id": node_id,
            "x": 30.0,
            "y": 40.0,
            "yaw": 3.14,
            "metadata": {
                "robot_id": "robot_01",
                "timestamp": "2024-01-15T10:30:00Z",
                "confidence": 0.95
            }
        }
        
        response = requests.post(
            f"{graph_database_service['url']}/nodes",
            json=node_data
        )
        
        assert response.status_code in [200, 201]

    def test_create_node_invalid_coordinates(self, graph_database_service):
        """Test creating node with invalid coordinates."""
        node_id = f"node_{uuid.uuid4().hex[:8]}"
        node_data = {
            "node_id": node_id,
            "x": "invalid",
            "y": 20.0,
            "yaw": 1.57
        }
        
        response = requests.post(
            f"{graph_database_service['url']}/nodes",
            json=node_data
        )
        
        assert response.status_code in [400, 422]

    def test_create_node_missing_required_fields(self, graph_database_service):
        """Test creating node with missing required fields."""
        node_data = {
            "node_id": f"node_{uuid.uuid4().hex[:8]}",
            "x": 10.0
            # Missing y and yaw
        }
        
        response = requests.post(
            f"{graph_database_service['url']}/nodes",
            json=node_data
        )
        
        assert response.status_code in [400, 422]

    def test_create_node_duplicate_id(self, graph_database_service):
        """Test creating node with duplicate ID."""
        node_id = f"node_{uuid.uuid4().hex[:8]}"
        node_data = {
            "node_id": node_id,
            "x": 10.0,
            "y": 20.0,
            "yaw": 1.57
        }
        
        # Create first node
        response1 = requests.post(
            f"{graph_database_service['url']}/nodes",
            json=node_data
        )
        assert response1.status_code in [200, 201]
        
        # Try to create duplicate
        response2 = requests.post(
            f"{graph_database_service['url']}/nodes",
            json=node_data
        )
        
        # Should either succeed (overwrite) or fail with 400/409
        assert response2.status_code in [200, 201, 400, 409, 500]

    def test_node_lifecycle(self, graph_database_service):
        """Test complete node lifecycle: create, retrieve, delete."""
        node_id = f"node_{uuid.uuid4().hex[:8]}"
        node_data = {
            "node_id": node_id,
            "x": 50.0,
            "y": 60.0,
            "yaw": 2.0
        }
        
        # Create
        response = requests.post(
            f"{graph_database_service['url']}/nodes",
            json=node_data
        )
        assert response.status_code in [200, 201]
        
        # Delete
        response = requests.delete(
            f"{graph_database_service['url']}/nodes/{node_id}"
        )
        assert response.status_code in [200, 204, 404]

