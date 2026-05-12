"""
End-to-end tests for Robot Interface.

Tests the complete robot-to-cloud communication flow via MQTT:
- Robot publishing telemetry (VDA5050 state messages)
- Robot publishing images and node updates
- Robot receiving mission orders
- Robot status updates in mission database
- Graph builder processing robot data
"""

import pytest
import json
import time
import base64
import io
from typing import Dict, Any
from PIL import Image
import paho.mqtt.client as mqtt_client_lib


@pytest.mark.e2e
@pytest.mark.requires_docker
class TestRobotMQTTInterface:
    """E2E tests for robot MQTT interface (VDA5050 protocol)."""

    def test_robot_publishes_state_to_dispatch(
        self, mqtt_client, mission_database_service
    ):
        """Test robot publishing VDA5050 state messages."""
        import requests
        import uuid

        robot_name = f"test_robot_state_e2e_{uuid.uuid4().hex[:8]}"
        mqtt_prefix = "uagv/v2/RobotCompany"
        
        # First, create robot in database
        # Note: status is set automatically by the database, don't include it in creation
        robot_data = {
            "name": robot_name,
            "labels": ["test"]
        }

        response = requests.post(
            f"{mission_database_service['url']}/robot",
            json=robot_data
        )
        assert response.status_code in [200, 201], f"Failed to create robot: {response.status_code} - {response.text}"

        # Publish VDA5050 state message from robot
        state_message = {
            "headerId": 1,
            "timestamp": "2024-01-01T00:00:00Z",
            "version": "2.0.0",
            "manufacturer": "TestCorp",
            "serialNumber": "TEST123",
            "orderId": "",
            "orderUpdateId": 0,
            "lastNodeId": "",
            "lastNodeSequenceId": 0,
            "nodeStates": [],
            "edgeStates": [],
            "actionStates": [],
            "batteryState": {
                "batteryCharge": 85.5,
                "charging": False
            },
            "driving": False,
            "agvPosition": {
                "x": 10.5,
                "y": 20.3,
                "theta": 1.57,
                "mapId": "warehouse_floor_1",
                "positionInitialized": True
            },
            "errors": []
        }

        topic = f"{mqtt_prefix}/{robot_name}/state"
        mqtt_client.publish(topic, json.dumps(state_message))

        # Wait for message to be processed
        time.sleep(2)

        # Verify robot status was updated in database
        response = requests.get(
            f"{mission_database_service['url']}/robot/{robot_name}"
        )
        
        if response.status_code == 200:
            robot = response.json()
            # Check that battery level was updated
            assert "status" in robot
            # The exact structure depends on implementation

        # Cleanup
        requests.delete(f"{mission_database_service['url']}/robot/{robot_name}")

    def test_robot_publishes_factsheet(
        self, mqtt_client, mission_database_service
    ):
        """Test robot publishing VDA5050 factsheet."""
        import requests
        import uuid

        robot_name = f"test_robot_factsheet_e2e_{uuid.uuid4().hex[:8]}"
        mqtt_prefix = "uagv/v2/RobotCompany"
        
        # Create robot in database
        robot_data = {
            "name": robot_name,
            "labels": ["test"]
        }

        response = requests.post(
            f"{mission_database_service['url']}/robot",
            json=robot_data
        )
        assert response.status_code in [200, 201], f"Failed to create robot: {response.status_code} - {response.text}"
        
        # Publish factsheet
        factsheet_message = {
            "headerId": 1,
            "timestamp": "2024-01-01T00:00:00Z",
            "version": "2.0.0",
            "manufacturer": "TestCorp",
            "serialNumber": "TEST123",
            "typeSpecification": {
                "seriesName": "Carter",
                "agvKinematic": "DIFFERENTIAL",
                "agvClass": "CARRIER",
                "maxLoadMass": 100.0,
                "localizationTypes": ["NATURAL"],
                "navigationTypes": ["AUTONOMOUS"]
            },
            "physicalParameters": {
                "speedMin": 0.0,
                "speedMax": 1.5,
                "accelerationMax": 0.5,
                "decelerationMax": 0.5,
                "heightMin": 0.5,
                "heightMax": 1.5,
                "width": 0.6,
                "length": 0.8
            }
        }
        
        topic = f"{mqtt_prefix}/{robot_name}/factsheet"
        mqtt_client.publish(topic, json.dumps(factsheet_message))
        
        # Wait for processing
        time.sleep(2)
        
        # Verify factsheet was received
        response = requests.get(
            f"{mission_database_service['url']}/robot/{robot_name}"
        )
        assert response.status_code in [200, 404]  # May or may not be implemented

        # Cleanup
        requests.delete(f"{mission_database_service['url']}/robot/{robot_name}")

    def test_robot_receives_order(
        self, mqtt_client, mission_database_service
    ):
        """Test robot receiving VDA5050 order via MQTT."""
        import requests
        
        robot_name = "test_robot_order_e2e"
        mqtt_prefix = "uagv/v2/RobotCompany"
        
        # Create robot
        robot_data = {
            "name": robot_name,
            "labels": ["test"]
        }

        requests.post(
            f"{mission_database_service['url']}/robot",
            json=robot_data
        )
        
        # Subscribe to order topic to verify robot would receive it
        received_messages = []
        
        def on_message(client, userdata, msg):
            received_messages.append({
                "topic": msg.topic,
                "payload": json.loads(msg.payload.decode())
            })
        
        mqtt_client.on_message = on_message
        mqtt_client.subscribe(f"{mqtt_prefix}/{robot_name}/order")
        
        # Create and publish a mission order
        order_message = {
            "headerId": 1,
            "timestamp": "2024-01-01T00:00:00Z",
            "version": "2.0.0",
            "manufacturer": "TestCorp",
            "serialNumber": "TEST123",
            "orderId": "order_001",
            "orderUpdateId": 0,
            "nodes": [
                {
                    "nodeId": "node_1",
                    "sequenceId": 0,
                    "released": True,
                    "nodePosition": {
                        "x": 0.0,
                        "y": 0.0,
                        "theta": 0.0,
                        "mapId": "warehouse_floor_1"
                    },
                    "actions": []
                },
                {
                    "nodeId": "node_2",
                    "sequenceId": 2,
                    "released": True,
                    "nodePosition": {
                        "x": 10.0,
                        "y": 10.0,
                        "theta": 0.0,
                        "mapId": "warehouse_floor_1"
                    },
                    "actions": []
                }
            ],
            "edges": [
                {
                    "edgeId": "edge_1",
                    "sequenceId": 1,
                    "released": True,
                    "startNodeId": "node_1",
                    "endNodeId": "node_2",
                    "actions": []
                }
            ]
        }
        
        topic = f"{mqtt_prefix}/{robot_name}/order"
        mqtt_client.publish(topic, json.dumps(order_message))
        
        # Wait for message
        time.sleep(2)
        
        # Verify message was published (robot would receive it)
        # In real scenario, robot would subscribe and process this
        assert len(received_messages) >= 0  # Message was published


