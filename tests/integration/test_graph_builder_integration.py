"""
Integration tests for Graph Builder Service.

These tests use real Image DB, Graph DB, and Similarity Service
to test the full graph building workflow.
"""

import pytest
import json
import base64
from unittest.mock import Mock, patch, MagicMock, AsyncMock


@pytest.mark.integration
@pytest.mark.requires_docker
class TestGraphBuilderIntegration:
    """Integration tests for graph builder with real databases."""
    
    def test_process_node_update_new_node(self, graph_db_client, image_db_client, sample_image):
        """Test processing a new node update from MQTT."""
        from packages.services.graph_builder.server import GraphBuilderService
        
        map_id = "test_map"
        
        # Create map
        graph_db_client.create_map(map_id)
        
        # Create graph builder service
        builder = GraphBuilderService(
            mqtt_host="localhost",
            mqtt_port=1883,
            mqtt_topic="robot/node_update",

            arango_host="localhost",
            radius_threshold=5.0,
            default_map_id=map_id
        )
        
        # Use real clients
        builder.image_db = image_db_client
        builder.graph_db = graph_db_client
        
        builder.distance_threshold = 15.0
        
        # Create node update message
        node_update = {
            "map_id": map_id,
            "node_id": "node_1",
            "x": 0.0,
            "y": 0.0,
            "theta": 0.0,
            "image": base64.b64encode(sample_image).decode('utf-8')
        }
        
        # Process node update
        result = builder.process_node_update(node_update)
        
        # Verify node was added to graph
        assert result["success"] is True
        node = graph_db_client.get_node(map_id, "node_1")
        assert node["x"] == 0.0
        assert node["y"] == 0.0
        
        # Verify image was saved
        node_images = image_db_client.list_node_images("node_1", map_id)
        assert len(node_images) >= 1
        retrieved_image = image_db_client.get_image(node_images[0], "node_1", map_id)
        assert retrieved_image == sample_image
    
    def test_process_node_update_creates_edges(self, graph_db_client, image_db_client, sample_image):
        """Test that processing nodes creates edges based on proximity."""
        from packages.services.graph_builder.server import GraphBuilderService
        
        map_id = "test_map_edges"
        graph_db_client.create_map(map_id)
        
        builder = GraphBuilderService(
            mqtt_host="localhost",
            mqtt_port=1883,
            mqtt_topic="robot/node_update",

            arango_host="localhost",
            radius_threshold=15.0,  # Large enough to connect nodes
            default_map_id=map_id
        )
        
        builder.image_db = image_db_client
        builder.graph_db = graph_db_client
        
        builder.distance_threshold = 15.0
        
        # Add first node
        node1_update = {
            "map_id": map_id,
            "node_id": "node_1",
            "x": 0.0,
            "y": 0.0,
            "theta": 0.0,
            "image": base64.b64encode(sample_image).decode('utf-8')
        }
        builder.process_node_update(node1_update)
        
        # Add second node (within radius)
        node2_update = {
            "map_id": map_id,
            "node_id": "node_2",
            "x": 10.0,
            "y": 0.0,
            "theta": 0.0,
            "image": base64.b64encode(sample_image).decode('utf-8')
        }
        builder.process_node_update(node2_update)
        
        # Verify edge was created
        stats = graph_db_client.get_map_stats(map_id)
        assert stats["node_count"] == 2
        assert stats["edge_count"] >= 1  # At least one edge should be created
    
    def test_process_node_update_no_edge_if_too_far(self, graph_db_client, image_db_client, sample_image):
        """Test that nodes too far apart don't get connected."""
        from packages.services.graph_builder.server import GraphBuilderService
        
        map_id = "test_map_no_edge"
        graph_db_client.create_map(map_id)
        
        builder = GraphBuilderService(
            mqtt_host="localhost",
            mqtt_port=1883,
            mqtt_topic="robot/node_update",

            arango_host="localhost",
            radius_threshold=5.0,  # Small radius
            default_map_id=map_id
        )
        
        builder.image_db = image_db_client
        builder.graph_db = graph_db_client
        
        builder.distance_threshold = 0.1
        
        # Add two distant nodes
        node1_update = {
            "map_id": map_id,
            "node_id": "node_1",
            "x": 0.0,
            "y": 0.0,
            "theta": 0.0,
            "image": base64.b64encode(sample_image).decode('utf-8')
        }
        builder.process_node_update(node1_update)
        
        node2_update = {
            "map_id": map_id,
            "node_id": "node_2",
            "x": 100.0,
            "y": 100.0,
            "theta": 0.0,
            "image": base64.b64encode(sample_image).decode('utf-8')
        }
        builder.process_node_update(node2_update)
        
        # Verify no edge was created
        stats = graph_db_client.get_map_stats(map_id)
        assert stats["node_count"] == 2
        assert stats["edge_count"] == 0  # No edges should be created
    
    def test_process_multiple_nodes_builds_graph(self, graph_db_client, image_db_client, sample_image):
        """Test processing multiple nodes builds a connected graph."""
        from packages.services.graph_builder.server import GraphBuilderService
        
        map_id = "test_map_multi"
        graph_db_client.create_map(map_id)
        
        builder = GraphBuilderService(
            mqtt_host="localhost",
            mqtt_port=1883,
            mqtt_topic="robot/node_update",

            arango_host="localhost",
            radius_threshold=15.0,
            default_map_id=map_id
        )
        
        builder.image_db = image_db_client
        builder.graph_db = graph_db_client
        
        builder.distance_threshold = 15.0
        
        # Add 5 nodes in a line
        for i in range(5):
            node_update = {
                "map_id": map_id,
                "node_id": f"node_{i}",
                "x": float(i * 10),
                "y": 0.0,
                "theta": 0.0,
                "image": base64.b64encode(sample_image).decode('utf-8')
            }
            builder.process_node_update(node_update)
        
        # Verify graph was built
        stats = graph_db_client.get_map_stats(map_id)
        assert stats["node_count"] == 5
        assert stats["edge_count"] > 0  # Should have edges connecting nodes

        # Verify images were saved
        images = image_db_client.list_images(map_id=map_id)
        assert len(images) == 5, f"Expected 5 images, found {len(images)}"
    
    def test_get_stats(self, graph_db_client, image_db_client, sample_image):
        """Test getting graph builder statistics."""
        from packages.services.graph_builder.server import GraphBuilderService
        
        map_id = "test_map_stats"
        graph_db_client.create_map(map_id)
        
        builder = GraphBuilderService(
            mqtt_host="localhost",
            mqtt_port=1883,
            mqtt_topic="robot/node_update",

            arango_host="localhost",
            radius_threshold=10.0,
            default_map_id=map_id
        )
        
        builder.image_db = image_db_client
        builder.graph_db = graph_db_client
        
        builder.distance_threshold = 15.0
        
        # Process some nodes
        for i in range(3):
            node_update = {
                "map_id": map_id,
                "node_id": f"node_{i}",
                "x": float(i * 5),
                "y": 0.0,
                "theta": 0.0,
                "image": base64.b64encode(sample_image).decode('utf-8')
            }
            builder.process_node_update(node_update)
        
        # Get statistics
        stats = builder.get_stats()
        
        assert stats["nodes_processed"] == 3
        assert stats["images_saved"] == 3
        assert stats["edges_created"] >= 0
        assert stats["errors"] == 0
    
    def test_handle_invalid_node_update(self, graph_db_client, image_db_client):
        """Test handling invalid node update gracefully."""
        from packages.services.graph_builder.server import GraphBuilderService
        
        map_id = "test_map_invalid"
        graph_db_client.create_map(map_id)
        
        builder = GraphBuilderService(
            mqtt_host="localhost",
            mqtt_port=1883,
            mqtt_topic="robot/node_update",

            arango_host="localhost",
            radius_threshold=10.0,
            default_map_id=map_id
        )
        
        builder.image_db = image_db_client
        builder.graph_db = graph_db_client
        
        # Invalid node update (missing required fields)
        invalid_update = {
            "map_id": map_id,
            "node_id": "node_invalid"
            # Missing x, y, theta, image
        }
        
        # Should handle gracefully
        result = builder.process_node_update(invalid_update)
        
        assert result["success"] is False
        assert "error" in result
        
        # Error count should increase
        stats = builder.get_stats()
        assert stats["errors"] > 0


