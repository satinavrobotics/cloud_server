"""
Unit tests for ApiDelegationService and WebSocketManager.

These tests mock the service dependencies to test the API delegation logic.
"""

import pytest
import asyncio
from unittest.mock import Mock, MagicMock, patch, AsyncMock
from packages.api.server import ApiDelegationService, WebSocketManager


@pytest.mark.unit
class TestWebSocketManagerInit:
    """Test WebSocketManager initialization."""
    
    async def test_manager_init(self):
        """Test that WebSocketManager initializes correctly."""
        manager = WebSocketManager()
        
        assert "map_updates" in manager.connections
        assert "mission_status" in manager.connections
        assert "robot_status" in manager.connections
        assert len(manager.connections["map_updates"]) == 0


@pytest.mark.unit
class TestWebSocketManagerConnect:
    """Test WebSocketManager.connect() method."""
    
    @pytest.mark.asyncio
    async def test_connect_websocket(self):
        """Test connecting a WebSocket."""
        manager = WebSocketManager()
        mock_websocket = AsyncMock()
        
        await manager.connect(mock_websocket, "map_updates", "map_1")
        
        mock_websocket.accept.assert_called_once()
        assert "map_1" in manager.connections["map_updates"]
        assert mock_websocket in manager.connections["map_updates"]["map_1"]


@pytest.mark.unit
class TestWebSocketManagerDisconnect:
    """Test WebSocketManager.disconnect() method."""
    
    @pytest.mark.asyncio
    async def test_disconnect_websocket(self):
        """Test disconnecting a WebSocket."""
        manager = WebSocketManager()
        mock_websocket = AsyncMock()
        
        # First connect
        await manager.connect(mock_websocket, "map_updates", "map_1")
        
        # Then disconnect
        manager.disconnect(mock_websocket, "map_updates", "map_1")
        
        assert "map_1" not in manager.connections["map_updates"]


@pytest.mark.unit
class TestWebSocketManagerBroadcast:
    """Test WebSocketManager.broadcast() method."""
    
    @pytest.mark.asyncio
    async def test_broadcast_message(self):
        """Test broadcasting a message to all connections."""
        manager = WebSocketManager()
        mock_ws1 = AsyncMock()
        mock_ws2 = AsyncMock()
        
        # Connect two websockets
        await manager.connect(mock_ws1, "map_updates", "map_1")
        await manager.connect(mock_ws2, "map_updates", "map_1")
        
        # Broadcast message
        message = {"type": "update", "data": "test"}
        await manager.broadcast("map_updates", "map_1", message)
        
        # Both should receive the message
        mock_ws1.send_json.assert_called_once_with(message)
        mock_ws2.send_json.assert_called_once_with(message)


@pytest.mark.unit
class TestApiDelegationServiceInit:
    """Test ApiDelegationService initialization."""
    
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.api.server.TopomapDatabaseClient')
    async def test_service_init_default_values(self, mock_topomap, mock_planner, mock_db):
        """Test that service initializes with default values."""
        service = ApiDelegationService()

        assert service.default_map_id == "default"
        assert service.arango_host is not None
        assert service.minio_host == "localhost"
        assert service.mission_planner_url == "http://localhost:8005"
        assert "postgresql://" in service.database_url
    
    @pytest.mark.asyncio
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.api.server.TopomapDatabaseClient')
    async def test_service_init_custom_values(self, mock_topomap, mock_planner, mock_db):
        """Test that service initializes with custom values."""
        service = ApiDelegationService(
            arango_host="192.168.1.100",
            minio_host="192.168.1.100",
            mission_planner_url="http://192.168.1.100:8005",
            postgres_host="192.168.1.100",
            postgres_port=5000,
            default_map_id="custom_map"
        )

        assert service.default_map_id == "custom_map"
        assert service.arango_host == "192.168.1.100"


