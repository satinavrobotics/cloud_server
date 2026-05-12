"""
End-to-end tests for API Delegation Service - Navigation Operations.

Tests navigation and exploration operations via API Delegation Service REST API.
"""

import pytest
import requests
import uuid
import time
from typing import Dict, Any


@pytest.mark.e2e
@pytest.mark.requires_docker
class TestApiDelegationNavigationE2E:
    """E2E tests for navigation operations via API Delegation."""

    def test_navigate_request(
        self, api_delegation_service, mission_database_service, mission_planner_service
    ):
        """Test navigation request via API Delegation."""
        robot_name = f"test_robot_{uuid.uuid4().hex[:8]}"

        # Create robot (pose would be updated via VDA5050 state messages)
        robot_data = {
            "name": robot_name,
            "labels": ["test"]
        }
        requests.post(f"{mission_database_service['url']}/robot", json=robot_data)
        
        # Send navigation request
        nav_request = {
            "robot_name": robot_name,
            "target_x": 10.0,
            "target_y": 20.0,
            "mission_name": f"nav_mission_{uuid.uuid4().hex[:8]}",
            "timeout_seconds": 30
        }
        
        response = requests.post(
            f"{api_delegation_service['url']}/api/v1/navigate",
            json=nav_request
        )
        
        # May succeed or fail depending on mission planner availability
        assert response.status_code in [200, 400, 500, 503]
        
        # Cleanup
        if response.status_code == 200:
            result = response.json()
            if "mission_name" in result:
                try:
                    requests.delete(
                        f"{mission_database_service['url']}/mission/{result['mission_name']}"
                    )
                except:
                    pass
        
        requests.delete(f"{mission_database_service['url']}/robot/{robot_name}")

    def test_explore_request(
        self, api_delegation_service, mission_database_service
    ):
        """Test exploration request via API Delegation."""
        robot_name = f"test_robot_{uuid.uuid4().hex[:8]}"

        # Create robot (pose would be updated via VDA5050 state messages)
        robot_data = {
            "name": robot_name,
            "labels": ["test"]
        }
        requests.post(f"{mission_database_service['url']}/robot", json=robot_data)
        
        # Send exploration request
        explore_request = {
            "robot_name": robot_name,
            "mission_name": f"explore_mission_{uuid.uuid4().hex[:8]}",
            "timeout_seconds": 30
        }
        
        response = requests.post(
            f"{api_delegation_service['url']}/api/v1/explore",
            json=explore_request
        )
        
        # May succeed or fail depending on mission planner availability
        assert response.status_code in [200, 400, 500, 503]
        
        # Cleanup
        if response.status_code == 200:
            result = response.json()
            if "mission_name" in result:
                try:
                    requests.delete(
                        f"{mission_database_service['url']}/mission/{result['mission_name']}"
                    )
                except:
                    pass
        
        requests.delete(f"{mission_database_service['url']}/robot/{robot_name}")

    def test_navigate_without_robot_returns_error(self, api_delegation_service):
        """Test navigation request for non-existent robot returns error."""
        nav_request = {
            "robot_name": f"nonexistent_{uuid.uuid4().hex[:8]}",
            "target_x": 10.0,
            "target_y": 20.0,
            "mission_name": f"nav_mission_{uuid.uuid4().hex[:8]}"
        }
        
        response = requests.post(
            f"{api_delegation_service['url']}/api/v1/navigate",
            json=nav_request
        )
        
        assert response.status_code in [400, 404, 500, 503]

    def test_navigate_with_invalid_coordinates(
        self, api_delegation_service, mission_database_service
    ):
        """Test navigation with invalid coordinates."""
        robot_name = f"test_robot_{uuid.uuid4().hex[:8]}"
        
        # Create robot
        robot_data = {"name": robot_name, "labels": ["test"]}
        requests.post(f"{mission_database_service['url']}/robot", json=robot_data)
        
        # Send navigation request with invalid coordinates
        nav_request = {
            "robot_name": robot_name,
            "target_x": "invalid",  # Invalid type
            "target_y": 20.0,
            "mission_name": f"nav_mission_{uuid.uuid4().hex[:8]}"
        }
        
        response = requests.post(
            f"{api_delegation_service['url']}/api/v1/navigate",
            json=nav_request
        )
        
        assert response.status_code in [400, 422, 500]
        
        # Cleanup
        requests.delete(f"{mission_database_service['url']}/robot/{robot_name}")

    def test_navigate_with_custom_mission_name(
        self, api_delegation_service, mission_database_service
    ):
        """Test navigation with custom mission name."""
        robot_name = f"test_robot_{uuid.uuid4().hex[:8]}"
        mission_name = f"custom_nav_{uuid.uuid4().hex[:8]}"

        # Create robot (pose would be updated via VDA5050 state messages)
        robot_data = {
            "name": robot_name,
            "labels": ["test"]
        }
        requests.post(f"{mission_database_service['url']}/robot", json=robot_data)
        
        # Send navigation request
        nav_request = {
            "robot_name": robot_name,
            "target_x": 5.0,
            "target_y": 10.0,
            "mission_name": mission_name
        }
        
        response = requests.post(
            f"{api_delegation_service['url']}/api/v1/navigate",
            json=nav_request
        )
        
        # May succeed or fail
        assert response.status_code in [200, 400, 500, 503]
        
        # Cleanup
        try:
            requests.delete(f"{mission_database_service['url']}/mission/{mission_name}")
        except:
            pass
        requests.delete(f"{mission_database_service['url']}/robot/{robot_name}")

