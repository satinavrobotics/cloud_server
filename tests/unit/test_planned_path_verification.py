import pytest
import asyncio
from unittest.mock import MagicMock, patch, Mock, AsyncMock
from typing import Dict, Any, List, Optional, Tuple
import datetime

from packages.services.mission_planner.server import MissionPlannerService
from cloud_common.objects import mission as mission_object
from cloud_common.objects import robot as robot_object
from cloud_common.objects import common

@pytest.mark.asyncio
class TestPlannedPathVerification:
    """Targeted verification for the planned_path storage and reconstruction fix."""

    @patch('packages.services.mission_planner.server.GraphDatabaseService')
    @patch('packages.services.mission_planner.server.PostgresDatabase')
    async def test_get_mission_plan_prioritizes_stored_path(self, mock_db_client, mock_graph_client):
        """Verify that get_mission_plan uses the stored planned_path and avoids KNN."""
        
        # 1. Setup mock mission with a stored path
        mock_mission = MagicMock()
        mock_mission.name = "test_mission_stored"
        mock_mission.planned_path = ["node_A", "node_B", "node_C"]
        
        # Mock mission status
        mock_mission.status = MagicMock()
        mock_mission.status.state.value = "RUNNING"
        mock_mission.status.start_timestamp = datetime.datetime.now()
        mock_mission.status.end_timestamp = None
        
        # Mock mission tree (necessary for waypoint extraction)
        mock_waypoint = common.Pose2D(x=1.0, y=2.0)
        mock_route_node = MagicMock()
        mock_route_node.route.waypoints = [mock_waypoint]
        mock_mission.mission_tree = [mock_route_node]
        
        # 2. Setup Database mock
        mock_db_instance = MagicMock()
        mock_db_instance.get_object = AsyncMock(return_value=mock_mission)
        mock_db_client.return_value = mock_db_instance
        
        # 3. Setup Graph DB mock
        mock_graph_instance = MagicMock()
        mock_graph_client.return_value = mock_graph_instance
        
        service = MissionPlannerService()
        
        # 4. Execute
        result = await service.get_mission_plan("test_mission_stored")
        
        # 5. Verify
        assert result is not None
        assert result["path"] == ["node_A", "node_B", "node_C"]
        
        # CRITICAL: Verify KNN was NOT called for reconstruction
        assert mock_graph_instance.k_nearest_neighbors.call_count == 0
        print("\n✅ Verified: get_mission_plan prioritized stored path and skipped KNN.")

    @patch('packages.services.mission_planner.server.GraphDatabaseService')
    @patch('packages.services.mission_planner.server.PostgresDatabase')
    async def test_plan_mission_stores_planned_path(self, mock_db_client, mock_graph_client):
        """Verify that plan_and_execute_mission stores the computed path in the mission."""
        
        # 1. Setup mocks
        mock_db_instance = MagicMock()
        mock_db_instance.get_object = AsyncMock() # For get_robot_status
        mock_db_instance.create_object = AsyncMock(return_value=True) # For submit_mission
        mock_db_client.return_value = mock_db_instance
        
        # Mock robot status
        mock_robot = MagicMock()
        mock_robot.status.pose.x = 0.0
        mock_robot.status.pose.y = 0.0
        mock_db_instance.get_object.return_value = mock_robot
        
        mock_graph_instance = MagicMock()
        # Mock KNN for robot node
        mock_graph_instance.k_nearest_neighbors.return_value = ([{"node_id": "start_node", "x": 0.0, "y": 0.0}], [0.0])
        # Mock range search for target node
        mock_graph_instance.nodes_in_range.return_value = ([{"node_id": "end_node", "x": 10.0, "y": 10.0}], [0.0])
        # Mock pathfinding
        mock_graph_instance.shortest_path.return_value = ["start_node", "mid_node", "end_node"]
        # Mock node poses for mission creation
        mock_graph_instance.get_node.return_value = {"x": 1.0, "y": 1.0, "node_id": "node_X"}
        
        mock_graph_client.return_value = mock_graph_instance
        
        service = MissionPlannerService()
        
        # 2. Execute
        result = await service.plan_and_execute_mission(
            robot_name="test_robot",
            target_x=10.0,
            target_y=10.0
        )
        
        # 3. Verify
        assert result["success"] is True
        assert result["path"] == ["start_node", "mid_node", "end_node"]
        
        # Verify that create_object was called with a mission containing the planned_path
        args, kwargs = mock_db_instance.create_object.call_args
        submitted_mission = args[0]
        assert submitted_mission.planned_path == ["start_node", "mid_node", "end_node"]
        print("✅ Verified: plan_and_execute_mission stored the computed path in the database.")
