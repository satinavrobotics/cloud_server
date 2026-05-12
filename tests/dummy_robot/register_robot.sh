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

# Script to register dummy robot in Mission Database

ROBOT_NAME="${1:-dummy_robot_01}"
DATABASE_URL="${2:-http://localhost:5000}"

echo "Registering robot: $ROBOT_NAME"
echo "Database URL: $DATABASE_URL"

# Register robot
curl -X POST "$DATABASE_URL/robot" \
  -H "Content-Type: application/json" \
  -d "{
    \"name\": \"$ROBOT_NAME\",
    \"labels\": [\"test\", \"dummy\", \"development\"]
  }"

echo ""
echo "Robot registered successfully!"
echo ""
echo "To verify, run:"
echo "  curl $DATABASE_URL/robot/$ROBOT_NAME"

