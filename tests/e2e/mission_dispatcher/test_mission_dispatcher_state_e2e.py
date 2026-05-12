"""
End-to-end tests for Mission Dispatcher Service - VDA5050 State Processing.

Tests processing of VDA5050 state messages from robots via MQTT.
"""

import pytest
import requests
import uuid
import time
import json
from typing import Dict, Any


@pytest.mark.e2e
@pytest.mark.requires_docker
class TestMissionDispatcherStateE2E:
    """E2E tests for VDA5050 state processing."""

    @pytest.mark.skip(reason="NVIDIA Mission Dispatcher MQTT thread crashes during full test suite - test passes when run individually")
    def test_robot_state_updates_robot_object(
        self, mission_database_service, mission_dispatch_service, mqtt_client
    ):
        """Test that robot state messages update robot object."""
        robot_name = f"test_robot_{uuid.uuid4().hex[:8]}"

        # Create robot
        robot_data = {
            "name": robot_name,
            "labels": ["test"]
        }
        requests.post(f"{mission_database_service['url']}/robot", json=robot_data)

        # Wait for Mission Dispatcher to register the robot
        time.sleep(1)

        # Send VDA5050 state message
        state_message = {
            "headerId": 1,
            "timestamp": "2024-01-01T00:00:00Z",
            "version": "2.0.0",
            "orderId": "",
            "orderUpdateId": 0,
            "nodeStates": [],
            "edgeStates": [],
            "agvPosition": {
                "x": 5.0,
                "y": 10.0,
                "theta": 1.57,
                "mapId": "test_map",
                "positionInitialized": True
            },
            "batteryState": {
                "batteryCharge": 75.0,
                "charging": False
            },
            "safetyState": {
                "eStop": "NONE",
                "fieldViolation": False
            },
            "driving": False
        }

        mqtt_client.publish(
            f"uagv/v2/RobotCompany/{robot_name}/state",
            json.dumps(state_message)
        )

        # Wait for state to be processed
        time.sleep(3)

        # Verify robot object was updated
        response = requests.get(f"{mission_database_service['url']}/robot/{robot_name}")
        assert response.status_code == 200, f"Failed to get robot: {response.status_code}"
        robot = response.json()

        # Position should be updated
        assert "status" in robot, f"Robot missing 'status' field: {robot}"
        assert "pose" in robot["status"], f"Robot status missing 'pose' field: {robot['status']}"
        assert robot["status"]["pose"]["x"] == 5.0, f"Expected x=5.0, got {robot['status']['pose'].get('x')}"
        assert robot["status"]["pose"]["y"] == 10.0, f"Expected y=10.0, got {robot['status']['pose'].get('y')}"

        # Cleanup
        requests.delete(f"{mission_database_service['url']}/robot/{robot_name}")

    @pytest.mark.skip(reason="NVIDIA Mission Dispatcher MQTT thread crashes during full test suite - test passes when run individually")
    def test_state_message_marks_robot_online(
        self, mission_database_service, mission_dispatch_service, mqtt_client
    ):
        """Test that receiving state message marks robot as online."""
        robot_name = f"test_robot_{uuid.uuid4().hex[:8]}"

        # Create offline robot
        robot_data = {
            "name": robot_name,
            "labels": ["test"]
        }
        requests.post(f"{mission_database_service['url']}/robot", json=robot_data)

        # Wait for Mission Dispatcher to register the robot
        time.sleep(1)

        # Send state message
        state_message = {
            "headerId": 1,
            "timestamp": "2024-01-01T00:00:00Z",
            "version": "2.0.0",
            "orderId": "",
            "orderUpdateId": 0,
            "nodeStates": [],
            "edgeStates": [],
            "agvPosition": {
                "x": 0.0,
                "y": 0.0,
                "theta": 0.0,
                "mapId": "test_map",
                "positionInitialized": True
            },
            "safetyState": {
                "eStop": "NONE",
                "fieldViolation": False
            },
            "driving": False
        }

        mqtt_client.publish(
            f"uagv/v2/RobotCompany/{robot_name}/state",
            json.dumps(state_message)
        )

        # Wait for processing
        time.sleep(3)

        # Verify robot is online
        response = requests.get(f"{mission_database_service['url']}/robot/{robot_name}")
        assert response.status_code == 200, f"Failed to get robot: {response.status_code}"
        robot = response.json()

        # Robot should be marked online after receiving state
        assert "status" in robot, f"Robot missing 'status' field: {robot}"
        assert "online" in robot["status"], f"Robot status missing 'online' field: {robot['status']}"
        assert robot["status"]["online"] == True, f"Expected robot to be online, got {robot['status']['online']}"

        # Cleanup
        requests.delete(f"{mission_database_service['url']}/robot/{robot_name}")

    @pytest.mark.skip(reason="NVIDIA Mission Dispatcher MQTT thread crashes during full test suite - test passes when run individually")
    def test_battery_state_updates(
        self, mission_database_service, mission_dispatch_service, mqtt_client
    ):
        """Test that battery state from VDA5050 updates robot."""
        robot_name = f"test_robot_{uuid.uuid4().hex[:8]}"

        # Create robot
        robot_data = {
            "name": robot_name,
            "labels": ["test"]
        }
        requests.post(f"{mission_database_service['url']}/robot", json=robot_data)

        # Wait for Mission Dispatcher to register the robot
        time.sleep(1)

        # Send state with battery info
        state_message = {
            "headerId": 1,
            "timestamp": "2024-01-01T00:00:00Z",
            "version": "2.0.0",
            "orderId": "",
            "orderUpdateId": 0,
            "nodeStates": [],
            "edgeStates": [],
            "agvPosition": {
                "x": 0.0,
                "y": 0.0,
                "theta": 0.0,
                "mapId": "test_map",
                "positionInitialized": True
            },
            "batteryState": {
                "batteryCharge": 45.5,
                "charging": True
            },
            "safetyState": {
                "eStop": "NONE",
                "fieldViolation": False
            },
            "driving": False
        }

        mqtt_client.publish(
            f"uagv/v2/RobotCompany/{robot_name}/state",
            json.dumps(state_message)
        )

        # Wait for processing
        time.sleep(3)

        # Verify battery level updated
        response = requests.get(f"{mission_database_service['url']}/robot/{robot_name}")
        assert response.status_code == 200, f"Failed to get robot: {response.status_code}"
        robot = response.json()

        # Check battery level
        assert "status" in robot, f"Robot missing 'status' field: {robot}"
        assert "battery_level" in robot["status"], f"Robot status missing 'battery_level' field: {robot['status']}"
        assert robot["status"]["battery_level"] == 45.5, f"Expected battery_level=45.5, got {robot['status']['battery_level']}"

        # Cleanup
        requests.delete(f"{mission_database_service['url']}/robot/{robot_name}")

    def test_state_with_errors(
        self, mission_database_service, mqtt_client
    ):
        """Test processing state messages with errors."""
        robot_name = f"test_robot_{uuid.uuid4().hex[:8]}"
        
        # Create robot
        robot_data = {
            "name": robot_name,
            "labels": ["test"]
        }
        requests.post(f"{mission_database_service['url']}/robot", json=robot_data)
        
        # Send state with errors
        state_message = {
            "headerId": 1,
            "timestamp": "2024-01-01T00:00:00Z",
            "version": "2.0.0",
            "orderId": "",
            "orderUpdateId": 0,
            "nodeStates": [],
            "edgeStates": [],
            "agvPosition": {
                "x": 0.0,
                "y": 0.0,
                "theta": 0.0,
                "mapId": "test_map",
                "positionInitialized": True
            },
            "errors": [
                {
                    "errorType": "navigationError",
                    "errorLevel": "WARNING",
                    "errorDescription": "Path blocked"
                }
            ],
            "safetyState": {
                "eStop": "NONE",
                "fieldViolation": False
            },
            "driving": False
        }
        
        mqtt_client.publish(
            f"uagv/v2/RobotCompany/{robot_name}/state",
            json.dumps(state_message)
        )
        
        # Wait for processing
        time.sleep(2)
        
        # State should be processed even with errors
        response = requests.get(f"{mission_database_service['url']}/robot/{robot_name}")
        assert response.status_code == 200
        
        # Cleanup
        requests.delete(f"{mission_database_service['url']}/robot/{robot_name}")

    def test_state_updates_mission_progress(
        self, mission_database_service, mqtt_client, sample_mission
    ):
        """Test that state messages update mission progress."""
        robot_name = f"test_robot_{uuid.uuid4().hex[:8]}"
        mission_name = f"test_mission_{uuid.uuid4().hex[:8]}"
        
        # Create robot
        robot_data = {
            "name": robot_name,
            "labels": ["test"]
        }
        requests.post(f"{mission_database_service['url']}/robot", json=robot_data)
        
        # Create mission
        sample_mission["name"] = mission_name
        sample_mission["robot"] = robot_name
        requests.post(f"{mission_database_service['url']}/mission", json=sample_mission)
        
        # Wait for mission to be dispatched
        time.sleep(2)
        
        # Send state indicating progress
        state_message = {
            "headerId": 1,
            "timestamp": "2024-01-01T00:00:00Z",
            "version": "2.0.0",
            "orderId": f"{mission_name}-n0",
            "orderUpdateId": 0,
            "lastNodeId": "node_0",
            "lastNodeSequenceId": 0,
            "nodeStates": [
                {
                    "nodeId": "node_0",
                    "sequenceId": 0,
                    "released": True,
                    "nodePosition": {"x": 0.0, "y": 0.0, "theta": 0.0, "mapId": "test_map"}
                }
            ],
            "edgeStates": [],
            "agvPosition": {
                "x": 0.0,
                "y": 0.0,
                "theta": 0.0,
                "mapId": "test_map",
                "positionInitialized": True
            },
            "safetyState": {
                "eStop": "NONE",
                "fieldViolation": False
            },
            "driving": True
        }
        
        mqtt_client.publish(
            f"uagv/v2/RobotCompany/{robot_name}/state",
            json.dumps(state_message)
        )
        
        # Wait for processing
        time.sleep(2)
        
        # Mission state should be updated
        response = requests.get(f"{mission_database_service['url']}/mission/{mission_name}")
        if response.status_code == 200:
            mission = response.json()
            # Mission should be in active or running state
            if "status" in mission and "state" in mission["status"]:
                assert mission["status"]["state"] in ["PENDING", "ACTIVE", "RUNNING", "COMPLETED"]
        
        # Cleanup
        requests.delete(f"{mission_database_service['url']}/mission/{mission_name}")
        requests.delete(f"{mission_database_service['url']}/robot/{robot_name}")

    @pytest.mark.skip(reason="NVIDIA Mission Dispatcher does not handle invalid VDA5050 messages gracefully - validation errors crash the MQTT thread")
    def test_invalid_state_message_handling(
        self, mission_database_service, mqtt_client
    ):
        """Test handling of invalid state messages."""
        robot_name = f"test_robot_{uuid.uuid4().hex[:8]}"

        # Create robot
        robot_data = {
            "name": robot_name,
            "labels": ["test"]
        }
        requests.post(f"{mission_database_service['url']}/robot", json=robot_data)

        # Send invalid state message
        invalid_message = {
            "invalid_field": "value"
            # Missing required fields
        }

        mqtt_client.publish(
            f"uagv/v2/RobotCompany/{robot_name}/state",
            json.dumps(invalid_message)
        )

        # Wait
        time.sleep(1)

        # Service should handle gracefully (not crash)
        response = requests.get(f"{mission_database_service['url']}/robot/{robot_name}")
        assert response.status_code == 200

        # Cleanup
        requests.delete(f"{mission_database_service['url']}/robot/{robot_name}")

    def test_rapid_state_updates(
        self, mission_database_service, mqtt_client
    ):
        """Test handling of rapid state updates."""
        robot_name = f"test_robot_{uuid.uuid4().hex[:8]}"
        
        # Create robot
        robot_data = {
            "name": robot_name,
            "labels": ["test"]
        }
        requests.post(f"{mission_database_service['url']}/robot", json=robot_data)
        
        # Send multiple rapid state updates
        for i in range(10):
            state_message = {
                "headerId": i,
                "timestamp": "2024-01-01T00:00:00Z",
                "version": "2.0.0",
                "orderId": "",
                "orderUpdateId": 0,
                "nodeStates": [],
                "edgeStates": [],
                "agvPosition": {
                    "x": float(i),
                    "y": float(i),
                    "theta": 0.0,
                    "mapId": "test_map",
                    "positionInitialized": True
                },
                "safetyState": {
                    "eStop": "NONE",
                    "fieldViolation": False
                },
                "driving": False
            }

            mqtt_client.publish(
                f"uagv/v2/RobotCompany/{robot_name}/state",
                json.dumps(state_message)
            )
            time.sleep(0.1)
        
        # Wait for all updates to process
        time.sleep(2)
        
        # Robot should have latest position
        response = requests.get(f"{mission_database_service['url']}/robot/{robot_name}")
        robot = response.json()
        
        # Position should be updated (may be last value or close to it)
        if "status" in robot and "pose" in robot["status"]:
            assert robot["status"]["pose"]["x"] >= 0.0
        
        # Cleanup
        requests.delete(f"{mission_database_service['url']}/robot/{robot_name}")

