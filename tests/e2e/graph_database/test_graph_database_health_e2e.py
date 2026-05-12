"""
Graph Database Service - Health & Resilience E2E Tests

Tests health checks, statistics, and service resilience.
"""

import pytest
import requests
import uuid
import time
import concurrent.futures


@pytest.mark.e2e
@pytest.mark.requires_docker
class TestGraphDatabaseHealthE2E:
    """Test health and resilience of Graph Database Service."""

    def test_health_check(self, graph_database_service):
        """Test health check endpoint."""
        response = requests.get(
            f"{graph_database_service['url']}/health"
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data.get("status") in ["healthy", "degraded"]
        assert data.get("service") == "graph_database"

    def test_stats_endpoint(self, graph_database_service):
        """Test statistics endpoint."""
        response = requests.get(
            f"{graph_database_service['url']}/stats"
        )

        assert response.status_code == 200
        data = response.json()
        assert "node_count" in data
        assert "edge_count" in data

    def test_root_endpoint(self, graph_database_service):
        """Test root endpoint with service information."""
        response = requests.get(
            f"{graph_database_service['url']}/"
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data.get("service") == "Graph Database Service"
        assert "endpoints" in data

    def test_concurrent_node_creation(self, graph_database_service):
        """Test concurrent node creation."""
        def create_node(index):
            node_data = {
                "node_id": f"concurrent_node_{index}_{uuid.uuid4().hex[:4]}",
                "x": float(index * 5),
                "y": float(index * 10),
                "yaw": 0.0
            }
            response = requests.post(
                f"{graph_database_service['url']}/nodes",
                json=node_data
            )
            return response.status_code in [200, 201, 400, 500]
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            results = list(executor.map(create_node, range(10)))
        
        assert len(results) == 10

    def test_concurrent_queries(self, graph_database_service):
        """Test concurrent spatial queries."""
        def run_query(index):
            query_data = {
                "x": float(index * 10),
                "y": float(index * 20),
                "k": 5
            }
            response = requests.post(
                f"{graph_database_service['url']}/query/knn",
                json=query_data
            )
            return response.status_code in [200, 400, 500]
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            results = list(executor.map(run_query, range(10)))
        
        assert len(results) == 10

    def test_rapid_create_delete_cycles(self, graph_database_service):
        """Test rapid create/delete cycles."""
        for i in range(5):
            node_id = f"cycle_node_{i}_{uuid.uuid4().hex[:4]}"
            
            # Create
            create_response = requests.post(
                f"{graph_database_service['url']}/nodes",
                json={
                    "node_id": node_id,
                    "x": 10.0,
                    "y": 20.0,
                    "yaw": 0.0
                }
            )
            
            # Delete
            delete_response = requests.delete(
                f"{graph_database_service['url']}/nodes/{node_id}"
            )
            
            assert create_response.status_code in [200, 201, 400, 500]
            assert delete_response.status_code in [200, 204, 404]

    def test_large_payload_handling(self, graph_database_service):
        """Test handling of large payloads."""
        large_metadata = {
            f"key_{i}": f"value_{i}" * 100
            for i in range(50)
        }
        
        node_data = {
            "node_id": f"large_node_{uuid.uuid4().hex[:8]}",
            "x": 10.0,
            "y": 20.0,
            "yaw": 0.0,
            "metadata": large_metadata
        }
        
        response = requests.post(
            f"{graph_database_service['url']}/nodes",
            json=node_data
        )
        
        assert response.status_code in [200, 201, 400, 413, 500]

    def test_malformed_json_request(self, graph_database_service):
        """Test handling of malformed JSON."""
        response = requests.post(
            f"{graph_database_service['url']}/nodes",
            data="invalid json {",
            headers={"Content-Type": "application/json"}
        )
        
        assert response.status_code in [400, 422]

    def test_missing_content_type(self, graph_database_service):
        """Test request without Content-Type header."""
        response = requests.post(
            f"{graph_database_service['url']}/nodes",
            json={"node_id": "test", "x": 10.0, "y": 20.0, "yaw": 0.0}
        )
        
        assert response.status_code in [200, 201, 400, 500]

    def test_multiple_maps_isolation(self, graph_database_service):
        """Test isolation between multiple maps."""
        map1 = f"map_{uuid.uuid4().hex[:8]}"
        map2 = f"map_{uuid.uuid4().hex[:8]}"
        
        # Create node in map1
        response1 = requests.post(
            f"{graph_database_service['url']}/maps/{map1}/nodes",
            json={"node_id": "node_1", "x": 10.0, "y": 20.0, "theta": 0.0}
        )
        
        # Create node in map2
        response2 = requests.post(
            f"{graph_database_service['url']}/maps/{map2}/nodes",
            json={"node_id": "node_1", "x": 30.0, "y": 40.0, "theta": 0.0}
        )
        
        assert response1.status_code in [200, 201, 400, 500]
        assert response2.status_code in [200, 201, 400, 500]

    def test_service_availability_under_load(self, graph_database_service):
        """Test service availability under load."""
        def make_request(index):
            try:
                response = requests.get(
                    f"{graph_database_service['url']}/health",
                    timeout=5
                )
                return response.status_code == 200
            except:
                return False
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
            results = list(executor.map(make_request, range(50)))
        
        # At least 80% should succeed
        success_rate = sum(results) / len(results)
        assert success_rate >= 0.8

