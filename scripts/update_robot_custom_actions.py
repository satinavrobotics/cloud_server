#!/usr/bin/env python3
"""
Script to update a robot's custom actions in the database.
This is a workaround for the NVIDIA Isaac mission dispatch server not storing custom actions from factsheets.
"""

import requests
import json
import sys

def update_robot_custom_actions(robot_name: str, controller_url: str = "http://localhost:5001"):
    """Update the robot's custom actions in the database using the controller API."""

    # Define the custom actions (matching what the dummy robot publishes in its factsheet)
    custom_actions = [
        {
            "action_type": "pick",
            "action_description": "Pick up an object from a location",
            "action_parameters": [
                {"key": "object_id", "value": "string"},
                {"key": "location", "value": "string"}
            ],
            "blocking_type": "HARD"
        },
        {
            "action_type": "place",
            "action_description": "Place an object at a location",
            "action_parameters": [
                {"key": "object_id", "value": "string"},
                {"key": "location", "value": "string"}
            ],
            "blocking_type": "HARD"
        },
        {
            "action_type": "scan",
            "action_description": "Scan a barcode or QR code",
            "action_parameters": [
                {"key": "code_type", "value": "string"}
            ],
            "blocking_type": "SOFT"
        },
        {
            "action_type": "charge",
            "action_description": "Start charging the robot",
            "action_parameters": [],
            "blocking_type": "HARD"
        },
        {
            "action_type": "honk",
            "action_description": "Sound the robot's horn",
            "action_parameters": [
                {"key": "duration_ms", "value": "1000"}
            ],
            "blocking_type": "NONE"
        }
    ]

    try:
        # Get the current robot data from the controller API (note: endpoint is /robot not /robots)
        response = requests.get(f"{controller_url}/robot/{robot_name}")
        response.raise_for_status()
        robot_data = response.json()

        print(f"Current robot data for {robot_name}:")
        print(f"  Online: {robot_data.get('status', {}).get('online', False)}")
        print(f"  State: {robot_data.get('status', {}).get('state', 'UNKNOWN')}")
        print(f"  Current custom actions: {len(robot_data.get('status', {}).get('factsheet', {}).get('custom_actions', []))}")

        # Update the custom actions in the factsheet
        if 'status' not in robot_data:
            robot_data['status'] = {}
        if 'factsheet' not in robot_data['status']:
            robot_data['status']['factsheet'] = {}

        robot_data['status']['factsheet']['custom_actions'] = custom_actions

        # Send the update to the controller API (only status field)
        update_payload = {
            "status": robot_data['status']
        }

        response = requests.put(f"{controller_url}/robot/{robot_name}", json=update_payload)
        response.raise_for_status()

        print(f"\n✅ Successfully updated {robot_name} with {len(custom_actions)} custom actions:")
        for action in custom_actions:
            print(f"  - {action['action_type']}: {action['action_description']}")

        return True

    except requests.exceptions.RequestException as e:
        print(f"❌ Error updating robot: {e}", file=sys.stderr)
        if hasattr(e, 'response') and e.response is not None:
            print(f"Response: {e.response.text}", file=sys.stderr)
        return False

if __name__ == "__main__":
    robot_name = sys.argv[1] if len(sys.argv) > 1 else "dummy_robot_01"
    controller_url = sys.argv[2] if len(sys.argv) > 2 else "http://localhost:5001"

    print(f"Updating custom actions for robot: {robot_name}")
    print(f"Controller API URL: {controller_url}\n")

    success = update_robot_custom_actions(robot_name, controller_url)
    sys.exit(0 if success else 1)

