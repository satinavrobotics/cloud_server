"""
Unit tests for GraphBuilderService.

These tests mock the service dependencies (TopomapDatabaseClient)
to test the graph building logic in isolation.
"""

import pytest
import json
import base64
import asyncio
from unittest.mock import Mock, MagicMock, AsyncMock, patch, call
from packages.services.graph_builder.server import GraphBuilderService


@pytest.mark.unit
class TestGraphBuilderServiceInit:
    """Test GraphBuilderService initialization."""

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_service_init_default_values(self, mock_topomap):
        """Test that service initializes with default values."""
        service = GraphBuilderService()

        assert service.mqtt_host == "localhost"
        assert service.mqtt_port == 1883
        assert service.radius_threshold == 5.0
        assert service.default_map_id == "default"
        assert service.stats["nodes_processed"] == 0
        assert service.stats["images_saved"] == 0
        assert service.stats["edges_created"] == 0

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_service_init_custom_values(self, mock_topomap):
        """Test that service initializes with custom values."""
        service = GraphBuilderService(
            mqtt_host="192.168.1.100",
            mqtt_port=1884,
            radius_threshold=10.0,
            default_map_id="custom_map"
        )

        assert service.mqtt_host == "192.168.1.100"
        assert service.mqtt_port == 1884
        assert service.radius_threshold == 10.0
        assert service.default_map_id == "custom_map"


@pytest.mark.unit
class TestGraphBuilderGetStats:
    """Test GraphBuilderService.stats property."""

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_get_stats_initial(self, mock_topomap):
        """Test getting stats from newly initialized service."""
        service = GraphBuilderService()

        stats = service.stats
        assert stats["nodes_processed"] == 0
        assert stats["images_saved"] == 0
        assert stats["edges_created"] == 0
        assert stats["errors"] == 0

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_stats_are_mutable(self, mock_topomap):
        """Test that stats can be modified."""
        service = GraphBuilderService()

        # Manually update stats (simulating processing)
        service.stats["nodes_processed"] = 5
        service.stats["images_saved"] = 3
        service.stats["edges_created"] = 10

        assert service.stats["nodes_processed"] == 5
        assert service.stats["images_saved"] == 3
        assert service.stats["edges_created"] == 10

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_get_stats_method(self, mock_topomap):
        """Test get_stats() method returns stats with MQTT status."""
        service = GraphBuilderService()
        service._mqtt_connected = True
        service.stats["nodes_processed"] = 10

        stats = service.get_stats()

        assert stats["nodes_processed"] == 10
        assert stats["mqtt_connected"] is True
        assert stats["radius_threshold"] == 5.0




@pytest.mark.unit
class TestGraphBuilderMQTTConnection:
    """Test GraphBuilderService MQTT connection."""

    @patch('packages.services.graph_builder.server.MQTTClient')
    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_connect_mqtt_success(self, mock_topomap, mock_mqtt):
        """Test successful MQTT connection."""
        mock_mqtt_instance = Mock()
        mock_mqtt.return_value = mock_mqtt_instance

        service = GraphBuilderService()
        result = service.connect_mqtt()

        assert result is True
        mock_mqtt.assert_called_once()
        mock_mqtt_instance.connect.assert_called_once()

    @patch('packages.services.graph_builder.server.MQTTClient')
    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_connect_mqtt_failure(self, mock_topomap, mock_mqtt):
        """Test MQTT connection failure."""
        mock_mqtt_instance = Mock()
        mock_mqtt_instance.connect.side_effect = Exception("Connection failed")
        mock_mqtt.return_value = mock_mqtt_instance

        service = GraphBuilderService()
        result = service.connect_mqtt()

        assert result is False

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_disconnect_mqtt(self, mock_topomap):
        """Test MQTT disconnection."""
        service = GraphBuilderService()
        service.mqtt_client = Mock()

        service.disconnect_mqtt()

        service.mqtt_client.disconnect.assert_called_once()

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_mqtt_connected_flag_set_after_connect(self, mock_topomap):
        """Test that _mqtt_connected is False by default and can be set."""
        service = GraphBuilderService()
        assert service._mqtt_connected is False

        service._mqtt_connected = True
        assert service._mqtt_connected is True

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_mqtt_connected_false_initially(self, mock_topomap):
        """Test _mqtt_connected starts False."""
        service = GraphBuilderService()
        assert service._mqtt_connected is False

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_disconnect_mqtt_no_client(self, mock_topomap):
        """Test disconnect when no client is connected is a no-op."""
        service = GraphBuilderService()
        # mqtt_client is None by default — should not raise
        service.disconnect_mqtt()


@pytest.mark.unit
class TestGraphBuilderMQTTMessageHandling:
    """Test GraphBuilderService MQTT message handling."""

    def _make_msg(self, payload: dict):
        """Helper: create a mock MQTT message."""
        msg = Mock()
        msg.payload = json.dumps(payload).encode('utf-8')
        return msg

    @patch('packages.services.graph_builder.server.asyncio.run_coroutine_threadsafe')
    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_on_mqtt_message_valid_json(self, mock_topomap, mock_run):
        """Valid JSON with event loop dispatches one coroutine (_handle_node_update)."""
        service = GraphBuilderService()
        service._event_loop = Mock()
        node_payload = {
            "session_node_id": 1000,
            "robot_name": "robot_1",
            "x": 1.0,
            "y": 2.0,
            "yaw": 0.0,
        }
        service._on_node_update_message(None, None, self._make_msg(node_payload))

        assert mock_run.call_count == 1

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_on_mqtt_message_invalid_json(self, mock_topomap):
        """Test node update callback handles malformed payload gracefully."""
        service = GraphBuilderService()

        bad_msg = Mock()
        bad_msg.payload = b"invalid json {"
        service._on_node_update_message(None, None, bad_msg)

        assert service.stats["errors"] == 1

    @patch('packages.services.graph_builder.server.asyncio.run_coroutine_threadsafe')
    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_on_mqtt_message_processing_error(self, mock_topomap, mock_run):
        """Valid JSON still dispatches the coroutine even when topology will fail inside."""
        service = GraphBuilderService()
        service._event_loop = Mock()
        node_payload = {
            "session_node_id": 1001,
            "robot_name": "robot_1",
            "x": 1.0,
            "y": 2.0,
            "yaw": 0.0,
        }
        service._on_node_update_message(None, None, self._make_msg(node_payload))

        assert mock_run.call_count == 1

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_on_mqtt_message_image_upload(self, mock_topomap):
        """Test image upload callback buffers image when no node mapping exists."""
        service = GraphBuilderService()
        image_payload = {
            "session_node_id": 1000,
            "robot_name": "robot_1",
            "camera_name": "front_camera",
            "image_data": base64.b64encode(b"fake").decode(),
            "content_type": "image/jpeg",
            "timestamp": "2024-01-01T00:00:00",
            "map_id": "default",
        }
        service._on_image_upload_message(None, None, self._make_msg(image_payload))

        assert ("robot_1", 1000) in service.image_buffer
        assert "front_camera" in service.image_buffer[("robot_1", 1000)]

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_on_mqtt_message_unknown_topic(self, mock_topomap):
        """Missing required fields cause _process_topology to increment errors."""
        mock_graph_instance = mock_topomap.return_value.graph
        mock_graph_instance.nodes_in_range.return_value = []

        service = GraphBuilderService()
        bad_payload = {"x": 1.0, "y": 2.0}  # missing session_node_id, robot_name
        result = service._process_topology(bad_payload)

        assert result is None
        assert service.stats["errors"] == 1


