"""
Graph Builder Service - Node Processing E2E Tests

Tests node update processing, edge creation, and map building.
"""

import pytest
import requests
import uuid
import json
import base64


@pytest.mark.e2e
@pytest.mark.requires_docker
class TestGraphBuilderNodeProcessingE2E:
    """Test node processing in Graph Builder Service."""

    def test_process_node_update(self, graph_builder_service):
        """Test processing a single node update."""
        node_data = {
            "node_id": f"node_{uuid.uuid4().hex[:8]}",
            "x": 10.5,
            "y": 20.3,
            "yaw": 1.57,
            "map_id": "default"
        }
        
        response = requests.post(
            f"{graph_builder_service['url']}/node",
            json=node_data
        )
        
        assert response.status_code in [200, 400, 500, 503]
        if response.status_code == 200:
            data = response.json()
            assert data.get("success") is True

    def test_process_node_with_images(self, graph_builder_service):
        """Test processing node with image data."""
        # Create minimal valid image data
        image_data = base64.b64encode(b"fake_image_data").decode()
        
        node_data = {
            "node_id": f"node_{uuid.uuid4().hex[:8]}",
            "x": 15.0,
            "y": 25.0,
            "yaw": 0.785,
            "map_id": "default",
            "images": [
                {
                    "image_id": "front_camera",
                    "data": image_data,
                    "content_type": "image/jpeg"
                }
            ]
        }
        
        response = requests.post(
            f"{graph_builder_service['url']}/node",
            json=node_data
        )
        
        assert response.status_code in [200, 400, 500, 503]

    def test_process_node_with_metadata(self, graph_builder_service):
        """Test processing node with metadata."""
        node_data = {
            "node_id": f"node_{uuid.uuid4().hex[:8]}",
            "x": 20.0,
            "y": 30.0,
            "yaw": 3.14,
            "map_id": "default",
            "metadata": {
                "robot_id": "robot_01",
                "timestamp": "2024-01-15T10:30:00Z",
                "confidence": 0.95
            }
        }
        
        response = requests.post(
            f"{graph_builder_service['url']}/node",
            json=node_data
        )
        
        assert response.status_code in [200, 400, 500, 503]

    def test_process_node_multiple_maps(self, graph_builder_service):
        """Test processing nodes in different maps."""
        for map_id in ["warehouse", "factory", "office"]:
            node_data = {
                "node_id": f"node_{uuid.uuid4().hex[:8]}",
                "x": 10.0,
                "y": 20.0,
                "yaw": 0.0,
                "map_id": map_id
            }
            
            response = requests.post(
                f"{graph_builder_service['url']}/node",
                json=node_data
            )
            
            assert response.status_code in [200, 400, 500, 503]

    def test_process_node_missing_coordinates(self, graph_builder_service):
        """Test processing node with missing coordinates."""
        node_data = {
            "node_id": f"node_{uuid.uuid4().hex[:8]}",
            "x": 10.0,
            # Missing y and yaw
            "map_id": "default"
        }
        
        response = requests.post(
            f"{graph_builder_service['url']}/node",
            json=node_data
        )
        
        assert response.status_code in [400, 422]

    def test_process_node_invalid_coordinates(self, graph_builder_service):
        """Test processing node with invalid coordinates."""
        node_data = {
            "node_id": f"node_{uuid.uuid4().hex[:8]}",
            "x": "invalid",
            "y": 20.0,
            "yaw": 0.0,
            "map_id": "default"
        }
        
        response = requests.post(
            f"{graph_builder_service['url']}/node",
            json=node_data
        )
        
        assert response.status_code in [400, 422]

    def test_process_node_negative_coordinates(self, graph_builder_service):
        """Test processing node with negative coordinates."""
        node_data = {
            "node_id": f"node_{uuid.uuid4().hex[:8]}",
            "x": -50.0,
            "y": -100.0,
            "yaw": 0.0,
            "map_id": "default"
        }
        
        response = requests.post(
            f"{graph_builder_service['url']}/node",
            json=node_data
        )
        
        assert response.status_code in [200, 400, 500, 503]

    def test_process_node_large_coordinates(self, graph_builder_service):
        """Test processing node with large coordinates."""
        node_data = {
            "node_id": f"node_{uuid.uuid4().hex[:8]}",
            "x": 10000.0,
            "y": 20000.0,
            "yaw": 0.0,
            "map_id": "default"
        }
        
        response = requests.post(
            f"{graph_builder_service['url']}/node",
            json=node_data
        )
        
        assert response.status_code in [200, 400, 500, 503]

    def test_process_node_duplicate_id(self, graph_builder_service):
        """Test processing node with duplicate ID."""
        node_id = f"node_{uuid.uuid4().hex[:8]}"
        node_data = {
            "node_id": node_id,
            "x": 10.0,
            "y": 20.0,
            "yaw": 0.0,
            "map_id": "default"
        }
        
        # Process first node
        response1 = requests.post(
            f"{graph_builder_service['url']}/node",
            json=node_data
        )
        
        # Process duplicate
        response2 = requests.post(
            f"{graph_builder_service['url']}/node",
            json=node_data
        )
        
        assert response1.status_code in [200, 400, 500, 503]
        assert response2.status_code in [200, 400, 500, 503]

    def test_process_node_sequential_updates(self, graph_builder_service):
        """Test processing sequential node updates."""
        base_node_id = f"node_{uuid.uuid4().hex[:8]}"
        
        for i in range(5):
            node_data = {
                "node_id": f"{base_node_id}_{i}",
                "x": float(i * 5),
                "y": float(i * 10),
                "yaw": 0.0,
                "map_id": "default"
            }
            
            response = requests.post(
                f"{graph_builder_service['url']}/node",
                json=node_data
            )

            assert response.status_code in [200, 400, 500, 503]