@pytest.mark.unit
class TestApiDelegationLoadMap:
    """Test ApiDelegationService.load_map() method."""

    @pytest.mark.asyncio
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.api.server.TopomapDatabaseClient')
    async def test_load_map_success(self, mock_topomap, mock_planner, mock_db):
        """Test successful map loading."""
        mock_db.return_value = AsyncMock()
        # Mock graph database client
        mock_graph_instance = Mock()
        mock_graph_instance.create_map.return_value = {"success": True}
        mock_graph_instance.get_map_stats.return_value = {"node_count": 0, "edge_count": 0}
        mock_topomap.return_value.graph = mock_graph_instance

        service = ApiDelegationService()
        result = await service.load_map(map_id="test_map")

        assert result["success"] is True
        assert result["map_id"] == "test_map"
        assert "stats" in result

    @pytest.mark.asyncio
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.api.server.TopomapDatabaseClient')
    async def test_load_map_failure(self, mock_topomap, mock_planner, mock_db):
        """Test map loading failure."""
        # Mock graph database client to raise exception on create_map
        mock_graph_instance = Mock()
        mock_graph_instance.create_map.side_effect = Exception("Database error")
        mock_topomap.return_value.graph = mock_graph_instance

        service = ApiDelegationService()
        result = await service.load_map(map_id="test_map")

        assert result["success"] is False
        assert "error" in result

    @pytest.mark.asyncio
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.api.server.TopomapDatabaseClient')
    async def test_load_map_with_data(self, mock_topomap, mock_planner, mock_db):
        """Test loading map with nodes and edges."""
        mock_db.return_value = AsyncMock()
        mock_graph_instance = Mock()
        mock_graph_instance.create_map.return_value = {"success": True}
        mock_graph_instance.add_node.return_value = {"success": True}
        mock_graph_instance.add_edge.return_value = {"success": True}
        mock_graph_instance.get_map_stats.return_value = {"node_count": 2, "edge_count": 1}
        mock_topomap.return_value.graph = mock_graph_instance

        service = ApiDelegationService()
        map_data = {
            "map_id": "test_map",
            "nodes": [
                {"id": "1", "x": 0.0, "y": 0.0, "theta": 0.0},
                {"id": "2", "x": 1.0, "y": 1.0, "theta": 1.57}
            ],
            "edges": [
                {"from": "1", "to": "2", "weight": 1.0}
            ]
        }
        result = await service.load_map(map_data)

        assert result["success"] is True
        assert result["map_id"] == "test_map"
        assert mock_graph_instance.add_node.call_count == 2
        assert mock_graph_instance.add_edge.call_count == 1

    @pytest.mark.asyncio
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.api.server.TopomapDatabaseClient')
    async def test_load_map_with_images(self, mock_topomap, mock_planner, mock_db):
        """Test loading map with node images."""
        import base64
        mock_db.return_value = AsyncMock()
        mock_graph_instance = Mock()
        mock_graph_instance.create_map.return_value = {"success": True}
        mock_graph_instance.add_node.return_value = {"success": True}
        mock_graph_instance.get_map_stats.return_value = {"node_count": 1, "edge_count": 0}
        mock_topomap.return_value.graph = mock_graph_instance

        mock_image_instance = Mock()
        mock_image_instance.store_image.return_value = True
        mock_topomap.return_value.image = mock_image_instance

        service = ApiDelegationService()

        # Create base64 encoded fake image
        fake_image = b"fake_image_data"
        encoded_image = base64.b64encode(fake_image).decode('utf-8')

        map_data = {
            "map_id": "test_map",
            "nodes": [
                {"id": "1", "x": 0.0, "y": 0.0, "theta": 0.0, "image": encoded_image}
            ],
            "edges": []
        }
        result = await service.load_map(map_data)

        assert result["success"] is True
        mock_image_instance.store_image.assert_called_once()

    @pytest.mark.asyncio
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.api.server.TopomapDatabaseClient')
    async def test_load_map_image_save_fails(self, mock_topomap, mock_planner, mock_db):
        """Test loading map when image save fails."""
        import base64
        mock_db.return_value = AsyncMock()
        mock_graph_instance = Mock()
        mock_graph_instance.create_map.return_value = {"success": True}
        mock_graph_instance.add_node.return_value = {"success": True}
        mock_graph_instance.get_map_stats.return_value = {"node_count": 1, "edge_count": 0}
        mock_topomap.return_value.graph = mock_graph_instance

        mock_image_instance = Mock()
        mock_image_instance.store_image.return_value = False
        mock_topomap.return_value.image = mock_image_instance

        service = ApiDelegationService()

        fake_image = b"fake_image_data"
        encoded_image = base64.b64encode(fake_image).decode('utf-8')

        map_data = {
            "map_id": "test_map",
            "nodes": [
                {"id": "1", "x": 0.0, "y": 0.0, "theta": 0.0, "image": encoded_image}
            ],
            "edges": []
        }
        result = await service.load_map(map_data)

        # Map should still load successfully even if image save fails
        assert result["success"] is True

    @pytest.mark.asyncio
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.api.server.TopomapDatabaseClient')
    async def test_load_map_image_decode_fails(self, mock_topomap, mock_planner, mock_db):
        """Test loading map when image decode fails."""
        mock_db.return_value = AsyncMock()
        mock_graph_instance = Mock()
        mock_graph_instance.create_map.return_value = {"success": True}
        mock_graph_instance.add_node.return_value = {"success": True}
        mock_graph_instance.get_map_stats.return_value = {"node_count": 1, "edge_count": 0}
        mock_topomap.return_value.graph = mock_graph_instance

        service = ApiDelegationService()

        map_data = {
            "map_id": "test_map",
            "nodes": [
                {"id": "1", "x": 0.0, "y": 0.0, "theta": 0.0, "image": "invalid_base64!!!"}
            ],
            "edges": []
        }
        result = await service.load_map(map_data)

        # Map should still load successfully even if image decode fails
        assert result["success"] is True

    @pytest.mark.asyncio
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    async def test_load_map_create_fails(self, mock_graph, mock_image, mock_planner, mock_db):
        """Test loading map when create_map fails."""
        mock_graph_instance = Mock()
        mock_graph_instance.create_map.return_value = False
        mock_graph.return_value = mock_graph_instance

        service = ApiDelegationService()
        result = await service.load_map(map_id="test_map")

        assert result["success"] is False
        assert result["error"] == "Failed to create map"

    @pytest.mark.asyncio
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    async def test_load_map_stats_exception(self, mock_graph, mock_image, mock_planner, mock_db):
        """Test loading map when get_map_stats raises exception."""
        mock_db.return_value = AsyncMock()
        mock_graph_instance = Mock()
        mock_graph_instance.create_map.return_value = {"success": True}
        mock_graph_instance.get_map_stats.side_effect = Exception("Stats error")
        mock_graph.return_value = mock_graph_instance

        service = ApiDelegationService()
        result = await service.load_map(map_id="test_map")

        # Should still succeed with fallback stats
        assert result["success"] is True
        assert "stats" in result