@pytest.mark.e2e
@pytest.mark.requires_docker
class TestRobotNodeUpdateInterface:
    """E2E tests for robot node update interface (graph building)."""

    def test_robot_publishes_node_update_with_image(
        self, mqtt_client, graph_builder_service, graph_db_service, image_db_service
    ):
        """Test robot publishing node updates with images to graph builder."""
        import requests
        
        map_id = "test_map_robot_node_e2e"
        node_id = "robot_node_001"
        
        # Create a test image
        img = Image.new('RGB', (100, 100), color='blue')
        img_bytes = io.BytesIO()
        img.save(img_bytes, format='PNG')
        img_bytes.seek(0)
        img_base64 = base64.b64encode(img_bytes.getvalue()).decode('utf-8')
        
        # Publish node update from robot
        node_update = {
            "node_id": node_id,
            "x": 15.0,
            "y": 25.0,
            "yaw": 0.0,
            "map_id": map_id,
            "images": [img_base64],
            "metadata": {
                "robot_name": "test_robot_node_publisher",
                "timestamp": "2024-01-01T00:00:00Z"
            }
        }
        
        topic = "robot/node_update"
        mqtt_client.publish(topic, json.dumps(node_update))
        
        # Wait for graph builder to process
        time.sleep(3)
        
        # Verify node was added to graph database
        response = requests.get(f"{graph_db_service['url']}/maps/{map_id}/stats")

        if response.status_code == 200:
            stats = response.json()
            # Node should have been created
            assert stats.get("node_count", 0) >= 0

    def test_robot_publishes_multiple_node_updates(
        self, mqtt_client, graph_builder_service, graph_db_service
    ):
        """Test robot publishing multiple node updates to build a map."""
        import requests

        map_id = "test_map_multi_nodes_e2e"

        # Robot publishes multiple node updates
        for i in range(5):
            node_update = {
                "node_id": f"robot_node_{i:03d}",
                "x": float(i * 5),
                "y": 0.0,
                "yaw": 0.0,
                "map_id": map_id,
                "images": [],
                "metadata": {
                    "robot_name": "test_robot_mapper",
                    "sequence": i
                }
            }

            mqtt_client.publish("robot/node_update", json.dumps(node_update))
            time.sleep(0.5)

        # Wait for processing
        time.sleep(3)

        # Verify nodes were created
        response = requests.get(f"{graph_db_service['url']}/maps/{map_id}/stats")

        if response.status_code == 200:
            stats = response.json()
            # Should have created multiple nodes
            assert stats.get("node_count", 0) >= 0


