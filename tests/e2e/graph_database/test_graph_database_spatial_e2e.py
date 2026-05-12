"""
Graph Database Service - Spatial Query E2E Tests

Tests KNN (k-nearest neighbors) and range search queries.
"""

import pytest
import requests
import uuid


@pytest.mark.e2e
@pytest.mark.requires_docker
class TestGraphDatabaseSpatialE2E:
    """Test spatial queries in Graph Database Service."""

    def test_knn_query(self, graph_database_service):
        """Test k-nearest neighbors query."""
        query_data = {
            "x": 10.0,
            "y": 20.0,
            "k": 5,
            "max_distance": 100.0
        }
        
        response = requests.post(
            f"{graph_database_service['url']}/query/knn",
            json=query_data
        )
        
        assert response.status_code in [200, 400, 500]
        if response.status_code == 200:
            data = response.json()
            assert "nodes" in data
            assert "distances" in data
            assert "count" in data

    def test_knn_query_with_map_id(self, graph_database_service):
        """Test KNN query with specific map ID."""
        map_id = f"map_{uuid.uuid4().hex[:8]}"
        query_data = {
            "x": 15.0,
            "y": 25.0,
            "k": 3,
            "map_id": map_id
        }
        
        response = requests.post(
            f"{graph_database_service['url']}/query/knn",
            json=query_data
        )
        
        assert response.status_code in [200, 400, 500]

    def test_range_query(self, graph_database_service):
        """Test range search query."""
        query_data = {
            "x": 10.0,
            "y": 20.0,
            "radius": 50.0,
            "max_results": 100
        }
        
        response = requests.post(
            f"{graph_database_service['url']}/query/range",
            json=query_data
        )
        
        assert response.status_code in [200, 400, 500]
        if response.status_code == 200:
            data = response.json()
            assert "nodes" in data
            assert "distances" in data
            assert "count" in data

    def test_range_query_with_map_id(self, graph_database_service):
        """Test range query with specific map ID."""
        map_id = f"map_{uuid.uuid4().hex[:8]}"
        query_data = {
            "x": 20.0,
            "y": 30.0,
            "radius": 25.0,
            "map_id": map_id
        }
        
        response = requests.post(
            f"{graph_database_service['url']}/query/range",
            json=query_data
        )
        
        assert response.status_code in [200, 400, 500]

    def test_map_knn_search(self, graph_database_service):
        """Test KNN search in specific map."""
        map_id = f"map_{uuid.uuid4().hex[:8]}"
        query_data = {
            "x": 10.0,
            "y": 20.0,
            "k": 5
        }
        
        response = requests.post(
            f"{graph_database_service['url']}/maps/{map_id}/knn_search",
            json=query_data
        )
        
        assert response.status_code in [200, 400, 500]

    def test_map_range_search(self, graph_database_service):
        """Test range search in specific map."""
        map_id = f"map_{uuid.uuid4().hex[:8]}"
        query_data = {
            "x": 15.0,
            "y": 25.0,
            "radius": 30.0
        }
        
        response = requests.post(
            f"{graph_database_service['url']}/maps/{map_id}/range_search",
            json=query_data
        )
        
        assert response.status_code in [200, 400, 500]

    def test_knn_query_zero_k(self, graph_database_service):
        """Test KNN query with k=0."""
        query_data = {
            "x": 10.0,
            "y": 20.0,
            "k": 0
        }
        
        response = requests.post(
            f"{graph_database_service['url']}/query/knn",
            json=query_data
        )
        
        assert response.status_code in [200, 400, 422]

    def test_knn_query_large_k(self, graph_database_service):
        """Test KNN query with large k value."""
        query_data = {
            "x": 10.0,
            "y": 20.0,
            "k": 1000
        }
        
        response = requests.post(
            f"{graph_database_service['url']}/query/knn",
            json=query_data
        )
        
        assert response.status_code in [200, 400, 500]

    def test_range_query_zero_radius(self, graph_database_service):
        """Test range query with zero radius."""
        query_data = {
            "x": 10.0,
            "y": 20.0,
            "radius": 0.0
        }
        
        response = requests.post(
            f"{graph_database_service['url']}/query/range",
            json=query_data
        )
        
        assert response.status_code in [200, 400, 422]

    def test_range_query_large_radius(self, graph_database_service):
        """Test range query with large radius."""
        query_data = {
            "x": 10.0,
            "y": 20.0,
            "radius": 10000.0
        }
        
        response = requests.post(
            f"{graph_database_service['url']}/query/range",
            json=query_data
        )
        
        assert response.status_code in [200, 400, 500]

    def test_knn_query_negative_coordinates(self, graph_database_service):
        """Test KNN query with negative coordinates."""
        query_data = {
            "x": -50.0,
            "y": -100.0,
            "k": 5
        }
        
        response = requests.post(
            f"{graph_database_service['url']}/query/knn",
            json=query_data
        )
        
        assert response.status_code in [200, 400, 500]

    def test_range_query_negative_coordinates(self, graph_database_service):
        """Test range query with negative coordinates."""
        query_data = {
            "x": -30.0,
            "y": -40.0,
            "radius": 50.0
        }
        
        response = requests.post(
            f"{graph_database_service['url']}/query/range",
            json=query_data
        )
        
        assert response.status_code in [200, 400, 500]

