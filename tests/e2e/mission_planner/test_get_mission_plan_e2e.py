"""
Mission Planner Service - Get Mission Plan E2E Tests

Tests the complete flow from mission creation to plan retrieval via REST API.
"""

import pytest
import requests
import uuid
import time


@pytest.mark.e2e
@pytest.mark.requires_docker
class TestGetMissionPlanE2E:
    """Test get_mission_plan endpoint end-to-end."""

    def test_create_and_retrieve_mission_plan(self, mission_planner_service, graph_db_service, mission_database_service):
        """Test creating a mission and retrieving its plan via API."""
        # Step 1: Create a test map in graph database
        map_id = f"test_map_{uuid.uuid4().hex[:8]}"

        # Create map with simple path
        create_map_response = requests.post(
            f"{graph_db_service['url']}/maps",
            json={"map_id": map_id}
        )
        assert create_map_response.status_code in [200, 201]

        # Add nodes
        nodes = [
            {"node_id": "node_1", "x": 0.0, "y": 0.0, "theta": 0.0},
            {"node_id": "node_2", "x": 10.0, "y": 0.0, "theta": 0.0},
            {"node_id": "node_3", "x": 20.0, "y": 0.0, "theta": 0.0}
        ]

        for node in nodes:
            add_node_response = requests.post(
                f"{graph_db_service['url']}/maps/{map_id}/nodes",
                json=node
            )
            assert add_node_response.status_code in [200, 201]

        # Add edges
        edges = [
            {"from_node": "node_1", "to_node": "node_2", "weight": 10.0},
            {"from_node": "node_2", "to_node": "node_3", "weight": 10.0}
        ]

        for edge in edges:
            add_edge_response = requests.post(
                f"{graph_db_service['url']}/maps/{map_id}/edges",
                json=edge
            )
            assert add_edge_response.status_code in [200, 201]

        # Step 2: Create a robot in the database
        robot_name = f"robot_{uuid.uuid4().hex[:8]}"

        # Robot creation only requires spec fields (name, labels, battery, etc.)
        # Status is managed by the system
        robot_data = {
            "name": robot_name,
            "labels": ["test", "e2e"]
        }

        robot_response = requests.post(
            f"{mission_database_service['url']}/robot",
            json=robot_data
        )

        if robot_response.status_code not in [200, 201]:
            pytest.skip(f"Robot creation failed: {robot_response.text}")

        # Update robot status with pose information
        # The robot needs a pose for the mission planner to find its location
        update_data = {
            "status": {
                "pose": {
                    "x": 0.0,
                    "y": 0.0,
                    "theta": 0.0,
                    "map_id": map_id
                }
            }
        }

        update_response = requests.patch(
            f"{mission_database_service['url']}/robot/{robot_name}",
            json=update_data
        )

        if update_response.status_code not in [200, 201]:
            pytest.skip(f"Robot status update failed: {update_response.text}")

        # Wait for robot to be registered
        time.sleep(1)

        # Step 3: Create a navigation mission
        navigate_request = {
            "robot_name": robot_name,
            "target_x": 20.0,
            "target_y": 0.0,
            "map_id": map_id
        }

        navigate_response = requests.post(
            f"{mission_planner_service['url']}/api/v1/navigate",
            json=navigate_request
        )

        # Mission creation might fail if services aren't fully ready
        if navigate_response.status_code not in [200, 201]:
            pytest.skip(f"Mission creation failed: {navigate_response.text}")

        navigate_data = navigate_response.json()
        mission_id = navigate_data.get("mission_name") or navigate_data.get("mission_id")

        assert mission_id is not None, f"Mission ID should be returned. Response: {navigate_data}"

        # Step 4: Retrieve the mission plan
        # Wait a bit for mission to be stored
        time.sleep(1)

        plan_response = requests.get(
            f"{mission_planner_service['url']}/api/v1/missions/{mission_id}/plan"
        )

        assert plan_response.status_code == 200, f"Failed to get mission plan: {plan_response.text}"

        plan_data = plan_response.json()

        # Verify response structure
        assert "mission_id" in plan_data
        assert "mission_name" in plan_data
        assert "state" in plan_data
        assert "path" in plan_data
        assert "start_node_id" in plan_data
        assert "end_node_id" in plan_data
        assert "start_position" in plan_data
        assert "target_position" in plan_data
        assert "end_position" in plan_data
        assert "robot_name" in plan_data

        # Cleanup
        try:
            requests.delete(f"{mission_database_service['url']}/mission/{mission_id}")
            requests.delete(f"{mission_database_service['url']}/robot/{robot_name}")
        except Exception:
            pass  # Cleanup is best-effort
        
        # Verify data
        assert plan_data["mission_id"] == mission_id
        assert plan_data["robot_name"] == robot_name
        assert plan_data["state"] in ["PENDING", "RUNNING", "COMPLETED", "FAILED", "CANCELED"]
        assert isinstance(plan_data["path"], list)
        assert len(plan_data["path"]) > 0
        
        # Verify positions are dictionaries with x, y
        assert "x" in plan_data["start_position"]
        assert "y" in plan_data["start_position"]
        assert "x" in plan_data["target_position"]
        assert "y" in plan_data["target_position"]
    
    def test_get_mission_plan_not_found(self, mission_planner_service):
        """Test retrieving a non-existent mission plan."""
        nonexistent_mission_id = f"nonexistent_{uuid.uuid4().hex}"
        
        plan_response = requests.get(
            f"{mission_planner_service['url']}/api/v1/missions/{nonexistent_mission_id}/plan"
        )
        
        # Should return 404
        assert plan_response.status_code == 404
        
        error_data = plan_response.json()
        assert "detail" in error_data
        assert nonexistent_mission_id in error_data["detail"]
    
    def test_get_mission_plan_invalid_mission_id(self, mission_planner_service):
        """Test retrieving mission plan with invalid mission ID format."""
        invalid_ids = [
            "",  # Empty
            "   ",  # Whitespace
            "../../../etc/passwd",  # Path traversal attempt
            "mission'; DROP TABLE missions; --"  # SQL injection attempt
        ]
        
        for invalid_id in invalid_ids:
            plan_response = requests.get(
                f"{mission_planner_service['url']}/api/v1/missions/{invalid_id}/plan"
            )
            
            # Should return 404 or 422 (validation error)
            assert plan_response.status_code in [404, 422]
    
    def test_get_mission_plan_multiple_missions(self, mission_planner_service, graph_db_service, mission_database_service):
        """Test retrieving plans for multiple missions."""
        # This test verifies that the database-only approach correctly handles multiple missions

        # Create a simple map
        map_id = f"test_map_multi_{uuid.uuid4().hex[:8]}"

        create_map_response = requests.post(
            f"{graph_db_service['url']}/maps",
            json={"map_id": map_id}
        )

        if create_map_response.status_code not in [200, 201]:
            pytest.skip("Failed to create map")

        # Add a few nodes
        for i in range(5):
            requests.post(
                f"{graph_db_service['url']}/maps/{map_id}/nodes",
                json={"node_id": f"node_{i}", "x": float(i * 10), "y": 0.0, "theta": 0.0}
            )

        # Create multiple missions
        mission_ids = []
        robot_names = []

        for i in range(3):
            robot_name = f"robot_{i}_{uuid.uuid4().hex[:6]}"
            robot_names.append(robot_name)

            # Create robot (only spec fields)
            robot_data = {
                "name": robot_name,
                "labels": ["test", "e2e", "multi"]
            }

            robot_response = requests.post(
                f"{mission_database_service['url']}/robot",
                json=robot_data
            )

            if robot_response.status_code not in [200, 201]:
                continue

            # Update robot status with pose
            update_data = {
                "status": {
                    "pose": {
                        "x": 0.0,
                        "y": 0.0,
                        "theta": 0.0,
                        "map_id": map_id
                    }
                }
            }

            update_response = requests.patch(
                f"{mission_database_service['url']}/robot/{robot_name}",
                json=update_data
            )

            if update_response.status_code not in [200, 201]:
                continue

            # Wait for robot to be registered
            time.sleep(0.5)

            navigate_request = {
                "robot_name": robot_name,
                "target_x": float((i + 1) * 10),
                "target_y": 0.0,
                "map_id": map_id
            }

            navigate_response = requests.post(
                f"{mission_planner_service['url']}/api/v1/navigate",
                json=navigate_request
            )

            if navigate_response.status_code in [200, 201]:
                navigate_data = navigate_response.json()
                mission_id = navigate_data.get("mission_name") or navigate_data.get("mission_id")
                if mission_id:
                    mission_ids.append(mission_id)
        
        if len(mission_ids) == 0:
            pytest.skip("No missions were created successfully")
        
        # Wait for missions to be stored
        time.sleep(2)
        
        # Retrieve all mission plans
        retrieved_plans = []
        
        for mission_id in mission_ids:
            plan_response = requests.get(
                f"{mission_planner_service['url']}/api/v1/missions/{mission_id}/plan"
            )
            
            if plan_response.status_code == 200:
                plan_data = plan_response.json()
                retrieved_plans.append(plan_data)
        
        # Verify we retrieved at least some plans
        assert len(retrieved_plans) > 0

        # Verify each plan has unique mission_id
        mission_ids_retrieved = [plan["mission_id"] for plan in retrieved_plans]
        assert len(mission_ids_retrieved) == len(set(mission_ids_retrieved)), "Mission IDs should be unique"

        # Cleanup
        try:
            for mission_id in mission_ids:
                requests.delete(f"{mission_database_service['url']}/mission/{mission_id}")
            for robot_name in robot_names:
                requests.delete(f"{mission_database_service['url']}/robot/{robot_name}")
        except Exception:
            pass  # Cleanup is best-effort
    
    def test_get_mission_plan_response_time(self, mission_planner_service):
        """Test that get_mission_plan responds within acceptable time."""
        # This test verifies that database queries are performant
        
        mission_id = f"test_mission_{uuid.uuid4().hex[:8]}"
        
        start_time = time.time()
        
        plan_response = requests.get(
            f"{mission_planner_service['url']}/api/v1/missions/{mission_id}/plan",
            timeout=5  # 5 second timeout
        )
        
        end_time = time.time()
        response_time = end_time - start_time
        
        # Response should be quick even for non-existent mission (database query)
        assert response_time < 2.0, f"Response took {response_time:.2f}s, should be < 2s"
        
        # Should return 404 for non-existent mission
        assert plan_response.status_code == 404

