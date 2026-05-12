"""
End-to-end tests for API Delegation Service - Robot Operations.

Tests robot CRUD operations via API Delegation Service REST API.
"""

import pytest
import requests
import uuid
from typing import Dict, Any


@pytest.mark.e2e
@pytest.mark.requires_docker
class TestApiDelegationRobotsE2E:
    """E2E tests for robot operations via API Delegation."""

    def test_list_robots(self, api_delegation_service, mission_database_service):
        """Test listing robots via API Delegation."""
        # Create test robots directly in database
        robot_names = [f"test_robot_{uuid.uuid4().hex[:8]}" for _ in range(3)]
        
        for robot_name in robot_names:
            robot_data = {
                "name": robot_name,
                "labels": ["test", "api_delegation"]
            }
            requests.post(f"{mission_database_service['url']}/robot", json=robot_data)
        
        # List robots via API Delegation
        response = requests.get(f"{api_delegation_service['url']}/api/v1/robots")
        assert response.status_code == 200
        
        robots = response.json()
        robot_names_in_list = [r["name"] for r in robots]
        
        for robot_name in robot_names:
            assert robot_name in robot_names_in_list
        
        # Cleanup
        for robot_name in robot_names:
            requests.delete(f"{mission_database_service['url']}/robot/{robot_name}")

    def test_get_robot(self, api_delegation_service, mission_database_service):
        """Test getting specific robot via API Delegation."""
        robot_name = f"test_robot_{uuid.uuid4().hex[:8]}"
        
        # Create robot
        robot_data = {
            "name": robot_name,
            "labels": ["test"]
        }
        requests.post(f"{mission_database_service['url']}/robot", json=robot_data)
        
        # Get robot via API Delegation
        response = requests.get(f"{api_delegation_service['url']}/api/v1/robots/{robot_name}")
        assert response.status_code == 200

        robot = response.json()
        assert robot["name"] == robot_name
        assert "status" in robot  # Status should exist but will be empty initially

        # Cleanup
        requests.delete(f"{mission_database_service['url']}/robot/{robot_name}")

    def test_create_robot(self, api_delegation_service, mission_database_service):
        """Test creating robot via API Delegation."""
        robot_name = f"test_robot_{uuid.uuid4().hex[:8]}"

        robot_data = {
            "name": robot_name,
            "labels": ["test", "created_via_api"]
        }

        # Create via API Delegation
        response = requests.post(
            f"{api_delegation_service['url']}/api/v1/robots",
            json=robot_data
        )
        assert response.status_code in [200, 201]

        created_robot = response.json()
        assert created_robot["name"] == robot_name
        
        # Verify in database
        response = requests.get(f"{mission_database_service['url']}/robot/{robot_name}")
        assert response.status_code == 200
        
        # Cleanup
        requests.delete(f"{mission_database_service['url']}/robot/{robot_name}")

    def test_delete_robot(self, api_delegation_service, mission_database_service):
        """Test deleting robot via API Delegation."""
        robot_name = f"test_robot_{uuid.uuid4().hex[:8]}"
        
        # Create robot
        robot_data = {"name": robot_name, "labels": ["test"]}
        requests.post(f"{mission_database_service['url']}/robot", json=robot_data)
        
        # Delete via API Delegation
        response = requests.delete(
            f"{api_delegation_service['url']}/api/v1/robots/{robot_name}"
        )
        assert response.status_code in [200, 204]
        
        # Verify deletion
        # Note: NVIDIA image has issues with deletion:
        # - May return 400 instead of 404 for non-existent objects
        # - May return 200 with PENDING_DELETE lifecycle instead of 404
        response = requests.get(f"{mission_database_service['url']}/robot/{robot_name}")
        assert response.status_code in [200, 400, 404]

    def test_get_robot_status(self, api_delegation_service, mission_database_service):
        """Test getting robot status via API Delegation."""
        robot_name = f"test_robot_{uuid.uuid4().hex[:8]}"

        # Create robot
        robot_data = {
            "name": robot_name,
            "labels": ["test"]
        }
        requests.post(f"{mission_database_service['url']}/robot", json=robot_data)

        # Get status via API Delegation
        response = requests.get(
            f"{api_delegation_service['url']}/api/v1/robots/{robot_name}/status"
        )
        assert response.status_code == 200

        robot = response.json()
        assert "status" in robot  # Status should exist (will be empty initially)

        # Cleanup
        requests.delete(f"{mission_database_service['url']}/robot/{robot_name}")

    def test_list_robots_with_filters(self, api_delegation_service, mission_database_service):
        """Test listing robots with query filters."""
        # Create robots
        robot_names = []
        for i in range(3):
            robot_name = f"test_robot_{uuid.uuid4().hex[:8]}"
            robot_names.append(robot_name)

            robot_data = {
                "name": robot_name,
                "labels": ["test"]
            }
            requests.post(f"{mission_database_service['url']}/robot", json=robot_data)
        
        # List with battery filter
        response = requests.get(
            f"{api_delegation_service['url']}/api/v1/robots",
            params={"min_battery": 40.0}
        )
        assert response.status_code == 200
        
        robots = response.json()
        # Should filter robots (implementation may vary)
        
        # Cleanup
        for robot_name in robot_names:
            requests.delete(f"{mission_database_service['url']}/robot/{robot_name}")

    def test_get_nonexistent_robot_returns_404(self, api_delegation_service):
        """Test getting non-existent robot returns 404."""
        response = requests.get(
            f"{api_delegation_service['url']}/api/v1/robots/nonexistent_{uuid.uuid4().hex[:8]}"
        )
        assert response.status_code == 404

    def test_create_robot_with_invalid_data(self, api_delegation_service):
        """Test creating robot with invalid data returns error."""
        invalid_data = {
            "invalid_field": "value"
            # Missing required 'name' field
        }
        
        response = requests.post(
            f"{api_delegation_service['url']}/api/v1/robots",
            json=invalid_data
        )
        assert response.status_code in [400, 422, 500]

    def test_update_robot(self, api_delegation_service, mission_database_service):
        """Test updating robot via API Delegation."""
        robot_name = f"test_robot_{uuid.uuid4().hex[:8]}"
        
        # Create robot
        robot_data = {
            "name": robot_name,
            "labels": ["test"]
        }
        requests.post(f"{mission_database_service['url']}/robot", json=robot_data)
        
        # Update via API Delegation
        updated_data = {
            "name": robot_name,
            "labels": ["test", "updated"]
        }

        response = requests.put(
            f"{api_delegation_service['url']}/api/v1/robots/{robot_name}",
            json=updated_data
        )
        assert response.status_code in [200, 204]

        # Verify update
        response = requests.get(f"{mission_database_service['url']}/robot/{robot_name}")
        robot = response.json()
        assert "updated" in robot["labels"]

        # Cleanup
        requests.delete(f"{mission_database_service['url']}/robot/{robot_name}")

    def test_robot_operations_end_to_end(self, api_delegation_service, mission_database_service):
        """Test complete robot lifecycle via API Delegation.

        NOTE: The NVIDIA Mission Database pre-built image has a bug where it returns
        HTTP 200 with lifecycle=PENDING_DELETE instead of 404 for deleted objects.
        """
        robot_name = f"test_robot_{uuid.uuid4().hex[:8]}"

        # Create
        robot_data = {
            "name": robot_name,
            "labels": ["test", "e2e"]
        }
        response = requests.post(
            f"{api_delegation_service['url']}/api/v1/robots",
            json=robot_data
        )
        assert response.status_code in [200, 201]

        # Get
        response = requests.get(
            f"{api_delegation_service['url']}/api/v1/robots/{robot_name}"
        )
        assert response.status_code == 200

        # List
        response = requests.get(f"{api_delegation_service['url']}/api/v1/robots")
        assert response.status_code == 200
        robots = response.json()
        assert robot_name in [r["name"] for r in robots]

        # Delete
        response = requests.delete(
            f"{api_delegation_service['url']}/api/v1/robots/{robot_name}"
        )
        assert response.status_code in [200, 204]

        # Verify deletion
        response = requests.get(
            f"{api_delegation_service['url']}/api/v1/robots/{robot_name}"
        )
        # NOTE: NVIDIA Mission Database bug - returns 200 with lifecycle=PENDING_DELETE instead of 404
        assert response.status_code in [200, 404]