@pytest.mark.unit
class TestGraphBuilderSessionMapping:
    """Test session ID to global ID mapping functionality."""

    def _make_msg(self, payload):
        from unittest.mock import Mock
        msg = Mock()
        msg.payload = json.dumps(payload).encode('utf-8')
        return msg

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_node_update_creates_global_id(self, mock_topomap):
        """Test that processing a node payload creates a global ID mapping."""
        mock_graph_instance = mock_topomap.return_value.graph
        mock_graph_instance.nodes_in_range.return_value = []
        mock_graph_instance.add_node.return_value = {"success": True}

        service = GraphBuilderService()

        node_data = {
            "session_node_id": 1000,
            "robot_name": "robot_1",
            "x": 1.0,
            "y": 2.0,
            "yaw": 0.0,
        }

        service._process_topology(node_data)

        # Check that a mapping was created
        assert len(service.session_to_global_map) == 1
        assert ("robot_1", 1000) in service.session_to_global_map
        global_id, timestamp = service.session_to_global_map[("robot_1", 1000)]
        assert isinstance(global_id, str)
        assert len(global_id) > 0  # UUID should be non-empty

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_image_upload_with_existing_mapping(self, mock_topomap):
        """Test image upload when node mapping already exists."""
        mock_image_instance = mock_topomap.return_value.image
        mock_image_instance.store_image.return_value = True

        service = GraphBuilderService()

        # Create a session mapping
        global_id = "test-global-id-123"
        from datetime import datetime
        service.session_to_global_map[("robot_1", 1000)] = (global_id, datetime.now())

        image_data = {
            "session_node_id": 1000,
            "robot_name": "robot_1",
            "camera_name": "front_camera",
            "image_data": base64.b64encode(b"fake_image_data").decode('utf-8'),
            "content_type": "image/jpeg",
            "timestamp": "2024-01-01T00:00:00",
            "map_id": "default"
        }

        service._on_image_upload_message(None, None, self._make_msg(image_data))

        # Image should be saved immediately
        mock_image_instance.store_image.assert_called_once()
        call_args = mock_image_instance.store_image.call_args
        assert call_args[1]["node_id"] == global_id

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_image_upload_buffered_when_no_mapping(self, mock_topomap):
        """Test image upload is buffered when node mapping doesn't exist yet."""
        service = GraphBuilderService()

        image_data = {
            "session_node_id": 1000,
            "robot_name": "robot_1",
            "camera_name": "front_camera",
            "image_data": base64.b64encode(b"fake_image_data").decode('utf-8'),
            "content_type": "image/jpeg",
            "timestamp": "2024-01-01T00:00:00",
            "map_id": "default"
        }

        service._on_image_upload_message(None, None, self._make_msg(image_data))

        # Image should be buffered
        assert len(service.image_buffer) == 1
        assert ("robot_1", 1000) in service.image_buffer
        assert "front_camera" in service.image_buffer[("robot_1", 1000)]

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_node_update_processes_buffered_images(self, mock_topomap):
        """Test that node update processes buffered images."""
        mock_graph_instance = mock_topomap.return_value.graph
        mock_graph_instance.nodes_in_range.return_value = []
        mock_graph_instance.add_node.return_value = {"success": True}
        mock_image_instance = mock_topomap.return_value.image
        mock_image_instance.store_image.return_value = True

        service = GraphBuilderService()

        # Buffer an image first
        from datetime import datetime
        image_dict = {
            'image_id': 'front_camera',
            'data': base64.b64encode(b"fake_image_data").decode('utf-8'),
            'content_type': 'image/jpeg',
            'metadata': {
                'camera_name': 'front_camera',
                'timestamp': '2024-01-01T00:00:00',
                'robot_name': 'robot_1',
                'session_node_id': 1000
            }
        }
        service.image_buffer[("robot_1", 1000)] = {"front_camera": (image_dict, datetime.now())}

        # Now send node update
        node_data = {
            "session_node_id": 1000,
            "robot_name": "robot_1",
            "x": 1.0,
            "y": 2.0,
            "yaw": 0.0,
        }

        service._process_topology(node_data)

        # Buffered image should be processed
        assert len(service.image_buffer) == 0  # Buffer should be cleared
        mock_image_instance.store_image.assert_called_once()


@pytest.mark.unit
class TestGraphBuilderNodeProcessing:
    """Test GraphBuilderService node processing workflow."""

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_process_node_update_missing_required_fields(self, mock_topomap):
        """Test processing node update with missing required fields."""
        service = GraphBuilderService()

        # Missing node_id
        result = service.process_node_update({"x": 1.0, "y": 2.0})
        assert result["success"] is False
        assert "Missing required fields" in result["error"]
        assert service.stats["errors"] == 1

        # Missing x
        result = service.process_node_update({"node_id": "node_1", "y": 2.0})
        assert result["success"] is False
        assert service.stats["errors"] == 2

        # Missing y
        result = service.process_node_update({"node_id": "node_1", "x": 1.0})
        assert result["success"] is False
        assert service.stats["errors"] == 3

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_process_node_update_success_no_images(self, mock_topomap):
        """Test successful node processing without images."""
        # Setup mocks
        mock_graph_instance = mock_topomap.return_value.graph
        mock_graph_instance.nodes_in_range.return_value = []
        mock_graph_instance.add_node.return_value = {"success": True}

        service = GraphBuilderService()

        node_data = {
            "node_id": "node_1",
            "x": 1.0,
            "y": 2.0,
            "yaw": 0.5
        }

        result = service.process_node_update(node_data)

        assert result["success"] is True
        assert result["node_id"] == "node_1"
        assert result["edges_created"] == 0
        assert service.stats["nodes_processed"] == 1
        mock_graph_instance.add_node.assert_called_once()

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_process_node_update_with_theta_instead_of_yaw(self, mock_topomap):
        """Test node processing supports 'theta' field as alias for 'yaw'."""
        mock_graph_instance = mock_topomap.return_value.graph
        mock_graph_instance.nodes_in_range.return_value = []
        mock_graph_instance.add_node.return_value = {"success": True}

        service = GraphBuilderService()

        node_data = {
            "node_id": "node_1",
            "x": 1.0,
            "y": 2.0,
            "theta": 1.5  # Using theta instead of yaw
        }

        result = service.process_node_update(node_data)

        assert result["success"] is True
        # Verify theta was used as yaw
        call_args = mock_graph_instance.add_node.call_args
        assert call_args[1]["yaw"] == 1.5

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_process_node_update_with_custom_map_id(self, mock_topomap):
        """Test node processing with custom map_id."""
        mock_graph_instance = mock_topomap.return_value.graph
        mock_graph_instance.nodes_in_range.return_value = []
        mock_graph_instance.add_node.return_value = {"success": True}

        service = GraphBuilderService()

        node_data = {
            "node_id": "node_1",
            "x": 1.0,
            "y": 2.0,
            "map_id": "custom_map"
        }

        result = service.process_node_update(node_data)

        assert result["success"] is True
        call_args = mock_graph_instance.add_node.call_args
        assert call_args[1]["map_id"] == "custom_map"

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_process_node_update_graph_db_failure(self, mock_topomap):
        """Test node processing when graph database fails to save node."""
        mock_graph_instance = mock_topomap.return_value.graph
        mock_graph_instance.nodes_in_range.return_value = []
        mock_graph_instance.add_node.return_value = False  # Simulate failure

        service = GraphBuilderService()

        node_data = {
            "node_id": "node_1",
            "x": 1.0,
            "y": 2.0
        }

        result = service.process_node_update(node_data)

        assert result["success"] is False
        assert "Failed to save node" in result["error"]
        assert service.stats["errors"] == 1
        assert service.stats["nodes_processed"] == 0

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_process_node_update_exception_handling(self, mock_topomap):
        """Test node processing handles exceptions gracefully."""
        mock_graph_instance = mock_topomap.return_value.graph
        # Make add_node raise exception to trigger the outer exception handler
        mock_graph_instance.add_node.side_effect = Exception("Database error")
        mock_graph_instance.nodes_in_range.return_value = []

        service = GraphBuilderService()

        node_data = {
            "node_id": "node_1",
            "x": 1.0,
            "y": 2.0
        }

        result = service.process_node_update(node_data)

        assert result["success"] is False
        assert "Database error" in result["error"]
        assert service.stats["errors"] == 1


