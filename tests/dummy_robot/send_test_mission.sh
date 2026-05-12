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

# Script to send a test mission to the dummy robot

ROBOT_NAME="${1:-dummy_robot_01}"
DATABASE_URL="${2:-http://localhost:5000}"
MISSION_NAME="${3:-test_mission_$(date +%s)}"

echo "Sending mission: $MISSION_NAME"
echo "Robot: $ROBOT_NAME"
echo "Database URL: $DATABASE_URL"

# Send mission
curl -X POST "$DATABASE_URL/mission" \
  -H "Content-Type: application/json" \
  -d "{
    \"robot\": \"$ROBOT_NAME\",
    \"name\": \"$MISSION_NAME\",
    \"mission_tree\": [
      {
        \"name\": \"goto_point_1\",
        \"parent\": \"root\",
        \"route\": {
          \"waypoints\": [
            {\"x\": 5.0, \"y\": 5.0, \"theta\": 0.0, \"map_id\": \"default\"}
          ]
        }
      },
      {
        \"name\": \"goto_point_2\",
        \"parent\": \"root\",
        \"route\": {
          \"waypoints\": [
            {\"x\": -5.0, \"y\": 5.0, \"theta\": 1.57, \"map_id\": \"default\"}
          ]
        }
      },
      {
        \"name\": \"goto_point_3\",
        \"parent\": \"root\",
        \"route\": {
          \"waypoints\": [
            {\"x\": -5.0, \"y\": -5.0, \"theta\": 3.14, \"map_id\": \"default\"}
          ]
        }
      },
      {
        \"name\": \"goto_origin\",
        \"parent\": \"root\",
        \"route\": {
          \"waypoints\": [
            {\"x\": 0.0, \"y\": 0.0, \"theta\": 0.0, \"map_id\": \"default\"}
          ]
        }
      }
    ]
  }"

echo ""
echo "Mission sent successfully!"
echo ""
echo "To check mission status, run:"
echo "  curl $DATABASE_URL/mission/$MISSION_NAME"