@pytest.mark.unit
class TestApiDelegationNavigate:
    """Test ApiDelegationService.navigate() method."""
    
    @pytest.mark.asyncio
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    async def test_navigate_success(self, mock_graph, mock_image, mock_planner, mock_db):
        """Test successful navigation request."""
        # Mock mission planner client
        mock_planner_instance = Mock()
        mock_planner_instance.navigate.return_value = {
            "success": True,
            "mission_name": "mission_1",
            "path": [1, 2, 3, 4]
        }
        mock_planner.return_value = mock_planner_instance
        
        service = ApiDelegationService()
        result = await service.navigate(
            robot_name="robot_1",
            target_x=50.0,
            target_y=60.0
        )
        
        assert result["success"] is True
        assert "mission_name" in result


@pytest.mark.unit
class TestApiDelegationRobotCRUD:
    """Test Robot CRUD operations in ApiDelegationService."""

    @pytest.mark.asyncio
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    @pytest.mark.asyncio
    async def test_list_robots_success(self, mock_graph, mock_image, mock_planner, mock_db):
        """Test successful robot listing."""
        # Mock database client
        mock_db_instance = AsyncMock()
        from cloud_common.objects.robot import RobotObjectV1
        mock_db_instance.list_objects.return_value = [
            RobotObjectV1(name="robot_1", status={"battery_level": 85.0, "online": True}),
            RobotObjectV1(name="robot_2", status={"battery_level": 50.0, "online": False})
        ]
        mock_db.return_value = mock_db_instance

        service = ApiDelegationService()
        result = await service.database.list_objects(RobotObjectV1)

        assert len(result) == 2
        assert result[0].name == "robot_1"
        mock_db_instance.list_objects.assert_called_once()

    @pytest.mark.asyncio
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    @pytest.mark.asyncio
    async def test_list_robots_with_filters(self, mock_graph, mock_image, mock_planner, mock_db):
        """Test robot listing with query filters."""
        # Mock database client
        mock_db_instance = AsyncMock()
        from cloud_common.objects.robot import RobotObjectV1
        mock_db_instance.list_objects.return_value = [
            RobotObjectV1(name="robot_1", status={"battery_level": 85.0, "online": True})
        ]
        mock_db.return_value = mock_db_instance

        service = ApiDelegationService()
        params = {"min_battery": 50, "online": True}
        result = await service.database.list_objects(RobotObjectV1, query_params=params)

        assert len(result) == 1
        assert result[0].status.battery_level >= 50
        mock_db_instance.list_objects.assert_called_once_with(RobotObjectV1, query_params=params)

    @pytest.mark.asyncio
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    @pytest.mark.asyncio
    async def test_get_robot_success(self, mock_graph, mock_image, mock_planner, mock_db):
        """Test successful robot retrieval."""
        # Mock database client
        mock_db_instance = AsyncMock()
        from cloud_common.objects.robot import RobotObjectV1
        mock_db_instance.get_object.return_value = RobotObjectV1(
            name="robot_1",
            status={"battery_level": 85.0, "state": "IDLE"}
        )
        mock_db.return_value = mock_db_instance

        service = ApiDelegationService()
        result = await service.database.get_object(RobotObjectV1, "robot_1")

        assert result.name == "robot_1"
        assert result.status.state.value == "IDLE"
        mock_db_instance.get_object.assert_called_once_with(RobotObjectV1, "robot_1")

    @pytest.mark.asyncio
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    @pytest.mark.asyncio
    async def test_get_robot_not_found(self, mock_graph, mock_image, mock_planner, mock_db):
        """Test robot retrieval when robot doesn't exist."""
        # Mock database client to raise exception
        mock_db_instance = AsyncMock()
        mock_db_instance.get_object.side_effect = Exception("Robot not found")
        mock_db.return_value = mock_db_instance

        service = ApiDelegationService()
        from cloud_common.objects.robot import RobotObjectV1

        with pytest.raises(Exception, match="Robot not found"):
            await service.database.get_object(RobotObjectV1, "nonexistent_robot")

    @pytest.mark.asyncio
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    @pytest.mark.asyncio
    async def test_create_robot_success(self, mock_graph, mock_image, mock_planner, mock_db):
        """Test successful robot creation."""
        # Mock database client
        mock_db_instance = AsyncMock()
        from cloud_common.objects.robot import RobotObjectV1
        mock_db_instance.create_object.return_value = RobotObjectV1(name="robot_new", status={})
        mock_db.return_value = mock_db_instance

        service = ApiDelegationService()
        robot = RobotObjectV1(
            name="robot_new",
            status={"battery_level": 100.0, "state": "IDLE"}
        )
        import uuid
        publisher_id = uuid.uuid4()
        result = await service.database.create_object(robot, publisher_id)

        assert result.name == "robot_new"
        mock_db_instance.create_object.assert_called_once()

    @pytest.mark.asyncio
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    @pytest.mark.asyncio
    async def test_update_robot_success(self, mock_graph, mock_image, mock_planner, mock_db):
        """Test successful robot update."""
        # Mock database client
        mock_db_instance = AsyncMock()
        mock_db_instance.update_status.return_value = {"success": True}
        mock_db.return_value = mock_db_instance

        service = ApiDelegationService()
        from cloud_common.objects.robot import RobotObjectV1
        status_data = {"battery_level": 75.0, "state": "ON_TASK"}
        import uuid
        publisher_id = uuid.uuid4()
        result = await service.database.update_status(RobotObjectV1, "robot_1", status_data, publisher_id)

        assert result["success"] is True
        mock_db_instance.update_status.assert_called_once()

    @pytest.mark.asyncio
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    @pytest.mark.asyncio
    async def test_delete_robot_success(self, mock_graph, mock_image, mock_planner, mock_db):
        """Test successful robot deletion."""
        # Mock database client
        mock_db_instance = AsyncMock()
        from cloud_common.objects.robot import RobotObjectV1
        from cloud_common.objects.object import ObjectLifecycleV1
        mock_db_instance.set_lifecycle.return_value = {"success": True, "message": "Robot deleted"}
        mock_db.return_value = mock_db_instance

        service = ApiDelegationService()
        import uuid
        publisher_id = uuid.uuid4()
        result = await service.database.set_lifecycle(RobotObjectV1, "robot_1", ObjectLifecycleV1.DELETED, publisher_id)

        assert result["success"] is True
        mock_db_instance.set_lifecycle.assert_called_once()