@pytest.mark.unit
class TestGraphBuilderImageSaving:
    """Test GraphBuilderService image saving functionality."""

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_save_images_with_base64_data(self, mock_topomap):
        """Test saving images with base64 encoded data."""
        mock_image_instance = mock_topomap.return_value.image
        mock_image_instance.store_image.return_value = True

        service = GraphBuilderService()

        # Create base64 encoded image data
        test_image_data = b"fake image data"
        base64_data = base64.b64encode(test_image_data).decode('utf-8')

        images = [
            {"image_id": "img_1", "data": base64_data},
            {"image_id": "img_2", "data": base64_data}
        ]

        saved_image_ids = service._save_images("node_1", "map_1", images)

        assert saved_image_ids == ['img_1', 'img_2']
        assert service.stats["images_saved"] == 2
        assert mock_image_instance.store_image.call_count == 2

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_save_images_with_bytes_data(self, mock_topomap):
        """Test saving images with raw bytes data."""
        mock_image_instance = mock_topomap.return_value.image
        mock_image_instance.store_image.return_value = True

        service = GraphBuilderService()

        test_image_data = b"fake image data"
        images = [{"image_id": "img_1", "data": test_image_data}]

        saved_image_ids = service._save_images("node_1", "map_1", images)

        assert saved_image_ids == ['img_1']
        assert service.stats["images_saved"] == 1

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_save_images_missing_data(self, mock_topomap):
        """Test saving images when image data is missing."""
        mock_image_instance = mock_topomap.return_value.image

        service = GraphBuilderService()

        images = [{"image_id": "img_1"}]  # No data field

        saved_image_ids = service._save_images("node_1", "map_1", images)

        assert saved_image_ids == []
        assert service.stats["images_saved"] == 0
        mock_image_instance.store_image.assert_not_called()

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_save_images_store_failure(self, mock_topomap):
        """Test saving images when store_image returns False."""
        mock_image_instance = mock_topomap.return_value.image
        mock_image_instance.store_image.return_value = False

        service = GraphBuilderService()

        test_image_data = b"fake image data"
        images = [{"image_id": "img_1", "data": test_image_data}]

        saved_image_ids = service._save_images("node_1", "map_1", images)

        assert saved_image_ids == []
        assert service.stats["images_saved"] == 0

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_save_images_exception_handling(self, mock_topomap):
        """Test saving images handles exceptions gracefully."""
        mock_image_instance = mock_topomap.return_value.image
        mock_image_instance.store_image.side_effect = Exception("Storage error")

        service = GraphBuilderService()

        test_image_data = b"fake image data"
        images = [{"image_id": "img_1", "data": test_image_data}]

        saved_image_ids = service._save_images("node_1", "map_1", images)

        assert saved_image_ids == []
        assert service.stats["images_saved"] == 0

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_save_images_with_metadata(self, mock_topomap):
        """Test saving images with metadata passes metadata to image database."""
        mock_image_instance = mock_topomap.return_value.image
        mock_image_instance.store_image.return_value = True

        service = GraphBuilderService()

        # Create base64 encoded image data with metadata
        test_image_data = b"fake image data"
        base64_data = base64.b64encode(test_image_data).decode('utf-8')

        images = [
            {
                "image_id": "front_camera",
                "data": base64_data,
                "content_type": "image/jpeg",
                "metadata": {
                    "camera_name": "front_camera",
                    "timestamp": "2024-01-01T12:00:00Z",
                    "node_id": 1000
                }
            }
        ]

        saved_image_ids = service._save_images("node_1", "map_1", images)

        assert saved_image_ids == ['front_camera']
        assert service.stats["images_saved"] == 1

        # Verify metadata was passed to store_image
        mock_image_instance.store_image.assert_called_once()
        call_args = mock_image_instance.store_image.call_args
        assert call_args[1]['metadata'] == {
            "camera_name": "front_camera",
            "timestamp": "2024-01-01T12:00:00Z",
            "node_id": 1000
        }

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_process_node_with_single_image_field(self, mock_topomap):
        """Test processing node with single 'image' field (legacy format)."""
        mock_image_instance = mock_topomap.return_value.image
        mock_image_instance.store_image.return_value = True
        mock_graph_instance = mock_topomap.return_value.graph
        mock_graph_instance.nodes_in_range.return_value = []
        mock_graph_instance.add_node.return_value = {"success": True}

        service = GraphBuilderService()

        test_image_data = base64.b64encode(b"fake image").decode('utf-8')
        node_data = {
            "node_id": "node_1",
            "x": 1.0,
            "y": 2.0,
            "image": test_image_data  # Single image field
        }

        result = service.process_node_update(node_data)

        assert result["success"] is True
        assert service.stats["images_saved"] == 1
        mock_image_instance.store_image.assert_called_once()

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_process_node_with_images_list(self, mock_topomap):
        """Test processing node with 'images' list field."""
        mock_image_instance = mock_topomap.return_value.image
        mock_image_instance.store_image.return_value = True
        mock_graph_instance = mock_topomap.return_value.graph
        mock_graph_instance.nodes_in_range.return_value = []
        mock_graph_instance.add_node.return_value = {"success": True}

        service = GraphBuilderService()

        test_image_data = base64.b64encode(b"fake image").decode('utf-8')
        node_data = {
            "node_id": "node_1",
            "x": 1.0,
            "y": 2.0,
            "images": [
                {"image_id": "img_1", "data": test_image_data},
                {"image_id": "img_2", "data": test_image_data}
            ]
        }

        result = service.process_node_update(node_data)

        assert result["success"] is True
        assert service.stats["images_saved"] == 2
        assert mock_image_instance.store_image.call_count == 2


