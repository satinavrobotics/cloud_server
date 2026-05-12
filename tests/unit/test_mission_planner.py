import pytest
from unittest.mock import MagicMock, patch, Mock, AsyncMock
from typing import Dict, Any, List, Optional, Tuple
import datetime
import uuid

from packages.services.mission_planner.server import MissionPlannerService
from cloud_common.objects import mission as mission_object
from cloud_common.objects import robot as robot_object
from cloud_common.objects import common
from packages.database.postgres import PostgresDatabase
@pytest.mark.unit
class TestMissionPlannerLegacy:
    """Legacy tests for MissionPlannerService."""

    @pytest.mark.asyncio
    @patch('packages.services.mission_planner.server.GraphDatabaseService')
    @patch('packages.services.mission_planner.server.PostgresDatabase')
    async def test_plan_mission_alias(self, mock_db_client, mock_graph_client):
        """Test plan_mission alias method."""
        # Setup robot status
        mock_pose = Mock()
        mock_pose.x = 10.0
        mock_pose.y = 20.0
        mock_pose.theta = 0.0

        mock_status = Mock()
        mock_status.pose = mock_pose
        mock_status.online = True

        mock_robot = Mock(spec=robot_object.RobotObjectV1)
        mock_robot.status = mock_status
        mock_robot.position_mode = 'local'

        mock_db_instance = AsyncMock(spec=PostgresDatabase)
        mock_db_instance.get_object = AsyncMock()
        mock_db_instance.create_object = AsyncMock()
        mock_db_instance.get_object.return_value = mock_robot
        mock_db_instance.create_object.return_value = True
        mock_db_client.return_value = mock_db_instance

        # Setup graph database
        mock_graph_instance = Mock()
        mock_graph_instance.shortest_path = Mock()
        mock_graph_instance.k_nearest_neighbors = Mock()
        mock_graph_instance.nodes_in_range = Mock()
        mock_graph_instance.get_node = Mock()
        mock_graph_instance.k_nearest_neighbors.return_value = (
            [{'node_id': 1, 'x': 10.5, 'y': 20.5, 'yaw': 0.0}], [0.7]
        )
        mock_graph_instance.nodes_in_range.return_value = (
            [{'node_id': 2, 'x': 50.5, 'y': 60.5, 'yaw': 1.5}], [1.2]
        )
        mock_graph_instance.shortest_path.return_value = ["1", "2"]
        mock_graph_instance.get_node.side_effect = [
            {'node_id': 1, 'x': 10.5, 'y': 20.5, 'yaw': 0.0},
            {'node_id': 2, 'x': 50.5, 'y': 60.5, 'yaw': 1.5}
        ]
        mock_graph_client.return_value = mock_graph_instance

        service = MissionPlannerService()

        result = await service.plan_mission(
            robot_id="robot_1",
            goal_x=50.0,
            goal_y=60.0
        )

        assert result["success"] is True
        assert result["robot_name"] == "robot_1"


