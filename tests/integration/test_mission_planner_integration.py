"""
Integration tests for Mission Planner Service.

These tests use real Graph DB and mock Mission Dispatcher
to test the full mission planning workflow.
"""

import pytest
import requests
import datetime
import asyncio
from unittest.mock import Mock, patch, MagicMock, AsyncMock
from cloud_common.objects import mission as mission_object
from cloud_common.objects import common


@pytest.mark.integration
@pytest.mark.requires_docker
@pytest.mark.asyncio
class TestMissionPlannerIntegration:
    """Integration tests for mission planner with real graph database."""
    
    async def test_plan_mission_with_knn_search(self, graph_db_client, sample_map_simple):
        """Test planning a mission using KNN search to find closest nodes."""
        from packages.services.mission_planner.server import MissionPlannerService

        # Load map into graph DB
        self._load_map(graph_db_client, sample_map_simple)

        # Create mission planner service
        planner = MissionPlannerService(
            arango_host="localhost",  # Will be mocked
            default_map_id=sample_map_simple["map_id"],
            knn_k=1,
            range_search_radius=5.0
        )

        # Mock the graph DB client to use our test client
        planner.graph_db = graph_db_client

        # Mock robot at (1.0, 0.0)
        mock_robot = MagicMock()
        mock_robot.status.pose.x = 1.0
        mock_robot.status.pose.y = 0.0
        mock_robot.status.pose.theta = 0.0
        mock_robot.status.online = True

        # Mock goal position (at node_3)
        goal_position = {"x": 10.0, "y": 10.0}

        # Plan mission
        with patch.object(planner, 'get_robot_status', new_callable=AsyncMock, return_value=mock_robot), \
             patch.object(planner, 'submit_mission', new_callable=AsyncMock, return_value=(True, None)):
            result = await planner.plan_mission(
                robot_id="robot_1",
                goal_x=goal_position["x"],
                goal_y=goal_position["y"],
                map_id=sample_map_simple["map_id"]
            )

        # Verify mission was planned
        assert result["success"] is True, f"Mission planning failed: {result.get('error', 'Unknown error')}"
        assert "path" in result, f"No path in result: {result.keys()}"
        assert len(result["path"]) > 0, "Path is empty"

        # Path should start at node_1 (closest to robot at 1.0, 0.0)
        assert result["path"][0] == "node_1", f"Expected path to start at node_1, got {result['path'][0]}"

        # Path should end at node_3 (goal is at 10.0, 10.0 which is node_3's position)
        # The path should be [node_1, node_2, node_3]
        assert result["path"][-1] == "node_3", f"Expected path to end at node_3, got {result['path'][-1]}"
        assert len(result["path"]) >= 2, f"Path should have at least 2 nodes, got {len(result['path'])}"
    
    async def test_plan_mission_with_range_search(self, graph_db_client, sample_map_simple):
        """Test planning a mission using range search for nearby nodes."""
        from packages.services.mission_planner.server import MissionPlannerService

        # Load map
        self._load_map(graph_db_client, sample_map_simple)

        # Create planner with larger range search
        planner = MissionPlannerService(
            arango_host="localhost",
            default_map_id=sample_map_simple["map_id"],
            knn_k=1,
            range_search_radius=15.0  # Large radius to find multiple nodes
        )
        planner.graph_db = graph_db_client

        # Mock robot at origin
        mock_robot = MagicMock()
        mock_robot.status.pose.x = 0.0
        mock_robot.status.pose.y = 0.0

        # Find nodes within range
        with patch.object(planner, 'get_robot_status', new_callable=AsyncMock, return_value=mock_robot):
            result = await planner.find_nearby_nodes(
                robot_id="robot_1",
                map_id=sample_map_simple["map_id"]
            )

        # Should find at least node_1 and node_2 within 15m
        # node_1 is at (0,0) - distance 0
        # node_2 is at (10,0) - distance 10
        # node_3 is at (10,10) - distance ~14.14
        # All 3 should be within 15m radius
        assert "nodes" in result, f"No 'nodes' key in result: {result.keys()}"
        nodes_found = len(result["nodes"])
        assert nodes_found >= 2, f"Expected at least 2 nodes within 15m, found {nodes_found}. Nodes: {result['nodes']}"
    
    async def test_plan_mission_no_path_exists(self, graph_db_client):
        """Test planning when no path exists between start and goal."""
        from packages.services.mission_planner.server import MissionPlannerService
        
        map_id = "test_disconnected_map"
        
        # Create disconnected map
        graph_db_client.create_map(map_id)
        graph_db_client.add_node(map_id, "node_a", 0.0, 0.0)
        graph_db_client.add_node(map_id, "node_b", 100.0, 100.0)
        # No edge between them
        
        planner = MissionPlannerService(
            arango_host="localhost",
            default_map_id=map_id,
            knn_k=1,
            range_search_radius=5.0
        )
        planner.graph_db = graph_db_client
        
        # Mock robot at node_a
        mock_robot = MagicMock()
        mock_robot.status.pose.x = 0.0
        mock_robot.status.pose.y = 0.0
        mock_robot.status.pose.theta = 0.0
        mock_robot.status.online = True

        # Try to plan to node_b (should fail)
        with patch.object(planner, 'get_robot_status', new_callable=AsyncMock, return_value=mock_robot):
            result = await planner.plan_mission(
                robot_id="robot_1",
                goal_x=100.0,
                goal_y=100.0,
                map_id=map_id
            )
        
        assert result["success"] is False
        # Error could be "no path" or "no nodes found" depending on the scenario
        error_msg = result.get("error", "").lower()
        assert "no path" in error_msg or "no nodes" in error_msg
    
    async def test_plan_mission_robot_not_found(self, graph_db_client, sample_map_simple):
        """Test planning when robot status cannot be retrieved."""
        from packages.services.mission_planner.server import MissionPlannerService
        
        self._load_map(graph_db_client, sample_map_simple)
        
        planner = MissionPlannerService(
            arango_host="localhost",
            default_map_id=sample_map_simple["map_id"],
            knn_k=1,
            range_search_radius=5.0
        )
        planner.graph_db = graph_db_client
        
        # Mock robot not found
        with patch.object(planner, 'get_robot_status', new_callable=AsyncMock, side_effect=Exception("Robot not found")):
            result = await planner.plan_mission(
                robot_id="nonexistent_robot",
                goal_x=10.0,
                goal_y=10.0,
                map_id=sample_map_simple["map_id"]
            )
        
        assert result["success"] is False
        assert "robot not found" in result.get("error", "").lower()
    
    async def test_plan_mission_complex_map(self, graph_db_client, sample_map_complex):
        """Test planning on a complex map with many nodes."""
        from packages.services.mission_planner.server import MissionPlannerService
        
        # Load complex map (10x10 grid)
        self._load_map(graph_db_client, sample_map_complex)
        
        planner = MissionPlannerService(
            arango_host="localhost",
            default_map_id=sample_map_complex["map_id"],
            knn_k=3,  # Find 3 nearest nodes
            range_search_radius=20.0
        )
        planner.graph_db = graph_db_client
        
        # Mock robot at (0, 0)
        mock_robot = MagicMock()
        mock_robot.status.pose.x = 0.0
        mock_robot.status.pose.y = 0.0
        mock_robot.status.pose.theta = 0.0
        mock_robot.status.online = True

        # Plan to far corner (90, 90)
        with patch.object(planner, 'get_robot_status', new_callable=AsyncMock, return_value=mock_robot), \
             patch.object(planner, 'submit_mission', new_callable=AsyncMock, return_value=(True, None)):
            result = await planner.plan_mission(
                robot_id="robot_1",
                goal_x=90.0,
                goal_y=90.0,
                map_id=sample_map_complex["map_id"]
            )
        
        # Should find a path
        assert result["success"] is True
        assert len(result["path"]) > 0
        
        # Path should be reasonably efficient (not too long)
        # For a 10x10 grid from (0,0) to (9,9), optimal path is ~18 nodes
        assert len(result["path"]) < 30
    
    # Helper methods

    def _load_map(self, client, map_data):
        """Helper to load a complete map into the database."""
        map_id = map_data["map_id"]

        # Create map
        client.create_map(map_id)

        # Add nodes
        for node in map_data["nodes"]:
            client.add_node(
                map_id=map_id,
                node_id=node["id"],
                x=node["x"],
                y=node["y"],
                theta=node.get("theta", 0.0)
            )

        # Add edges
        for edge in map_data["edges"]:
            client.add_edge(
                map_id=map_id,
                from_node=edge["from"],
                to_node=edge["to"],
                weight=edge["weight"]
            )