@pytest.mark.unit
class TestGraphBuilderEdgeCreation:
    """Test GraphBuilderService edge creation functionality."""

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_create_edges_no_nearby_nodes(self, mock_topomap):
        """Test edge creation when no nearby nodes exist."""
        service = GraphBuilderService()

        edges = service._create_edges("node_1", 1.0, 2.0, 0.5, [])

        assert len(edges) == 0

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_create_edges_skip_self_connection(self, mock_topomap):
        """Test edge creation skips self-connections."""
        service = GraphBuilderService()

        nearby_nodes = [
            {"node": {"node_id": "node_1", "x": 1.0, "y": 2.0, "yaw": 0.5}}
        ]

        edges = service._create_edges("node_1", 1.0, 2.0, 0.5, nearby_nodes)

        assert len(edges) == 0

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_create_edges_traversable_nodes(self, mock_topomap):
        """Test edge creation for traversable nearby nodes."""
        import math
        # node_1 at (1.0, 2.0), node_2 at (4.0, 5.0) → distance ≈ 4.24 m
        # Set distance_threshold=10.0 so nodes are traversable
        service = GraphBuilderService(distance_threshold=10.0)

        nearby_nodes = [
            {"node": {"node_id": "node_2", "x": 4.0, "y": 5.0, "yaw": 1.0}}
        ]

        edges = service._create_edges("node_1", 1.0, 2.0, 0.5, nearby_nodes)

        # Should create bidirectional edges
        assert len(edges) == 2
        assert edges[0]["from_node_id"] == "node_1"
        assert edges[0]["to_node_id"] == "node_2"
        assert edges[1]["from_node_id"] == "node_2"
        assert edges[1]["to_node_id"] == "node_1"
        expected_distance = math.sqrt((4.0 - 1.0)**2 + (5.0 - 2.0)**2)
        assert abs(edges[0]["metadata"]["distance"] - expected_distance) < 1e-9

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_create_edges_non_traversable_nodes(self, mock_topomap):
        """Test edge creation skips nodes beyond the distance threshold."""
        # node_1 at (1.0, 2.0), node_2 at (10.0, 10.0) → distance ≈ 12.04 m
        # Set distance_threshold=5.0 so nodes are NOT traversable
        service = GraphBuilderService(distance_threshold=5.0)

        nearby_nodes = [
            {"node": {"node_id": "node_2", "x": 10.0, "y": 10.0, "yaw": 1.0}}
        ]

        edges = service._create_edges("node_1", 1.0, 2.0, 0.5, nearby_nodes)

        assert len(edges) == 0

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_create_edges_with_pose_field(self, mock_topomap):
        """Test edge creation with coordinates in 'pose' field (ArangoDB format)."""
        # node_1 at (1.0, 2.0), node_2 at (3.0, 4.0) → distance ≈ 2.83 m
        # Set distance_threshold=5.0 so nodes are traversable
        service = GraphBuilderService(distance_threshold=5.0)

        # ArangoDB format with pose field
        nearby_nodes = [
            {"node": {"node_id": "node_2", "pose": {"x": 3.0, "y": 4.0, "yaw": 0.8}}}
        ]

        edges = service._create_edges("node_1", 1.0, 2.0, 0.5, nearby_nodes)

        assert len(edges) == 2

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_create_edges_missing_coordinates(self, mock_topomap):
        """Test edge creation skips nodes with missing coordinates."""
        service = GraphBuilderService()

        nearby_nodes = [
            {"node": {"node_id": "node_2"}}  # Missing x, y
        ]

        edges = service._create_edges("node_1", 1.0, 2.0, 0.5, nearby_nodes)

        assert len(edges) == 0

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_create_edges_exception_in_node_data(self, mock_topomap):
        """Test edge creation handles exceptions in node data gracefully."""
        service = GraphBuilderService(distance_threshold=5.0)

        # Non-numeric coordinates trigger a TypeError in the distance math;
        # the except block catches it and skips the node.
        nearby_nodes = [
            {"node": {"node_id": "node_2", "x": "bad", "y": "data", "yaw": 0.0}}
        ]

        edges = service._create_edges("node_1", 1.0, 2.0, 0.5, nearby_nodes)

        assert len(edges) == 0

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_process_node_creates_edges(self, mock_topomap):
        """Test full node processing creates edges correctly."""
        mock_graph_instance = mock_topomap.return_value.graph
        # node_2 at (3.0, 4.0), node_1 at (1.0, 2.0) → distance ≈ 2.83 m
        mock_graph_instance.nodes_in_range.return_value = (
            [{"node_id": "node_2", "x": 3.0, "y": 4.0, "yaw": 0.8}],
            [2.83]
        )
        mock_graph_instance.add_node.return_value = True
        mock_graph_instance.add_edge.return_value = True

        # distance_threshold=5.0 makes node_2 traversable
        service = GraphBuilderService(distance_threshold=5.0)

        node_data = {
            "node_id": "node_1",
            "x": 1.0,
            "y": 2.0,
            "yaw": 0.5
        }

        result = service.process_node_update(node_data)

        assert result["success"] is True
        assert result["edges_created"] == 2  # Bidirectional
        assert service.stats["edges_created"] == 2
        assert mock_graph_instance.add_edge.call_count == 2


@pytest.mark.unit
class TestGraphBuilderHealthChecks:
    """Test GraphBuilderService health check functionality."""

    @patch('packages.services.graph_builder.server.PostgresDatabase')
    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_is_healthy_all_services_healthy(self, mock_topomap, mock_postgres):
        """Test is_healthy returns True when all services are healthy."""
        mock_topomap.return_value.is_healthy.return_value = True
        mock_postgres.return_value.is_running.return_value = True

        service = GraphBuilderService()
        service._mqtt_connected = True

        assert service.is_healthy() is True

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_is_healthy_mqtt_disconnected(self, mock_topomap):
        """Test is_healthy returns False when MQTT is disconnected."""
        mock_image_instance = mock_topomap.return_value.image
        mock_image_instance.is_healthy.return_value = True
        mock_graph_instance = mock_topomap.return_value.graph
        mock_graph_instance.is_healthy.return_value = True

        service = GraphBuilderService()
        service._mqtt_connected = False

        assert service.is_healthy() is False

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_is_healthy_image_db_unhealthy(self, mock_topomap):
        """Test is_healthy returns False when image DB is unhealthy."""
        mock_image_instance = mock_topomap.return_value.image
        mock_image_instance.is_healthy.return_value = False
        mock_graph_instance = mock_topomap.return_value.graph
        mock_graph_instance.is_healthy.return_value = True
        mock_topomap.return_value.is_healthy.return_value = False

        service = GraphBuilderService()
        service._mqtt_connected = True

        assert service.is_healthy() is False

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_is_healthy_exception_handling(self, mock_topomap):
        """Test is_healthy handles exceptions gracefully."""
        mock_image_instance = mock_topomap.return_value.image
        mock_image_instance.is_healthy.side_effect = Exception("Health check error")
        mock_topomap.return_value.is_healthy.return_value = False

        service = GraphBuilderService()
        service._mqtt_connected = True

        assert service.is_healthy() is False

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_get_health_details(self, mock_topomap):
        """Test get_health_details returns detailed status."""
        mock_image_instance = mock_topomap.return_value.image
        mock_image_instance.is_healthy.return_value = True
        mock_graph_instance = mock_topomap.return_value.graph
        mock_graph_instance.is_healthy.return_value = False

        service = GraphBuilderService()
        service._mqtt_connected = True

        details = service.get_health_details()

        assert details["mqtt_connected"] is True
        assert details["image_db"] is True
        assert details["graph_db"] is False

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_get_health_details_exception_handling(self, mock_topomap):
        """Test get_health_details handles exceptions gracefully."""
        mock_image_instance = mock_topomap.return_value.image
        mock_image_instance.is_healthy.side_effect = Exception("Error")
        mock_graph_instance = mock_topomap.return_value.graph
        mock_graph_instance.is_healthy.return_value = True

        service = GraphBuilderService()
        service._mqtt_connected = False

        details = service.get_health_details()

        assert details["mqtt_connected"] is False
        assert details["image_db"] is False  # Exception handled
        assert details["graph_db"] is True


