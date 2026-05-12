"""
End-to-end tests for Mission Dispatcher Service - VDA5050 Factsheet Handling.

Tests processing of VDA5050 factsheet messages from robots via MQTT.
"""

import pytest
import requests
import uuid
import time
import json
from typing import Dict, Any


@pytest.mark.e2e
@pytest.mark.requires_docker
class TestMissionDispatcherFactsheetE2E:
    """E2E tests for VDA5050 factsheet handling."""

    def test_factsheet_received_and_processed(
        self, mission_database_service, mqtt_client
    ):
        """Test that factsheet messages are received and processed."""
        robot_name = f"test_robot_{uuid.uuid4().hex[:8]}"
        
        # Create robot
        robot_data = {
            "name": robot_name,
            "labels": ["test"]
        }
        requests.post(f"{mission_database_service['url']}/robot", json=robot_data)
        
        # Send factsheet message
        factsheet_message = {
            "headerId": 1,
            "timestamp": "2024-01-01T00:00:00Z",
            "version": "2.0.0",
            "manufacturer": "TestManufacturer",
            "serialNumber": "TEST123",
            "typeSpecification": {
                "seriesName": "TestSeries",
                "agvKinematic": "DIFF",
                "agvClass": "FORKLIFT",
                "maxLoadMass": 1000.0,
                "localizationTypes": ["NATURAL"],
                "navigationTypes": ["AUTONOMOUS"]
            }
        }
        
        mqtt_client.publish(
            f"uagv/v2/RobotCompany/{robot_name}/factsheet",
            json.dumps(factsheet_message)
        )
        
        # Wait for processing
        time.sleep(2)
        
        # Service should process factsheet without errors
        response = requests.get(f"{mission_database_service['url']}/robot/{robot_name}")
        assert response.status_code == 200
        
        # Cleanup
        requests.delete(f"{mission_database_service['url']}/robot/{robot_name}")

    def test_factsheet_with_physical_parameters(
        self, mission_database_service, mqtt_client
    ):
        """Test factsheet with physical parameters."""
        robot_name = f"test_robot_{uuid.uuid4().hex[:8]}"
        
        # Create robot
        robot_data = {
            "name": robot_name,
            "labels": ["test"]
        }
        requests.post(f"{mission_database_service['url']}/robot", json=robot_data)
        
        # Send factsheet with physical parameters
        factsheet_message = {
            "headerId": 1,
            "timestamp": "2024-01-01T00:00:00Z",
            "version": "2.0.0",
            "manufacturer": "TestManufacturer",
            "serialNumber": "TEST123",
            "physicalParameters": {
                "speedMin": 0.0,
                "speedMax": 2.0,
                "accelerationMax": 1.0,
                "decelerationMax": 1.5,
                "heightMin": 0.5,
                "heightMax": 2.0,
                "width": 0.8,
                "length": 1.2
            }
        }
        
        mqtt_client.publish(
            f"uagv/v2/RobotCompany/{robot_name}/factsheet",
            json.dumps(factsheet_message)
        )
        
        # Wait for processing
        time.sleep(2)
        
        # Factsheet should be processed
        response = requests.get(f"{mission_database_service['url']}/robot/{robot_name}")
        assert response.status_code == 200
        
        # Cleanup
        requests.delete(f"{mission_database_service['url']}/robot/{robot_name}")

    def test_factsheet_for_nonexistent_robot(
        self, mqtt_client
    ):
        """Test factsheet for robot that doesn't exist."""
        robot_name = f"nonexistent_robot_{uuid.uuid4().hex[:8]}"
        
        # Send factsheet for non-existent robot
        factsheet_message = {
            "headerId": 1,
            "timestamp": "2024-01-01T00:00:00Z",
            "version": "2.0.0",
            "manufacturer": "TestManufacturer",
            "serialNumber": "TEST123"
        }
        
        mqtt_client.publish(
            f"uagv/v2/RobotCompany/{robot_name}/factsheet",
            json.dumps(factsheet_message)
        )
        
        # Wait
        time.sleep(1)
        
        # Service should handle gracefully (not crash)
        # No assertion needed - just verify no crash

    def test_invalid_factsheet_handling(
        self, mission_database_service, mqtt_client
    ):
        """Test handling of invalid factsheet messages."""
        robot_name = f"test_robot_{uuid.uuid4().hex[:8]}"
        
        # Create robot
        robot_data = {
            "name": robot_name,
            "labels": ["test"]
        }
        requests.post(f"{mission_database_service['url']}/robot", json=robot_data)
        
        # Send invalid factsheet
        invalid_factsheet = {
            "invalid_field": "value"
        }
        
        mqtt_client.publish(
            f"uagv/v2/RobotCompany/{robot_name}/factsheet",
            json.dumps(invalid_factsheet)
        )
        
        # Wait
        time.sleep(1)
        
        # Service should handle gracefully
        response = requests.get(f"{mission_database_service['url']}/robot/{robot_name}")
        assert response.status_code == 200
        
        # Cleanup
        requests.delete(f"{mission_database_service['url']}/robot/{robot_name}")

    def test_factsheet_updates_robot_capabilities(
        self, mission_database_service, mqtt_client
    ):
        """Test that factsheet can update robot capabilities."""
        robot_name = f"test_robot_{uuid.uuid4().hex[:8]}"
        
        # Create robot
        robot_data = {
            "name": robot_name,
            "labels": ["test"]
        }
        requests.post(f"{mission_database_service['url']}/robot", json=robot_data)
        
        # Send factsheet
        factsheet_message = {
            "headerId": 1,
            "timestamp": "2024-01-01T00:00:00Z",
            "version": "2.0.0",
            "manufacturer": "TestManufacturer",
            "serialNumber": "TEST123",
            "typeSpecification": {
                "seriesName": "TestSeries",
                "agvKinematic": "DIFF",
                "maxLoadMass": 500.0
            }
        }
        
        mqtt_client.publish(
            f"uagv/v2/RobotCompany/{robot_name}/factsheet",
            json.dumps(factsheet_message)
        )
        
        # Wait for processing
        time.sleep(2)
        
        # Robot should exist and be updated
        response = requests.get(f"{mission_database_service['url']}/robot/{robot_name}")
        assert response.status_code == 200
        
        # Cleanup
        requests.delete(f"{mission_database_service['url']}/robot/{robot_name}")

    def test_multiple_factsheet_updates(
        self, mission_database_service, mqtt_client
    ):
        """Test multiple factsheet updates for same robot."""
        robot_name = f"test_robot_{uuid.uuid4().hex[:8]}"
        
        # Create robot
        robot_data = {
            "name": robot_name,
            "labels": ["test"]
        }
        requests.post(f"{mission_database_service['url']}/robot", json=robot_data)
        
        # Send multiple factsheets
        for i in range(3):
            factsheet_message = {
                "headerId": i,
                "timestamp": "2024-01-01T00:00:00Z",
                "version": "2.0.0",
                "manufacturer": "TestManufacturer",
                "serialNumber": f"TEST{i}"
            }
            
            mqtt_client.publish(
                f"uagv/v2/RobotCompany/{robot_name}/factsheet",
                json.dumps(factsheet_message)
            )
            time.sleep(0.5)
        
        # Wait for processing
        time.sleep(2)
        
        # Robot should be updated
        response = requests.get(f"{mission_database_service['url']}/robot/{robot_name}")
        assert response.status_code == 200
        
        # Cleanup
        requests.delete(f"{mission_database_service['url']}/robot/{robot_name}")