@pytest.mark.unit
class TestApiDelegationMissionCRUD:
    """Test Mission CRUD operations in ApiDelegationService."""

    @pytest.mark.asyncio
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    @pytest.mark.asyncio
    async def test_list_missions_success(self, mock_graph, mock_image, mock_planner, mock_db):
        """Test successful mission listing."""
        # Mock database client
        mock_db_instance = AsyncMock()
        from cloud_common.objects.mission import MissionObjectV1
        mock_db_instance.list_objects.return_value = [
            MissionObjectV1(name="mission_1", robot="robot_1", mission_tree=[{"sequence": {}}], status={}),
            MissionObjectV1(name="mission_2", robot="robot_2", mission_tree=[{"sequence": {}}], status={})
        ]
        mock_db.return_value = mock_db_instance

        service = ApiDelegationService()
        result = await service.database.list_objects(MissionObjectV1)

        assert len(result) == 2
        assert result[0].name == "mission_1"
        mock_db_instance.list_objects.assert_called_once()

    @pytest.mark.asyncio
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    @pytest.mark.asyncio
    async def test_get_mission_success(self, mock_graph, mock_image, mock_planner, mock_db):
        """Test successful mission retrieval."""
        # Mock database client
        mock_db_instance = AsyncMock()
        from cloud_common.objects.mission import MissionObjectV1
        mock_db_instance.get_object.return_value = MissionObjectV1(
            name="mission_1",
            robot="robot_1",
            mission_tree=[{"sequence": {}}],
            status={"state": "RUNNING", "current_node": 2}
        )
        mock_db.return_value = mock_db_instance

        service = ApiDelegationService()
        result = await service.database.get_object(MissionObjectV1, "mission_1")

        assert result.name == "mission_1"
        assert result.status.state.value == "RUNNING"
        mock_db_instance.get_object.assert_called_once_with(MissionObjectV1, "mission_1")

    @pytest.mark.asyncio
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    @pytest.mark.asyncio
    async def test_create_mission_success(self, mock_graph, mock_image, mock_planner, mock_db):
        """Test successful mission creation."""
        # Mock database client
        mock_db_instance = AsyncMock()
        from cloud_common.objects.mission import MissionObjectV1
        mock_db_instance.create_object.return_value = MissionObjectV1(name="mission_new", robot="robot_1", mission_tree=[{"sequence": {}}], status={})
        mock_db.return_value = mock_db_instance

        service = ApiDelegationService()
        mission = MissionObjectV1(
            name="mission_new",
            robot="robot_1",
            mission_tree=[{"sequence": {}}],
            status={"state": "PENDING"}
        )
        import uuid
        publisher_id = uuid.uuid4()
        result = await service.database.create_object(mission, publisher_id)

        assert result.name == "mission_new"
        mock_db_instance.create_object.assert_called_once()

    @pytest.mark.asyncio
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    @pytest.mark.asyncio
    async def test_update_mission_success(self, mock_graph, mock_image, mock_planner, mock_db):
        """Test successful mission update."""
        # Mock database client
        mock_db_instance = AsyncMock()
        mock_db_instance.update_status.return_value = {"success": True}
        mock_db.return_value = mock_db_instance

        service = ApiDelegationService()
        from cloud_common.objects.mission import MissionObjectV1
        status_data = {"state": "RUNNING", "current_node": 3}
        import uuid
        publisher_id = uuid.uuid4()
        result = await service.database.update_status(MissionObjectV1, "mission_1", status_data, publisher_id)

        assert result["success"] is True
        mock_db_instance.update_status.assert_called_once()

    @pytest.mark.asyncio
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    @pytest.mark.asyncio
    async def test_cancel_mission_success(self, mock_graph, mock_image, mock_planner, mock_db):
        """Test successful mission cancellation."""
        # Mock database client
        mock_db_instance = AsyncMock()
        from cloud_common.objects.mission import MissionObjectV1
        from cloud_common.objects.object import ObjectLifecycleV1
        mock_db_instance.set_lifecycle.return_value = {"success": True}
        mock_db.return_value = mock_db_instance

        service = ApiDelegationService()
        import uuid
        publisher_id = uuid.uuid4()
        result = await service.database.set_lifecycle(MissionObjectV1, "mission_1", ObjectLifecycleV1.DELETED, publisher_id)

        assert result["success"] is True
        mock_db_instance.set_lifecycle.assert_called_once()

    @pytest.mark.asyncio
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    @pytest.mark.asyncio
    async def test_delete_mission_success(self, mock_graph, mock_image, mock_planner, mock_db):
        """Test successful mission deletion."""
        # Mock database client
        mock_db_instance = AsyncMock()
        from cloud_common.objects.mission import MissionObjectV1
        from cloud_common.objects.object import ObjectLifecycleV1
        mock_db_instance.set_lifecycle.return_value = {"success": True, "message": "Mission deleted"}
        mock_db.return_value = mock_db_instance

        service = ApiDelegationService()
        import uuid
        publisher_id = uuid.uuid4()
        result = await service.database.set_lifecycle(MissionObjectV1, "mission_1", ObjectLifecycleV1.DELETED, publisher_id)

        assert result["success"] is True
        mock_db_instance.set_lifecycle.assert_called_once()