@pytest.mark.unit
class TestGraphBuilderEventLoop:
    """Test GraphBuilderService event loop integration."""

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_set_event_loop(self, mock_topomap):
        """Test setting event loop for async operations."""
        service = GraphBuilderService()
        mock_loop = Mock()

        service.set_event_loop(mock_loop)

        assert service._event_loop == mock_loop

    @patch('packages.services.graph_builder.server.asyncio.run_coroutine_threadsafe')
    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_process_node_publishes_update_with_event_loop(
        self, mock_topomap, mock_run_coro
    ):
        """Test node processing publishes WebSocket update when event loop is set."""
        mock_graph_instance = mock_topomap.return_value.graph
        mock_graph_instance.nodes_in_range.return_value = []
        mock_graph_instance.add_node.return_value = {"success": True}

        service = GraphBuilderService()
        mock_loop = Mock()
        service.set_event_loop(mock_loop)

        node_data = {
            "node_id": "node_1",
            "x": 1.0,
            "y": 2.0,
            "yaw": 0.5
        }

        result = service.process_node_update(node_data)

        assert result["success"] is True
        # Verify asyncio.run_coroutine_threadsafe was called
        mock_run_coro.assert_called_once()

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_process_node_skips_publish_without_event_loop(
        self, mock_topomap
    ):
        """Test node processing skips WebSocket publish when event loop is not set."""
        mock_graph_instance = mock_topomap.return_value.graph
        mock_graph_instance.nodes_in_range.return_value = []
        mock_graph_instance.add_node.return_value = {"success": True}

        service = GraphBuilderService()
        # Don't set event loop

        node_data = {
            "node_id": "node_1",
            "x": 1.0,
            "y": 2.0,
            "yaw": 0.5
        }

        result = service.process_node_update(node_data)

        # Should still succeed, just skip WebSocket publish
        assert result["success"] is True


@pytest.mark.unit
class TestGraphBuilderRobotManagement:
    """Test GraphBuilderService robot management functionality."""

    @patch('packages.services.graph_builder.server.PostgresDatabase')
    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    async def test_check_robot_exists_cached(self, mock_topomap, mock_postgres):
        """Test checking robot existence when robot is in cache."""
        service = GraphBuilderService()
        service.known_robots.add("robot_1")

        result = await service._check_robot_exists("robot_1")

        assert result is True
        # Should not make database call if cached
        mock_postgres.return_value.get_object.assert_not_called()

    @patch('packages.services.graph_builder.server.PostgresDatabase')
    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    async def test_check_robot_exists_success(self, mock_topomap, mock_postgres):
        """Test checking robot existence when robot exists in database."""
        mock_postgres.return_value.get_object = AsyncMock(return_value=Mock())

        service = GraphBuilderService()
        result = await service._check_robot_exists("robot_1")

        assert result is True
        assert "robot_1" in service.known_robots
        mock_postgres.return_value.get_object.assert_called_once()

    @patch('packages.services.graph_builder.server.PostgresDatabase')
    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    async def test_check_robot_exists_not_found(self, mock_topomap, mock_postgres):
        """Test checking robot existence when robot doesn't exist."""
        mock_postgres.return_value.get_object = AsyncMock(return_value=None)

        service = GraphBuilderService()
        result = await service._check_robot_exists("robot_1")

        assert result is False
        assert "robot_1" not in service.known_robots

    @patch('packages.services.graph_builder.server.PostgresDatabase')
    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    async def test_check_robot_exists_request_exception(self, mock_topomap, mock_postgres):
        """Test checking robot existence when database call fails."""
        mock_postgres.return_value.get_object = AsyncMock(side_effect=Exception("Connection error"))

        service = GraphBuilderService()
        result = await service._check_robot_exists("robot_1")

        assert result is False

    @patch('packages.services.graph_builder.server.PostgresDatabase')
    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    async def test_create_robot_success(self, mock_topomap, mock_postgres):
        """Test successful robot creation."""
        mock_postgres.return_value.create_object = AsyncMock()

        service = GraphBuilderService()
        result = await service._create_robot("robot_1")

        assert result is True
        assert "robot_1" in service.known_robots
        assert service.stats["robots_auto_created"] == 1
        mock_postgres.return_value.create_object.assert_called_once()

    @patch('packages.services.graph_builder.server.PostgresDatabase')
    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    async def test_create_robot_failure(self, mock_topomap, mock_postgres):
        """Test robot creation failure."""
        mock_postgres.return_value.create_object = AsyncMock(side_effect=Exception("Internal server error"))

        service = GraphBuilderService()
        result = await service._create_robot("robot_1")

        assert result is False
        assert "robot_1" not in service.known_robots
        assert service.stats["robots_auto_created"] == 0

    @patch('packages.services.graph_builder.server.PostgresDatabase')
    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    async def test_create_robot_request_exception(self, mock_topomap, mock_postgres):
        """Test robot creation when database call fails."""
        mock_postgres.return_value.create_object = AsyncMock(side_effect=Exception("Connection error"))

        service = GraphBuilderService()
        result = await service._create_robot("robot_1")

        assert result is False

    @patch('packages.services.graph_builder.server.PostgresDatabase')
    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    async def test_ensure_robot_exists_already_exists(self, mock_topomap, mock_postgres):
        """Test ensure_robot_exists when robot already exists."""
        service = GraphBuilderService()
        service.known_robots.add("robot_1")

        result = await service._ensure_robot_exists("robot_1")

        assert result is True

    @patch('packages.services.graph_builder.server.PostgresDatabase')
    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    async def test_ensure_robot_exists_creates_new_robot(self, mock_topomap, mock_postgres):
        """Test ensure_robot_exists creates robot when it doesn't exist."""
        mock_postgres.return_value.get_object = AsyncMock(return_value=None)
        mock_postgres.return_value.create_object = AsyncMock()

        service = GraphBuilderService()
        result = await service._ensure_robot_exists("robot_1")

        assert result is True
        assert "robot_1" in service.known_robots
        mock_postgres.return_value.get_object.assert_called_once()
        mock_postgres.return_value.create_object.assert_called_once()

    @patch('packages.services.graph_builder.server.PostgresDatabase')
    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    async def test_ensure_robot_exists_creation_fails(self, mock_topomap, mock_postgres):
        """Test ensure_robot_exists when robot creation fails."""
        mock_postgres.return_value.get_object = AsyncMock(return_value=None)
        mock_postgres.return_value.create_object = AsyncMock(side_effect=Exception("Error"))

        service = GraphBuilderService()
        result = await service._ensure_robot_exists("robot_1")

        assert result is False


