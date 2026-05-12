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

import argparse
import datetime
import json
import logging
import math
import time
import random
import base64
import io
from typing import Optional, List
import sys
import os

import paho.mqtt.client as mqtt_client

# Add parent directories to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from packages.controllers.mission.vda5050_types import vda5050_types as types
from packages.config import CAMERA_YAW_OFFSETS


# Try to import PIL for image generation, fall back to simple placeholder
try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)


class DummyRobot:
    """
    Dummy robot that:
    1. Publishes VDA5050 state messages to mission dispatch
    2. Listens for VDA5050 orders from mission dispatch
    3. Publishes node updates for graph building
    4. Simulates robot movement in a loop
    """

    def __init__(
        self,
        robot_name: str = "dummy_robot_01",
        manufacturer: str = "DummyManufacturer",
        serial_number: str = "DUMMY001",
        mqtt_host: str = "localhost",
        mqtt_port: int = 1883,
        mqtt_prefix: str = "uagv/v2/RobotCompany",
        node_topic: str = "robot/node_update",
        image_topic: str = "robot/image_upload",
        map_id: str = "default",
        loop_radius: float = 10.0,
        speed: float = 1.0,
        tick_period: float = 1.0,
        publish_nodes: bool = True,
        publish_images: bool = True,
    ):
        self.logger = logging.getLogger(f"DummyRobot[{robot_name}]")
        self.robot_name = robot_name
        self.manufacturer = manufacturer
        self.serial_number = serial_number
        self.mqtt_prefix = mqtt_prefix
        self.node_topic = node_topic
        self.image_topic = image_topic
        self.map_id = map_id
        self.loop_radius = loop_radius
        self.speed = speed
        self.tick_period = tick_period
        self.publish_nodes = publish_nodes
        self.publish_images = publish_images

        # Available cameras on this robot
        self.available_cameras = ["front_camera", "back_camera", "left_camera", "right_camera"]

        # Robot state
        self.x = 0.0
        self.y = 0.0
        self.theta = 0.0
        self.battery = 100.0
        self.header_id = 0
        self.node_id_counter = 1000  # Session-local node ID counter

        # Current order
        self.current_order: Optional[types.VDA5050Order] = None
        self.order_id = ""
        self.order_update_id = 0

        # Movement simulation
        self.angle = 0.0  # For circular movement
        self.running = True

        # MQTT client
        self.client = mqtt_client.Client(client_id=f"{robot_name}_client")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.connect(mqtt_host, mqtt_port, 60)
        self.client.loop_start()

        self.logger.info(f"Dummy robot initialized: {robot_name}")
        self.logger.info(f"MQTT: {mqtt_host}:{mqtt_port}")
        self.logger.info(f"VDA5050 prefix: {mqtt_prefix}")
        self.logger.info(f"Node topic: {node_topic}")
        self.logger.info(f"Image topic: {image_topic}")
        if self.publish_images:
            self.logger.info(f"Image publishing enabled with cameras: {self.available_cameras}")

    def _on_connect(self, client, userdata, flags, rc):
        """Callback when connected to MQTT broker."""
        self.logger.info(f"Connected to MQTT broker with result code {rc}")
        
        # Subscribe to order and instant actions topics
        order_topic = f"{self.mqtt_prefix}/{self.robot_name}/order"
        instant_actions_topic = f"{self.mqtt_prefix}/{self.robot_name}/instantActions"
        
        client.subscribe(order_topic)
        client.subscribe(instant_actions_topic)
        
        self.logger.info(f"Subscribed to: {order_topic}")
        self.logger.info(f"Subscribed to: {instant_actions_topic}")

        # Publish initial factsheet
        self._publish_factsheet()

    def _on_message(self, client, userdata, msg):
        """Callback when MQTT message received."""
        try:
            payload = json.loads(msg.payload.decode('utf-8'))
            
            if "order" in msg.topic:
                self.logger.info(f"📦 Received order: {payload.get('orderId', 'unknown')}")
                self._handle_order(payload)
            elif "instantActions" in msg.topic:
                self.logger.info(f"⚡ Received instant actions")
                self._handle_instant_actions(payload)
            else:
                self.logger.warning(f"Unknown topic: {msg.topic}")
                
        except json.JSONDecodeError as e:
            self.logger.error(f"Failed to parse MQTT message: {e}")
        except Exception as e:
            self.logger.error(f"Error processing MQTT message: {e}")

    def _handle_order(self, payload: dict):
        """Handle incoming VDA5050 order."""
        try:
            order = types.VDA5050Order(**payload)
            self.current_order = order
            self.order_id = order.orderId
            self.order_update_id = order.orderUpdateId
            
            self.logger.info(f"Order accepted: {order.orderId}")
            self.logger.info(f"  Nodes: {len(order.nodes)}")
            self.logger.info(f"  Edges: {len(order.edges)}")
            
        except Exception as e:
            self.logger.error(f"Failed to parse order: {e}")

    def _handle_instant_actions(self, payload: dict):
        """Handle incoming VDA5050 instant actions."""
        try:
            instant_actions = types.VDA5050InstantActions(**payload)
            self.logger.info(f"Instant actions: {len(instant_actions.instantActions)}")
            for action in instant_actions.instantActions:
                self.logger.info(f"  Action: {action.actionType}")
        except Exception as e:
            self.logger.error(f"Failed to parse instant actions: {e}")

    def _publish_factsheet(self):
        """Publish VDA5050 factsheet."""
        # Define custom actions available on this robot
        custom_actions = [
            types.VDA5050ActionDefinition(
                actionType="pick_object",
                actionDescription="Pick up an object from a specified location",
                actionParameters=[
                    types.VDA5050ActionParameter(key="object_id", value=""),
                    types.VDA5050ActionParameter(key="height", value="0.0")
                ],
                blockingType=types.VDA5050ActionBlockingType.HARD
            ),
            types.VDA5050ActionDefinition(
                actionType="place_object",
                actionDescription="Place an object at a specified location",
                actionParameters=[
                    types.VDA5050ActionParameter(key="object_id", value=""),
                    types.VDA5050ActionParameter(key="height", value="0.0")
                ],
                blockingType=types.VDA5050ActionBlockingType.HARD
            ),
            types.VDA5050ActionDefinition(
                actionType="scan_area",
                actionDescription="Scan the surrounding area for objects",
                actionParameters=[
                    types.VDA5050ActionParameter(key="radius", value="5.0"),
                    types.VDA5050ActionParameter(key="duration", value="10.0")
                ],
                blockingType=types.VDA5050ActionBlockingType.SOFT
            ),
            types.VDA5050ActionDefinition(
                actionType="play_sound",
                actionDescription="Play a sound or announcement",
                actionParameters=[
                    types.VDA5050ActionParameter(key="sound_id", value=""),
                    types.VDA5050ActionParameter(key="volume", value="50")
                ],
                blockingType=types.VDA5050ActionBlockingType.NONE
            ),
            types.VDA5050ActionDefinition(
                actionType="toggle_lights",
                actionDescription="Toggle robot indicator lights",
                actionParameters=[
                    types.VDA5050ActionParameter(key="state", value="on"),
                    types.VDA5050ActionParameter(key="color", value="blue")
                ],
                blockingType=types.VDA5050ActionBlockingType.NONE
            )
        ]

        factsheet = types.VDA5050Factsheet(
            headerId=self.header_id,
            timestamp=datetime.datetime.now().isoformat(),
            version="2.0.0",
            manufacturer=self.manufacturer,
            serialNumber=self.serial_number,
            typeSpecification=types.VDA5050TypeSpecification(
                seriesName="DummySeries",
                agvClass="CARRIER",
                maxLoadMass=100.0,
                localizationTypes=["NATURAL"],
                navigationTypes=["AUTONOMOUS"]
            ),
            actions=custom_actions
        )

        topic = f"{self.mqtt_prefix}/{self.robot_name}/factsheet"
        self.client.publish(topic, factsheet.json())
        self.header_id += 1
        self.logger.info(f"📄 Published factsheet with {len(custom_actions)} custom actions to {topic}")

    def _publish_state(self):
        """Publish VDA5050 state message."""
        # Add camera information to the information field
        information = []
        if self.publish_images:
            camera_info = types.VDA5050Info(
                infoType="available_cameras",
                infoLevel="INFO",  # infoLevel is a string, not an enum
                infoDescription=json.dumps({
                    "cameras": self.available_cameras,
                    "image_format": "jpeg",
                    "image_encoding": "base64"
                })
            )
            information.append(camera_info)

        state = types.VDA5050State(
            headerId=self.header_id,
            timestamp=datetime.datetime.now().isoformat(),
            version="2.0.0",
            manufacturer=self.manufacturer,
            serialNumber=self.serial_number,
            orderId=self.order_id,
            orderUpdateId=self.order_update_id,
            lastNodeId="",
            lastNodeSequenceId=0,
            nodeStates=[],
            edgeStates=[],
            actionStates=[],
            batteryState=types.VDA5050BatteryState(
                batteryCharge=self.battery,
                batteryVoltage=48.0,
                charging=False
            ),
            driving=True,
            agvPosition=types.VDA5050AgvPosition(
                positionInitialized=True,
                x=self.x,
                y=self.y,
                theta=self.theta,
                mapId=self.map_id
            ),
            velocity=types.VDA5050Velocity(
                vx=self.speed * math.cos(self.theta),
                vy=self.speed * math.sin(self.theta),
                omega=0.0
            ),
            errors=[],
            information=information if information else None,
            safetyState=types.VDA5050SafetyStatus(
                eStop=types.VDA5050EStop.NONE,
                fieldViolation=False
            )
        )

        topic = f"{self.mqtt_prefix}/{self.robot_name}/state"
        self.client.publish(topic, state.json())
        self.header_id += 1

    def _generate_synthetic_image(self, camera_name: str, node_id: int) -> bytes:
        """
        Generate a synthetic image for testing.
        Creates distinct solid colored images for each camera:
        - front_camera: Blue
        - back_camera: Red
        - left_camera: Green
        - right_camera: Yellow

        Args:
            camera_name: Name of the camera
            node_id: Node ID for labeling

        Returns:
            Image data as bytes (JPEG format)
        """
        if PIL_AVAILABLE:
            # Create a distinct colored image for each camera
            width, height = 640, 480

            # Distinct solid colors for different cameras
            colors = {
                "front_camera": (0, 100, 255),      # Blue
                "back_camera": (255, 50, 50),       # Red
                "left_camera": (50, 255, 50),       # Green
                "right_camera": (255, 220, 0)       # Yellow
            }
            bg_color = colors.get(camera_name, (128, 128, 128))

            # Create solid color image
            img = Image.new('RGB', (width, height), color=bg_color)
            draw = ImageDraw.Draw(img)

            # Add text labels with high contrast
            try:
                # Try to use a default font, fall back to basic if not available
                font = ImageFont.load_default()
            except:
                font = None

            # Draw camera name prominently in the center
            camera_display_name = camera_name.replace('_', ' ').title()

            # Draw large camera name in center
            text_lines = [
                camera_display_name,
                "",
                f"Node: {node_id}",
                f"Robot: {self.robot_name}",
                f"Position: ({self.x:.1f}, {self.y:.1f})",
                f"Time: {datetime.datetime.now().strftime('%H:%M:%S')}"
            ]

            # Calculate text position to center it
            y_offset = height // 2 - (len(text_lines) * 15)

            for i, line in enumerate(text_lines):
                if i == 0:  # Camera name - make it larger and bold
                    # Draw text shadow for better visibility
                    draw.text((width // 2 - 80, y_offset - 2), line, fill=(0, 0, 0), font=font)
                    draw.text((width // 2 - 80 + 2, y_offset), line, fill=(255, 255, 255), font=font)
                else:
                    draw.text((width // 2 - 80, y_offset), line, fill=(255, 255, 255), font=font)
                y_offset += 30

            # Draw a border to make it more distinct
            border_width = 10
            draw.rectangle(
                [(0, 0), (width - 1, height - 1)],
                outline=(255, 255, 255),
                width=border_width
            )

            # Draw corner markers
            marker_size = 30
            # Top-left
            draw.line([(0, 0), (marker_size, 0)], fill=(255, 255, 255), width=5)
            draw.line([(0, 0), (0, marker_size)], fill=(255, 255, 255), width=5)
            # Top-right
            draw.line([(width - marker_size, 0), (width, 0)], fill=(255, 255, 255), width=5)
            draw.line([(width - 1, 0), (width - 1, marker_size)], fill=(255, 255, 255), width=5)
            # Bottom-left
            draw.line([(0, height - 1), (marker_size, height - 1)], fill=(255, 255, 255), width=5)
            draw.line([(0, height - marker_size), (0, height)], fill=(255, 255, 255), width=5)
            # Bottom-right
            draw.line([(width - marker_size, height - 1), (width, height - 1)], fill=(255, 255, 255), width=5)
            draw.line([(width - 1, height - marker_size), (width - 1, height)], fill=(255, 255, 255), width=5)

            # Convert to JPEG bytes
            img_byte_arr = io.BytesIO()
            img.save(img_byte_arr, format='JPEG', quality=85)
            return img_byte_arr.getvalue()
        else:
            # Fallback: create a minimal JPEG header (1x1 pixel)
            # This is a valid minimal JPEG image
            minimal_jpeg = base64.b64decode(
                '/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a'
                'HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIy'
                'MjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAABAAEDASIA'
                'AhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAv/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/8QAFQEB'
                'AQAAAAAAAAAAAAAAAAAAAAX/xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIRAxEAPwCwAA8A'
                'Af/Z'
            )
            self.logger.warning(f"PIL not available, using minimal placeholder image for {camera_name}")
            return minimal_jpeg

    def _publish_node_update(self):
        """Publish node update for graph building (without embedded images)."""
        if not self.publish_nodes:
            return

        # Prepare camera metadata (without image data)
        camera_metadata = []
        if self.publish_images:
            timestamp = datetime.datetime.now().isoformat()
            for camera_name in self.available_cameras:
                camera_metadata.append({
                    "camera_name": camera_name,
                    "timestamp": timestamp
                })

        # Publish node update with metadata only (no image data)
        node_data = {
            "session_node_id": self.node_id_counter,
            "robot_name": self.robot_name,
            "x": float(self.x),
            "y": float(self.y),
            "yaw": float(self.theta),
            "map_id": self.map_id,
            "camera_metadata": camera_metadata,
            "metadata": {
                "robot_name": self.robot_name,
                "timestamp": datetime.datetime.now().isoformat(),
                "battery": self.battery,
                "node_type": "auto_generated"
            }
        }

        self.client.publish(self.node_topic, json.dumps(node_data))
        self.logger.info(f"📍 Published node update for session_node_id {self.node_id_counter} at ({self.x:.2f}, {self.y:.2f})")

        # Publish images separately on image topic
        if self.publish_images:
            self._publish_images(self.node_id_counter)

        self.node_id_counter += 1

    def _publish_images(self, session_node_id: int):
        """
        Publish images separately on the image upload topic.

        Args:
            session_node_id: The robot's local session node ID
        """
        for camera_name in self.available_cameras:
            try:
                # Generate synthetic image
                image_data = self._generate_synthetic_image(camera_name, session_node_id)

                # Encode to base64
                image_base64 = base64.b64encode(image_data).decode('utf-8')

                # Get yaw offset for this camera
                yaw_offset = CAMERA_YAW_OFFSETS.get(camera_name, 0.0)

                # Publish individual image message
                image_message = {
                    "session_node_id": session_node_id,
                    "robot_name": self.robot_name,
                    "camera_name": camera_name,
                    "image_data": image_base64,
                    "content_type": "image/jpeg",
                    "timestamp": datetime.datetime.now().isoformat(),
                    "map_id": self.map_id,
                    "yaw_offset": yaw_offset
                }

                self.client.publish(self.image_topic, json.dumps(image_message))
                self.logger.debug(f"📷 Published image for session_node_id {session_node_id}, camera {camera_name}")

            except Exception as e:
                self.logger.error(f"Failed to publish image for {camera_name}: {e}")

    def _update_position(self):
        """Update robot position (circular movement)."""
        # Move in a circle
        self.angle += (self.speed * self.tick_period) / self.loop_radius
        
        self.x = self.loop_radius * math.cos(self.angle)
        self.y = self.loop_radius * math.sin(self.angle)
        self.theta = self.angle + math.pi / 2  # Tangent to circle
        
        # Simulate battery drain
        self.battery = max(0.0, self.battery - 0.01)
        if self.battery < 20.0:
            self.battery = 100.0  # "Recharge"

    def run(self):
        """Main loop."""
        self.logger.info("🤖 Starting dummy robot main loop...")

        iteration = 0
        try:
            while self.running:
                # Update position
                self._update_position()

                # Publish state every tick
                self._publish_state()

                # Publish node update every 5 ticks (5 seconds by default)
                if iteration % 5 == 0:
                    self._publish_node_update()

                # Publish factsheet every 60 ticks (60 seconds by default)
                # This ensures custom actions are always available even if the database is cleared
                if iteration % 60 == 0:
                    self._publish_factsheet()

                iteration += 1
                time.sleep(self.tick_period)

        except KeyboardInterrupt:
            self.logger.info("Shutting down...")
        finally:
            self.client.loop_stop()
            self.client.disconnect()


def main():
    parser = argparse.ArgumentParser(description="Dummy Robot for Testing")
    parser.add_argument("--robot_name", default="dummy_robot_01",
                        help="Name of the robot")
    parser.add_argument("--manufacturer", default="DummyManufacturer",
                        help="Manufacturer name")
    parser.add_argument("--serial_number", default="DUMMY001",
                        help="Serial number")
    parser.add_argument("--mqtt_host", default="localhost",
                        help="MQTT broker host")
    parser.add_argument("--mqtt_port", type=int, default=1883,
                        help="MQTT broker port")
    parser.add_argument("--mqtt_prefix", default="uagv/v2/RobotCompany",
                        help="VDA5050 MQTT topic prefix")
    parser.add_argument("--node_topic", default="robot/node_update",
                        help="Topic for publishing node updates")
    parser.add_argument("--image_topic", default="robot/image_upload",
                        help="Topic for publishing images")
    parser.add_argument("--map_id", default="default",
                        help="Map ID")
    parser.add_argument("--loop_radius", type=float, default=10.0,
                        help="Radius of circular movement")
    parser.add_argument("--speed", type=float, default=1.0,
                        help="Movement speed (m/s)")
    parser.add_argument("--tick_period", type=float, default=1.0,
                        help="Update period in seconds")
    parser.add_argument("--no_nodes", action="store_true",
                        help="Disable node publishing")
    parser.add_argument("--no_images", action="store_true",
                        help="Disable image publishing")

    args = parser.parse_args()

    robot = DummyRobot(
        robot_name=args.robot_name,
        manufacturer=args.manufacturer,
        serial_number=args.serial_number,
        mqtt_host=args.mqtt_host,
        mqtt_port=args.mqtt_port,
        mqtt_prefix=args.mqtt_prefix,
        node_topic=args.node_topic,
        image_topic=args.image_topic,
        map_id=args.map_id,
        loop_radius=args.loop_radius,
        speed=args.speed,
        tick_period=args.tick_period,
        publish_nodes=not args.no_nodes,
        publish_images=not args.no_images,
    )

    robot.run()


if __name__ == "__main__":
    main()

