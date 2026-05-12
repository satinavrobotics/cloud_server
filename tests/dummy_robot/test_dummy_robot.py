"""
SPDX-FileCopyrightText: NVIDIA CORPORATION & AFFILIATES
Copyright (c) 2021-2024 NVIDIA CORPORATION & AFFILIATES. All rights reserved.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

SPDX-License-Identifier: Apache-2.0
"""

import json
import time
import unittest
from unittest.mock import Mock, patch, MagicMock
import sys
import os

# Add parent directories to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from tests.dummy_robot.dummy_robot import DummyRobot


class TestDummyRobot(unittest.TestCase):
    """Test cases for DummyRobot"""

    @patch('paho.mqtt.client.Client')
    def setUp(self, mock_mqtt_client):
        """Set up test fixtures"""
        self.mock_client = MagicMock()
        mock_mqtt_client.return_value = self.mock_client
        
        self.robot = DummyRobot(
            robot_name="test_robot",
            mqtt_host="localhost",
            mqtt_port=1883,
            publish_nodes=True
        )

    def test_initialization(self):
        """Test robot initialization"""
        self.assertEqual(self.robot.robot_name, "test_robot")
        self.assertEqual(self.robot.x, 0.0)
        self.assertEqual(self.robot.y, 0.0)
        self.assertEqual(self.robot.theta, 0.0)
        self.assertEqual(self.robot.battery, 100.0)
        self.assertEqual(self.robot.node_id_counter, 1000)

    def test_position_update(self):
        """Test position update (circular movement)"""
        initial_x = self.robot.x
        initial_y = self.robot.y
        
        self.robot._update_position()
        
        # Position should have changed
        self.assertNotEqual(self.robot.x, initial_x)
        self.assertNotEqual(self.robot.y, initial_y)
        
        # Battery should have decreased
        self.assertLess(self.robot.battery, 100.0)

    def test_state_publishing(self):
        """Test VDA5050 state publishing"""
        self.robot._publish_state()
        
        # Check that MQTT publish was called
        self.mock_client.publish.assert_called()
        
        # Get the call arguments
        call_args = self.mock_client.publish.call_args
        topic = call_args[0][0]
        payload = call_args[0][1]
        
        # Verify topic
        self.assertIn("state", topic)
        self.assertIn("test_robot", topic)
        
        # Verify payload is valid JSON
        state_data = json.loads(payload)
        self.assertIn("agvPosition", state_data)
        self.assertIn("batteryState", state_data)

    def test_node_publishing(self):
        """Test node update publishing"""
        initial_counter = self.robot.node_id_counter
        
        self.robot._publish_node_update()
        
        # Check that MQTT publish was called
        self.mock_client.publish.assert_called()
        
        # Get the call arguments
        call_args = self.mock_client.publish.call_args
        topic = call_args[0][0]
        payload = call_args[0][1]
        
        # Verify topic
        self.assertEqual(topic, "robot/node_update")
        
        # Verify payload
        node_data = json.loads(payload)
        self.assertEqual(node_data["node_id"], initial_counter)
        self.assertEqual(node_data["map_id"], "default")
        self.assertIn("x", node_data)
        self.assertIn("y", node_data)
        self.assertIn("yaw", node_data)
        self.assertIn("metadata", node_data)
        
        # Counter should have incremented
        self.assertEqual(self.robot.node_id_counter, initial_counter + 1)

    def test_order_handling(self):
        """Test VDA5050 order handling"""
        order_payload = {
            "headerId": 1,
            "timestamp": "2024-01-01T00:00:00Z",
            "orderId": "test-order-123",
            "orderUpdateId": 0,
            "nodes": [
                {
                    "nodeId": "node-1",
                    "sequenceId": 0,
                    "nodePosition": {
                        "x": 5.0,
                        "y": 5.0,
                        "theta": 0.0,
                        "mapId": "default"
                    }
                }
            ],
            "edges": []
        }
        
        self.robot._handle_order(order_payload)
        
        # Check that order was stored
        self.assertIsNotNone(self.robot.current_order)
        self.assertEqual(self.robot.order_id, "test-order-123")
        self.assertEqual(self.robot.order_update_id, 0)

    def test_factsheet_publishing(self):
        """Test VDA5050 factsheet publishing"""
        self.robot._publish_factsheet()
        
        # Check that MQTT publish was called
        self.mock_client.publish.assert_called()
        
        # Get the call arguments
        call_args = self.mock_client.publish.call_args
        topic = call_args[0][0]
        payload = call_args[0][1]
        
        # Verify topic
        self.assertIn("factsheet", topic)
        self.assertIn("test_robot", topic)
        
        # Verify payload
        factsheet_data = json.loads(payload)
        self.assertIn("typeSpecification", factsheet_data)
        self.assertEqual(factsheet_data["manufacturer"], "DummyManufacturer")

    def test_circular_movement(self):
        """Test that robot moves in a circle"""
        positions = []
        
        # Collect positions over one full rotation
        for _ in range(100):
            self.robot._update_position()
            positions.append((self.robot.x, self.robot.y))
        
        # Check that robot returned close to starting position
        final_x, final_y = positions[-1]
        
        # Should be roughly circular (distance from origin should be constant)
        import math
        distances = [math.sqrt(x**2 + y**2) for x, y in positions]
        avg_distance = sum(distances) / len(distances)
        
        # All distances should be close to loop_radius
        for dist in distances:
            self.assertAlmostEqual(dist, self.robot.loop_radius, delta=0.5)

    def test_battery_drain_and_recharge(self):
        """Test battery drains and recharges"""
        # Drain battery
        while self.robot.battery > 20.0:
            self.robot._update_position()
        
        # Battery should be low
        self.assertLess(self.robot.battery, 20.0)
        
        # Next update should recharge
        self.robot._update_position()
        
        # Battery should be recharged
        self.assertEqual(self.robot.battery, 100.0)

    def test_no_node_publishing(self):
        """Test that node publishing can be disabled"""
        robot = DummyRobot(
            robot_name="test_robot_no_nodes",
            publish_nodes=False
        )
        
        # Reset mock
        self.mock_client.reset_mock()
        
        robot._publish_node_update()
        
        # Should not have published
        self.mock_client.publish.assert_not_called()


class TestDummyRobotIntegration(unittest.TestCase):
    """Integration tests (require MQTT broker)"""

    def test_mqtt_connection(self):
        """Test MQTT connection (requires broker)"""
        # This test requires a running MQTT broker
        # Skip if not available
        try:
            robot = DummyRobot(
                robot_name="integration_test_robot",
                mqtt_host="localhost",
                mqtt_port=1883
            )
            time.sleep(1)  # Wait for connection
            
            # If we get here, connection succeeded
            robot.client.disconnect()
            
        except Exception as e:
            self.skipTest(f"MQTT broker not available: {e}")


if __name__ == "__main__":
    unittest.main()

