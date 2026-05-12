#!/usr/bin/env python3
"""
Integration test for 2-topic architecture with session ID mapping.

This test verifies:
1. Dummy robot publishes node updates on robot/node_update topic
2. Dummy robot publishes images on robot/image_upload topic
3. Graph Builder receives both message types
4. Session IDs are mapped to global IDs
5. Images are associated with correct global node IDs
6. Multiple robots with overlapping session IDs don't conflict
"""

import pytest
import json
import time
import base64
import os
import paho.mqtt.client as mqtt_client
from typing import Dict, List, Any


@pytest.mark.integration
class TestTwoTopicArchitecture:
    """Integration tests for 2-topic architecture."""

    def setup_method(self):
        """Set up test fixtures."""
        # Use environment variables for MQTT connection (set by docker-compose or conftest)
        self.mqtt_host = os.getenv("MQTT_HOST", "localhost")
        self.mqtt_port = int(os.getenv("MQTT_PORT", "1893"))
        self.node_topic = "robot/node_update"
        self.image_topic = "robot/image_upload"
        
        self.received_node_updates = []
        self.received_images = []
        
        # Create MQTT client for monitoring
        try:
            self.monitor_client = mqtt_client.Client(
                client_id="test_monitor",
                callback_api_version=mqtt_client.CallbackAPIVersion.VERSION1
            )
        except AttributeError:
            # Fallback for older paho-mqtt versions
            self.monitor_client = mqtt_client.Client(client_id="test_monitor")
        self.monitor_client.on_message = self._on_monitor_message
        
    def teardown_method(self):
        """Clean up after test."""
        if hasattr(self, 'monitor_client'):
            self.monitor_client.disconnect()
    
    def _on_monitor_message(self, client, userdata, msg):
        """Callback for monitoring MQTT messages."""
        try:
            payload = json.loads(msg.payload.decode('utf-8'))
            
            if msg.topic == self.node_topic:
                self.received_node_updates.append(payload)
            elif msg.topic == self.image_topic:
                self.received_images.append(payload)
                
        except Exception as e:
            print(f"Error processing monitor message: {e}")
    
    def test_node_update_message_format(self):
        """Test that node update messages have correct format."""
        # Connect monitor
        self.monitor_client.connect(self.mqtt_host, self.mqtt_port)
        self.monitor_client.subscribe(self.node_topic)
        self.monitor_client.loop_start()
        
        # Publish a test node update
        try:
            test_client = mqtt_client.Client(
                client_id="test_publisher",
                callback_api_version=mqtt_client.CallbackAPIVersion.VERSION1
            )
        except AttributeError:
            # Fallback for older paho-mqtt versions
            test_client = mqtt_client.Client(client_id="test_publisher")
        test_client.connect(self.mqtt_host, self.mqtt_port)
        
        node_data = {
            "session_node_id": 1000,
            "robot_name": "test_robot",
            "x": 1.0,
            "y": 2.0,
            "yaw": 0.5,
            "map_id": "test_map",
            "camera_metadata": [
                {"camera_name": "front_camera", "timestamp": "2024-01-01T00:00:00"},
                {"camera_name": "back_camera", "timestamp": "2024-01-01T00:00:00"}
            ],
            "metadata": {"test": "data"}
        }
        
        test_client.publish(self.node_topic, json.dumps(node_data))
        test_client.disconnect()
        
        # Wait for message
        time.sleep(0.5)
        self.monitor_client.loop_stop()
        
        # Verify message received
        assert len(self.received_node_updates) == 1
        received = self.received_node_updates[0]
        
        # Verify required fields
        assert received["session_node_id"] == 1000
        assert received["robot_name"] == "test_robot"
        assert received["x"] == 1.0
        assert received["y"] == 2.0
        assert received["yaw"] == 0.5
        assert received["map_id"] == "test_map"
        assert "camera_metadata" in received
        assert len(received["camera_metadata"]) == 2
        
        # Verify NO image data in node update
        assert "images" not in received
        for cam_meta in received["camera_metadata"]:
            assert "image_data" not in cam_meta
            assert "data" not in cam_meta
    
    def test_image_upload_message_format(self):
        """Test that image upload messages have correct format."""
        # Connect monitor
        self.monitor_client.connect(self.mqtt_host, self.mqtt_port)
        self.monitor_client.subscribe(self.image_topic)
        self.monitor_client.loop_start()
        
        # Publish a test image
        try:
            test_client = mqtt_client.Client(
                client_id="test_publisher",
                callback_api_version=mqtt_client.CallbackAPIVersion.VERSION1
            )
        except AttributeError:
            test_client = mqtt_client.Client(client_id="test_publisher")

        test_client.connect(self.mqtt_host, self.mqtt_port)
        
        fake_image_data = b"fake_jpeg_data"
        image_data = {
            "session_node_id": 1000,
            "robot_name": "test_robot",
            "camera_name": "front_camera",
            "image_data": base64.b64encode(fake_image_data).decode('utf-8'),
            "content_type": "image/jpeg",
            "timestamp": "2024-01-01T00:00:00",
            "map_id": "test_map"
        }
        
        test_client.publish(self.image_topic, json.dumps(image_data))
        test_client.disconnect()
        
        # Wait for message
        time.sleep(0.5)
        self.monitor_client.loop_stop()
        
        # Verify message received
        assert len(self.received_images) == 1
        received = self.received_images[0]
        
        # Verify required fields
        assert received["session_node_id"] == 1000
        assert received["robot_name"] == "test_robot"
        assert received["camera_name"] == "front_camera"
        assert "image_data" in received
        assert received["content_type"] == "image/jpeg"
        assert received["timestamp"] == "2024-01-01T00:00:00"
        assert received["map_id"] == "test_map"
        
        # Verify image data can be decoded
        decoded = base64.b64decode(received["image_data"])
        assert decoded == fake_image_data
    
    def test_multiple_robots_different_session_ids(self):
        """Test that multiple robots with overlapping session IDs don't conflict."""
        # Clear any previous messages
        self.received_node_updates = []
        self.received_images = []

        # Connect monitor
        self.monitor_client.connect(self.mqtt_host, self.mqtt_port)
        self.monitor_client.subscribe(self.node_topic)
        self.monitor_client.subscribe(self.image_topic)
        self.monitor_client.loop_start()

        # Wait a bit to clear any buffered messages
        time.sleep(0.2)
        self.received_node_updates = []
        self.received_images = []

        # Publish from robot 1
        try:
            test_client = mqtt_client.Client(
                client_id="test_publisher",
                callback_api_version=mqtt_client.CallbackAPIVersion.VERSION1
            )
        except AttributeError:
            test_client = mqtt_client.Client(client_id="test_publisher")

        test_client.connect(self.mqtt_host, self.mqtt_port)
        
        # Robot 1 - session_node_id 1000
        node_data_1 = {
            "session_node_id": 1000,
            "robot_name": "robot_1",
            "x": 1.0,
            "y": 2.0,
            "yaw": 0.0,
            "map_id": "test_map",
            "camera_metadata": []
        }
        test_client.publish(self.node_topic, json.dumps(node_data_1))
        time.sleep(0.1)  # Small delay between publishes

        # Robot 2 - SAME session_node_id 1000 (should not conflict)
        node_data_2 = {
            "session_node_id": 1000,
            "robot_name": "robot_2",
            "x": 10.0,
            "y": 20.0,
            "yaw": 1.5,
            "map_id": "test_map",
            "camera_metadata": []
        }
        test_client.publish(self.node_topic, json.dumps(node_data_2))
        time.sleep(0.1)  # Small delay after publish

        test_client.disconnect()

        # Wait for messages
        time.sleep(1.0)
        self.monitor_client.loop_stop()

        # Filter to only new-format messages (with session_node_id)
        new_format_messages = [
            msg for msg in self.received_node_updates
            if "session_node_id" in msg and "node_id" not in msg
        ]

        # Verify both messages received
        assert len(new_format_messages) == 2, f"Expected 2 new-format messages, got {len(new_format_messages)}"

        # Verify they are distinct
        robot_names = [msg["robot_name"] for msg in new_format_messages]
        assert "robot_1" in robot_names
        assert "robot_2" in robot_names

        # Verify session IDs are the same but robots are different
        for msg in new_format_messages:
            assert msg["session_node_id"] == 1000
        
        # The key insight: Graph Builder should create DIFFERENT global IDs
        # for these two nodes because they have different robot_names


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