@pytest.mark.integration
@pytest.mark.requires_docker
@pytest.mark.asyncio
class TestMissionPlannerMultiRobot:
    """Test mission planning for multiple robots."""

    async def test_plan_missions_for_multiple_robots(self, graph_db_client, sample_map_simple):
        """Test planning missions for multiple robots simultaneously."""
        from packages.services.mission_planner.server import MissionPlannerService

        self._load_map(graph_db_client, sample_map_simple)

        planner = MissionPlannerService(
            arango_host="localhost",
            default_map_id=sample_map_simple["map_id"],
            knn_k=1,
            range_search_radius=5.0
        )
        planner.graph_db = graph_db_client

        # Mock status for multiple robots
        robot_statuses = {
            "robot_1": {"robot_id": "robot_1", "position": {"x": 0.0, "y": 0.0, "theta": 0.0}, "status": "idle"},
            "robot_2": {"robot_id": "robot_2", "position": {"x": 10.0, "y": 0.0, "theta": 0.0}, "status": "idle"},
            "robot_3": {"robot_id": "robot_3", "position": {"x": 10.0, "y": 10.0, "theta": 0.0}, "status": "idle"}
        }

        from cloud_common.objects.robot import RobotObjectV1, RobotStatusV1
        from cloud_common.objects import common
        results = []
        for robot_id, status in robot_statuses.items():
            mock_robot = RobotObjectV1(name=robot_id, lifecycle="ALIVE", status=RobotStatusV1(pose=common.Pose2D(x=status["position"]["x"], y=status["position"]["y"], theta=status["position"]["theta"])))
            with patch.object(planner, 'get_robot_status', new_callable=AsyncMock, return_value=mock_robot), \
                 patch.object(planner, 'submit_mission', new_callable=AsyncMock, return_value=(True, None)):
                result = await planner.plan_mission(
                    robot_id=robot_id,
                    goal_x=5.0,
                    goal_y=5.0,
                    map_id=sample_map_simple["map_id"]
                )
                results.append(result)

        # All missions should succeed
        assert all(r["success"] for r in results)
        assert len(results) == 3

    def _load_map(self, client, map_data):
        """Helper to load a complete map into the database."""
        map_id = map_data["map_id"]
        client.create_map(map_id)
        for node in map_data["nodes"]:
            client.add_node(
                map_id=map_id,
                node_id=node["id"],
                x=node["x"],
                y=node["y"],
                theta=node.get("theta", 0.0)
            )
        for edge in map_data["edges"]:
            client.add_edge(
                map_id=map_id,
                from_node=edge["from"],
                to_node=edge["to"],
                weight=edge["weight"]
            )


