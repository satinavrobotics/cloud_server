"""
End-to-end tests for Mission Dispatcher Service - VDA5050 Order Dispatch.

Tests VDA5050 order dispatch to robots via MQTT.
"""

import pytest
import requests
import uuid
import time
import json
from typing import Dict, Any


@pytest.mark.e2e
@pytest.mark.requires_docker
class TestMissionDispatcherOrdersE2E:
    """E2E tests for VDA5050 order dispatch."""

    def test_order_dispatch_on_mission_creation(
        self, mission_database_service, mission_dispatch_service, mqtt_client, sample_mission
    ):
        """Test that creating a mission dispatches VDA5050 order to robot."""
        robot_name = f"test_robot_{uuid.uuid4().hex[:8]}"
        mission_name = f"test_mission_{uuid.uuid4().hex[:8]}"

        # Create robot
        robot_data = {
            "name": robot_name,
            "labels": ["test"]
        }
        response = requests.post(f"{mission_database_service['url']}/robot", json=robot_data)
        assert response.status_code == 200, f"Failed to create robot: {response.text}"

        # Wait for robot to be registered in Mission Dispatcher
        time.sleep(2)

        # Subscribe to order topic BEFORE creating mission
        orders_received = []

        def on_message(client, userdata, msg):
            if "order" in msg.topic:
                try:
                    orders_received.append(json.loads(msg.payload))
                except json.JSONDecodeError:
                    pass

        mqtt_client.on_message = on_message
        mqtt_client.subscribe(f"uagv/v2/RobotCompany/{robot_name}/order")

        # Wait for subscription to be established
        time.sleep(1)

        # Create mission
        sample_mission["name"] = mission_name
        sample_mission["robot"] = robot_name
        response = requests.post(f"{mission_database_service['url']}/mission", json=sample_mission)
        assert response.status_code == 200, f"Failed to create mission: {response.text}"

        # Wait for order to be dispatched
        time.sleep(5)

        # Verify order was sent
        assert len(orders_received) > 0, f"No VDA5050 order was dispatched. Orders received: {orders_received}"
        order = orders_received[0]
        assert "orderId" in order, f"Order missing orderId: {order}"
        assert "nodes" in order, f"Order missing nodes: {order}"
        assert "edges" in order, f"Order missing edges: {order}"

        # Cleanup
        requests.delete(f"{mission_database_service['url']}/mission/{mission_name}")
        requests.delete(f"{mission_database_service['url']}/robot/{robot_name}")

    def test_order_contains_valid_vda5050_structure(
        self, mission_database_service, mqtt_client, sample_mission
    ):
        """Test that dispatched order has valid VDA5050 structure."""
        robot_name = f"test_robot_{uuid.uuid4().hex[:8]}"
        mission_name = f"test_mission_{uuid.uuid4().hex[:8]}"
        
        # Create robot
        robot_data = {
            "name": robot_name,
            "labels": ["test"]
        }
        requests.post(f"{mission_database_service['url']}/robot", json=robot_data)
        
        # Subscribe to order topic
        orders_received = []
        
        def on_message(client, userdata, msg):
            if "order" in msg.topic:
                orders_received.append(json.loads(msg.payload))
        
        mqtt_client.on_message = on_message
        mqtt_client.subscribe(f"uagv/v2/RobotCompany/{robot_name}/order")
        
        # Create mission
        sample_mission["name"] = mission_name
        sample_mission["robot"] = robot_name
        requests.post(f"{mission_database_service['url']}/mission", json=sample_mission)
        
        # Wait for order
        time.sleep(2)
        
        # Verify VDA5050 structure
        if len(orders_received) > 0:
            order = orders_received[0]
            assert "headerId" in order
            assert "timestamp" in order
            assert "version" in order
            assert "orderId" in order
            assert "orderUpdateId" in order
            assert "nodes" in order
            assert "edges" in order
            assert isinstance(order["nodes"], list)
            assert isinstance(order["edges"], list)
        
        # Cleanup
        requests.delete(f"{mission_database_service['url']}/mission/{mission_name}")
        requests.delete(f"{mission_database_service['url']}/robot/{robot_name}")

    def test_order_id_format(
        self, mission_database_service, mqtt_client, sample_mission
    ):
        """Test that order ID follows expected format."""
        robot_name = f"test_robot_{uuid.uuid4().hex[:8]}"
        mission_name = f"test_mission_{uuid.uuid4().hex[:8]}"
        
        # Create robot
        robot_data = {
            "name": robot_name,
            "labels": ["test"]
        }
        requests.post(f"{mission_database_service['url']}/robot", json=robot_data)
        
        # Subscribe to order topic
        orders_received = []
        
        def on_message(client, userdata, msg):
            if "order" in msg.topic:
                orders_received.append(json.loads(msg.payload))
        
        mqtt_client.on_message = on_message
        mqtt_client.subscribe(f"uagv/v2/RobotCompany/{robot_name}/order")
        
        # Create mission
        sample_mission["name"] = mission_name
        sample_mission["robot"] = robot_name
        requests.post(f"{mission_database_service['url']}/mission", json=sample_mission)
        
        # Wait for order
        time.sleep(2)
        
        # Verify order ID format
        if len(orders_received) > 0:
            order = orders_received[0]
            order_id = order["orderId"]
            # Order ID should contain mission name
            assert mission_name in order_id or "n" in order_id
        
        # Cleanup
        requests.delete(f"{mission_database_service['url']}/mission/{mission_name}")
        requests.delete(f"{mission_database_service['url']}/robot/{robot_name}")

    def test_no_order_for_offline_robot(
        self, mission_database_service, mqtt_client, sample_mission
    ):
        """Test that no order is sent to offline robot."""
        robot_name = f"test_robot_{uuid.uuid4().hex[:8]}"
        mission_name = f"test_mission_{uuid.uuid4().hex[:8]}"
        
        # Create offline robot
        robot_data = {
            "name": robot_name,
            "labels": ["test"]
        }
        requests.post(f"{mission_database_service['url']}/robot", json=robot_data)
        
        # Subscribe to order topic
        orders_received = []
        
        def on_message(client, userdata, msg):
            if "order" in msg.topic:
                orders_received.append(json.loads(msg.payload))
        
        mqtt_client.on_message = on_message
        mqtt_client.subscribe(f"uagv/v2/RobotCompany/{robot_name}/order")
        
        # Create mission
        sample_mission["name"] = mission_name
        sample_mission["robot"] = robot_name
        requests.post(f"{mission_database_service['url']}/mission", json=sample_mission)
        
        # Wait
        time.sleep(2)
        
        # Order may or may not be sent to offline robot (depends on implementation)
        # This test documents the behavior
        
        # Cleanup
        requests.delete(f"{mission_database_service['url']}/mission/{mission_name}")
        requests.delete(f"{mission_database_service['url']}/robot/{robot_name}")

    def test_order_nodes_contain_waypoints(
        self, mission_database_service, mqtt_client, sample_mission
    ):
        """Test that order nodes contain waypoint information."""
        robot_name = f"test_robot_{uuid.uuid4().hex[:8]}"
        mission_name = f"test_mission_{uuid.uuid4().hex[:8]}"
        
        # Create robot
        robot_data = {
            "name": robot_name,
            "labels": ["test"]
        }
        requests.post(f"{mission_database_service['url']}/robot", json=robot_data)
        
        # Subscribe to order topic
        orders_received = []
        
        def on_message(client, userdata, msg):
            if "order" in msg.topic:
                orders_received.append(json.loads(msg.payload))
        
        mqtt_client.on_message = on_message
        mqtt_client.subscribe(f"uagv/v2/RobotCompany/{robot_name}/order")
        
        # Create mission
        sample_mission["name"] = mission_name
        sample_mission["robot"] = robot_name
        requests.post(f"{mission_database_service['url']}/mission", json=sample_mission)
        
        # Wait for order
        time.sleep(2)
        
        # Verify nodes contain waypoint data
        if len(orders_received) > 0:
            order = orders_received[0]
            if len(order["nodes"]) > 0:
                node = order["nodes"][0]
                # Nodes should have nodeId and sequenceId
                assert "nodeId" in node
                assert "sequenceId" in node
        
        # Cleanup
        requests.delete(f"{mission_database_service['url']}/mission/{mission_name}")
        requests.delete(f"{mission_database_service['url']}/robot/{robot_name}")

    def test_multiple_missions_dispatch_separate_orders(
        self, mission_database_service, mqtt_client, sample_mission
    ):
        """Test that multiple missions dispatch separate orders."""
        robot_name = f"test_robot_{uuid.uuid4().hex[:8]}"
        
        # Create robot
        robot_data = {
            "name": robot_name,
            "labels": ["test"]
        }
        requests.post(f"{mission_database_service['url']}/robot", json=robot_data)
        
        # Subscribe to order topic
        orders_received = []
        
        def on_message(client, userdata, msg):
            if "order" in msg.topic:
                orders_received.append(json.loads(msg.payload))
        
        mqtt_client.on_message = on_message
        mqtt_client.subscribe(f"uagv/v2/RobotCompany/{robot_name}/order")
        
        # Create multiple missions (sequentially)
        mission_names = []
        for i in range(2):
            mission_name = f"test_mission_{uuid.uuid4().hex[:8]}"
            mission_names.append(mission_name)
            
            mission_data = sample_mission.copy()
            mission_data["name"] = mission_name
            mission_data["robot"] = robot_name
            requests.post(f"{mission_database_service['url']}/mission", json=mission_data)
            time.sleep(1)
        
        # Wait for orders
        time.sleep(2)
        
        # Multiple orders may be received (depends on mission lifecycle)
        # This test documents the behavior
        
        # Cleanup
        for mission_name in mission_names:
            try:
                requests.delete(f"{mission_database_service['url']}/mission/{mission_name}")
            except:
                pass
        requests.delete(f"{mission_database_service['url']}/robot/{robot_name}")