@pytest.mark.unit
class TestApiDelegationNavigate:
    """Test navigation operations in ApiDelegationService."""

    @pytest.mark.asyncio
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    async def test_navigate_no_path_found(self, mock_graph, mock_image, mock_planner, mock_db):
        """Test navigation when no path is found."""
        # Mock database client
        mock_db_instance = AsyncMock()
        from cloud_common.objects.robot import RobotObjectV1
        mock_db_instance.get_object.return_value = RobotObjectV1(name="robot_1", status={})
        mock_db.return_value = mock_db_instance

        # Mock mission planner client to raise exception
        mock_planner_instance = Mock()
        mock_planner_instance.navigate.side_effect = Exception("No path found")
        mock_planner.return_value = mock_planner_instance

        service = ApiDelegationService()
        result = await service.navigate(
            robot_name="robot_1",
            target_x=50.0,
            target_y=60.0
        )

        assert result["success"] is False
        assert "error" in result


@pytest.mark.unit
class TestWebSocketManagerBroadcastErrors:
    """Test WebSocketManager.broadcast() error handling."""

    @pytest.mark.asyncio
    async def test_broadcast_unknown_connection_type(self):
        """Test broadcasting to unknown connection type."""
        manager = WebSocketManager()
        message = {"type": "update", "data": "test"}

        # Should not raise exception, just return
        await manager.broadcast("unknown_type", "map_1", message)

    @pytest.mark.asyncio
    async def test_broadcast_unknown_identifier(self):
        """Test broadcasting to unknown identifier."""
        manager = WebSocketManager()
        message = {"type": "update", "data": "test"}

        # Should not raise exception, just return
        await manager.broadcast("map_updates", "nonexistent_map", message)

    @pytest.mark.asyncio
    async def test_broadcast_with_failed_websocket(self):
        """Test broadcasting when one websocket fails."""
        manager = WebSocketManager()
        mock_ws1 = AsyncMock()
        mock_ws2 = AsyncMock()

        # First websocket fails, second succeeds
        mock_ws1.send_json.side_effect = Exception("Connection lost")
        mock_ws2.send_json.return_value = None

        # Connect both websockets
        await manager.connect(mock_ws1, "map_updates", "map_1")
        await manager.connect(mock_ws2, "map_updates", "map_1")

        # Broadcast message
        message = {"type": "update", "data": "test"}
        await manager.broadcast("map_updates", "map_1", message)

        # Both should have been attempted
        mock_ws1.send_json.assert_called_once_with(message)
        mock_ws2.send_json.assert_called_once_with(message)

        # Failed websocket should be disconnected
        assert mock_ws1 not in manager.connections["map_updates"].get("map_1", set())


@pytest.mark.unit
class TestWebSocketManagerBroadcastToAll:
    """Test WebSocketManager.broadcast_to_all() method."""

    @pytest.mark.asyncio
    async def test_broadcast_to_all_success(self):
        """Test broadcasting to all identifiers of a type."""
        manager = WebSocketManager()
        mock_ws1 = AsyncMock()
        mock_ws2 = AsyncMock()
        mock_ws3 = AsyncMock()

        # Connect to different maps
        await manager.connect(mock_ws1, "map_updates", "map_1")
        await manager.connect(mock_ws2, "map_updates", "map_2")
        await manager.connect(mock_ws3, "map_updates", "map_2")

        # Broadcast to all
        message = {"type": "global_update", "data": "test"}
        await manager.broadcast_to_all("map_updates", message)

        # All should receive the message
        mock_ws1.send_json.assert_called_once_with(message)
        mock_ws2.send_json.assert_called_once_with(message)
        mock_ws3.send_json.assert_called_once_with(message)

    @pytest.mark.asyncio
    async def test_broadcast_to_all_unknown_type(self):
        """Test broadcasting to all with unknown connection type."""
        manager = WebSocketManager()
        message = {"type": "update", "data": "test"}

        # Should not raise exception
        await manager.broadcast_to_all("unknown_type", message)