@pytest.mark.unit
class TestGraphBuilderCleanup:
    """Test GraphBuilderService cleanup functionality."""

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_cleanup_old_mappings_removes_old_sessions(self, mock_topomap):
        """Test cleanup removes old session mappings."""
        from datetime import datetime, timedelta

        service = GraphBuilderService()

        # Add old mapping (2 hours ago)
        old_time = datetime.now() - timedelta(hours=2)
        service.session_to_global_map[("robot_1", 1000)] = ("global_id_1", old_time)

        # Add recent mapping (5 minutes ago)
        recent_time = datetime.now() - timedelta(minutes=5)
        service.session_to_global_map[("robot_2", 2000)] = ("global_id_2", recent_time)

        # Cleanup with 1 hour threshold
        service._cleanup_old_mappings(threshold_seconds=3600)

        # Old mapping should be removed, recent one should remain
        assert ("robot_1", 1000) not in service.session_to_global_map
        assert ("robot_2", 2000) in service.session_to_global_map

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_cleanup_old_mappings_removes_old_buffered_images(self, mock_topomap):
        """Test cleanup removes old buffered images."""
        from datetime import datetime, timedelta

        service = GraphBuilderService()

        # Add old buffered image (2 hours ago)
        old_time = datetime.now() - timedelta(hours=2)
        old_image = {"image_id": "img_1", "data": "old_data"}
        service.image_buffer[("robot_1", 1000)] = {"camera_1": (old_image, old_time)}

        # Add recent buffered image (5 minutes ago)
        recent_time = datetime.now() - timedelta(minutes=5)
        recent_image = {"image_id": "img_2", "data": "recent_data"}
        service.image_buffer[("robot_2", 2000)] = {"camera_2": (recent_image, recent_time)}

        # Cleanup with 1 hour threshold (default is 3600 seconds = 1 hour)
        service._cleanup_old_mappings(threshold_seconds=3600)

        # Old buffer should be removed, recent one should remain
        assert ("robot_1", 1000) not in service.image_buffer
        assert ("robot_2", 2000) in service.image_buffer
        assert "camera_2" in service.image_buffer[("robot_2", 2000)]

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_cleanup_old_mappings_empty_collections(self, mock_topomap):
        """Test cleanup with empty collections doesn't raise errors."""
        service = GraphBuilderService()

        # Should not raise any exceptions
        service._cleanup_old_mappings()

        assert len(service.session_to_global_map) == 0
        assert len(service.image_buffer) == 0

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_cleanup_old_mappings_custom_threshold(self, mock_topomap):
        """Test cleanup with custom age threshold."""
        from datetime import datetime, timedelta

        service = GraphBuilderService()

        # Add mapping 30 minutes ago
        old_time = datetime.now() - timedelta(minutes=30)
        service.session_to_global_map[("robot_1", 1000)] = ("global_id_1", old_time)

        # Cleanup with 1 hour threshold - should keep it
        service._cleanup_old_mappings(threshold_seconds=3600)
        assert ("robot_1", 1000) in service.session_to_global_map

        # Cleanup with 10 minute threshold - should remove it
        service._cleanup_old_mappings(threshold_seconds=600)
        assert ("robot_1", 1000) not in service.session_to_global_map


# ===========================================================================
# Tests for new topology/logging separation
# ===========================================================================

@pytest.mark.unit
class TestGraphBuilderProcessTopology:
    """Tests for _process_topology — the graph-only concern extracted from the MQTT handler."""

    def _make_service(self, mock_topomap):
        service = GraphBuilderService()
        service.graph_db.add_node = Mock(return_value=True)
        service.graph_db.add_edge = Mock(return_value=True)
        service.graph_db.nodes_in_range = Mock(return_value=([], []))
        return service

    def _valid_payload(self):
        # Note: map_id is intentionally omitted — it is now resolved from the
        # robot's DB record by _handle_node_update and passed into
        # _process_topology as an explicit argument.  Tests that need a specific
        # map_id should pass it via the map_id parameter of _process_topology.
        return {
            "session_node_id": 1,
            "robot_name": "robot1",
            "x": 1.0,
            "y": 2.0,
            "yaw": 0.5,
        }

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_success_returns_eight_tuple(self, mock_topomap):
        """Successful topology processing returns a full 8-element tuple."""
        service = self._make_service(mock_topomap)
        result = service._process_topology(self._valid_payload(), map_id="test_map")
        assert result is not None
        assert len(result) == 8
        global_node_id, x, y, yaw, map_id, edges, seq, robot_name = result
        assert x == 1.0
        assert y == 2.0
        assert yaw == 0.5
        assert map_id == "test_map"
        assert seq == 1
        assert robot_name == "robot1"
        assert isinstance(global_node_id, str)

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_missing_session_node_id_returns_none(self, mock_topomap):
        """Missing session_node_id is a hard validation failure."""
        service = self._make_service(mock_topomap)
        payload = self._valid_payload()
        del payload["session_node_id"]
        result = service._process_topology(payload)
        assert result is None
        assert service.stats["errors"] == 1

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_missing_robot_name_returns_none(self, mock_topomap):
        service = self._make_service(mock_topomap)
        payload = self._valid_payload()
        del payload["robot_name"]
        result = service._process_topology(payload)
        assert result is None

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_missing_x_returns_none(self, mock_topomap):
        service = self._make_service(mock_topomap)
        payload = self._valid_payload()
        del payload["x"]
        result = service._process_topology(payload)
        assert result is None

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_graph_db_write_failure_returns_none(self, mock_topomap):
        """add_node returning False stops processing and increments error count."""
        service = self._make_service(mock_topomap)
        service.graph_db.add_node = Mock(return_value=False)
        result = service._process_topology(self._valid_payload())
        assert result is None
        assert service.stats["nodes_processed"] == 0
        assert service.stats["errors"] == 1

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_increments_nodes_processed_on_success(self, mock_topomap):
        service = self._make_service(mock_topomap)
        service._process_topology(self._valid_payload())
        assert service.stats["nodes_processed"] == 1

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_stores_session_to_global_mapping(self, mock_topomap):
        """The session→global mapping must be populated so image uploads can look it up."""
        service = self._make_service(mock_topomap)
        result = service._process_topology(self._valid_payload())
        assert result is not None
        global_node_id = result[0]
        assert ("robot1", 1) in service.session_to_global_map
        stored_id, _ = service.session_to_global_map[("robot1", 1)]
        assert stored_id == global_node_id

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_theta_accepted_as_yaw_alias(self, mock_topomap):
        """Robots that publish theta instead of yaw should still work."""
        service = self._make_service(mock_topomap)
        payload = self._valid_payload()
        del payload["yaw"]
        payload["theta"] = 1.23
        result = service._process_topology(payload)
        assert result is not None
        _, _, _, yaw, _, _, _, _ = result
        assert yaw == 1.23

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_bidirectional_edges_created_for_nearby_nodes(self, mock_topomap):
        """Each nearby node within distance_threshold gets two edges (one in each direction)."""
        service = self._make_service(mock_topomap)
        service.graph_db.nodes_in_range = Mock(return_value=(
            [{"node_id": "nearby_1", "pose": {"x": 1.1, "y": 2.1}}],
            [0.5]
        ))
        service.graph_db.add_edges_bulk = Mock(return_value=2)
        result = service._process_topology(self._valid_payload())
        assert result is not None
        assert service.stats["edges_created"] == 2

    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_map_id_defaults_to_service_default(self, mock_topomap):
        """When no map_id is passed to _process_topology, falls back to service.default_map_id."""
        service = self._make_service(mock_topomap)
        # Do not pass map_id — the method must use service.default_map_id
        result = service._process_topology(self._valid_payload())
        assert result is not None
        _, _, _, _, map_id, _, _, _ = result
        assert map_id == service.default_map_id