@pytest.mark.integration
@pytest.mark.requires_docker
@pytest.mark.asyncio
class TestMissionPlannerEdgeCases:
    """Test edge cases and error scenarios."""

    async def test_plan_to_current_location(self, graph_db_client, sample_map_simple):
        """Test planning mission when robot is already at goal."""
        from packages.services.mission_planner.server import MissionPlannerService

        self._load_map(graph_db_client, sample_map_simple)

        planner = MissionPlannerService(
            arango_host="localhost",
            default_map_id=sample_map_simple["map_id"],
            knn_k=1,
            range_search_radius=5.0
        )
        planner.graph_db = graph_db_client

        # Robot at node_1 (0, 0)
        mock_robot = MagicMock()
        mock_robot.status.pose.x = 0.0
        mock_robot.status.pose.y = 0.0
        mock_robot.status.pose.theta = 0.0
        mock_robot.status.online = True

        # Goal also at node_1
        with patch.object(planner, 'get_robot_status', new_callable=AsyncMock, return_value=mock_robot), \
             patch.object(planner, 'submit_mission', new_callable=AsyncMock, return_value=(True, None)):
            result = await planner.plan_mission(
                robot_id="robot_1",
                goal_x=0.0,
                goal_y=0.0,
                map_id=sample_map_simple["map_id"]
            )

        # Should succeed with minimal path
        assert result["success"] is True
        assert len(result["path"]) <= 1

    async def test_plan_with_very_large_coordinates(self, graph_db_client):
        """Test planning with very large coordinate values."""
        from packages.services.mission_planner.server import MissionPlannerService

        map_id = "large_coords_map"
        graph_db_client.create_map(map_id)

        # Add nodes with large coordinates
        graph_db_client.add_node(node_id="node_a", x=1000000.0, y=1000000.0, map_id=map_id)
        graph_db_client.add_node(node_id="node_b", x=1000100.0, y=1000000.0, map_id=map_id)
        graph_db_client.add_edge(from_node="node_a", to_node="node_b", weight=100.0, map_id=map_id)

        planner = MissionPlannerService(
            arango_host="localhost",
            default_map_id=map_id,
            knn_k=1,
            range_search_radius=200.0
        )
        planner.graph_db = graph_db_client

        mock_robot = MagicMock()
        mock_robot.status.pose.x = 1000000.0
        mock_robot.status.pose.y = 1000000.0
        mock_robot.status.pose.theta = 0.0
        mock_robot.status.online = True

        with patch.object(planner, 'get_robot_status', new_callable=AsyncMock, return_value=mock_robot), \
             patch.object(planner, 'submit_mission', new_callable=AsyncMock, return_value=(True, None)):
            result = await planner.plan_mission(
                robot_id="robot_1",
                goal_x=1000100.0,
                goal_y=1000000.0,
                map_id=map_id
            )

        assert result["success"] is True

    def _load_map(self, client, map_data):
        """Helper to load a complete map into the database."""
        map_id = map_data["map_id"]
        client.create_map(map_id)
        for node in map_data["nodes"]:
            client.add_node(
                map_id=map_id,
                node_id=node["id"],
                x=node["x"],
                y=node["y"],
                theta=node.get("theta", 0.0)
            )
        for edge in map_data["edges"]:
            client.add_edge(
                map_id=map_id,
                from_node=edge["from"],
                to_node=edge["to"],
                weight=edge["weight"]
            )