@pytest.mark.unit
class TestMissionPlannerGetMissionPlan:
    """Test get_mission_plan method with database-only approach."""

    @pytest.mark.asyncio
    @patch('packages.services.mission_planner.server.GraphDatabaseService')
    @patch('packages.services.mission_planner.server.PostgresDatabase')
    async def test_get_mission_plan_success(self, mock_db_client, mock_graph_client):
        """Test successful retrieval of mission plan from database using KNN."""
        from cloud_common.objects import mission as mission_object

        # Create mock mission object
        mock_mission = Mock(spec=mission_object.MissionObjectV1)
        mock_mission.planned_path = None
        mock_mission.planned_path = None
        mock_mission.planned_path = None
        mock_mission.name = "nav_robot_1_20250104_123456"
        mock_mission.robot = "robot_1"
        mock_mission.planned_path = None # Force KNN reconstruction

        # Mock mission status
        mock_status = Mock()
        mock_status.state = Mock()
        mock_status.state.value = "PENDING"
        mock_status.start_timestamp = None
        mock_status.end_timestamp = None
        mock_mission.status = mock_status

        # Mock mission tree with waypoints
        mock_waypoint_1 = common.Pose2D(x=10.0, y=20.0, theta=0.0, map_id="default")
        mock_waypoint_2 = common.Pose2D(x=30.0, y=40.0, theta=0.5, map_id="default")
        mock_waypoint_3 = common.Pose2D(x=50.0, y=60.0, theta=1.0, map_id="default")

        mock_route = Mock()
        mock_route.waypoints = [mock_waypoint_1, mock_waypoint_2, mock_waypoint_3]

        mock_node = Mock()
        mock_node.route = mock_route

        mock_mission.mission_tree = [mock_node]

        # Setup database mock
        mock_db_instance = MagicMock()
        mock_db_instance.get_object = AsyncMock(return_value=mock_mission)
        mock_db_client.return_value = mock_db_instance

        # Setup graph database mock for KNN search
        mock_graph_instance = Mock()
        mock_graph_instance.shortest_path = Mock()
        mock_graph_instance.k_nearest_neighbors = Mock()
        mock_graph_instance.nodes_in_range = Mock()
        mock_graph_instance.get_node = Mock()
        mock_graph_instance.k_nearest_neighbors.side_effect = [
            ([{'node_id': 'node_1', 'x': 10.0, 'y': 20.0}], [0.0]),
            ([{'node_id': 'node_2', 'x': 30.0, 'y': 40.0}], [0.0]),
            ([{'node_id': 'node_3', 'x': 50.0, 'y': 60.0}], [0.0]),
        ]
        mock_graph_client.return_value = mock_graph_instance

        service = MissionPlannerService()

        # Call get_mission_plan
        result = await service.get_mission_plan("nav_robot_1_20250104_123456")

        # Assertions
        assert result is not None
        assert result["path"] == ['node_1', 'node_2', 'node_3']
        assert result["start_node_id"] == 'node_1'

        # Verify KNN search was called for each waypoint
        assert mock_graph_instance.k_nearest_neighbors.call_count == 3

    @pytest.mark.asyncio
    @patch('packages.services.mission_planner.server.GraphDatabaseService')
    @patch('packages.services.mission_planner.server.PostgresDatabase')
    async def test_get_mission_plan_from_stored_path(self, mock_db_client, mock_graph_client):
        """Test successful retrieval of mission plan using stored planned_path."""
        from cloud_common.objects import mission as mission_object

        # Create mock mission object
        mock_mission = Mock(spec=mission_object.MissionObjectV1)
        mock_mission.planned_path = None
        mock_mission.name = "stored_path_mission"
        mock_mission.robot = "robot_1"
        mock_mission.planned_path = ['node_A', 'node_B', 'node_C'] # Stored path

        # Mock mission status
        mock_status = Mock()
        mock_status.state = Mock()
        mock_status.state.value = "PENDING"
        mock_status.start_timestamp = None
        mock_status.end_timestamp = None
        mock_mission.status = mock_status

        # Mock mission tree with waypoints
        mock_waypoint_1 = common.Pose2D(x=10.0, y=20.0, theta=0.0, map_id="default")
        mock_route = Mock()
        mock_route.waypoints = [mock_waypoint_1]
        mock_node = Mock()
        mock_node.route = mock_route
        mock_mission.mission_tree = [mock_node]

        # Setup database mock
        mock_db_instance = MagicMock()
        mock_db_instance.get_object = AsyncMock(return_value=mock_mission)
        mock_db_client.return_value = mock_db_instance

        mock_graph_instance = Mock()
        mock_graph_instance.shortest_path = Mock()
        mock_graph_instance.k_nearest_neighbors = Mock()
        mock_graph_instance.nodes_in_range = Mock()
        mock_graph_instance.get_node = Mock()
        mock_graph_client.return_value = mock_graph_instance

        service = MissionPlannerService()

        # Call get_mission_plan
        result = await service.get_mission_plan("stored_path_mission")

        # Assertions
        assert result is not None
        assert result["path"] == ['node_A', 'node_B', 'node_C']
        
        # Verify KNN search was NOT called because we used the stored path
        assert mock_graph_instance.k_nearest_neighbors.call_count == 0

    @pytest.mark.asyncio
    @patch('packages.services.mission_planner.server.GraphDatabaseService')
    @patch('packages.services.mission_planner.server.PostgresDatabase')
    async def test_get_mission_plan_mission_not_found(self, mock_db_client, mock_graph_client):
        """Test get_mission_plan when mission doesn't exist in database."""
        # Setup database mock to raise exception
        mock_db_instance = AsyncMock(spec=PostgresDatabase)
        mock_db_instance.get_object = AsyncMock()
        mock_db_instance.create_object = AsyncMock()
        mock_db_instance.get_object.side_effect = Exception("Mission not found")
        mock_db_client.return_value = mock_db_instance

        mock_graph_client.return_value = Mock()

        service = MissionPlannerService()

        # Call get_mission_plan
        result = await service.get_mission_plan("nonexistent_mission")

        # Should return None
        assert result is None

    @pytest.mark.asyncio
    @patch('packages.services.mission_planner.server.GraphDatabaseService')
    @patch('packages.services.mission_planner.server.PostgresDatabase')
    async def test_get_mission_plan_no_mission_tree(self, mock_db_client, mock_graph_client):
        """Test get_mission_plan when mission has no mission tree."""
        from cloud_common.objects import mission as mission_object

        # Create mock mission with empty mission tree
        mock_mission = Mock(spec=mission_object.MissionObjectV1)
        mock_mission.planned_path = None
        mock_mission.name = "test_mission"
        mock_mission.robot = "robot_1"
        mock_mission.planned_path = None

        mock_db_instance = AsyncMock(spec=PostgresDatabase)
        mock_db_instance.get_object = AsyncMock()
        mock_db_instance.create_object = AsyncMock()
        mock_db_instance.get_object.return_value = mock_mission
        mock_db_client.return_value = mock_db_instance

        mock_graph_client.return_value = Mock()

        service = MissionPlannerService()

        result = await service.get_mission_plan("test_mission")

        assert result is None

    @pytest.mark.asyncio
    @patch('packages.services.mission_planner.server.GraphDatabaseService')
    @patch('packages.services.mission_planner.server.PostgresDatabase')
    async def test_get_mission_plan_no_route_node(self, mock_db_client, mock_graph_client):
        """Test get_mission_plan when mission has no route node."""
        from cloud_common.objects import mission as mission_object

        # Create mock mission with node but no route
        mock_mission = Mock(spec=mission_object.MissionObjectV1)
        mock_mission.planned_path = None
        mock_mission.planned_path = None
        mock_mission.planned_path = None
        mock_mission.name = "test_mission"

        mock_node = Mock()
        mock_node.route = None
        mock_mission.mission_tree = [mock_node]

        mock_db_instance = AsyncMock(spec=PostgresDatabase)
        mock_db_instance.get_object = AsyncMock()
        mock_db_instance.create_object = AsyncMock()
        mock_db_instance.get_object.return_value = mock_mission
        mock_db_client.return_value = mock_db_instance

        mock_graph_client.return_value = Mock()

        service = MissionPlannerService()

        result = await service.get_mission_plan("test_mission")

        assert result is None

    @pytest.mark.asyncio
    @patch('packages.services.mission_planner.server.GraphDatabaseService')
    @patch('packages.services.mission_planner.server.PostgresDatabase')
    async def test_get_mission_plan_empty_waypoints(self, mock_db_client, mock_graph_client):
        """Test get_mission_plan when route has no waypoints."""
        from cloud_common.objects import mission as mission_object

        mock_mission = Mock(spec=mission_object.MissionObjectV1)
        mock_mission.planned_path = None
        mock_mission.planned_path = None
        mock_mission.planned_path = None
        mock_mission.name = "test_mission"

        mock_route = Mock()
        mock_route.waypoints = []

        mock_node = Mock()
        mock_node.route = mock_route

        mock_mission.mission_tree = [mock_node]

        mock_db_instance = AsyncMock(spec=PostgresDatabase)
        mock_db_instance.get_object = AsyncMock()
        mock_db_instance.create_object = AsyncMock()
        mock_db_instance.get_object.return_value = mock_mission
        mock_db_client.return_value = mock_db_instance

        mock_graph_client.return_value = Mock()

        service = MissionPlannerService()

        result = await service.get_mission_plan("test_mission")

        assert result is None

    @pytest.mark.asyncio
    @patch('packages.services.mission_planner.server.GraphDatabaseService')
    @patch('packages.services.mission_planner.server.PostgresDatabase')
    async def test_get_mission_plan_knn_no_results(self, mock_db_client, mock_graph_client):
        """Test get_mission_plan when KNN search returns no results for a waypoint."""
        from cloud_common.objects import mission as mission_object

        mock_mission = Mock(spec=mission_object.MissionObjectV1)
        mock_mission.planned_path = None
        mock_mission.planned_path = None
        mock_mission.planned_path = None
        mock_mission.name = "test_mission"
        mock_mission.robot = "robot_1"

        mock_status = Mock()
        mock_status.state = Mock()
        mock_status.state.value = "PENDING"
        mock_status.start_timestamp = None
        mock_status.end_timestamp = None
        mock_mission.status = mock_status

        mock_waypoint = common.Pose2D(x=10.0, y=20.0, theta=0.0, map_id="default")

        mock_route = Mock()
        mock_route.waypoints = [mock_waypoint]

        mock_node = Mock()
        mock_node.route = mock_route

        mock_mission.mission_tree = [mock_node]

        mock_db_instance = AsyncMock(spec=PostgresDatabase)
        mock_db_instance.get_object = AsyncMock()
        mock_db_instance.create_object = AsyncMock()
        mock_db_instance.get_object.return_value = mock_mission
        mock_db_client.return_value = mock_db_instance

        # KNN returns empty result
        mock_graph_instance = Mock()
        mock_graph_instance.shortest_path = Mock()
        mock_graph_instance.k_nearest_neighbors = Mock()
        mock_graph_instance.nodes_in_range = Mock()
        mock_graph_instance.get_node = Mock()
        mock_graph_instance.k_nearest_neighbors.return_value = ([], [])
        mock_graph_client.return_value = mock_graph_instance

        service = MissionPlannerService()

        result = await service.get_mission_plan("test_mission")

        # Should still return result with placeholder node ID
        assert result is not None
        assert result["path"] == ['unknown_0']
        assert result["start_node_id"] == 'unknown_0'

    @pytest.mark.asyncio
    @patch('packages.services.mission_planner.server.GraphDatabaseService')
    @patch('packages.services.mission_planner.server.PostgresDatabase')
    async def test_get_mission_plan_knn_exception(self, mock_db_client, mock_graph_client):
        """Test get_mission_plan when KNN search raises exception."""
        from cloud_common.objects import mission as mission_object

        mock_mission = Mock(spec=mission_object.MissionObjectV1)
        mock_mission.planned_path = None
        mock_mission.planned_path = None
        mock_mission.planned_path = None
        mock_mission.name = "test_mission"
        mock_mission.robot = "robot_1"

        mock_status = Mock()
        mock_status.state = Mock()
        mock_status.state.value = "PENDING"
        mock_status.start_timestamp = None
        mock_status.end_timestamp = None
        mock_mission.status = mock_status

        mock_waypoint = common.Pose2D(x=10.0, y=20.0, theta=0.0, map_id="default")

        mock_route = Mock()
        mock_route.waypoints = [mock_waypoint]

        mock_node = Mock()
        mock_node.route = mock_route

        mock_mission.mission_tree = [mock_node]

        mock_db_instance = AsyncMock(spec=PostgresDatabase)
        mock_db_instance.get_object = AsyncMock()
        mock_db_instance.create_object = AsyncMock()
        mock_db_instance.get_object.return_value = mock_mission
        mock_db_client.return_value = mock_db_instance

        # KNN raises exception
        mock_graph_instance = Mock()
        mock_graph_instance.shortest_path = Mock()
        mock_graph_instance.k_nearest_neighbors = Mock()
        mock_graph_instance.nodes_in_range = Mock()
        mock_graph_instance.get_node = Mock()
        mock_graph_instance.k_nearest_neighbors.side_effect = Exception("Graph DB error")
        mock_graph_client.return_value = mock_graph_instance

        service = MissionPlannerService()

        result = await service.get_mission_plan("test_mission")

        # Should still return result with error placeholder
        assert result is not None
        assert result["path"] == ['error_0']

    @pytest.mark.asyncio
    @patch('packages.services.mission_planner.server.GraphDatabaseService')
    @patch('packages.services.mission_planner.server.PostgresDatabase')
    async def test_get_mission_plan_with_custom_map_id(self, mock_db_client, mock_graph_client):
        """Test get_mission_plan with custom map_id parameter."""
        from cloud_common.objects import mission as mission_object

        mock_mission = Mock(spec=mission_object.MissionObjectV1)
        mock_mission.planned_path = None
        mock_mission.planned_path = None
        mock_mission.planned_path = None
        mock_mission.name = "test_mission"
        mock_mission.robot = "robot_1"

        mock_status = Mock()
        mock_status.state = Mock()
        mock_status.state.value = "RUNNING"
        mock_status.start_timestamp = None
        mock_status.end_timestamp = None
        mock_mission.status = mock_status

        mock_waypoint = common.Pose2D(x=10.0, y=20.0, theta=0.0, map_id="custom_map")

        mock_route = Mock()
        mock_route.waypoints = [mock_waypoint]

        mock_node = Mock()
        mock_node.route = mock_route

        mock_mission.mission_tree = [mock_node]

        mock_db_instance = AsyncMock(spec=PostgresDatabase)
        mock_db_instance.get_object = AsyncMock()
        mock_db_instance.create_object = AsyncMock()
        mock_db_instance.get_object.return_value = mock_mission
        mock_db_client.return_value = mock_db_instance

        mock_graph_instance = Mock()
        mock_graph_instance.shortest_path = Mock()
        mock_graph_instance.k_nearest_neighbors = Mock()
        mock_graph_instance.nodes_in_range = Mock()
        mock_graph_instance.get_node = Mock()
        mock_graph_instance.k_nearest_neighbors.return_value = [
            {'node': {'node_id': 'custom_node_1'}, 'distance': 0.0}
        ]
        mock_graph_client.return_value = mock_graph_instance

        service = MissionPlannerService()

        result = await service.get_mission_plan("test_mission", map_id="custom_map")

        assert result is not None
        assert result["state"] == "RUNNING"

        # Verify KNN was called with custom map_id
        mock_graph_instance.k_nearest_neighbors.assert_called_once_with(
            x=10.0,
            y=20.0,
            k=1,
            map_id="custom_map"
        )

    @pytest.mark.asyncio
    @patch('packages.services.mission_planner.server.GraphDatabaseService')
    @patch('packages.services.mission_planner.server.PostgresDatabase')
    async def test_get_mission_plan_completed_mission(self, mock_db_client, mock_graph_client):
        """Test get_mission_plan for a completed mission with timestamps."""
        from cloud_common.objects import mission as mission_object
        import datetime

        mock_mission = Mock(spec=mission_object.MissionObjectV1)
        mock_mission.planned_path = None
        mock_mission.planned_path = None
        mock_mission.planned_path = None
        mock_mission.name = "completed_mission"
        mock_mission.robot = "robot_1"

        start_time = datetime.datetime(2025, 1, 4, 12, 0, 0)
        end_time = datetime.datetime(2025, 1, 4, 12, 5, 30)

        mock_status = Mock()
        mock_status.state = Mock()
        mock_status.state.value = "COMPLETED"
        mock_status.start_timestamp = start_time
        mock_status.end_timestamp = end_time
        mock_mission.status = mock_status

        mock_waypoint = common.Pose2D(x=10.0, y=20.0, theta=0.0, map_id="default")

        mock_route = Mock()
        mock_route.waypoints = [mock_waypoint]

        mock_node = Mock()
        mock_node.route = mock_route

        mock_mission.mission_tree = [mock_node]

        mock_db_instance = AsyncMock(spec=PostgresDatabase)
        mock_db_instance.get_object = AsyncMock()
        mock_db_instance.create_object = AsyncMock()
        mock_db_instance.get_object.return_value = mock_mission
        mock_db_client.return_value = mock_db_instance

        mock_graph_instance = Mock()
        mock_graph_instance.shortest_path = Mock()
        mock_graph_instance.k_nearest_neighbors = Mock()
        mock_graph_instance.nodes_in_range = Mock()
        mock_graph_instance.get_node = Mock()
        mock_graph_instance.k_nearest_neighbors.return_value = [
            {'node': {'node_id': 'node_1'}, 'distance': 0.0}
        ]
        mock_graph_client.return_value = mock_graph_instance

        service = MissionPlannerService()

        result = await service.get_mission_plan("completed_mission")

        assert result is not None
        assert result["state"] == "COMPLETED"
        assert result["created_at"] == start_time.isoformat()
        assert result["updated_at"] == end_time.isoformat()