@pytest.mark.unit
class TestGraphBuilderMissionWaypointLogging:
    """Tests for _log_mission_waypoint — the mission-logging concern."""

    @patch('packages.services.graph_builder.server.PostgresDatabase')
    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    async def test_no_running_mission_skips_db_write(self, mock_topomap, mock_postgres):
        """When no mission is RUNNING for the robot, nothing is written."""
        mock_postgres.return_value.list_objects = AsyncMock(return_value=[])
        mock_postgres.return_value.log_mission_waypoint = AsyncMock()

        service = GraphBuilderService()
        await service._log_mission_waypoint("robot1", "node-uuid", 1, 1.0, 2.0, 0.0, "map1")

        mock_postgres.return_value.log_mission_waypoint.assert_not_called()

    @patch('packages.services.graph_builder.server.PostgresDatabase')
    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    async def test_running_mission_writes_correct_params(self, mock_topomap, mock_postgres):
        """All fields are forwarded exactly to log_mission_waypoint."""
        from cloud_common.objects.mission import MissionObjectV1
        running_mission = Mock()
        running_mission.name = "nav_robot1_20260507"
        mock_postgres.return_value.list_objects = AsyncMock(return_value=[running_mission])
        mock_postgres.return_value.log_mission_waypoint = AsyncMock()

        service = GraphBuilderService()
        await service._log_mission_waypoint("robot1", "node-uuid", 5, 1.5, 2.5, 0.7, "main")

        mock_postgres.return_value.log_mission_waypoint.assert_called_once_with(
            mission_id="nav_robot1_20260507",
            robot_name="robot1",
            node_id="node-uuid",
            seq=5,
            x=1.5,
            y=2.5,
            yaw=0.7,
            map_id="main",
        )

    @patch('packages.services.graph_builder.server.PostgresDatabase')
    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    async def test_queries_correct_robot_and_running_state(self, mock_topomap, mock_postgres):
        """list_objects must be called with robot filter and RUNNING state."""
        from cloud_common.objects.mission import MissionObjectV1, MissionStateV1
        mock_postgres.return_value.list_objects = AsyncMock(return_value=[])

        service = GraphBuilderService()
        await service._log_mission_waypoint("robot_x", "node-uuid", 1, 0.0, 0.0, 0.0, "map")

        mock_postgres.return_value.list_objects.assert_called_once()
        args, kwargs = mock_postgres.return_value.list_objects.call_args
        object_class = args[0] if args else kwargs.get("object_class")
        query_params = args[1] if len(args) > 1 else kwargs.get("query_params")
        assert object_class is MissionObjectV1
        assert query_params.robot == "robot_x"
        assert query_params.state == MissionStateV1.RUNNING

    @patch('packages.services.graph_builder.server.PostgresDatabase')
    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    async def test_list_objects_error_does_not_propagate(self, mock_topomap, mock_postgres):
        """A broken DB connection must not crash the MQTT callback."""
        mock_postgres.return_value.list_objects = AsyncMock(side_effect=Exception("DB down"))

        service = GraphBuilderService()
        await service._log_mission_waypoint("robot1", "node-uuid", 1, 0.0, 0.0, 0.0, "map")

    @patch('packages.services.graph_builder.server.PostgresDatabase')
    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    async def test_write_error_does_not_propagate(self, mock_topomap, mock_postgres):
        """A write failure must not crash the MQTT callback."""
        running_mission = Mock()
        running_mission.name = "mission1"
        mock_postgres.return_value.list_objects = AsyncMock(return_value=[running_mission])
        mock_postgres.return_value.log_mission_waypoint = AsyncMock(
            side_effect=Exception("write failed")
        )

        service = GraphBuilderService()
        await service._log_mission_waypoint("robot1", "node-uuid", 1, 0.0, 0.0, 0.0, "map")


@pytest.mark.unit
class TestGraphBuilderOnNodeUpdateDispatching:
    """Tests that _on_node_update_message dispatches exactly the right coroutines."""

    def _make_msg(self, data):
        msg = Mock()
        msg.payload = json.dumps(data).encode('utf-8')
        return msg

    def _valid_payload(self):
        # map_id is no longer read from the payload; it is resolved server-side
        # from the robot's DB record.  Including it here would be silently ignored.
        return {
            "session_node_id": 3,
            "robot_name": "robot1",
            "x": 5.0,
            "y": 6.0,
            "yaw": 1.0,
        }

    @patch('packages.services.graph_builder.server.asyncio.run_coroutine_threadsafe')
    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_dispatches_one_coroutine_on_valid_payload(self, mock_topomap, mock_run):
        """_handle_node_update is scheduled as a single coroutine for valid JSON."""
        service = GraphBuilderService()
        service._event_loop = Mock()

        service._on_node_update_message(None, None, self._make_msg(self._valid_payload()))

        assert mock_run.call_count == 1

    @patch('packages.services.graph_builder.server.asyncio.run_coroutine_threadsafe')
    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_dispatches_one_coroutine_even_when_topology_would_fail(self, mock_topomap, mock_run):
        """Topology logic runs inside the coroutine; dispatching is unconditional for valid JSON."""
        service = GraphBuilderService()
        service.graph_db.add_node = Mock(return_value=False)
        service.graph_db.nodes_in_range = Mock(return_value=([], []))
        service._event_loop = Mock()

        service._on_node_update_message(None, None, self._make_msg(self._valid_payload()))

        assert mock_run.call_count == 1

    @patch('packages.services.graph_builder.server.asyncio.run_coroutine_threadsafe')
    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_no_dispatch_without_event_loop(self, mock_topomap, mock_run):
        """With no event loop set, no coroutine is scheduled."""
        service = GraphBuilderService()
        service._event_loop = None

        service._on_node_update_message(None, None, self._make_msg(self._valid_payload()))

        mock_run.assert_not_called()

    @patch('packages.services.graph_builder.server.asyncio.run_coroutine_threadsafe')
    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    def test_invalid_json_increments_error_and_does_not_dispatch(self, mock_topomap, mock_run):
        service = GraphBuilderService()
        service._event_loop = Mock()
        bad_msg = Mock()
        bad_msg.payload = b"not valid json {"

        service._on_node_update_message(None, None, bad_msg)

        assert service.stats["errors"] == 1
        mock_run.assert_not_called()