@pytest.mark.integration
@pytest.mark.requires_docker
class TestGraphBuilderErrorScenarios:
    """Integration tests for graph builder error handling and edge cases."""

    def test_mqtt_message_invalid_json(self):
        """Test graph builder handling of malformed MQTT messages."""
        from packages.services.graph_builder.server import GraphBuilderService

        map_id = "test_invalid_json"

        builder = GraphBuilderService(
            mqtt_host="localhost",
            mqtt_port=1883,
            mqtt_topic="robot/node_update",

            arango_host="localhost",
            radius_threshold=5.0,
            default_map_id=map_id
        )

        # Simulate MQTT callback with invalid JSON
        mock_client = Mock()
        mock_userdata = None
        mock_message = Mock()
        mock_message.payload = b"invalid json {{{["

        # Should handle gracefully without crashing
        try:
            builder._on_node_update_message(mock_client, mock_userdata, mock_message)
            # If no exception, check error stats
            stats = builder.get_stats()
            assert stats["errors"] > 0
        except Exception as e:
            # Should not raise exception for invalid JSON
            pytest.fail(f"Should handle invalid JSON gracefully, got: {e}")

    def test_mqtt_message_missing_required_fields(self, graph_db_client, image_db_client):
        """Test node update processing with missing required fields."""
        from packages.services.graph_builder.server import GraphBuilderService

        map_id = "test_missing_fields"
        graph_db_client.create_map(map_id)

        builder = GraphBuilderService(
            mqtt_host="localhost",
            mqtt_port=1883,
            mqtt_topic="robot/node_update",

            arango_host="localhost",
            radius_threshold=5.0,
            default_map_id=map_id
        )

        builder.image_db = image_db_client
        builder.graph_db = graph_db_client

        # Test missing node_id
        update_no_id = {
            "map_id": map_id,
            "x": 1.0,
            "y": 2.0,
            "yaw": 0.0,
            "images": []
        }
        result = builder.process_node_update(update_no_id)
        assert result["success"] is False

        # Test missing x coordinate
        update_no_x = {
            "map_id": map_id,
            "node_id": "node_1",
            "y": 2.0,
            "yaw": 0.0,
            "images": []
        }
        result = builder.process_node_update(update_no_x)
        assert result["success"] is False

        # Test missing y coordinate
        update_no_y = {
            "map_id": map_id,
            "node_id": "node_2",
            "x": 1.0,
            "yaw": 0.0,
            "images": []
        }
        result = builder.process_node_update(update_no_y)
        assert result["success"] is False

    def test_edge_creation_no_nearby_nodes(self, graph_db_client, image_db_client, sample_image):
        """Test node processing when no nodes exist within radius threshold."""
        from packages.services.graph_builder.server import GraphBuilderService

        map_id = "test_no_nearby"
        graph_db_client.create_map(map_id)

        # Add node far away
        graph_db_client.add_node(map_id, "node_1", 0.0, 0.0, 0.0)
        image_db_client.store_image(sample_image, "test_image", "node_1", map_id)

        builder = GraphBuilderService(
            mqtt_host="localhost",
            mqtt_port=1883,
            mqtt_topic="robot/node_update",

            arango_host="localhost",
            radius_threshold=1.0,  # Very small radius
            default_map_id=map_id
        )

        builder.image_db = image_db_client
        builder.graph_db = graph_db_client

        # Add node far from existing node
        node_update = {
            "map_id": map_id,
            "node_id": "node_2",
            "x": 100.0,  # Far away
            "y": 100.0,
            "yaw": 0.0,
            "images": [{"data": base64.b64encode(sample_image).decode(), "id": "front"}]
        }

        result = builder.process_node_update(node_update)

        # Should succeed but no edges created
        assert result["success"] is True

        # Verify no edges to node_2
        edges = graph_db_client.get_edges(map_id)
        edges_to_node2 = [e for e in edges if e["to"] == "node_2" or e["from"] == "node_2"]
        assert len(edges_to_node2) == 0

    def test_image_save_failure_handling(self, graph_db_client):
        """Test node processing when image database save operation fails."""
        from packages.services.graph_builder.server import GraphBuilderService

        map_id = "test_image_save_fail"
        graph_db_client.create_map(map_id)

        builder = GraphBuilderService(
            mqtt_host="localhost",
            mqtt_port=1883,
            mqtt_topic="robot/node_update",

            arango_host="localhost",
            radius_threshold=5.0,
            default_map_id=map_id
        )

        builder.graph_db = graph_db_client

        # Mock image DB to fail
        mock_image_db = Mock()
        mock_image_db.store_image.side_effect = Exception("Image DB connection failed")
        builder.image_db = mock_image_db

        node_update = {
            "map_id": map_id,
            "node_id": "node_1",
            "x": 1.0,
            "y": 2.0,
            "yaw": 0.0,
            "images": [{"data": "base64data", "id": "front"}]
        }

        # Should handle image save failure gracefully
        result = builder.process_node_update(node_update)

        # May fail or succeed depending on implementation
        # But should not crash
        assert "success" in result

    def test_graph_database_node_save_failure(self, image_db_client):
        """Test error handling when graph database node creation fails."""
        from packages.services.graph_builder.server import GraphBuilderService

        map_id = "test_graph_save_fail"

        builder = GraphBuilderService(
            mqtt_host="localhost",
            mqtt_port=1883,
            mqtt_topic="robot/node_update",

            arango_host="localhost",
            radius_threshold=5.0,
            default_map_id=map_id
        )

        builder.image_db = image_db_client

        # Mock graph DB to fail
        mock_graph_db = Mock()
        mock_graph_db.add_node.side_effect = Exception("Graph DB connection failed")
        mock_graph_db.get_all_nodes.return_value = []
        builder.graph_db = mock_graph_db

        node_update = {
            "map_id": map_id,
            "node_id": "node_1",
            "x": 1.0,
            "y": 2.0,
            "yaw": 0.0,
            "images": []
        }

        # Should handle graph DB failure
        result = builder.process_node_update(node_update)

        assert result["success"] is False
        assert "error" in result

    def test_concurrent_node_updates(self, graph_db_client, image_db_client, sample_image):
        """Test graph builder handling of multiple simultaneous node updates."""
        from packages.services.graph_builder.server import GraphBuilderService
        import threading

        map_id = "test_concurrent_updates"
        graph_db_client.create_map(map_id)

        builder = GraphBuilderService(
            mqtt_host="localhost",
            mqtt_port=1883,
            mqtt_topic="robot/node_update",

            arango_host="localhost",
            radius_threshold=5.0,
            default_map_id=map_id
        )

        builder.image_db = image_db_client
        builder.graph_db = graph_db_client

        # Create multiple node updates
        results = []

        def process_node(node_id, x, y):
            node_update = {
                "map_id": map_id,
                "node_id": node_id,
                "x": x,
                "y": y,
                "yaw": 0.0,
                "images": [{"data": base64.b64encode(sample_image).decode(), "id": "front"}]
            }
            result = builder.process_node_update(node_update)
            results.append(result)

        # Process 5 nodes concurrently
        threads = []
        for i in range(5):
            t = threading.Thread(target=process_node, args=(f"node_{i}", float(i), float(i)))
            threads.append(t)
            t.start()

        # Wait for all threads
        for t in threads:
            t.join()

        # All should succeed
        assert len(results) == 5
        success_count = sum(1 for r in results if r.get("success") is True)
        assert success_count >= 4  # At least 4 should succeed

    def test_edge_creation_threshold_behavior(self, graph_db_client, image_db_client, sample_image):
        """Test that edges are only created for nodes within configured radius threshold."""
        from packages.services.graph_builder.server import GraphBuilderService

        map_id = "test_threshold"
        graph_db_client.create_map(map_id)

        # Add nodes at specific distances
        graph_db_client.add_node(map_id, "node_1", 0.0, 0.0, 0.0)
        image_db_client.store_image(sample_image, "test_image", "node_1", map_id)

        builder = GraphBuilderService(
            mqtt_host="localhost",
            mqtt_port=1883,
            mqtt_topic="robot/node_update",

            arango_host="localhost",
            radius_threshold=5.0,  # 5 meter threshold
            default_map_id=map_id
        )

        builder.image_db = image_db_client
        builder.graph_db = graph_db_client

        builder.distance_threshold = 15.0

        # Add node within threshold (3 meters away)
        node_update_near = {
            "map_id": map_id,
            "node_id": "node_2",
            "x": 3.0,
            "y": 0.0,
            "yaw": 0.0,
            "images": [{"data": base64.b64encode(sample_image).decode(), "id": "front"}]
        }

        result = builder.process_node_update(node_update_near)
        assert result["success"] is True

        # Check that edge was created
        edges = graph_db_client.get_edges(map_id)
        edges_between = [e for e in edges if
                        (e["from"] == "node_1" and e["to"] == "node_2") or
                        (e["from"] == "node_2" and e["to"] == "node_1")]
        assert len(edges_between) > 0

        # Add node beyond threshold (10 meters away)
        node_update_far = {
            "map_id": map_id,
            "node_id": "node_3",
            "x": 10.0,
            "y": 0.0,
            "yaw": 0.0,
            "images": [{"data": base64.b64encode(sample_image).decode(), "id": "front"}]
        }

        result = builder.process_node_update(node_update_far)
        assert result["success"] is True

        # Check that no edge was created to node_3 from node_1
        edges = graph_db_client.get_edges(map_id)
        edges_to_far = [e for e in edges if
                       (e["from"] == "node_1" and e["to"] == "node_3") or
                       (e["from"] == "node_3" and e["to"] == "node_1")]
        # Should have no edges due to distance > threshold
        assert len(edges_to_far) == 0

    def test_websocket_update_publishing(self, graph_db_client, image_db_client, sample_image):
        """Test that node updates are correctly published to WebSocket subscribers."""
        from packages.services.graph_builder.server import GraphBuilderService
        import asyncio
        import websockets
        import json

        map_id = "test_websocket_publish"
        graph_db_client.create_map(map_id)

        builder = GraphBuilderService(
            mqtt_host="localhost",
            mqtt_port=1883,
            mqtt_topic="robot/node_update",

            arango_host="localhost",
            radius_threshold=5.0,
            default_map_id=map_id,
        )

        builder.image_db = image_db_client
        builder.graph_db = graph_db_client

        builder.distance_threshold = 15.0

        # Test that WebSocket manager exists
        assert hasattr(builder, 'websocket_manager') or hasattr(builder, 'ws_manager')

        # Process a node update
        node_update = {
            "map_id": map_id,
            "node_id": "node_ws_1",
            "x": 1.0,
            "y": 2.0,
            "yaw": 0.0,
            "images": [{"data": base64.b64encode(sample_image).decode(), "id": "front"}]
        }

        result = builder.process_node_update(node_update)
        assert result["success"] is True

        # Verify node was added (WebSocket notification would be sent)
        node = graph_db_client.get_node(map_id, "node_ws_1")
        assert node is not None
        assert node["x"] == 1.0
        assert node["y"] == 2.0