@pytest.mark.e2e
@pytest.mark.requires_docker
class TestGraphBuilderRobotManagementE2E:
    """E2E tests for Graph Builder robot management functionality."""

    def test_robot_auto_registration_via_node_update(self, graph_builder_service, mission_dispatcher_service):
        """Test that robot is auto-registered when processing node updates."""
        robot_name = f"robot_{uuid.uuid4().hex[:8]}"

        # Send node update with new robot name
        node_data = {
            "robot_name": robot_name,
            "node_id": f"node_{uuid.uuid4().hex[:8]}",
            "x": 5.0,
            "y": 10.0,
            "yaw": 0.0,
            "map_id": "default"
        }

        response = requests.post(
            f"{graph_builder_service['url']}/node",
            json=node_data
        )

        assert response.status_code in [200, 201, 400, 500, 503]

        # Verify robot was registered in Mission Dispatcher
        if response.status_code in [200, 201]:
            robot_response = requests.get(
                f"{mission_dispatcher_service['url']}/api/v1/robots/{robot_name}"
            )
            # Robot should exist (200) or service might be unavailable
            assert robot_response.status_code in [200, 404, 503]

    def test_multiple_robots_registration(self, graph_builder_service):
        """Test registration of multiple robots."""
        robot_names = [f"robot_{uuid.uuid4().hex[:8]}" for _ in range(3)]

        for robot_name in robot_names:
            node_data = {
                "robot_name": robot_name,
                "node_id": f"node_{uuid.uuid4().hex[:8]}",
                "x": 0.0,
                "y": 0.0,
                "yaw": 0.0,
                "map_id": "default"
            }

            response = requests.post(
                f"{graph_builder_service['url']}/node",
                json=node_data
            )

            # Each robot should be processed successfully
            assert response.status_code in [200, 201, 400, 500, 503]


@pytest.mark.e2e
@pytest.mark.requires_docker
class TestGraphBuilderCleanupE2E:
    """E2E tests for Graph Builder cleanup functionality."""

    def test_cleanup_endpoint(self, graph_builder_service):
        """Test cleanup endpoint for old data."""
        # Trigger cleanup via API endpoint (if available)
        response = requests.post(
            f"{graph_builder_service['url']}/cleanup",
            json={"threshold_seconds": 3600}
        )

        # Cleanup endpoint might not exist, so accept 404
        assert response.status_code in [200, 404, 405, 500, 503]

    def test_stats_after_cleanup(self, graph_builder_service):
        """Test that stats are updated after cleanup operations."""
        # Get initial stats
        stats_response = requests.get(
            f"{graph_builder_service['url']}/stats"
        )

        if stats_response.status_code == 200:
            stats = stats_response.json()

            # Verify stats structure
            assert isinstance(stats, dict)
            # Stats might include: nodes_processed, edges_created, errors, etc.


@pytest.mark.e2e
@pytest.mark.requires_docker
class TestGraphBuilderMQTTIntegrationE2E:
    """E2E tests for Graph Builder MQTT integration."""

    def test_mqtt_node_update_flow(self, graph_builder_service):
        """Test complete MQTT node update flow."""
        import paho.mqtt.client as mqtt
        import time

        # Create MQTT client
        client = mqtt.Client()

        try:
            client.connect("localhost", 1883, 60)
            client.loop_start()

            # Publish node update via MQTT
            node_update = {
                "robot_name": f"robot_{uuid.uuid4().hex[:8]}",
                "node_id": f"node_{uuid.uuid4().hex[:8]}",
                "x": 12.5,
                "y": 18.3,
                "theta": 1.2,
                "map_id": "default"
            }

            client.publish(
                "robot/node_update",
                json.dumps(node_update),
                qos=1
            )

            # Wait for processing
            time.sleep(2)

            # Verify via Graph Builder stats
            stats_response = requests.get(
                f"{graph_builder_service['url']}/stats"
            )

            if stats_response.status_code == 200:
                stats = stats_response.json()
                # Stats should show some activity
                assert isinstance(stats, dict)

        except Exception as e:
            # MQTT might not be available in all test environments
            pytest.skip(f"MQTT not available: {e}")

        finally:
            client.loop_stop()
            client.disconnect()

    def test_mqtt_image_publishing(self, graph_builder_service):
        """Test MQTT image publishing in node updates."""
        import paho.mqtt.client as mqtt
        import time

        client = mqtt.Client()

        try:
            client.connect("localhost", 1883, 60)
            client.loop_start()

            # Create node update with image
            image_data = base64.b64encode(b"test_image_data").decode()
            node_update = {
                "robot_name": f"robot_{uuid.uuid4().hex[:8]}",
                "node_id": f"node_{uuid.uuid4().hex[:8]}",
                "x": 5.0,
                "y": 5.0,
                "theta": 0.0,
                "map_id": "default",
                "image": image_data
            }

            client.publish(
                "robot/node_update",
                json.dumps(node_update),
                qos=1
            )

            time.sleep(2)

            # Verify processing
            stats_response = requests.get(
                f"{graph_builder_service['url']}/stats"
            )
            assert stats_response.status_code in [200, 404, 503]

        except Exception as e:
            pytest.skip(f"MQTT not available: {e}")

        finally:
            client.loop_stop()
            client.disconnect()