@pytest.mark.unit
class TestGraphBuilderHandleNodeUpdate:
    """Tests for _handle_node_update — the async dispatcher that checks register_map."""

    def _make_service(self, mock_topomap, mock_postgres=None):
        service = GraphBuilderService()
        service.graph_db.add_node = Mock(return_value=True)
        service.graph_db.add_edge = Mock(return_value=True)
        service.graph_db.nodes_in_range = Mock(return_value=([], []))
        # Stub out _get_robot_map_id so tests don't need a live DB for map lookup
        service._get_robot_map_id = AsyncMock(return_value="test_map")
        return service

    def _valid_payload(self):
        # map_id is no longer read from the payload; it is resolved from the
        # robot's DB record by _handle_node_update via _get_robot_map_id.
        return {
            "session_node_id": 1,
            "robot_name": "robot1",
            "x": 1.0,
            "y": 2.0,
            "yaw": 0.5,
        }

    def _make_mission(self, register_map=True):
        m = Mock()
        m.name = "mission_001"
        m.register_map = register_map
        return m

    @patch('packages.services.graph_builder.server.PostgresDatabase')
    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    async def test_register_map_true_calls_process_topology(self, mock_topomap, mock_postgres):
        """When register_map=True the graph builder runs _process_topology."""
        running_mission = self._make_mission(register_map=True)
        mock_postgres.return_value.list_objects = AsyncMock(return_value=[running_mission])
        mock_postgres.return_value.log_mission_waypoint = AsyncMock()

        service = self._make_service(mock_topomap)
        process_spy = Mock(wraps=service._process_topology)
        service._process_topology = process_spy

        await service._handle_node_update(self._valid_payload())

        process_spy.assert_called_once()

    @patch('packages.services.graph_builder.server.PostgresDatabase')
    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    async def test_register_map_false_skips_process_topology(self, mock_topomap, mock_postgres):
        """When register_map=False _process_topology must NOT be called."""
        running_mission = self._make_mission(register_map=False)
        mock_postgres.return_value.list_objects = AsyncMock(return_value=[running_mission])
        mock_postgres.return_value.log_mission_waypoint = AsyncMock()

        service = self._make_service(mock_topomap)
        process_spy = Mock(wraps=service._process_topology)
        service._process_topology = process_spy

        await service._handle_node_update(self._valid_payload())

        process_spy.assert_not_called()

    @patch('packages.services.graph_builder.server.PostgresDatabase')
    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    async def test_register_map_false_still_logs_waypoint(self, mock_topomap, mock_postgres):
        """Logging-only mode must still write to mission_trajectory."""
        running_mission = self._make_mission(register_map=False)
        mock_postgres.return_value.list_objects = AsyncMock(return_value=[running_mission])
        mock_postgres.return_value.log_mission_waypoint = AsyncMock()

        service = self._make_service(mock_topomap)

        await service._handle_node_update(self._valid_payload())

        mock_postgres.return_value.log_mission_waypoint.assert_called_once()

    @patch('packages.services.graph_builder.server.PostgresDatabase')
    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    async def test_no_mission_still_builds_topology(self, mock_topomap, mock_postgres):
        """When no mission is running, topology is built (register_map defaults True)."""
        mock_postgres.return_value.list_objects = AsyncMock(return_value=[])
        mock_postgres.return_value.log_mission_waypoint = AsyncMock()

        service = self._make_service(mock_topomap)
        process_spy = Mock(wraps=service._process_topology)
        service._process_topology = process_spy

        await service._handle_node_update(self._valid_payload())

        process_spy.assert_called_once()
        mock_postgres.return_value.log_mission_waypoint.assert_not_called()

    @patch('packages.services.graph_builder.server.PostgresDatabase')
    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    async def test_register_map_false_missing_fields_increments_error(self, mock_topomap, mock_postgres):
        """In logging-only mode, missing required fields must increment error count."""
        running_mission = self._make_mission(register_map=False)
        mock_postgres.return_value.list_objects = AsyncMock(return_value=[running_mission])

        service = self._make_service(mock_topomap)

        bad_payload = {"robot_name": "robot1"}  # missing x, y, session_node_id
        await service._handle_node_update(bad_payload)

        assert service.stats["errors"] == 1


class TestGraphBuilderGetRobotMapIdGeo:
    """Tests for the GEO sentinel in _get_robot_map_id."""

    def _make_service(self, mock_topomap):
        return GraphBuilderService()

    @patch('packages.services.graph_builder.server.PostgresDatabase')
    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    async def test_geo_sentinel_returns_none(self, mock_topomap, mock_postgres):
        """current_map == 'GEO' must return None, not the string 'GEO'."""
        robot = Mock()
        robot.current_map = 'GEO'
        mock_postgres.return_value.get_object = AsyncMock(return_value=robot)

        service = self._make_service(mock_topomap)
        result = await service._get_robot_map_id('sati-beta')

        assert result is None

    @patch('packages.services.graph_builder.server.PostgresDatabase')
    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    async def test_real_map_id_returned_unchanged(self, mock_topomap, mock_postgres):
        """A real map ID passes through unmodified."""
        robot = Mock()
        robot.current_map = 'warehouse-floor1'
        mock_postgres.return_value.get_object = AsyncMock(return_value=robot)

        service = self._make_service(mock_topomap)
        result = await service._get_robot_map_id('sati-alpha')

        assert result == 'warehouse-floor1'

    @patch('packages.services.graph_builder.server.PostgresDatabase')
    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    async def test_no_current_map_falls_back_to_default(self, mock_topomap, mock_postgres):
        """Robot with current_map=None falls back to service default."""
        robot = Mock()
        robot.current_map = None
        mock_postgres.return_value.get_object = AsyncMock(return_value=robot)

        service = self._make_service(mock_topomap)
        result = await service._get_robot_map_id('unknown-robot')

        assert result == service.default_map_id


class TestGraphBuilderHandleNodeUpdateGeoMode:
    """Tests for _handle_node_update when the robot is in GEO mode (map_id is None)."""

    def _make_service(self, mock_topomap, mock_postgres=None):
        service = GraphBuilderService()
        service.graph_db.add_node = Mock(return_value=True)
        service.graph_db.add_edge = Mock(return_value=True)
        service.graph_db.nodes_in_range = Mock(return_value=([], []))
        service._get_robot_map_id = AsyncMock(return_value=None)
        return service

    def _valid_payload(self):
        return {
            "session_node_id": 1,
            "robot_name": "sati-beta",
            "x": 1.0,
            "y": 2.0,
            "yaw": 0.5,
        }

    def _make_mission(self, register_map=True):
        m = Mock()
        m.name = "geo_mission_001"
        m.register_map = register_map
        return m

    @patch('packages.services.graph_builder.server.PostgresDatabase')
    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    async def test_geo_mode_skips_topology(self, mock_topomap, mock_postgres):
        """GEO mode must never call _process_topology, even with register_map=True mission."""
        running_mission = self._make_mission(register_map=True)
        mock_postgres.return_value.list_objects = AsyncMock(return_value=[running_mission])
        mock_postgres.return_value.log_mission_waypoint = AsyncMock()

        service = self._make_service(mock_topomap)
        process_spy = Mock(wraps=service._process_topology)
        service._process_topology = process_spy

        await service._handle_node_update(self._valid_payload())

        process_spy.assert_not_called()

    @patch('packages.services.graph_builder.server.PostgresDatabase')
    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    async def test_geo_mode_still_logs_waypoint(self, mock_topomap, mock_postgres):
        """GEO mode with an active mission still writes a position-only waypoint."""
        running_mission = self._make_mission(register_map=True)
        mock_postgres.return_value.list_objects = AsyncMock(return_value=[running_mission])
        mock_postgres.return_value.log_mission_waypoint = AsyncMock()

        service = self._make_service(mock_topomap)
        await service._handle_node_update(self._valid_payload())

        mock_postgres.return_value.log_mission_waypoint.assert_called_once()
        call_kwargs = mock_postgres.return_value.log_mission_waypoint.call_args.kwargs
        assert call_kwargs.get('map_id') == ''

    @patch('packages.services.graph_builder.server.PostgresDatabase')
    @patch('packages.services.graph_builder.server.TopomapDatabaseClient')
    async def test_geo_mode_no_mission_no_waypoint(self, mock_topomap, mock_postgres):
        """GEO mode with no running mission skips waypoint logging."""
        mock_postgres.return_value.list_objects = AsyncMock(return_value=[])
        mock_postgres.return_value.log_mission_waypoint = AsyncMock()

        service = self._make_service(mock_topomap)
        await service._handle_node_update(self._valid_payload())

        mock_postgres.return_value.log_mission_waypoint.assert_not_called()
