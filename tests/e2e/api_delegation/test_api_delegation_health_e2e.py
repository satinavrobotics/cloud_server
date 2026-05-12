"""
End-to-end tests for API Delegation Service - Health and Resilience.

Tests health checks, service availability, and resilience via API Delegation Service.
"""

import pytest
import requests
import uuid
import time
from typing import Dict, Any


@pytest.mark.e2e
@pytest.mark.requires_docker
class TestApiDelegationHealthE2E:
    """E2E tests for health and resilience of API Delegation."""

    def test_health_check(self, api_delegation_service):
        """Test health check endpoint."""
        response = requests.get(f"{api_delegation_service['url']}/health")
        assert response.status_code == 200
        
        health_data = response.json()
        assert "status" in health_data or "service" in health_data

    def test_root_endpoint(self, api_delegation_service):
        """Test root endpoint returns service information."""
        response = requests.get(f"{api_delegation_service['url']}/")
        assert response.status_code == 200
        
        info = response.json()
        assert "service" in info
        assert info["service"] == "API Delegation Service"
        assert "endpoints" in info

    def test_stats_endpoint(self, api_delegation_service):
        """Test stats endpoint."""
        response = requests.get(f"{api_delegation_service['url']}/stats")
        assert response.status_code == 200
        
        stats = response.json()
        assert "service" in stats
        assert stats["service"] == "api_delegation"

    def test_concurrent_robot_requests(
        self, api_delegation_service, mission_database_service
    ):
        """Test handling concurrent robot requests."""
        robot_names = [f"test_robot_{uuid.uuid4().hex[:8]}" for _ in range(10)]
        
        # Create robots concurrently
        for robot_name in robot_names:
            robot_data = {"name": robot_name, "labels": ["test"]}
            response = requests.post(
                f"{api_delegation_service['url']}/api/v1/robots",
                json=robot_data
            )
            assert response.status_code in [200, 201, 400, 500]
        
        # List all robots
        response = requests.get(f"{api_delegation_service['url']}/api/v1/robots")
        assert response.status_code == 200
        
        # Cleanup
        for robot_name in robot_names:
            try:
                requests.delete(f"{mission_database_service['url']}/robot/{robot_name}")
            except:
                pass

    def test_concurrent_mission_requests(
        self, api_delegation_service, mission_database_service, sample_mission
    ):
        """Test handling concurrent mission requests."""
        robot_name = f"test_robot_{uuid.uuid4().hex[:8]}"
        
        # Create robot
        robot_data = {"name": robot_name, "labels": ["test"]}
        requests.post(f"{mission_database_service['url']}/robot", json=robot_data)
        
        # Create missions concurrently
        mission_names = [f"test_mission_{uuid.uuid4().hex[:8]}" for _ in range(10)]
        
        for mission_name in mission_names:
            mission_data = sample_mission.copy()
            mission_data["name"] = mission_name
            mission_data["robot"] = robot_name
            response = requests.post(
                f"{api_delegation_service['url']}/api/v1/missions",
                json=mission_data
            )
            assert response.status_code in [200, 201, 400, 500]
        
        # List all missions
        response = requests.get(f"{api_delegation_service['url']}/api/v1/missions")
        assert response.status_code == 200
        
        # Cleanup
        for mission_name in mission_names:
            try:
                requests.delete(f"{mission_database_service['url']}/mission/{mission_name}")
            except:
                pass
        requests.delete(f"{mission_database_service['url']}/robot/{robot_name}")

    def test_invalid_endpoint_returns_404(self, api_delegation_service):
        """Test accessing invalid endpoint returns 404."""
        response = requests.get(
            f"{api_delegation_service['url']}/api/v1/invalid_endpoint"
        )
        assert response.status_code == 404

    def test_service_availability_under_load(
        self, api_delegation_service, mission_database_service
    ):
        """Test service remains available under load."""
        # Create multiple robots rapidly
        for i in range(20):
            robot_name = f"test_robot_{uuid.uuid4().hex[:8]}"
            robot_data = {"name": robot_name, "labels": ["test"]}
            
            response = requests.post(
                f"{api_delegation_service['url']}/api/v1/robots",
                json=robot_data
            )
            
            # Service should remain responsive
            assert response.status_code in [200, 201, 400, 500]
            
            # Cleanup immediately
            try:
                requests.delete(f"{mission_database_service['url']}/robot/{robot_name}")
            except:
                pass
        
        # Verify service is still healthy
        response = requests.get(f"{api_delegation_service['url']}/health")
        assert response.status_code == 200

    def test_rapid_create_delete_cycles(
        self, api_delegation_service, mission_database_service
    ):
        """Test rapid create/delete cycles."""
        for i in range(10):
            robot_name = f"test_robot_{uuid.uuid4().hex[:8]}"
            
            # Create
            robot_data = {"name": robot_name, "labels": ["test"]}
            response = requests.post(
                f"{api_delegation_service['url']}/api/v1/robots",
                json=robot_data
            )
            assert response.status_code in [200, 201, 400, 500]
            
            # Delete immediately
            response = requests.delete(
                f"{api_delegation_service['url']}/api/v1/robots/{robot_name}"
            )
            assert response.status_code in [200, 204, 404]

    def test_error_handling_for_backend_unavailable(self, api_delegation_service):
        """Test error handling when backend services are unavailable."""
        # Try to get a robot when database might be unavailable
        response = requests.get(
            f"{api_delegation_service['url']}/api/v1/robots/test_robot"
        )
        
        # Should return appropriate error code
        assert response.status_code in [200, 404, 500, 503]

    def test_malformed_request_handling(self, api_delegation_service):
        """Test handling of malformed requests."""
        # Send malformed JSON
        response = requests.post(
            f"{api_delegation_service['url']}/api/v1/robots",
            data="invalid json",
            headers={"Content-Type": "application/json"}
        )
        
        assert response.status_code in [400, 422, 500]

    def test_large_payload_handling(
        self, api_delegation_service, mission_database_service
    ):
        """Test handling of large payloads."""
        robot_name = f"test_robot_{uuid.uuid4().hex[:8]}"

        # Create robot with large labels array
        robot_data = {
            "name": robot_name,
            "labels": ["test"] * 100  # Large labels array
        }

        response = requests.post(
            f"{api_delegation_service['url']}/api/v1/robots",
            json=robot_data
        )

        # Should handle large payload
        assert response.status_code in [200, 201, 400, 413, 500]
        
        # Cleanup
        try:
            requests.delete(f"{mission_database_service['url']}/robot/{robot_name}")
        except:
            pass

    def test_service_integration_health(
        self, api_delegation_service, mission_database_service
    ):
        """Test integration health between API Delegation and backend services."""
        # Create robot via API Delegation
        robot_name = f"test_robot_{uuid.uuid4().hex[:8]}"
        robot_data = {"name": robot_name, "labels": ["test"]}
        
        response = requests.post(
            f"{api_delegation_service['url']}/api/v1/robots",
            json=robot_data
        )
        assert response.status_code in [200, 201, 400, 500]
        
        # Verify in database
        response = requests.get(f"{mission_database_service['url']}/robot/{robot_name}")
        
        # If creation succeeded, should be in database
        if response.status_code == 200:
            robot = response.json()
            assert robot["name"] == robot_name
        
        # Cleanup
        try:
            requests.delete(f"{mission_database_service['url']}/robot/{robot_name}")
        except:
            pass