@pytest.mark.unit
class TestWebSocketManagerConnect:
    """Test WebSocketManager.connect() edge cases."""

    @pytest.mark.asyncio
    async def test_connect_unknown_connection_type(self):
        """Test connecting with unknown connection type."""
        manager = WebSocketManager()
        mock_websocket = AsyncMock()

        await manager.connect(mock_websocket, "unknown_type", "id_1")

        # Should accept but log warning
        mock_websocket.accept.assert_called_once()
        # Connection should not be added
        assert "unknown_type" not in manager.connections or "id_1" not in manager.connections.get("unknown_type", {})


@pytest.mark.unit
class TestApiDelegationServiceHealthCheck:
    """Test ApiDelegationService.is_healthy() method."""

    @pytest.mark.asyncio
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    async def test_is_healthy_all_services_healthy(self, mock_graph, mock_image, mock_planner, mock_db):
        """Test health check when all services are healthy."""
        # Mock all clients as healthy
        mock_graph_instance = Mock()
        mock_graph_instance.is_healthy.return_value = True
        mock_graph.return_value = mock_graph_instance

        mock_image_instance = Mock()
        mock_image_instance.is_healthy.return_value = True
        mock_image.return_value = mock_image_instance

        mock_db_instance = Mock()
        mock_db_instance.is_running.return_value = True
        mock_db.return_value = mock_db_instance

        service = ApiDelegationService()
        result = service.is_healthy()

        assert result is True

    @pytest.mark.asyncio
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    async def test_is_healthy_graph_db_unhealthy(self, mock_graph, mock_image, mock_planner, mock_db):
        """Test health check when graph DB is unhealthy."""
        mock_graph_instance = Mock()
        mock_graph_instance.is_healthy.return_value = False
        mock_graph.return_value = mock_graph_instance

        mock_image_instance = Mock()
        mock_image_instance.is_healthy.return_value = True
        mock_image.return_value = mock_image_instance

        mock_db_instance = Mock()
        mock_db_instance.is_running.return_value = True
        mock_db.return_value = mock_db_instance

        service = ApiDelegationService()
        result = service.is_healthy()

        assert result is False

    @pytest.mark.asyncio
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    async def test_is_healthy_exception(self, mock_graph, mock_image, mock_planner, mock_db):
        """Test health check when exception occurs."""
        mock_graph_instance = Mock()
        mock_graph_instance.is_healthy.side_effect = Exception("Connection error")
        mock_graph.return_value = mock_graph_instance

        service = ApiDelegationService()
        result = service.is_healthy()

        assert result is False


@pytest.mark.unit
class TestApiDelegationServiceGetStats:
    """Test ApiDelegationService.get_stats() method."""

    @pytest.mark.asyncio
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    async def test_get_stats_success(self, mock_graph, mock_image, mock_planner, mock_db):
        """Test getting service statistics."""
        service = ApiDelegationService(
            arango_host="localhost",
            minio_host="localhost",
            mission_planner_url="http://localhost:8005",
            postgres_host="localhost",
            postgres_port=5000,
            default_map_id="test_map"
        )

        result = service.get_stats()

        assert result["service"] == "api_delegation"
        assert result["graph_db_url"] == "arangodb://localhost:8529"
        assert result["minio_host"] == "localhost"
        assert result["mission_planner_url"] == "http://localhost:8005"
        assert "postgresql://localhost:5000" in result["database_url"]
        assert result["default_map_id"] == "test_map"
        assert "websocket_connections" in result
        assert result["websocket_connections"]["map_updates"] == 0
        assert result["websocket_connections"]["mission_status"] == 0
        assert result["websocket_connections"]["robot_status"] == 0


@pytest.mark.unit
class TestApiDelegationServiceGetMapStatus:
    """Test ApiDelegationService.get_map_status() method."""

    @pytest.mark.asyncio
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    async def test_get_map_status_success(self, mock_graph, mock_image, mock_planner, mock_db):
        """Test getting map status successfully."""
        mock_graph_instance = Mock()
        mock_graph_instance.get_map_stats.return_value = {
            "node_count": 100,
            "edge_count": 250
        }
        mock_graph.return_value = mock_graph_instance

        service = ApiDelegationService()
        result = await service.get_map_status("test_map")

        assert result["map_id"] == "test_map"
        assert result["node_count"] == 100
        assert result["edge_count"] == 250
        assert result["status"] == "ready"

    @pytest.mark.asyncio
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    async def test_get_map_status_default_map(self, mock_graph, mock_image, mock_planner, mock_db):
        """Test getting map status with default map ID."""
        mock_graph_instance = Mock()
        mock_graph_instance.get_map_stats.return_value = {
            "node_count": 50,
            "edge_count": 100
        }
        mock_graph.return_value = mock_graph_instance

        service = ApiDelegationService(default_map_id="default")
        result = await service.get_map_status()

        assert result["map_id"] == "default"
        assert result["node_count"] == 50

    @pytest.mark.asyncio
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    async def test_get_map_status_error_in_stats(self, mock_graph, mock_image, mock_planner, mock_db):
        """Test getting map status when stats contain error."""
        mock_graph_instance = Mock()
        mock_graph_instance.get_map_stats.return_value = {
            "error": "Map not found"
        }
        mock_graph.return_value = mock_graph_instance

        service = ApiDelegationService()
        result = await service.get_map_status("nonexistent_map")

        assert result["success"] is False
        assert "error" in result

    @pytest.mark.asyncio
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    async def test_get_map_status_exception(self, mock_graph, mock_image, mock_planner, mock_db):
        """Test getting map status when exception occurs."""
        mock_graph_instance = Mock()
        mock_graph_instance.get_map_stats.side_effect = Exception("Connection error")
        mock_graph.return_value = mock_graph_instance

        service = ApiDelegationService()
        result = await service.get_map_status("test_map")

        assert result["success"] is False
        assert "error" in result