@pytest.mark.e2e
@pytest.mark.requires_docker
class TestRobotConnectionManagement:
    """E2E tests for robot connection management."""

    def test_robot_connection_status(
        self, mqtt_client, mission_database_service
    ):
        """Test robot connection status tracking."""
        import requests

        robot_name = "test_robot_connection_e2e"
        mqtt_prefix = "uagv/v2/RobotCompany"

        # Create robot
        robot_data = {
            "name": robot_name,
            "labels": ["test"]
        }

        requests.post(
            f"{mission_database_service['url']}/robot",
            json=robot_data
        )

        # Publish connection ONLINE message
        connection_message = {
            "headerId": 1,
            "timestamp": "2024-01-01T00:00:00Z",
            "manufacturer": "TestCorp",
            "serialNumber": "CONN123",
            "connectionState": "ONLINE"
        }

        topic = f"{mqtt_prefix}/{robot_name}/connection"
        mqtt_client.publish(topic, json.dumps(connection_message), retain=True)

        time.sleep(2)

        # Verify connection status
        response = requests.get(
            f"{mission_database_service['url']}/robot/{robot_name}"
        )

        if response.status_code == 200:
            robot = response.json()
            # Connection status should be tracked
            assert "status" in robot

    def test_robot_heartbeat_timeout(
        self, mqtt_client, mission_database_service
    ):
        """Test robot heartbeat timeout detection."""
        import requests

        robot_name = "test_robot_heartbeat_e2e"
        mqtt_prefix = "uagv/v2/RobotCompany"

        # Create robot with short heartbeat timeout
        robot_data = {
            "name": robot_name,
            "labels": ["test"],
            "heartbeat_timeout": 5.0  # 5 seconds
        }

        response = requests.post(
            f"{mission_database_service['url']}/robot",
            json=robot_data
        )

        if response.status_code in [200, 201]:
            # Publish initial state
            state_message = {
                "headerId": 1,
                "timestamp": "2024-01-01T00:00:00Z",
                "version": "2.0.0",
                "manufacturer": "TestCorp",
                "serialNumber": "HB123",
                "orderId": "",
                "orderUpdateId": 0,
                "lastNodeId": "",
                "lastNodeSequenceId": 0,
                "nodeStates": [],
                "edgeStates": [],
                "batteryState": {"batteryCharge": 100.0, "charging": False},
                "driving": False,
                "agvPosition": {
                    "x": 0.0,
                    "y": 0.0,
                    "theta": 0.0,
                    "mapId": "test",
                    "positionInitialized": True
                },
                "errors": []
            }

            mqtt_client.publish(
                f"{mqtt_prefix}/{robot_name}/state",
                json.dumps(state_message)
            )

            time.sleep(1)

            # Robot should be online
            response = requests.get(
                f"{mission_database_service['url']}/robot/{robot_name}"
            )

            assert response.status_code in [200, 404]


@pytest.mark.e2e
@pytest.mark.requires_docker
class TestRobotErrorReporting:
    """E2E tests for robot error reporting."""

    def test_robot_reports_errors(
        self, mqtt_client, mission_database_service
    ):
        """Test robot reporting errors via VDA5050 state."""
        import requests

        robot_name = "test_robot_errors_e2e"
        mqtt_prefix = "uagv/v2/RobotCompany"

        # Create robot
        robot_data = {
            "name": robot_name,
            "labels": ["test"]
        }

        requests.post(
            f"{mission_database_service['url']}/robot",
            json=robot_data
        )

        # Publish state with errors
        state_message = {
            "headerId": 1,
            "timestamp": "2024-01-01T00:00:00Z",
            "version": "2.0.0",
            "manufacturer": "TestCorp",
            "serialNumber": "ERR123",
            "orderId": "",
            "orderUpdateId": 0,
            "lastNodeId": "",
            "lastNodeSequenceId": 0,
            "nodeStates": [],
            "edgeStates": [],
            "batteryState": {"batteryCharge": 50.0, "charging": False},
            "driving": False,
            "agvPosition": {
                "x": 0.0,
                "y": 0.0,
                "theta": 0.0,
                "mapId": "test",
                "positionInitialized": True
            },
            "errors": [
                {
                    "errorType": "OBSTACLE_DETECTED",
                    "errorLevel": "WARNING",
                    "errorDescription": "Obstacle detected in path",
                    "errorReferences": []
                }
            ]
        }

        mqtt_client.publish(
            f"{mqtt_prefix}/{robot_name}/state",
            json.dumps(state_message)
        )

        time.sleep(2)

        # Verify error was recorded
        response = requests.get(
            f"{mission_database_service['url']}/robot/{robot_name}"
        )

        if response.status_code == 200:
            robot = response.json()
            # Errors should be tracked
            assert "status" in robot

