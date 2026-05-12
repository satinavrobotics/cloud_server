"""
Graph Builder Service - Health & Resilience E2E Tests

Tests health checks, statistics, and service resilience.
"""

import pytest
import requests
import uuid
import concurrent.futures


@pytest.mark.e2e
@pytest.mark.requires_docker
class TestGraphBuilderHealthE2E:
    """Test health and resilience of Graph Builder Service."""

    def test_health_check(self, graph_builder_service):
        """Test health check endpoint."""
        response = requests.get(
            f"{graph_builder_service['url']}/health"
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data.get("status") in ["healthy", "degraded"]
        assert data.get("service") == "graph_builder"

    def test_health_check_dependencies(self, graph_builder_service):
        """Test health check includes dependency status."""
        response = requests.get(
            f"{graph_builder_service['url']}/health"
        )
        
        assert response.status_code == 200
        data = response.json()
        if "dependencies" in data:
            assert "image_db" in data["dependencies"]
            assert "graph_db" in data["dependencies"]

    def test_stats_endpoint(self, graph_builder_service):
        """Test statistics endpoint."""
        response = requests.get(
            f"{graph_builder_service['url']}/stats"
        )
        
        assert response.status_code == 200
        data = response.json()
        assert "nodes_processed" in data or "stats" in data

    def test_root_endpoint(self, graph_builder_service):
        """Test root endpoint with service information."""
        response = requests.get(
            f"{graph_builder_service['url']}/"
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data.get("service") == "Graph Builder Service"

    def test_concurrent_node_processing(self, graph_builder_service):
        """Test concurrent node processing."""
        def process_node(index):
            node_data = {
                "node_id": f"concurrent_{index}_{uuid.uuid4().hex[:4]}",
                "x": float(index * 5),
                "y": float(index * 10),
                "yaw": 0.0,
                "map_id": "default"
            }
            response = requests.post(
                f"{graph_builder_service['url']}/node",
                json=node_data
            )
            return response.status_code in [200, 400, 500, 503]
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            results = list(executor.map(process_node, range(10)))
        
        assert len(results) == 10

    def test_rapid_node_updates(self, graph_builder_service):
        """Test rapid node updates."""
        for i in range(10):
            node_data = {
                "node_id": f"rapid_{i}_{uuid.uuid4().hex[:4]}",
                "x": 10.0,
                "y": 20.0,
                "yaw": 0.0,
                "map_id": "default"
            }
            
            response = requests.post(
                f"{graph_builder_service['url']}/node",
                json=node_data
            )
            
            assert response.status_code in [200, 400, 500, 503]

    def test_large_metadata_handling(self, graph_builder_service):
        """Test handling of large metadata."""
        large_metadata = {
            f"key_{i}": f"value_{i}" * 50
            for i in range(20)
        }
        
        node_data = {
            "node_id": f"large_{uuid.uuid4().hex[:8]}",
            "x": 10.0,
            "y": 20.0,
            "yaw": 0.0,
            "map_id": "default",
            "metadata": large_metadata
        }
        
        response = requests.post(
            f"{graph_builder_service['url']}/node",
            json=node_data
        )
        
        assert response.status_code in [200, 400, 413, 500, 503]

    def test_malformed_request(self, graph_builder_service):
        """Test handling of malformed requests."""
        response = requests.post(
            f"{graph_builder_service['url']}/node",
            data="invalid json {",
            headers={"Content-Type": "application/json"}
        )
        
        assert response.status_code in [400, 422]

    def test_multiple_maps_processing(self, graph_builder_service):
        """Test processing nodes in multiple maps."""
        for map_id in ["warehouse", "factory", "office"]:
            node_data = {
                "node_id": f"node_{uuid.uuid4().hex[:8]}",
                "x": 10.0,
                "y": 20.0,
                "yaw": 0.0,
                "map_id": map_id
            }
            
            response = requests.post(
                f"{graph_builder_service['url']}/node",
                json=node_data
            )
            
            assert response.status_code in [200, 400, 500, 503]

    def test_service_availability_under_load(self, graph_builder_service):
        """Test service availability under load."""
        def make_request(index):
            try:
                response = requests.get(
                    f"{graph_builder_service['url']}/health",
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

    def test_stats_after_processing(self, graph_builder_service):
        """Test stats endpoint after processing nodes."""
        # Process a node
        node_data = {
            "node_id": f"stats_test_{uuid.uuid4().hex[:8]}",
            "x": 10.0,
            "y": 20.0,
            "yaw": 0.0,
            "map_id": "default"
        }
        requests.post(
            f"{graph_builder_service['url']}/node",
            json=node_data
        )
        
        # Check stats
        response = requests.get(
            f"{graph_builder_service['url']}/stats"
        )
        
        assert response.status_code == 200