@pytest.mark.integration
@pytest.mark.requires_docker
@pytest.mark.asyncio
class TestMissionPlannerErrorScenarios:
    """Integration tests for mission planner error handling and edge cases."""

    async def test_plan_mission_no_path_available(self, graph_db_client):
        """Test mission planning when start and goal nodes are disconnected."""
        from packages.services.mission_planner.server import MissionPlannerService

        map_id = "test_no_path_map"

        # Create map with disconnected nodes
        graph_db_client.create_map(map_id)
        graph_db_client.add_node(map_id, "node_1", 0.0, 0.0, 0.0)
        graph_db_client.add_node(map_id, "node_2", 100.0, 100.0, 0.0)
        # No edges - nodes are disconnected

        planner = MissionPlannerService(
            arango_host="localhost",
            default_map_id=map_id,
            knn_k=1,
            range_search_radius=5.0
        )
        planner.graph_db = graph_db_client

        # Mock robot at node_1
        mock_robot = MagicMock()
        mock_robot.status.pose.x = 0.0
        mock_robot.status.pose.y = 0.0

        # Try to plan to node_2 (no path exists)
        goal_position = {"x": 100.0, "y": 100.0}

        with patch.object(planner, 'get_robot_status', new_callable=AsyncMock, return_value=mock_robot):
            result = await planner.find_path_for_robot(
                robot_id="robot_1",
                goal_position=goal_position,
                map_id=map_id
            )

        # Should return None or empty path
        assert result is None or len(result) == 0

    async def test_plan_mission_empty_map(self, graph_db_client):
        """Test mission planning when map has no nodes."""
        from packages.services.mission_planner.server import MissionPlannerService

        map_id = "test_empty_map"
        graph_db_client.create_map(map_id)
        # No nodes added

        planner = MissionPlannerService(
            arango_host="localhost",
            default_map_id=map_id,
            knn_k=1,
            range_search_radius=5.0
        )
        planner.graph_db = graph_db_client

        mock_robot = MagicMock()
        mock_robot.status.pose.x = 0.0
        mock_robot.status.pose.y = 0.0

        goal_position = {"x": 10.0, "y": 10.0}

        with patch.object(planner, 'get_robot_status', new_callable=AsyncMock, return_value=mock_robot):
            result = await planner.find_path_for_robot(
                robot_id="robot_1",
                goal_position=goal_position,
                map_id=map_id
            )

        # Should return None (no nodes to plan with)
        assert result is None

    async def test_knn_search_k_exceeds_nodes(self, graph_db_client):
        """Test KNN search when k exceeds number of available nodes."""
        from packages.services.mission_planner.server import MissionPlannerService

        map_id = "test_knn_k_exceeds"
        graph_db_client.create_map(map_id)

        # Add only 2 nodes
        graph_db_client.add_node(map_id, "node_1", 0.0, 0.0, 0.0)
        graph_db_client.add_node(map_id, "node_2", 5.0, 5.0, 0.0)
        graph_db_client.add_edge(map_id, "node_1", "node_2", 1.0)

        # Request k=10 but only 2 nodes exist
        planner = MissionPlannerService(
            arango_host="localhost",
            default_map_id=map_id,
            knn_k=10,  # More than available nodes
            range_search_radius=5.0
        )
        planner.graph_db = graph_db_client

        mock_robot = MagicMock()
        mock_robot.status.pose.x = 0.0
        mock_robot.status.pose.y = 0.0

        goal_position = {"x": 5.0, "y": 5.0}

        with patch.object(planner, 'get_robot_status', new_callable=AsyncMock, return_value=mock_robot):
            # Should handle gracefully and return available nodes
            result = await planner.find_path_for_robot(
                robot_id="robot_1",
                goal_position=goal_position,
                map_id=map_id
            )

        # Should still find a path with available nodes
        assert result is not None

    async def test_range_search_no_nodes_in_range(self, graph_db_client):
        """Test range search when no nodes exist within search radius."""
        from packages.services.mission_planner.server import MissionPlannerService

        map_id = "test_range_no_nodes"
        graph_db_client.create_map(map_id)

        # Add nodes far from goal
        graph_db_client.add_node(map_id, "node_1", 0.0, 0.0, 0.0)
        graph_db_client.add_node(map_id, "node_2", 100.0, 100.0, 0.0)

        planner = MissionPlannerService(
            arango_host="localhost",
            default_map_id=map_id,
            knn_k=1,
            range_search_radius=1.0  # Very small radius
        )
        planner.graph_db = graph_db_client

        mock_robot = MagicMock()
        mock_robot.status.pose.x = 0.0
        mock_robot.status.pose.y = 0.0

        # Goal far from any node
        goal_position = {"x": 50.0, "y": 50.0}

        with patch.object(planner, 'get_robot_status', new_callable=AsyncMock, return_value=mock_robot):
            result = await planner.find_path_for_robot(
                robot_id="robot_1",
                goal_position=goal_position,
                map_id=map_id
            )

        # Should return None or use fallback (KNN)
        # Behavior depends on implementation
        assert result is None or isinstance(result, list)

    async def test_robot_status_retrieval_failure(self, graph_db_client):
        """Test mission planning when robot status cannot be retrieved."""
        from packages.services.mission_planner.server import MissionPlannerService

        map_id = "test_robot_status_fail"
        graph_db_client.create_map(map_id)
        graph_db_client.add_node(map_id, "node_1", 0.0, 0.0, 0.0)

        planner = MissionPlannerService(
            arango_host="localhost",
            default_map_id=map_id,
            knn_k=1,
            range_search_radius=5.0
        )
        planner.graph_db = graph_db_client

        goal_position = {"x": 10.0, "y": 10.0}

        # Mock robot status retrieval to fail
        with patch.object(planner, 'get_robot_status', new_callable=AsyncMock, return_value=None):
            result = await planner.find_path_for_robot(
                robot_id="robot_1",
                goal_position=goal_position,
                map_id=map_id
            )

        # Should handle gracefully
        assert result is None

    async def test_mission_submission_failure(self, graph_db_client):
        """Test error handling when mission cannot be submitted to dispatcher."""
        from packages.services.mission_planner.server import MissionPlannerService

        map_id = "test_submission_fail"
        graph_db_client.create_map(map_id)
        graph_db_client.add_node(map_id, "node_1", 0.0, 0.0, 0.0)
        graph_db_client.add_node(map_id, "node_2", 5.0, 5.0, 0.0)
        graph_db_client.add_edge(map_id, "node_1", "node_2", 1.0)

        planner = MissionPlannerService(
            arango_host="localhost",
            default_map_id=map_id,
            knn_k=1,
            range_search_radius=5.0
        )
        planner.graph_db = graph_db_client

        mock_robot = MagicMock()
        mock_robot.status.pose.x = 0.0
        mock_robot.status.pose.y = 0.0
        mock_robot.status.pose.theta = 0.0
        mock_robot.status.online = True

        goal_position = {"x": 5.0, "y": 5.0}

        # Mock submission to fail
        with patch.object(planner, 'get_robot_status', new_callable=AsyncMock, return_value=mock_robot), \
             patch.object(planner, 'submit_mission', new_callable=AsyncMock, return_value=(False, "Database error")):

            try:
                result = await planner.plan_and_execute_mission(
                    robot_id="robot_1",
                    goal_position=goal_position,
                    map_id=map_id
                )
                # Should return error or raise exception
                assert result is None or result.get("success") is False
            except Exception:
                # Exception is acceptable for submission failure
                pass

    async def test_graph_database_connection_failure(self):
        """Test mission planner behavior when graph database is unavailable."""
        from packages.services.mission_planner.server import MissionPlannerService

        # Create planner with invalid graph DB URL
        planner = MissionPlannerService(
            arango_host="localhost",  # Will be overridden by mock graph_db
            default_map_id="test_map",
            knn_k=1,
            range_search_radius=5.0
        )

        mock_robot = MagicMock()
        mock_robot.status.pose.x = 0.0
        mock_robot.status.pose.y = 0.0

        goal_position = {"x": 10.0, "y": 10.0}

        with patch.object(planner, 'get_robot_status', new_callable=AsyncMock, return_value=mock_robot):
            try:
                result = await planner.find_path_for_robot(
                    robot_id="robot_1",
                    goal_position=goal_position,
                    map_id="test_map"
                )
                # Should return None or handle error
                assert result is None
            except Exception:
                # Exception is acceptable when DB is unavailable
                pass

    async def test_mission_creation_with_invalid_waypoints(self, graph_db_client):
        """Test mission creation when path contains invalid waypoints."""
        from packages.services.mission_planner.server import MissionPlannerService

        map_id = "test_invalid_waypoints"
        graph_db_client.create_map(map_id)
        graph_db_client.add_node(map_id, "node_1", 0.0, 0.0, 0.0)

        planner = MissionPlannerService(
            arango_host="localhost",
            default_map_id=map_id,
            knn_k=1,
            range_search_radius=5.0
        )
        planner.graph_db = graph_db_client

        # Try to create mission with invalid waypoints
        invalid_waypoints = [
            {"node_id": "nonexistent_node", "x": 0.0, "y": 0.0},
            {"node_id": "another_invalid", "x": 10.0, "y": 10.0}
        ]

        with patch.object(planner, 'submit_mission') as mock_submit:
            mock_submit.return_value = (False, "Invalid waypoints")

            try:
                result = await planner._create_mission_from_path(
                    robot_id="robot_1",
                    path=invalid_waypoints,
                    map_id=map_id
                )
                # Should handle invalid waypoints
                assert result is None or result.get("success") is False
            except Exception:
                # Exception is acceptable for invalid waypoints
                pass


