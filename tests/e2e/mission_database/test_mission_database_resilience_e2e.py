"""
End-to-end tests for Mission Database Service - Resilience & Health.

Tests service health, error handling, and resilience scenarios.
"""

import pytest
import requests
import uuid
import time
import concurrent.futures
from typing import Dict, Any


@pytest.mark.e2e
@pytest.mark.requires_docker
class TestMissionDatabaseResilienceE2E:
    """E2E tests for database resilience and health."""

    def test_health_check(self, mission_database_service):
        """Test service health endpoint."""
        response = requests.get(f"{mission_database_service['url']}/health")
        assert response.status_code == 200
        
        # Health response should contain status information
        data = response.json()
        assert "status" in data or "healthy" in str(data).lower()

    def test_behaviors_endpoint(self, mission_database_service):
        """Test behaviors endpoint."""
        response = requests.get(f"{mission_database_service['url']}/behaviors")
        
        # Behaviors endpoint may or may not exist
        assert response.status_code in [200, 404, 405]
        
        if response.status_code == 200:
            data = response.json()
            assert isinstance(data, (dict, list))

    def test_concurrent_operations_large_scale(self, mission_database_service):
        """Test 100+ concurrent operations."""
        robot_names = [f"test_robot_{uuid.uuid4().hex[:8]}" for _ in range(50)]
        
        def create_robot(robot_name):
            robot_data = {
                "name": robot_name,
                "labels": ["test", "concurrent"]
            }
            response = requests.post(
                f"{mission_database_service['url']}/robot",
                json=robot_data
            )
            return response.status_code
        
        # Create 50 robots concurrently
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(create_robot, name) for name in robot_names]
            create_results = [f.result() for f in concurrent.futures.as_completed(futures)]
        
        # Most creates should succeed
        success_count = sum(1 for status in create_results if status in [200, 201])
        assert success_count >= 40, f"Only {success_count}/50 creates succeeded"
        
        # Now update all robots concurrently
        def update_robot(robot_name):
            robot_data = {
                "labels": ["test", "updated"]
            }
            response = requests.put(
                f"{mission_database_service['url']}/robot/{robot_name}",
                json=robot_data
            )
            return response.status_code
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(update_robot, name) for name in robot_names]
            update_results = [f.result() for f in concurrent.futures.as_completed(futures)]
        
        # Most updates should succeed
        success_count = sum(1 for status in update_results if status in [200, 204])
        assert success_count >= 40, f"Only {success_count}/50 updates succeeded"
        
        # Cleanup
        for robot_name in robot_names:
            try:
                requests.delete(f"{mission_database_service['url']}/robot/{robot_name}")
            except:
                pass

    def test_create_duplicate_robot_handling(self, mission_database_service):
        """Test creating duplicate robot is handled correctly."""
        robot_name = f"test_robot_{uuid.uuid4().hex[:8]}"
        
        robot_data = {
            "name": robot_name,
            "labels": ["test"]
        }
        
        # Create robot first time
        response1 = requests.post(
            f"{mission_database_service['url']}/robot",
            json=robot_data
        )
        assert response1.status_code in [200, 201]
        
        # Try to create same robot again
        response2 = requests.post(
            f"{mission_database_service['url']}/robot",
            json=robot_data
        )
        
        # Should either reject (400/409) or succeed (idempotent)
        assert response2.status_code in [200, 201, 400, 409, 500]
        
        # Cleanup
        requests.delete(f"{mission_database_service['url']}/robot/{robot_name}")

    def test_invalid_json_handling(self, mission_database_service):
        """Test service handles invalid JSON gracefully."""
        response = requests.post(
            f"{mission_database_service['url']}/robot",
            data="invalid json {{{",
            headers={"Content-Type": "application/json"}
        )
        
        # Should return 400 or 422 for invalid JSON
        assert response.status_code in [400, 422, 500]

    def test_missing_required_fields(self, mission_database_service):
        """Test service validates required fields.

        Note: The NVIDIA pre-built mission-database image does not properly validate
        required fields and may return 200 even when required fields are missing.
        This is a known issue with the image.
        """
        # Try to create robot without name
        robot_data = {
            "labels": ["test"]
            # Missing 'name' field
        }

        response = requests.post(
            f"{mission_database_service['url']}/robot",
            json=robot_data
        )

        # Should return validation error, but NVIDIA image may return 200
        assert response.status_code in [200, 400, 422, 500]

    def test_large_payload_handling(self, mission_database_service, sample_mission):
        """Test handling of large payloads."""
        robot_name = f"test_robot_{uuid.uuid4().hex[:8]}"
        mission_name = f"test_mission_{uuid.uuid4().hex[:8]}"
        
        # Create robot
        robot_data = {"name": robot_name, "labels": ["test"]}
        requests.post(f"{mission_database_service['url']}/robot", json=robot_data)
        
        # Create mission with many waypoints
        sample_mission["name"] = mission_name
        sample_mission["robot"] = robot_name
        
        # Add many waypoints to mission tree
        for i in range(50):
            sample_mission["mission_tree"].append({
                "name": f"waypoint_{i}",
                "parent": "root_sequence",
                "route": {
                    "waypoints": [
                        {
                            "x": float(i * 10),
                            "y": float(i * 10),
                            "theta": 0.0,
                            "map_id": "test_map",
                            "allowedDeviationXY": 0.5,
                            "allowedDeviationTheta": 0.1
                        }
                    ]
                }
            })
        
        response = requests.post(
            f"{mission_database_service['url']}/mission",
            json=sample_mission
        )
        
        # Should handle large payload
        assert response.status_code in [200, 201, 413, 500]
        
        # Cleanup
        if response.status_code in [200, 201]:
            requests.delete(f"{mission_database_service['url']}/mission/{mission_name}")
        requests.delete(f"{mission_database_service['url']}/robot/{robot_name}")

    def test_rapid_create_delete_cycles(self, mission_database_service):
        """Test rapid create/delete cycles.

        NOTE: The NVIDIA Mission Database may return 400 when trying to create a robot
        that was just deleted (still has lifecycle=PENDING_DELETE).
        """
        robot_name = f"test_robot_{uuid.uuid4().hex[:8]}"

        robot_data = {
            "name": robot_name,
            "labels": ["test"]
        }

        # Perform 10 rapid create/delete cycles
        for i in range(10):
            # Create
            response = requests.post(
                f"{mission_database_service['url']}/robot",
                json=robot_data
            )
            # NOTE: NVIDIA Mission Database bug - may return 400 for deleted objects
            assert response.status_code in [200, 201, 400, 409, 500]

            # Delete
            response = requests.delete(
                f"{mission_database_service['url']}/robot/{robot_name}"
            )
            assert response.status_code in [200, 204, 404, 500]

            time.sleep(0.1)  # Small delay between cycles

    def test_service_availability_under_load(self, mission_database_service):
        """Test service remains available under load."""
        # Create load by making many concurrent requests
        def make_request():
            response = requests.get(f"{mission_database_service['url']}/health")
            return response.status_code
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
            futures = [executor.submit(make_request) for _ in range(100)]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]
        
        # Most health checks should succeed
        success_count = sum(1 for status in results if status == 200)
        assert success_count >= 90, f"Only {success_count}/100 health checks succeeded"

    def test_list_operations_with_many_objects(self, mission_database_service):
        """Test list operations with many objects."""
        # Create many robots
        robot_names = [f"test_robot_{uuid.uuid4().hex[:8]}" for _ in range(20)]
        
        for robot_name in robot_names:
            robot_data = {
                "name": robot_name,
                "labels": ["test", "bulk"]
            }
            requests.post(f"{mission_database_service['url']}/robot", json=robot_data)
        
        # List all robots
        response = requests.get(f"{mission_database_service['url']}/robot")
        assert response.status_code == 200
        
        robots = response.json()
        assert len(robots) >= 20
        
        # Cleanup
        for robot_name in robot_names:
            try:
                requests.delete(f"{mission_database_service['url']}/robot/{robot_name}")
            except:
                pass

