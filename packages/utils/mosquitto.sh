#!/bin/sh
# SPDX-FileCopyrightText: NVIDIA CORPORATION & AFFILIATES
# Copyright (c) 2021-2024 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0

# Mosquitto MQTT Broker Startup Script
# Configures and starts mosquitto with both TCP and WebSocket listeners

# Get ports from arguments or use defaults
MQTT_PORT_TCP=${1:-1883}
MQTT_PORT_WEBSOCKET=${2:-9001}

echo "Starting Mosquitto MQTT Broker..."
echo "TCP Port: $MQTT_PORT_TCP"
echo "WebSocket Port: $MQTT_PORT_WEBSOCKET"

# Create mosquitto configuration file
cat > /tmp/mosquitto.conf <<EOF
# Mosquitto Configuration for Testing
# Allow anonymous connections (for testing only)
allow_anonymous true

# TCP Listener
listener $MQTT_PORT_TCP 0.0.0.0
protocol mqtt

# WebSocket Listener
listener $MQTT_PORT_WEBSOCKET 0.0.0.0
protocol websockets

# Logging
log_dest stdout
log_type all
EOF

echo "Configuration created at /tmp/mosquitto.conf"
cat /tmp/mosquitto.conf

# Start mosquitto with the configuration
exec mosquitto -c /tmp/mosquitto.conf

