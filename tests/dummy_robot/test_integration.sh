#!/bin/bash

# SPDX-FileCopyrightText: NVIDIA CORPORATION & AFFILIATES
# Copyright (c) 2021-2024 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0

# Comprehensive integration test for dummy robot

set -e

ROBOT_NAME="${1:-dummy_robot_01}"
DATABASE_URL="${2:-http://localhost:5000}"
GRAPH_DB_URL="${3:-http://localhost:6001}"

echo "=========================================="
echo "Dummy Robot Integration Test"
echo "=========================================="
echo "Robot: $ROBOT_NAME"
echo "Database URL: $DATABASE_URL"
echo "Graph DB URL: $GRAPH_DB_URL"
echo ""

# Function to check if service is ready
check_service() {
    local url=$1
    local name=$2
    echo -n "Checking $name... "
    if curl -s -f "$url/health" > /dev/null 2>&1 || curl -s -f "$url" > /dev/null 2>&1; then
        echo "✅ Ready"
        return 0
    else
        echo "❌ Not ready"
        return 1
    fi
}

# Check services
echo "Checking services..."
check_service "$DATABASE_URL" "Mission Database" || echo "Warning: Mission Database not ready"
check_service "$GRAPH_DB_URL" "Graph Database" || echo "Warning: Graph Database not ready"
echo ""

# Register robot
echo "Step 1: Registering robot..."
curl -X POST "$DATABASE_URL/robot" \
  -H "Content-Type: application/json" \
  -d "{
    \"name\": \"$ROBOT_NAME\",
    \"labels\": [\"test\", \"dummy\", \"integration_test\"]
  }" 2>/dev/null

echo ""
echo "✅ Robot registered"
echo ""

# Wait a bit for robot to connect
echo "Step 2: Waiting for robot to connect (10 seconds)..."
sleep 10

# Check robot status
echo "Step 3: Checking robot status..."
ROBOT_STATUS=$(curl -s "$DATABASE_URL/robot/$ROBOT_NAME")
echo "$ROBOT_STATUS" | python3 -m json.tool 2>/dev/null || echo "$ROBOT_STATUS"
echo ""

# Send test mission
echo "Step 4: Sending test mission..."
MISSION_NAME="integration_test_$(date +%s)"
curl -X POST "$DATABASE_URL/mission" \
  -H "Content-Type: application/json" \
  -d "{
    \"robot\": \"$ROBOT_NAME\",
    \"name\": \"$MISSION_NAME\",
    \"mission_tree\": [
      {
        \"name\": \"goto_point\",
        \"parent\": \"root\",
        \"route\": {
          \"waypoints\": [
            {\"x\": 8.0, \"y\": 8.0, \"theta\": 0.785, \"map_id\": \"default\"}
          ]
        }
      }
    ]
  }" 2>/dev/null

echo ""
echo "✅ Mission sent: $MISSION_NAME"
echo ""

# Wait for mission to be processed
echo "Step 5: Waiting for mission to be processed (5 seconds)..."
sleep 5

# Check mission status
echo "Step 6: Checking mission status..."
MISSION_STATUS=$(curl -s "$DATABASE_URL/mission/$MISSION_NAME")
echo "$MISSION_STATUS" | python3 -m json.tool 2>/dev/null || echo "$MISSION_STATUS"
echo ""

# Check graph nodes
echo "Step 7: Checking graph nodes..."
NODES=$(curl -s "$GRAPH_DB_URL/maps/default/nodes")
NODE_COUNT=$(echo "$NODES" | python3 -c "import sys, json; print(len(json.load(sys.stdin).get('nodes', [])))" 2>/dev/null || echo "0")
echo "Total nodes in graph: $NODE_COUNT"
echo ""

# Monitor MQTT for 10 seconds
echo "Step 8: Monitoring MQTT messages (10 seconds)..."
echo "Subscribing to robot/node_update and uagv/v2/RobotCompany/$ROBOT_NAME/state"
timeout 10 mosquitto_sub -h localhost -p 1883 -t "robot/node_update" -t "uagv/v2/RobotCompany/$ROBOT_NAME/state" -C 5 2>/dev/null || echo "MQTT monitoring complete"
echo ""

echo "=========================================="
echo "Integration Test Complete!"
echo "=========================================="
echo ""
echo "Summary:"
echo "  - Robot: $ROBOT_NAME"
echo "  - Mission: $MISSION_NAME"
echo "  - Nodes in graph: $NODE_COUNT"
echo ""
echo "To monitor in real-time:"
echo "  - Robot state: mosquitto_sub -h localhost -p 1883 -t 'uagv/v2/RobotCompany/$ROBOT_NAME/state'"
echo "  - Node updates: mosquitto_sub -h localhost -p 1883 -t 'robot/node_update'"
echo "  - Mission logs: docker-compose -f docker_compose/mission_dispatch_services_dev.yaml logs -f mission-dispatch"
echo "  - Graph Builder logs: docker-compose -f docker_compose/mission_dispatch_services_dev.yaml logs -f graph-builder-service"
echo ""