@pytest.mark.unit
class TestApiDelegationServiceUpdateNode:
    """Test ApiDelegationService.update_node() method."""

    @pytest.mark.asyncio
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    async def test_update_node_success(self, mock_graph, mock_image, mock_planner, mock_db):
        """Test updating node successfully."""
        mock_graph_instance = Mock()
        mock_graph_instance.update_node.return_value = {"success": True}
        mock_graph.return_value = mock_graph_instance

        service = ApiDelegationService()
        result = await service.update_node(
            map_id="test_map",
            node_id="node_1",
            x=10.0,
            y=20.0,
            theta=1.57
        )

        assert result["success"] is True
        mock_graph_instance.update_node.assert_called_once()

    @pytest.mark.asyncio
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    async def test_update_node_with_metadata(self, mock_graph, mock_image, mock_planner, mock_db):
        """Test updating node with metadata."""
        mock_graph_instance = Mock()
        mock_graph_instance.update_node.return_value = {"success": True}
        mock_graph.return_value = mock_graph_instance

        service = ApiDelegationService()
        result = await service.update_node(
            map_id="test_map",
            node_id="node_1",
            metadata={"label": "checkpoint"}
        )

        assert result["success"] is True

    @pytest.mark.asyncio
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    async def test_update_node_exception(self, mock_graph, mock_image, mock_planner, mock_db):
        """Test updating node when exception occurs."""
        mock_graph_instance = Mock()
        mock_graph_instance.update_node.side_effect = Exception("Update failed")
        mock_graph.return_value = mock_graph_instance

        service = ApiDelegationService()
        result = await service.update_node(
            map_id="test_map",
            node_id="node_1",
            x=10.0
        )

        assert result["success"] is False
        assert "error" in result


@pytest.mark.unit
class TestApiDelegationServiceDeleteMap:
    """Test ApiDelegationService.delete_map() method."""

    @pytest.mark.asyncio
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    async def test_delete_map_success(self, mock_graph, mock_image, mock_planner, mock_db):
        """Test deleting map successfully."""
        mock_graph_instance = Mock()
        mock_graph_instance.delete_map.return_value = {"success": True}
        mock_graph.return_value = mock_graph_instance

        mock_image_instance = Mock()
        mock_image_instance.delete_map.return_value = {"success": True}
        mock_image.return_value = mock_image_instance

        service = ApiDelegationService()
        result = await service.delete_map("test_map")

        assert result["success"] is True
        assert result["map_id"] == "test_map"
        mock_graph_instance.delete_map.assert_called_once_with("test_map")
        mock_image_instance.delete_map.assert_called_once_with("test_map")

    @pytest.mark.asyncio
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    async def test_delete_map_graph_db_fails(self, mock_graph, mock_image, mock_planner, mock_db):
        """Test deleting map when graph DB deletion fails."""
        mock_graph_instance = Mock()
        mock_graph_instance.delete_map.return_value = {"success": False}
        mock_graph.return_value = mock_graph_instance

        mock_image_instance = Mock()
        mock_image_instance.delete_map.return_value = {"success": True}
        mock_image.return_value = mock_image_instance

        service = ApiDelegationService()
        result = await service.delete_map("test_map")

        assert result["success"] is False
        assert "error" in result

    @pytest.mark.asyncio
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    async def test_delete_map_image_db_fails(self, mock_graph, mock_image, mock_planner, mock_db):
        """Test deleting map when image DB deletion fails."""
        mock_graph_instance = Mock()
        mock_graph_instance.delete_map.return_value = {"success": True}
        mock_graph.return_value = mock_graph_instance

        mock_image_instance = Mock()
        mock_image_instance.delete_map.return_value = {"success": False}
        mock_image.return_value = mock_image_instance

        service = ApiDelegationService()
        result = await service.delete_map("test_map")

        assert result["success"] is False

    @pytest.mark.asyncio
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    async def test_delete_map_returns_boolean(self, mock_graph, mock_image, mock_planner, mock_db):
        """Test deleting map when services return boolean instead of dict."""
        mock_graph_instance = Mock()
        mock_graph_instance.delete_map.return_value = True
        mock_graph.return_value = mock_graph_instance

        mock_image_instance = Mock()
        mock_image_instance.delete_map.return_value = True
        mock_image.return_value = mock_image_instance

        service = ApiDelegationService()
        result = await service.delete_map("test_map")

        assert result["success"] is True

    @pytest.mark.asyncio
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    async def test_delete_map_exception(self, mock_graph, mock_image, mock_planner, mock_db):
        """Test deleting map when exception occurs."""
        mock_graph_instance = Mock()
        mock_graph_instance.delete_map.side_effect = Exception("Delete failed")
        mock_graph.return_value = mock_graph_instance

        service = ApiDelegationService()
        with pytest.raises(Exception, match="Delete failed"):
            await service.delete_map("test_map")