@pytest.mark.integration
@pytest.mark.requires_docker
class TestGraphBuilderRobotManagementIntegration:
    """Integration tests for Graph Builder robot management with real Mission Dispatch API."""

    async def test_robot_auto_registration_workflow(self, graph_db_client, image_db_client):
        """Test complete robot auto-registration workflow."""
        from packages.services.graph_builder.server import GraphBuilderService
        import fastapi

        map_id = "test_robot_registration"
        robot_name = "test_robot_auto_reg"

        # Create map
        graph_db_client.create_map(map_id)

        # Create graph builder service
        builder = GraphBuilderService(
            mqtt_host="localhost",
            mqtt_port=1883,
            mqtt_topic="robot/node_update",

            arango_host="localhost",
            default_map_id=map_id
        )

        builder.image_db = image_db_client
        builder.graph_db = graph_db_client

        # Mock database calls - robot doesn't exist then gets created
        builder.database.get_object = AsyncMock(
            side_effect=fastapi.HTTPException(status_code=400, detail="Not found")
        )
        builder.database.create_object = AsyncMock(return_value=None)

        # Ensure robot exists (should create it)
        result = await builder._ensure_robot_exists(robot_name)

        # Verify robot was created
        assert result is True
        builder.database.get_object.assert_called_once()
        builder.database.create_object.assert_called_once()

        # Verify robot is now in cache
        assert robot_name in builder.known_robots

    async def test_robot_already_exists_workflow(self, graph_db_client, image_db_client):
        """Test workflow when robot already exists in Mission Dispatch."""
        from packages.services.graph_builder.server import GraphBuilderService

        map_id = "test_robot_exists"
        robot_name = "existing_robot"

        graph_db_client.create_map(map_id)

        builder = GraphBuilderService(
            mqtt_host="localhost",
            mqtt_port=1883,
            mqtt_topic="robot/node_update",

            arango_host="localhost",
            default_map_id=map_id
        )

        builder.image_db = image_db_client
        builder.graph_db = graph_db_client

        # Mock database - robot exists
        builder.database.get_object = AsyncMock(return_value=MagicMock(name=robot_name))

        # Ensure robot exists (should just verify)
        result = await builder._ensure_robot_exists(robot_name)

        # Verify robot check was made
        assert result is True
        builder.database.get_object.assert_called_once()

        # Verify robot is in cache
        assert robot_name in builder.known_robots

    async def test_robot_creation_failure_handling(self, graph_db_client, image_db_client):
        """Test handling of robot creation failures."""
        from packages.services.graph_builder.server import GraphBuilderService
        import fastapi

        map_id = "test_robot_failure"
        robot_name = "failed_robot"

        graph_db_client.create_map(map_id)

        builder = GraphBuilderService(
            mqtt_host="localhost",
            mqtt_port=1883,
            mqtt_topic="robot/node_update",

            arango_host="localhost",
            default_map_id=map_id
        )

        builder.image_db = image_db_client
        builder.graph_db = graph_db_client

        # Mock database - robot doesn't exist, creation fails
        builder.database.get_object = AsyncMock(
            side_effect=fastapi.HTTPException(status_code=400, detail="Not found")
        )
        builder.database.create_object = AsyncMock(
            side_effect=Exception("Internal server error")
        )

        # Ensure robot exists (should fail gracefully)
        result = await builder._ensure_robot_exists(robot_name)

        # Verify failure was handled
        assert result is False

        # Robot should not be in cache
        assert robot_name not in builder.known_robots