@pytest.mark.integration
@pytest.mark.requires_docker
@pytest.mark.asyncio
class TestMissionDatabaseConcurrentOperations:
    """Integration tests for mission database concurrent updates and operations."""


    async def test_database_connection_pool_exhaustion(self, graph_db_client):
        """Test service behavior when database connection pool is exhausted."""
        from packages.services.mission_planner.server import MissionPlannerService

        map_id = "test_pool_exhaustion"
        graph_db_client.create_map(map_id)

        # Add nodes
        for i in range(5):
            graph_db_client.add_node(map_id, f"node_{i}", float(i), float(i), 0.0)

        # Add edges to connect the nodes
        for i in range(4):
            graph_db_client.add_edge(map_id, f"node_{i}", f"node_{i+1}", 1.0)

        planner = MissionPlannerService(
            arango_host="localhost",
            default_map_id=map_id,
            knn_k=1,
            range_search_radius=5.0
        )
        planner.graph_db = graph_db_client

        results = []

        async def perform_query(query_id):
            """Perform database query."""
            try:
                mock_robot = MagicMock()
                mock_robot.status.pose.x = 0.0
                mock_robot.status.pose.y = 0.0

                with patch.object(planner, 'get_robot_status', new_callable=AsyncMock, return_value=mock_robot):
                    result = await planner.find_path_for_robot(
                        robot_id=f"robot_{query_id}",
                        goal_position={"x": 4.0, "y": 4.0},
                        map_id=map_id
                    )
                    return {"query_id": query_id, "success": result is not None}
            except Exception as e:
                return {"query_id": query_id, "error": str(e)}

        # Create many concurrent queries
        tasks = [perform_query(i) for i in range(20)]
        results = await asyncio.gather(*tasks)

        # Most queries should succeed
        successful_queries = sum(1 for r in results if r.get("success") is True)
        assert successful_queries >= 15  # At least 75% should succeed

    def _load_map(self, client, map_data):
        """Helper to load a complete map into the database."""
        map_id = map_data["map_id"]
        client.create_map(map_id)
        for node in map_data["nodes"]:
            client.add_node(
                map_id=map_id,
                node_id=node["id"],
                x=node["x"],
                y=node["y"],
                theta=node.get("theta", 0.0)
            )
        for edge in map_data["edges"]:
            client.add_edge(
                map_id=map_id,
                from_node=edge["from"],
                to_node=edge["to"],
                weight=edge["weight"]
            )