@pytest.mark.unit
class TestApiDelegationServiceLoadImage:
    """Test ApiDelegationService.load_image() method."""

    @pytest.mark.asyncio
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    async def test_load_image_success(self, mock_graph, mock_image, mock_planner, mock_db):
        """Test loading image successfully."""
        mock_image_instance = Mock()
        mock_image_instance.store_image.return_value = True
        mock_image.return_value = mock_image_instance

        service = ApiDelegationService()
        result = await service.load_image(
            map_id="test_map",
            node_id="node_1",
            image_data=b"fake_image_data"
        )

        assert result["success"] is True
        assert "image_id" in result
        mock_image_instance.store_image.assert_called_once()

    @pytest.mark.asyncio
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    async def test_load_image_with_image_id(self, mock_graph, mock_image, mock_planner, mock_db):
        """Test loading image with specific image ID."""
        mock_image_instance = Mock()
        mock_image_instance.store_image.return_value = True
        mock_image.return_value = mock_image_instance

        service = ApiDelegationService()
        result = await service.load_image(
            map_id="test_map",
            node_id="node_1",
            image_data=b"fake_image_data",
            image_id="custom_img_001"
        )

        assert result["success"] is True
        assert result["image_id"] == "custom_img_001"

    @pytest.mark.asyncio
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    async def test_load_image_default_map(self, mock_graph, mock_image, mock_planner, mock_db):
        """Test loading image with default map ID."""
        mock_image_instance = Mock()
        mock_image_instance.store_image.return_value = True
        mock_image.return_value = mock_image_instance

        service = ApiDelegationService(default_map_id="default_map")
        result = await service.load_image(
            map_id=None,
            node_id="node_1",
            image_data=b"fake_image_data"
        )

        assert result["success"] is True
        # Verify it used the default map ID
        call_args = mock_image_instance.store_image.call_args
        assert call_args[1]["map_id"] == "default_map"


@pytest.mark.unit
class TestApiDelegationServiceGetImage:
    """Test ApiDelegationService.get_image() method."""

    @pytest.mark.asyncio
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    async def test_get_image_success(self, mock_graph, mock_image, mock_planner, mock_db):
        """Test getting image successfully."""
        mock_image_instance = Mock()
        mock_image_instance.get_image.return_value = b"fake_image_data"
        mock_image.return_value = mock_image_instance

        service = ApiDelegationService()
        result = await service.get_image(
            map_id="test_map",
            node_id="node_1",
            image_id="img_001"
        )

        assert result == b"fake_image_data"
        mock_image_instance.get_image.assert_called_once()

    @pytest.mark.asyncio
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    async def test_get_image_not_found(self, mock_graph, mock_image, mock_planner, mock_db):
        """Test getting image when not found."""
        mock_image_instance = Mock()
        mock_image_instance.get_image.return_value = None
        mock_image.return_value = mock_image_instance

        service = ApiDelegationService()
        result = await service.get_image(
            map_id="test_map",
            node_id="node_1",
            image_id="nonexistent"
        )

        assert result is None

    @pytest.mark.asyncio
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    async def test_get_image_default_map(self, mock_graph, mock_image, mock_planner, mock_db):
        """Test getting image with default map ID."""
        mock_image_instance = Mock()
        mock_image_instance.get_image.return_value = b"fake_image_data"
        mock_image.return_value = mock_image_instance

        service = ApiDelegationService(default_map_id="default_map")
        result = await service.get_image(
            map_id=None,
            node_id="node_1",
            image_id="img_001"
        )

        assert result == b"fake_image_data"
        # Verify it used the default map ID
        call_args = mock_image_instance.get_image.call_args
        assert call_args[1]["map_id"] == "default_map"


@pytest.mark.unit
class TestApiDelegationServicePlanMission:
    """Test ApiDelegationService.plan_mission() method."""

    @pytest.mark.asyncio
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    async def test_plan_mission_success(self, mock_graph, mock_image, mock_planner, mock_db):
        """Test planning mission successfully."""
        mock_planner_instance = AsyncMock()
        mock_planner_instance.navigate.return_value = {
            "success": True,
            "path": [{"x": 0, "y": 0}, {"x": 1, "y": 1}]
        }
        mock_planner.return_value = mock_planner_instance

        service = ApiDelegationService()
        result = await service.plan_mission(
            robot_id="robot_1",
            goal_x=10.0,
            goal_y=20.0
        )

        assert result["success"] is True
        assert "path" in result
        mock_planner_instance.navigate.assert_called_once()

    @pytest.mark.asyncio
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    async def test_plan_mission_with_map_id(self, mock_graph, mock_image, mock_planner, mock_db):
        """Test planning mission with specific map ID."""
        mock_planner_instance = AsyncMock()
        mock_planner_instance.navigate.return_value = {
            "success": True,
            "path": []
        }
        mock_planner.return_value = mock_planner_instance

        service = ApiDelegationService()
        result = await service.plan_mission(
            robot_id="robot_1",
            goal_x=10.0,
            goal_y=20.0,
            map_id="custom_map"
        )

        assert result["success"] is True

    @pytest.mark.asyncio
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    async def test_plan_mission_exception(self, mock_graph, mock_image, mock_planner, mock_db):
        """Test planning mission when exception occurs."""
        mock_planner_instance = Mock()
        mock_planner_instance.navigate.side_effect = Exception("Planning failed")
        mock_planner.return_value = mock_planner_instance

        service = ApiDelegationService()
        result = await service.plan_mission(
            robot_id="robot_1",
            goal_x=10.0,
            goal_y=20.0
        )

        assert result["success"] is False
        assert "error" in result