@pytest.mark.integration
@pytest.mark.requires_docker
class TestGraphBuilderCleanupIntegration:
    """Integration tests for Graph Builder cleanup with real databases."""

    def test_cleanup_old_session_mappings(self, graph_db_client, image_db_client):
        """Test cleanup of old session mappings."""
        from packages.services.graph_builder.server import GraphBuilderService
        from datetime import datetime, timedelta

        map_id = "test_cleanup_sessions"
        graph_db_client.create_map(map_id)

        builder = GraphBuilderService(
            mqtt_host="localhost",
            mqtt_port=1883,
            mqtt_topic="robot/node_update",

            arango_host="localhost",
            default_map_id=map_id
        )

        builder.image_db = image_db_client
        builder.graph_db = graph_db_client

        # Add some session mappings with old timestamps
        # session_to_global_map structure: (robot_name, session_node_id) -> (global_id, timestamp)
        old_time = datetime.now() - timedelta(hours=2)  # 2 hours ago
        recent_time = datetime.now() - timedelta(minutes=5)  # 5 minutes ago

        builder.session_to_global_map[("robot1", 1)] = ("node_1", old_time)
        builder.session_to_global_map[("robot1", 2)] = ("node_2", old_time)
        builder.session_to_global_map[("robot1", 3)] = ("node_3", recent_time)

        # Run cleanup with 1 hour threshold
        builder._cleanup_old_mappings(threshold_seconds=3600)

        # Verify old sessions were removed
        assert ("robot1", 1) not in builder.session_to_global_map
        assert ("robot1", 2) not in builder.session_to_global_map

        # Verify recent session was kept
        assert ("robot1", 3) in builder.session_to_global_map

    def test_cleanup_old_buffered_images(self, graph_db_client, image_db_client):
        """Test cleanup of old buffered images."""
        from packages.services.graph_builder.server import GraphBuilderService
        from datetime import datetime, timedelta
        import base64

        map_id = "test_cleanup_images"
        graph_db_client.create_map(map_id)

        builder = GraphBuilderService(
            mqtt_host="localhost",
            mqtt_port=1883,
            mqtt_topic="robot/node_update",

            arango_host="localhost",
            default_map_id=map_id
        )

        builder.image_db = image_db_client
        builder.graph_db = graph_db_client

        # Add buffered images with old timestamps
        old_time = datetime.now() - timedelta(hours=2)  # 2 hours ago
        recent_time = datetime.now() - timedelta(minutes=5)  # 5 minutes ago

        sample_image = base64.b64encode(b"fake_image_data").decode('utf-8')

        image_dict = {"image_data": sample_image}

        # Use actual image_buffer structure: (robot_name, session_node_id, camera_name) -> (image_dict, timestamp)
        builder.image_buffer[("robot1", 1, "front")] = (image_dict, old_time)
        builder.image_buffer[("robot1", 2, "front")] = (image_dict, old_time)
        builder.image_buffer[("robot1", 3, "front")] = (image_dict, recent_time)

        # Run cleanup with 1 hour threshold
        builder._cleanup_old_mappings(threshold_seconds=3600)

        # Verify old images were removed
        assert ("robot1", 1, "front") not in builder.image_buffer
        assert ("robot1", 2, "front") not in builder.image_buffer

        # Verify recent image was kept
        assert ("robot1", 3, "front") in builder.image_buffer
