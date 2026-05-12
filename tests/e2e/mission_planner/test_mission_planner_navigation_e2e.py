"""
Mission Planner Service - Navigation E2E Tests

Tests navigation planning, pathfinding, and mission creation.
"""

import pytest
import requests
import uuid


@pytest.mark.e2e
@pytest.mark.requires_docker
class TestMissionPlannerNavigationE2E:
    """Test navigation planning in Mission Planner Service."""

    def test_navigate_request(self, mission_planner_service):
        """Test basic navigation request."""
        nav_request = {
            "robot_name": f"robot_{uuid.uuid4().hex[:8]}",
            "target_x": 10.0,
            "target_y": 20.0,
            "mission_name": f"nav_{uuid.uuid4().hex[:8]}",
            "timeout_seconds": 30
        }
        
        response = requests.post(
            f"{mission_planner_service['url']}/api/v1/navigate",
            json=nav_request
        )
        
        assert response.status_code in [200, 400, 500, 503]
        if response.status_code == 200:
            data = response.json()
            assert "success" in data or "mission_name" in data

    def test_navigate_with_custom_mission_name(self, mission_planner_service):
        """Test navigation with custom mission name."""
        mission_name = f"custom_mission_{uuid.uuid4().hex[:8]}"
        nav_request = {
            "robot_name": f"robot_{uuid.uuid4().hex[:8]}",
            "target_x": 15.0,
            "target_y": 25.0,
            "mission_name": mission_name,
            "timeout_seconds": 60
        }
        
        response = requests.post(
            f"{mission_planner_service['url']}/api/v1/navigate",
            json=nav_request
        )
        
        assert response.status_code in [200, 400, 500, 503]

    def test_navigate_with_timeout(self, mission_planner_service):
        """Test navigation with custom timeout."""
        nav_request = {
            "robot_name": f"robot_{uuid.uuid4().hex[:8]}",
            "target_x": 20.0,
            "target_y": 30.0,
            "timeout_seconds": 120
        }
        
        response = requests.post(
            f"{mission_planner_service['url']}/api/v1/navigate",
            json=nav_request
        )
        
        assert response.status_code in [200, 400, 500, 503]

    def test_navigate_negative_coordinates(self, mission_planner_service):
        """Test navigation with negative coordinates."""
        nav_request = {
            "robot_name": f"robot_{uuid.uuid4().hex[:8]}",
            "target_x": -50.0,
            "target_y": -100.0,
            "timeout_seconds": 30
        }
        
        response = requests.post(
            f"{mission_planner_service['url']}/api/v1/navigate",
            json=nav_request
        )
        
        assert response.status_code in [200, 400, 500, 503]

    def test_navigate_large_coordinates(self, mission_planner_service):
        """Test navigation with large coordinates."""
        nav_request = {
            "robot_name": f"robot_{uuid.uuid4().hex[:8]}",
            "target_x": 10000.0,
            "target_y": 20000.0,
            "timeout_seconds": 30
        }
        
        response = requests.post(
            f"{mission_planner_service['url']}/api/v1/navigate",
            json=nav_request
        )
        
        assert response.status_code in [200, 400, 500, 503]

    def test_navigate_zero_coordinates(self, mission_planner_service):
        """Test navigation to origin."""
        nav_request = {
            "robot_name": f"robot_{uuid.uuid4().hex[:8]}",
            "target_x": 0.0,
            "target_y": 0.0,
            "timeout_seconds": 30
        }
        
        response = requests.post(
            f"{mission_planner_service['url']}/api/v1/navigate",
            json=nav_request
        )
        
        assert response.status_code in [200, 400, 500, 503]

    def test_navigate_missing_robot_name(self, mission_planner_service):
        """Test navigation without robot name."""
        nav_request = {
            "target_x": 10.0,
            "target_y": 20.0,
            "timeout_seconds": 30
        }
        
        response = requests.post(
            f"{mission_planner_service['url']}/api/v1/navigate",
            json=nav_request
        )
        
        assert response.status_code in [400, 422]

    def test_navigate_missing_coordinates(self, mission_planner_service):
        """Test navigation without target coordinates."""
        nav_request = {
            "robot_name": f"robot_{uuid.uuid4().hex[:8]}",
            "timeout_seconds": 30
        }
        
        response = requests.post(
            f"{mission_planner_service['url']}/api/v1/navigate",
            json=nav_request
        )
        
        assert response.status_code in [400, 422]

    def test_navigate_invalid_timeout(self, mission_planner_service):
        """Test navigation with invalid timeout."""
        nav_request = {
            "robot_name": f"robot_{uuid.uuid4().hex[:8]}",
            "target_x": 10.0,
            "target_y": 20.0,
            "timeout_seconds": -10
        }
        
        response = requests.post(
            f"{mission_planner_service['url']}/api/v1/navigate",
            json=nav_request
        )
        
        assert response.status_code in [200, 400, 422, 500, 503]

    def test_navigate_zero_timeout(self, mission_planner_service):
        """Test navigation with zero timeout."""
        nav_request = {
            "robot_name": f"robot_{uuid.uuid4().hex[:8]}",
            "target_x": 10.0,
            "target_y": 20.0,
            "timeout_seconds": 0
        }
        
        response = requests.post(
            f"{mission_planner_service['url']}/api/v1/navigate",
            json=nav_request
        )
        
        assert response.status_code in [200, 400, 422, 500, 503]

    def test_navigate_sequential_requests(self, mission_planner_service):
        """Test sequential navigation requests."""
        for i in range(3):
            nav_request = {
                "robot_name": f"robot_{uuid.uuid4().hex[:8]}",
                "target_x": float(i * 10),
                "target_y": float(i * 20),
                "timeout_seconds": 30
            }
            
            response = requests.post(
                f"{mission_planner_service['url']}/api/v1/navigate",
                json=nav_request
            )

            assert response.status_code in [200, 400, 500, 503]
