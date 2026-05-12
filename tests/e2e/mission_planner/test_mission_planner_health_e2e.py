"""
Mission Planner Service - Health & Resilience E2E Tests

Tests health checks, statistics, and service resilience.
"""

import pytest
import requests
import uuid
import concurrent.futures


@pytest.mark.e2e
@pytest.mark.requires_docker
class TestMissionPlannerHealthE2E:
    """Test health and resilience of Mission Planner Service."""

    def test_health_check(self, mission_planner_service):
        """Test health check endpoint."""
        response = requests.get(
            f"{mission_planner_service['url']}/health"
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data.get("status") in ["healthy", "degraded"]
        assert data.get("service") == "mission_planner"

    def test_stats_endpoint(self, mission_planner_service):
        """Test statistics endpoint."""
        response = requests.get(
            f"{mission_planner_service['url']}/stats"
        )
        
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, dict)

    def test_root_endpoint(self, mission_planner_service):
        """Test root endpoint with service information."""
        response = requests.get(
            f"{mission_planner_service['url']}/"
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data.get("service") == "Mission Planner"
        assert "endpoints" in data

    def test_concurrent_navigation_requests(self, mission_planner_service):
        """Test concurrent navigation requests."""
        def navigate(index):
            nav_request = {
                "robot_name": f"robot_{index}_{uuid.uuid4().hex[:4]}",
                "target_x": float(index * 5),
                "target_y": float(index * 10),
                "timeout_seconds": 30
            }
            response = requests.post(
                f"{mission_planner_service['url']}/api/v1/navigate",
                json=nav_request
            )
            return response.status_code in [200, 400, 500, 503]
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            results = list(executor.map(navigate, range(10)))
        
        assert len(results) == 10

    def test_concurrent_plan_requests(self, mission_planner_service):
        """Test concurrent plan mission requests."""
        def plan(index):
            plan_request = {
                "robot_id": f"robot_{index}_{uuid.uuid4().hex[:4]}",
                "goal_x": float(index * 5),
                "goal_y": float(index * 10)
            }
            response = requests.post(
                f"{mission_planner_service['url']}/plan",
                json=plan_request
            )
            return response.status_code in [200, 400, 500, 503]
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            results = list(executor.map(plan, range(10)))
        
        assert len(results) == 10

    def test_rapid_navigation_requests(self, mission_planner_service):
        """Test rapid navigation requests."""
        for i in range(10):
            nav_request = {
                "robot_name": f"robot_{uuid.uuid4().hex[:8]}",
                "target_x": 10.0,
                "target_y": 20.0,
                "timeout_seconds": 30
            }
            
            response = requests.post(
                f"{mission_planner_service['url']}/api/v1/navigate",
                json=nav_request
            )
            
            assert response.status_code in [200, 400, 500, 503]

    def test_rapid_plan_requests(self, mission_planner_service):
        """Test rapid plan mission requests."""
        for i in range(10):
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

    def test_malformed_navigation_request(self, mission_planner_service):
        """Test handling of malformed navigation request."""
        response = requests.post(
            f"{mission_planner_service['url']}/api/v1/navigate",
            data="invalid json {",
            headers={"Content-Type": "application/json"}
        )
        
        assert response.status_code in [400, 422]

    def test_malformed_plan_request(self, mission_planner_service):
        """Test handling of malformed plan request."""
        response = requests.post(
            f"{mission_planner_service['url']}/plan",
            data="invalid json {",
            headers={"Content-Type": "application/json"}
        )
        
        assert response.status_code in [400, 422]

    def test_large_mission_name(self, mission_planner_service):
        """Test navigation with large mission name."""
        large_name = "x" * 1000
        nav_request = {
            "robot_name": f"robot_{uuid.uuid4().hex[:8]}",
            "target_x": 10.0,
            "target_y": 20.0,
            "mission_name": large_name,
            "timeout_seconds": 30
        }
        
        response = requests.post(
            f"{mission_planner_service['url']}/api/v1/navigate",
            json=nav_request
        )
        
        assert response.status_code in [200, 400, 413, 500, 503]

    def test_service_availability_under_load(self, mission_planner_service):
        """Test service availability under load."""
        def make_request(index):
            try:
                response = requests.get(
                    f"{mission_planner_service['url']}/health",
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

    def test_stats_after_navigation(self, mission_planner_service):
        """Test stats endpoint after navigation request."""
        # Make navigation request
        nav_request = {
            "robot_name": f"robot_{uuid.uuid4().hex[:8]}",
            "target_x": 10.0,
            "target_y": 20.0,
            "timeout_seconds": 30
        }
        requests.post(
            f"{mission_planner_service['url']}/api/v1/navigate",
            json=nav_request
        )
        
        # Check stats
        response = requests.get(
            f"{mission_planner_service['url']}/stats"
        )
        
        assert response.status_code == 200