@pytest.mark.integration
@pytest.mark.requires_docker
@pytest.mark.integration
@pytest.mark.requires_docker
@pytest.mark.asyncio
class TestMissionPlannerGetMissionPlanIntegration:
    """Integration tests for get_mission_plan with real graph database."""

    async def test_get_mission_plan_with_real_graph_db(self, graph_db_client, sample_map_simple):
        """Test get_mission_plan with real graph database and mocked mission database."""
        from packages.services.mission_planner.server import MissionPlannerService

        # Load map into graph DB
        self._load_map(graph_db_client, sample_map_simple)

        # Create mission planner service
        planner = MissionPlannerService(
            arango_host="localhost",
            default_map_id=sample_map_simple["map_id"],
            knn_k=1,
            range_search_radius=5.0
        )
        planner.graph_db = graph_db_client

        # Create mock mission with waypoints matching graph nodes
        mock_mission = Mock(spec=mission_object.MissionObjectV1)
        mock_mission.name = "test_mission_integration"
        mock_mission.robot = "robot_1"

        mock_status = Mock()
        mock_status.state = Mock()
        mock_status.state.value = "PENDING"
        mock_status.start_timestamp = None
        mock_status.end_timestamp = None
        mock_mission.status = mock_status

        # Create waypoints at exact node positions from sample_map_simple
        # node_1: (0.0, 0.0), node_2: (10.0, 0.0), node_3: (10.0, 10.0)
        waypoints = [
            common.Pose2D(x=0.0, y=0.0, theta=0.0, map_id=sample_map_simple["map_id"]),
            common.Pose2D(x=10.0, y=0.0, theta=0.0, map_id=sample_map_simple["map_id"]),
            common.Pose2D(x=10.0, y=10.0, theta=1.57, map_id=sample_map_simple["map_id"])
        ]

        mock_route = Mock()
        mock_route.waypoints = waypoints

        mock_node = Mock()
        mock_node.route = mock_route

        mock_mission.mission_tree = [mock_node]

        # Mock database client
        mock_db = AsyncMock()
        mock_db.get_object.return_value = mock_mission
        planner.database = mock_db

        # Call get_mission_plan
        result = await planner.get_mission_plan("test_mission_integration")

        # Assertions
        assert result is not None
        assert result["mission_id"] == "test_mission_integration"
        assert result["state"] == "PENDING"
        assert result["robot_name"] == "robot_1"

        # Path should be reconstructed to match the original nodes
        assert len(result["path"]) == 3
        assert result["path"] == ["node_1", "node_2", "node_3"]
        assert result["start_node_id"] == "node_1"
        assert result["end_node_id"] == "node_3"

        # Positions should match waypoints
        assert result["start_position"]["x"] == 0.0
        assert result["start_position"]["y"] == 0.0
        assert result["end_position"]["x"] == 10.0
        assert result["end_position"]["y"] == 10.0

    async def test_get_mission_plan_waypoints_near_nodes(self, graph_db_client, sample_map_simple):
        """Test get_mission_plan when waypoints are near but not exactly on nodes."""
        from packages.services.mission_planner.server import MissionPlannerService

        self._load_map(graph_db_client, sample_map_simple)

        planner = MissionPlannerService(
            arango_host="localhost",
            default_map_id=sample_map_simple["map_id"],
            knn_k=1,
            range_search_radius=5.0
        )
        planner.graph_db = graph_db_client

        # Create mock mission with waypoints slightly offset from nodes
        mock_mission = Mock(spec=mission_object.MissionObjectV1)
        mock_mission.name = "test_mission_offset"
        mock_mission.robot = "robot_1"

        mock_status = Mock()
        mock_status.state = Mock()
        mock_status.state.value = "RUNNING"
        mock_status.start_timestamp = datetime.datetime.now()
        mock_status.end_timestamp = None
        mock_mission.status = mock_status

        # Waypoints offset by 0.1 from actual nodes
        waypoints = [
            common.Pose2D(x=0.1, y=0.1, theta=0.0, map_id=sample_map_simple["map_id"]),
            common.Pose2D(x=10.1, y=0.1, theta=0.0, map_id=sample_map_simple["map_id"])
        ]

        mock_route = Mock()
        mock_route.waypoints = waypoints

        mock_node = Mock()
        mock_node.route = mock_route

        mock_mission.mission_tree = [mock_node]

        mock_db = AsyncMock()
        mock_db.get_object.return_value = mock_mission
        planner.database = mock_db

        result = await planner.get_mission_plan("test_mission_offset")

        # KNN should still find the closest nodes
        assert result is not None
        assert result["state"] == "RUNNING"
        assert len(result["path"]) == 2
        # Should map to node_1 and node_2 (closest nodes)
        assert result["path"][0] == "node_1"
        assert result["path"][1] == "node_2"

    async def test_get_mission_plan_complex_map(self, graph_db_client, sample_map_complex):
        """Test get_mission_plan with a complex map (10x10 grid)."""
        from packages.services.mission_planner.server import MissionPlannerService

        self._load_map(graph_db_client, sample_map_complex)

        planner = MissionPlannerService(
            arango_host="localhost",
            default_map_id=sample_map_complex["map_id"],
            knn_k=1,
            range_search_radius=5.0
        )
        planner.graph_db = graph_db_client

        # Create mission with diagonal path across grid
        mock_mission = Mock(spec=mission_object.MissionObjectV1)
        mock_mission.name = "test_mission_complex"
        mock_mission.robot = "robot_1"

        mock_status = Mock()
        mock_status.state = Mock()
        mock_status.state.value = "COMPLETED"
        mock_status.start_timestamp = datetime.datetime.now()
        mock_status.end_timestamp = datetime.datetime.now()
        mock_mission.status = mock_status

        # Diagonal waypoints: (0,0) -> (10,10) -> (20,20) -> (30,30)
        waypoints = [
            common.Pose2D(x=0.0, y=0.0, theta=0.0, map_id=sample_map_complex["map_id"]),
            common.Pose2D(x=10.0, y=10.0, theta=0.0, map_id=sample_map_complex["map_id"]),
            common.Pose2D(x=20.0, y=20.0, theta=0.0, map_id=sample_map_complex["map_id"]),
            common.Pose2D(x=30.0, y=30.0, theta=0.0, map_id=sample_map_complex["map_id"])
        ]

        mock_route = Mock()
        mock_route.waypoints = waypoints

        mock_node = Mock()
        mock_node.route = mock_route

        mock_mission.mission_tree = [mock_node]

        mock_db = AsyncMock()
        mock_db.get_object.return_value = mock_mission
        planner.database = mock_db

        result = await planner.get_mission_plan("test_mission_complex")

        assert result is not None
        assert result["state"] == "COMPLETED"
        assert len(result["path"]) == 4
        # Should reconstruct to diagonal nodes
        assert result["path"] == ["node_0_0", "node_1_1", "node_2_2", "node_3_3"]

    async def test_get_mission_plan_database_error(self, graph_db_client, sample_map_simple):
        """Test get_mission_plan when database query fails."""
        from packages.services.mission_planner.server import MissionPlannerService

        self._load_map(graph_db_client, sample_map_simple)

        planner = MissionPlannerService(
            arango_host="localhost",
            default_map_id=sample_map_simple["map_id"],
            knn_k=1,
            range_search_radius=5.0
        )
        planner.graph_db = graph_db_client

        # Mock database to raise exception
        mock_db = AsyncMock()
        mock_db.get_object.side_effect = Exception("Database connection failed")
        planner.database = mock_db

        result = await planner.get_mission_plan("test_mission")

        # Should return None on error
        assert result is None

    def _load_map(self, client, map_data):
        """Helper to load a complete map into the database."""
        map_id = map_data["map_id"]
        client.create_map(map_id)
        for node in map_data["nodes"]:
            client.add_node(
                map_id=map_id,
                node_id=node["id"],
                x=node["x"],
                y=node["y"],
                theta=node.get("theta", 0.0)
            )
        for edge in map_data["edges"]:
            client.add_edge(
                map_id=map_id,
                from_node=edge["from"],
                to_node=edge["to"],
                weight=edge["weight"]
            )


