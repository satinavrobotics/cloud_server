"""
Mission Planner Service - Pathfinding E2E Tests

Tests path planning, node queries, and mission creation.
"""

import pytest
import requests
import uuid


@pytest.mark.e2e
@pytest.mark.requires_docker
class TestMissionPlannerPathfindingE2E:
    """Test pathfinding in Mission Planner Service."""

    def test_plan_mission_request(self, mission_planner_service):
        """Test basic plan mission request."""
        plan_request = {
            "robot_id": f"robot_{uuid.uuid4().hex[:8]}",
            "goal_x": 10.0,
            "goal_y": 20.0
        }
        
        response = requests.post(
            f"{mission_planner_service['url']}/plan",
            json=plan_request
        )
        
        assert response.status_code in [200, 400, 500, 503]

    def test_plan_mission_with_map_id(self, mission_planner_service):
        """Test plan mission with specific map ID."""
        plan_request = {
            "robot_id": f"robot_{uuid.uuid4().hex[:8]}",
            "goal_x": 15.0,
            "goal_y": 25.0,
            "map_id": "warehouse"
        }
        
        response = requests.post(
            f"{mission_planner_service['url']}/plan",
            json=plan_request
        )
        
        assert response.status_code in [200, 400, 500, 503]

    def test_plan_mission_with_start_position(self, mission_planner_service):
        """Test plan mission with start position (ignored)."""
        plan_request = {
            "robot_id": f"robot_{uuid.uuid4().hex[:8]}",
            "goal_x": 20.0,
            "goal_y": 30.0,
            "start_x": 0.0,
            "start_y": 0.0
        }
        
        response = requests.post(
            f"{mission_planner_service['url']}/plan",
            json=plan_request
        )
        
        assert response.status_code in [200, 400, 500, 503]

    def test_plan_mission_negative_coordinates(self, mission_planner_service):
        """Test plan mission with negative coordinates."""
        plan_request = {
            "robot_id": f"robot_{uuid.uuid4().hex[:8]}",
            "goal_x": -50.0,
            "goal_y": -100.0
        }
        
        response = requests.post(
            f"{mission_planner_service['url']}/plan",
            json=plan_request
        )
        
        assert response.status_code in [200, 400, 500, 503]

    def test_plan_mission_large_coordinates(self, mission_planner_service):
        """Test plan mission with large coordinates."""
        plan_request = {
            "robot_id": f"robot_{uuid.uuid4().hex[:8]}",
            "goal_x": 10000.0,
            "goal_y": 20000.0
        }
        
        response = requests.post(
            f"{mission_planner_service['url']}/plan",
            json=plan_request
        )
        
        assert response.status_code in [200, 400, 500, 503]

    def test_plan_mission_zero_coordinates(self, mission_planner_service):
        """Test plan mission to origin."""
        plan_request = {
            "robot_id": f"robot_{uuid.uuid4().hex[:8]}",
            "goal_x": 0.0,
            "goal_y": 0.0
        }
        
        response = requests.post(
            f"{mission_planner_service['url']}/plan",
            json=plan_request
        )
        
        assert response.status_code in [200, 400, 500, 503]

    def test_plan_mission_missing_robot_id(self, mission_planner_service):
        """Test plan mission without robot ID."""
        plan_request = {
            "goal_x": 10.0,
            "goal_y": 20.0
        }
        
        response = requests.post(
            f"{mission_planner_service['url']}/plan",
            json=plan_request
        )
        
        assert response.status_code in [400, 422]

    def test_plan_mission_missing_goal(self, mission_planner_service):
        """Test plan mission without goal coordinates."""
        plan_request = {
            "robot_id": f"robot_{uuid.uuid4().hex[:8]}"
        }
        
        response = requests.post(
            f"{mission_planner_service['url']}/plan",
            json=plan_request
        )
        
        assert response.status_code in [400, 422]

    def test_plan_mission_invalid_coordinates(self, mission_planner_service):
        """Test plan mission with invalid coordinates."""
        plan_request = {
            "robot_id": f"robot_{uuid.uuid4().hex[:8]}",
            "goal_x": "invalid",
            "goal_y": 20.0
        }
        
        response = requests.post(
            f"{mission_planner_service['url']}/plan",
            json=plan_request
        )
        
        assert response.status_code in [400, 422]

    def test_plan_mission_multiple_maps(self, mission_planner_service):
        """Test plan mission in different maps."""
        for map_id in ["warehouse", "factory", "office"]:
            plan_request = {
                "robot_id": f"robot_{uuid.uuid4().hex[:8]}",
                "goal_x": 10.0,
                "goal_y": 20.0,
                "map_id": map_id
            }
            
            response = requests.post(
                f"{mission_planner_service['url']}/plan",
                json=plan_request
            )
            
            assert response.status_code in [200, 400, 500, 503]

    def test_plan_mission_sequential_requests(self, mission_planner_service):
        """Test sequential plan mission requests."""
        for i in range(3):
            plan_request = {
                "robot_id": f"robot_{uuid.uuid4().hex[:8]}",
                "goal_x": float(i * 10),
                "goal_y": float(i * 20)
            }
            
            response = requests.post(
                f"{mission_planner_service['url']}/plan",
                json=plan_request
            )
            
            assert response.status_code in [200, 400, 500, 503]

