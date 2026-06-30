#!/usr/bin/env python3
"""
Graph Builder Service

Processes new node updates from MQTT and builds the topological graph.
"""

import logging
import json
import base64
import asyncio
import uuid
from typing import Dict, Any, Optional, List, Union, Set, Tuple
from datetime import datetime
from collections import defaultdict

import math

from packages.utils.mqtt_client import MQTTClient
from packages.database.postgres import PostgresDatabase
from cloud_common.objects.robot import RobotObjectV1, RobotStatusV1
from cloud_common.objects.object import ObjectLifecycleV1
from cloud_common.objects.mission import MissionObjectV1, MissionQueryParamsV1, MissionStateV1

from packages.topomap_dbs.client import TopomapDatabaseClient
from packages.config import (
    MQTT_KEEPALIVE,
    MINIO_HOST, MINIO_PORT, MINIO_ACCESS_KEY, MINIO_SECRET_KEY, MINIO_SECURE,
    DEFAULT_MAP_ID,
    GPS_MAP_SENTINEL,
    LOCAL_MAP_SENTINEL,
)


class UpdatePublisher:
    """
    Manages WebSocket subscriptions and publishes real-time updates.

    This class allows multiple WebSocket clients to subscribe to updates
    for specific map IDs and broadcasts updates to all subscribers.
    """

    def __init__(self):
        self.logger = logging.getLogger("UpdatePublisher")
        # Map ID -> Set of asyncio.Queue objects
        self.subscribers: Dict[str, Set[asyncio.Queue]] = {}

    def subscribe(self, map_id: str, queue: asyncio.Queue):
        """
        Subscribe to updates for a specific map.

        Args:
            map_id: Map identifier
            queue: Asyncio queue to receive updates
        """
        if map_id not in self.subscribers:
            self.subscribers[map_id] = set()
        self.subscribers[map_id].add(queue)
        self.logger.info(f"New subscriber for map '{map_id}' (total: {len(self.subscribers[map_id])})")

    def unsubscribe(self, map_id: str, queue: asyncio.Queue):
        """
        Unsubscribe from updates for a specific map.

        Args:
            map_id: Map identifier
            queue: Asyncio queue to remove
        """
        if map_id in self.subscribers:
            self.subscribers[map_id].discard(queue)
            self.logger.info(f"Subscriber removed for map '{map_id}' (remaining: {len(self.subscribers[map_id])})")

            # Clean up empty sets
            if not self.subscribers[map_id]:
                del self.subscribers[map_id]

    async def publish(self, map_id: str, message: Dict[str, Any]):
        """
        Publish an update to all subscribers of a specific map.

        Args:
            map_id: Map identifier
            message: Update message to broadcast
        """
        if map_id not in self.subscribers:
            self.logger.debug(f"No subscribers for map '{map_id}'")
            return

        self.logger.debug(f"Publishing update to {len(self.subscribers[map_id])} subscribers for map '{map_id}'")

        # Send to all subscribers
        for queue in list(self.subscribers[map_id]):
            try:
                await queue.put(message)
            except Exception as e:
                self.logger.error(f"Failed to publish to subscriber: {e}")
                # Remove failed subscriber
                self.subscribers[map_id].discard(queue)


class GraphBuilderService:
    """
    Service for building topological maps from robot node updates.
    
    Subscribes to MQTT for new node updates, saves images, creates edges
    based on spatial proximity and distance threshold, and updates the graph database.
    """
    
    def __init__(
        self,
        mqtt_host: str = "localhost",
        mqtt_port: int = 1883,
        mqtt_topic: str = "robot/node_update",
        mqtt_image_topic: str = "robot/image_upload",
        arango_host: str = "localhost",
        arango_port: int = 8529,
        arango_username: str = "root",
        arango_password: Optional[str] = None,
        arango_database: str = "topomap_db",
        minio_host: str = MINIO_HOST,
        minio_port: int = MINIO_PORT,
        minio_access_key: Optional[str] = None,
        minio_secret_key: Optional[str] = None,
        minio_secure: bool = MINIO_SECURE,
        distance_threshold: float = 3.0,
        postgres_db: str = "mission",
        postgres_user: str = "postgres",
        postgres_password: str = "postgres",
        postgres_host: str = "localhost",
        postgres_port: int = 5432,
        radius_threshold: float = 5.0,
        default_map_id: str = DEFAULT_MAP_ID,
        image_buffer_timeout: float = 30.0
    ):
        """
        Initialize the Graph Builder Service.

        Args:
            mqtt_host: MQTT broker host
            mqtt_port: MQTT broker port
            mqtt_topic: MQTT topic to subscribe to for node updates
            mqtt_image_topic: MQTT topic to subscribe to for image uploads
            arango_host: ArangoDB host
            arango_port: ArangoDB port
            arango_username: ArangoDB username
            arango_password: ArangoDB password
            arango_database: ArangoDB database name
            minio_host: MinIO server host
            minio_port: MinIO server port
            minio_access_key: MinIO access key
            minio_secret_key: MinIO secret key
            minio_secure: Whether to use HTTPS for MinIO
            distance_threshold: Maximum distance (in meters) for nodes to be considered traversable
            radius_threshold: Radius in meters for finding nearby nodes
            default_map_id: Default map ID for nodes
            image_buffer_timeout: Timeout in seconds for buffering images
        """
        self.logger = logging.getLogger("GraphBuilderService")

        # Configuration
        self.mqtt_host = mqtt_host
        self.mqtt_port = mqtt_port
        self.mqtt_topic = mqtt_topic
        self.mqtt_image_topic = mqtt_image_topic
        self.radius_threshold = radius_threshold
        self.distance_threshold = distance_threshold
        self.default_map_id = default_map_id
        self.image_buffer_timeout = image_buffer_timeout

        # Initialize PostgreSQL connection
        self.database = PostgresDatabase(
            dbname=postgres_db,
            user=postgres_user,
            password=postgres_password,
            host=postgres_host,
            port=postgres_port
        )

        # Initialize service clients
        from packages.config import ARANGO_PASSWORD
        self.topomap_db = TopomapDatabaseClient(
            arango_host=arango_host,
            arango_port=arango_port,
            arango_username=arango_username,
            arango_password=arango_password or ARANGO_PASSWORD or "openSesame",
            arango_database=arango_database,
            minio_host=minio_host,
            minio_port=minio_port,
            minio_access_key=minio_access_key,
            minio_secret_key=minio_secret_key,
            minio_secure=minio_secure,
            default_map_id=default_map_id,
        )
        self.graph_db = self.topomap_db.graph
        self.image_db = self.topomap_db.image

        # MQTT client
        self.mqtt_client: Optional[MQTTClient] = None
        self._mqtt_connected = False

        # Session ID to Global ID mapping
        # Key: (robot_name, session_node_id) -> Value: (global_node_id, timestamp)
        self.session_to_global_map: Dict[Tuple[str, int], Tuple[str, datetime]] = {}

        # Image buffer for out-of-order arrivals
        # Key: (robot_name, session_node_id) -> {camera_name: (image_data, timestamp)}
        self.image_buffer: Dict[Tuple[str, int], Dict[str, Tuple[Dict[str, Any], datetime]]] = {}

        # Robot registration cache
        # Set of robot names that are known to exist in Mission Dispatch
        self.known_robots: Set[str] = set()

        # Statistics
        self.stats = {
            "nodes_processed": 0,
            "images_saved": 0,
            "edges_created": 0,
            "errors": 0,
            "buffered_images": 0,
            "session_mappings": 0,
            "robots_auto_created": 0
        }

        # WebSocket update publisher
        self.update_publisher = UpdatePublisher()
        # Alias for compatibility with tests
        self.websocket_manager = self.update_publisher

        # Event loop reference for scheduling async tasks from MQTT callbacks
        self._event_loop = None

        self.logger.info("✅ Graph Builder Service initialized")
        self.logger.info(f"   MQTT: {mqtt_host}:{mqtt_port}")
        self.logger.info(f"   Node topic: {mqtt_topic}")
        self.logger.info(f"   Image topic: {mqtt_image_topic}")
        self.logger.info(f"   Radius threshold: {radius_threshold}m")
        self.logger.info(f"   Distance threshold: {distance_threshold}m")
        self.logger.info(f"   Image buffer timeout: {image_buffer_timeout}s")

    def set_event_loop(self, loop: asyncio.AbstractEventLoop):
        """
        Set the event loop for scheduling async tasks from MQTT callbacks.

        Args:
            loop: The asyncio event loop to use
        """
        self._event_loop = loop
        self.logger.debug(f"Event loop set: {loop}")

    # ==================== MQTT Connection ====================
    
    def connect_mqtt(self) -> bool:
        """
        Connect to MQTT broker and subscribe to node update topic.
        
        Returns:
            True if connection successful
        """
        try:
            # Create shared MQTT client
            client_id = f"graph_builder_{datetime.now().timestamp()}"
            self.mqtt_client = MQTTClient(
                client_id=client_id,
                broker=self.mqtt_host,
                port=self.mqtt_port,
                keepalive=MQTT_KEEPALIVE
            )

            # Register callbacks
            self.mqtt_client.register_callback(self.mqtt_topic, self._on_node_update_message)
            self.mqtt_client.register_callback(self.mqtt_image_topic, self._on_image_upload_message)

            # Connect and start background loop
            self.mqtt_client.connect()
            
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to setup MQTT client: {e}")
            return False
    
    def disconnect_mqtt(self):
        """Disconnect from MQTT broker."""
        if self.mqtt_client:
            self.mqtt_client.disconnect()
            self.logger.info("Disconnected from MQTT broker")
            
    # ==================== Message Handlers ====================

    def _on_node_update_message(self, client, userdata, msg):
        """Handle node update message from MQTT."""
        try:
            payload = json.loads(msg.payload.decode('utf-8'))
            if self._event_loop is not None:
                asyncio.run_coroutine_threadsafe(
                    self._handle_node_update(payload),
                    self._event_loop
                )
        except Exception as e:
            self.logger.error(f"Error processing node update message: {e}")
            self.stats["errors"] += 1

    async def _get_robot_map_id(self, robot_name: str) -> Optional[str]:
        """
        Look up the robot's current_map from its database record.

        Returns None when the robot is in GEO or LOCAL mode
        (current_map == GPS_MAP_SENTINEL or LOCAL_MAP_SENTINEL), which
        signals _handle_node_update to skip topology recording entirely.

        Falls back to self.default_map_id if the robot is not found, has no
        current_map set, or the DB query fails.

        Args:
            robot_name: Name of the robot to look up

        Returns:
            The map ID to use, or None if the robot is in GEO or LOCAL mode.
        """
        try:
            robot = await self.database.get_object(RobotObjectV1, robot_name)
            if robot is not None and robot.current_map:
                if robot.current_map in (GPS_MAP_SENTINEL, LOCAL_MAP_SENTINEL):
                    return None
                return robot.current_map
        except Exception as e:
            self.logger.warning(
                f"Could not fetch current_map for robot '{robot_name}': {e}. "
                f"Falling back to default map '{self.default_map_id}'."
            )
        return self.default_map_id

    async def _handle_node_update(self, payload: Dict[str, Any]):
        """
        Async handler for MQTT node update messages.

        Looks up the robot's active mission once to read the register_map flag.
        When register_map=True (or no mission is active): builds topology and logs.
        When register_map=False: skips topology entirely, logs position only.
        When map_id is None (robot in GEO or LOCAL mode): skips topology, logs position only
          (register_map is not available in GEO/LOCAL mode regardless of mission settings).

        The map_id is always resolved from the robot's database record
        (robot.current_map), falling back to self.default_map_id. Any map_id
        field present in the MQTT payload is silently ignored.
        """
        robot_name = payload.get('robot_name')

        # Resolve map_id from the robot's DB record, not from the payload.
        # None means GEO/LOCAL mode — topology recording is not available.
        map_id: Optional[str] = self.default_map_id
        if robot_name:
            map_id = await self._get_robot_map_id(robot_name)

        active_mission = None
        register_map = True  # default when no mission is running
        if robot_name:
            try:
                missions = await self.database.list_objects(
                    MissionObjectV1,
                    MissionQueryParamsV1(
                        robot=robot_name,
                        state=MissionStateV1.RUNNING,
                        started_after=None,
                        started_before=None,
                        most_recent=None,
                    )
                )
                if missions:
                    active_mission = missions[0]
                    register_map = active_mission.register_map
            except Exception as e:
                self.logger.error(f"Failed to look up active mission for '{robot_name}': {e}")

        # GEO/LOCAL mode: no map assigned, skip topology regardless of register_map
        if map_id is None:
            register_map = False

        if register_map:
            result = await asyncio.to_thread(self._process_topology, payload, map_id=map_id)
            if result is None:
                return
            global_node_id, x, y, yaw, map_id, edges, session_node_id, robot_name = result
            await self._publish_node_update(map_id, global_node_id, x, y, yaw, edges, image_ids=None)
            if active_mission is not None:
                await self._log_mission_waypoint(
                    robot_name, global_node_id, session_node_id, x, y, yaw, map_id,
                    _mission=active_mission,
                )
        else:
            x = payload.get('x')
            y = payload.get('y')
            yaw = payload.get('yaw', payload.get('theta', 0.0))
            session_node_id = payload.get('session_node_id')

            if session_node_id is None or robot_name is None or x is None or y is None:
                self.logger.error(
                    "Missing required fields for waypoint logging: session_node_id, robot_name, x, y"
                )
                self.stats["errors"] += 1
                return

            await self._log_mission_waypoint(
                robot_name, str(session_node_id), session_node_id, x, y, yaw,
                map_id or '',
                _mission=active_mission,
            )

    def _process_topology(self, payload: Dict[str, Any], map_id: Optional[str] = None) -> Optional[Tuple]:
        """
        Build topological graph from a node-update payload.

        Validates fields, assigns a global node UUID, creates edges to nearby nodes,
        and writes the node and edges to the graph database.

        Args:
            payload: The incoming MQTT node-update dict.
            map_id: Map identifier resolved externally (e.g. from robot.current_map).
                    When called from the MQTT path this is always provided by
                    _handle_node_update.  When called directly (e.g. via
                    process_node_update or tests) it falls back to self.default_map_id.
                    Any ``map_id`` key present in the payload is silently ignored.

        Returns (global_node_id, x, y, yaw, map_id, edges, session_node_id, robot_name)
        or None on failure.
        """
        session_node_id = payload.get('session_node_id')
        robot_name = payload.get('robot_name')
        x = payload.get('x')
        y = payload.get('y')
        yaw = payload.get('yaw', payload.get('theta', 0.0))
        if map_id is None:
            map_id = self.default_map_id
        camera_metadata = payload.get('camera_metadata', [])
        metadata = payload.get('metadata', {})

        if session_node_id is None or robot_name is None or x is None or y is None:
            self.logger.error("Missing required fields: session_node_id, robot_name, x, y")
            self.stats["errors"] += 1
            return None

        self.logger.info(f"📨 Received node update from {robot_name}, session_node_id={session_node_id}")

        self._detect_and_clear_session_reset(robot_name, session_node_id)

        if not self._ensure_robot_exists(robot_name):
            self.logger.error(f"Failed to ensure robot '{robot_name}' exists in Mission Dispatch database")

        global_node_id = self._generate_global_node_id()

        session_key = (robot_name, session_node_id)
        self.session_to_global_map[session_key] = (global_node_id, datetime.now())
        self.stats["session_mappings"] += 1

        self.logger.info(f"🔑 Mapped ({robot_name}, {session_node_id}) -> {global_node_id}")

        metadata['robot_name'] = robot_name
        metadata['session_node_id'] = session_node_id
        metadata['camera_metadata'] = camera_metadata

        buffered_images = self._get_buffered_images(robot_name, session_node_id)
        if buffered_images:
            self.logger.info(f"Found {len(buffered_images)} buffered images for node {global_node_id}")
            self._save_images(global_node_id, map_id, buffered_images)

        nearby_nodes = self._find_nearby_nodes(x, y, map_id)
        edges = self._create_edges(global_node_id, x, y, yaw, nearby_nodes)

        metadata['map_id'] = map_id
        metadata['timestamp'] = datetime.now().isoformat()

        result = self.graph_db.add_node(
            map_id=map_id,
            node_id=global_node_id,
            x=x,
            y=y,
            yaw=yaw,
            metadata=metadata
        )

        if not result:
            self.logger.error(f"Failed to save node {global_node_id} to graph database")
            self.stats["errors"] += 1
            return None

        inserted = self.graph_db.add_edges_bulk(edges, map_id=map_id)
        self.stats["edges_created"] += inserted

        self.stats["nodes_processed"] += 1
        self.logger.info(
            f"Processed node: global_id={global_node_id}, session_id={session_node_id}, "
            f"robot={robot_name}, edges={len(edges)}"
        )

        return global_node_id, x, y, yaw, map_id, edges, session_node_id, robot_name

    async def _log_mission_waypoint(
        self,
        robot_name: str,
        node_id: str,
        seq: int,
        x: float,
        y: float,
        yaw: float,
        map_id: str,
        _mission=None,
    ):
        """
        Record this node against the robot's currently RUNNING mission, if any.

        If _mission is provided (pre-fetched by the caller) the database lookup is skipped.
        Otherwise, queries PostgreSQL for a RUNNING mission and no-ops if none is found.
        """
        try:
            mission = _mission
            if mission is None:
                missions = await self.database.list_objects(
                    MissionObjectV1,
                    MissionQueryParamsV1(
                        robot=robot_name,
                        state=MissionStateV1.RUNNING,
                        started_after=None,
                        started_before=None,
                        most_recent=None,
                    )
                )
                if not missions:
                    return
                mission = missions[0]
            await self.database.log_mission_waypoint(
                mission_id=mission.name,
                robot_name=robot_name,
                node_id=node_id,
                seq=seq,
                x=x,
                y=y,
                yaw=yaw,
                map_id=map_id,
            )
            self.logger.debug(f"Logged waypoint for mission '{mission.name}': node {node_id} seq={seq}")
        except Exception as e:
            self.logger.error(f"Failed to log mission waypoint: {e}")

    def _on_image_upload_message(self, client, userdata, msg):
        """
        Handle image upload message from MQTT.

        Looks up the global node ID and saves the image.

        Args:
            payload: Image upload message containing:
                - session_node_id: Robot local session node ID
                - robot_name: Name of the robot
                - camera_name: Camera identifier
                - image_data: Base64-encoded image data
                - content_type: Image content type
                - timestamp: Image timestamp
                - map_id: Map identifier
                - yaw_offset: Camera yaw offset in radians (optional)
        """
        try:
            payload = json.loads(msg.payload.decode('utf-8'))
            # Extract required fields
            session_node_id = payload.get('session_node_id')
            robot_name = payload.get('robot_name')
            camera_name = payload.get('camera_name')
            image_data_b64 = payload.get('image_data')
            content_type = payload.get('content_type', 'image/jpeg')
            timestamp = payload.get('timestamp')
            map_id = payload.get('map_id', self.default_map_id)
            yaw_offset = payload.get('yaw_offset', 0.0)

            # Validate required fields
            if session_node_id is None or robot_name is None or camera_name is None or image_data_b64 is None:
                self.logger.error("Missing required fields in image upload: session_node_id, robot_name, camera_name, image_data")
                self.stats["errors"] += 1
                return

            # Look up global node ID
            session_key = (robot_name, session_node_id)
            mapping = self.session_to_global_map.get(session_key)

            self.logger.info(f"📸 Image upload: camera={camera_name}, yaw_offset={yaw_offset}, robot={robot_name}, session_node={session_node_id}")

            if mapping:
                # Node update already received, save image immediately
                global_node_id, _ = mapping

                # Prepare image data
                image_dict = {
                    'image_id': camera_name,
                    'data': image_data_b64,
                    'content_type': content_type,
                    'metadata': {
                        'camera_name': camera_name,
                        'timestamp': timestamp,
                        'robot_name': robot_name,
                        'session_node_id': session_node_id,
                        'yaw_offset': yaw_offset
                    }
                }

                # Save image with global node ID
                saved_image_ids = self._save_images(global_node_id, map_id, [image_dict])

                # Notify WebSocket clients that image is available
                if saved_image_ids and self._event_loop is not None:
                    asyncio.run_coroutine_threadsafe(
                        self._publish_image_update(map_id, global_node_id, saved_image_ids),
                        self._event_loop
                    )

            else:
                # Node update not yet received, buffer the image
                self.logger.debug(f"Buffering image for ({robot_name}, {session_node_id}, {camera_name})")

                node_key = (robot_name, session_node_id)
                image_dict = {
                    'image_id': camera_name,
                    'data': image_data_b64,
                    'content_type': content_type,
                    'metadata': {
                        'camera_name': camera_name,
                        'timestamp': timestamp,
                        'robot_name': robot_name,
                        'session_node_id': session_node_id,
                        'yaw_offset': yaw_offset
                    }
                }

                if node_key not in self.image_buffer:
                    self.image_buffer[node_key] = {}
                self.image_buffer[node_key][camera_name] = (image_dict, datetime.now())
                self.stats["buffered_images"] += 1

        except Exception as e:
            self.logger.error(f"Error processing image upload message: {e}")
            self.stats["errors"] += 1

    # ==================== Node Processing ====================

    def process_node_update(self, node_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Process a new node update.

        Steps:
        1. Save images to image database
        2. Find nearby nodes using radius search
        3. Check traversability using distance threshold
        4. Create edges to traversable nodes
        5. Save node to graph database

        Args:
            node_data: Node update data containing:
                - node_id: Unique node identifier
                - x, y, theta/yaw: Node pose
                - map_id: Map identifier (optional)
                - image/images: Image data (base64 encoded)
                - metadata: Additional node metadata (optional)

        Returns:
            Dictionary with success status and details
        """
        try:
            # Extract node information
            node_id = node_data.get('node_id')
            x = node_data.get('x')
            y = node_data.get('y')
            # Support both theta and yaw
            yaw = node_data.get('yaw', node_data.get('theta', 0.0))
            map_id = node_data.get('map_id', self.default_map_id)
            metadata = node_data.get('metadata', {})

            # Validate required fields
            if node_id is None or x is None or y is None:
                self.logger.error("Missing required fields: node_id, x, y")
                self.stats["errors"] += 1
                return {"success": False, "error": "Missing required fields"}

            self.logger.info(f"Processing node {node_id} at ({x:.2f}, {y:.2f}, {yaw:.2f})")

            # Step 1: Save images to image database
            # Handle both 'image' (single base64) and 'images' (list of dicts) formats
            images = node_data.get('images', [])
            if not images and 'image' in node_data:
                # Convert single image to list format
                images = [{'data': node_data['image'], 'image_id': f'image_0'}]

            saved_image_ids = []
            if images:
                saved_image_ids = self._save_images(node_id, map_id, images)

            # Step 2: Find nearby nodes using radius search
            nearby_nodes = self._find_nearby_nodes(x, y, map_id)
            self.logger.info(f"Found {len(nearby_nodes)} nearby nodes within {self.radius_threshold}m")

            # Step 3: Check traversability and create edges
            edges = self._create_edges(node_id, x, y, yaw, nearby_nodes)
            self.logger.info(f"Created {len(edges)} edges to traversable nodes")

            # Step 4: Save node to graph database with edge information
            metadata['map_id'] = map_id
            metadata['timestamp'] = datetime.now().isoformat()

            result = self.graph_db.add_node(
                map_id=map_id,
                node_id=node_id,
                x=x,
                y=y,
                yaw=yaw,
                metadata=metadata
            )

            if not result:
                self.logger.error(f"Failed to save node {node_id} to graph database")
                self.stats["errors"] += 1
                return {"success": False, "error": "Failed to save node"}

            # Step 5: Save edges to graph database
            for edge in edges:
                edge_result = self.graph_db.add_edge(
                    from_node_id=edge['from_node_id'],
                    to_node_id=edge['to_node_id'],
                    map_id=map_id,
                    metadata=edge.get('metadata', {})
                )
                if edge_result:
                    self.stats["edges_created"] += 1

            self.stats["nodes_processed"] += 1
            self.logger.info(f"✅ Successfully processed node {node_id}")

            # Step 6: Publish update to WebSocket subscribers
            if self._event_loop is not None:
                # Schedule the coroutine on the main event loop from this thread
                asyncio.run_coroutine_threadsafe(
                    self._publish_node_update(map_id, node_id, x, y, yaw, edges, saved_image_ids),
                    self._event_loop
                )
            else:
                self.logger.debug("No event loop set, skipping WebSocket publish")

            return {"success": True, "node_id": node_id, "edges_created": len(edges)}

        except Exception as e:
            self.logger.error(f"Error processing node update: {e}")
            self.stats["errors"] += 1
            return {"success": False, "error": str(e)}

    async def _publish_node_update(
        self,
        map_id: str,
        node_id: Union[int, str],
        x: float,
        y: float,
        yaw: float,
        edges: List[Dict[str, Any]],
        image_ids: Optional[List[str]] = None
    ):
        """
        Publish node update to WebSocket subscribers.

        Args:
            map_id: Map identifier
            node_id: Node identifier
            x, y, yaw: Node pose
            edges: List of created edges
            image_ids: Optional list of image IDs associated with this node
        """
        try:
            # Ensure node_id is a string (UUIDs are strings)
            node_id_str = str(node_id)

            # Convert edge node IDs to strings as well
            edges_converted = []
            for edge in edges:
                edges_converted.append({
                    "from_node_id": str(edge["from_node_id"]),
                    "to_node_id": str(edge["to_node_id"])
                })

            update_message = {
                "type": "node_added",
                "map_id": map_id,
                "node": {
                    "node_id": node_id_str,
                    "x": x,
                    "y": y,
                    "yaw": yaw
                },
                "edges": edges_converted,
                "timestamp": datetime.now().isoformat()
            }

            self.logger.info(f"📡 Publishing WebSocket node metadata for GLOBAL node_id={node_id_str}, map_id={map_id}")
            await self.update_publisher.publish(map_id, update_message)
        except Exception as e:
            self.logger.error(f"Failed to publish node update: {e}")

    async def _publish_image_update(
        self,
        map_id: str,
        node_id: Union[int, str],
        image_ids: List[str]
    ):
        """
        Publish image update to WebSocket subscribers when images arrive after node creation.

        Args:
            map_id: Map identifier
            node_id: Node identifier
            image_ids: List of image IDs that were added
        """
        try:
            # Ensure node_id is a string (UUIDs are strings)
            node_id_str = str(node_id)

            update_message = {
                "type": "node_image_updated",
                "map_id": map_id,
                "node_id": node_id_str,
                "image_ids": image_ids,
                "timestamp": datetime.now().isoformat()
            }

            await self.update_publisher.publish(map_id, update_message)
            self.logger.info(f"📡 Published image update for node {node_id_str}: {image_ids}")
        except Exception as e:
            self.logger.error(f"Failed to publish image update: {e}")

    def _save_images(self, node_id: Union[int, str], map_id: str, images: List[Dict[str, Any]]) -> List[str]:
        """
        Save images to image database.

        Args:
            node_id: Node ID (MUST be global node ID, not session node ID)
            map_id: Map ID
            images: List of image data dictionaries with 'data' (base64 or bytes) and 'image_id'

        Returns:
            List of successfully saved image_ids
        """
        saved_image_ids = []

        self.logger.debug(f"_save_images called with node_id={node_id}, map_id={map_id}, num_images={len(images)}")

        for img in images:
            try:
                image_id = img.get('image_id', f'image_{len(saved_image_ids)}')
                image_data_raw = img.get('data')
                content_type = img.get('content_type', 'image/jpeg')
                metadata = img.get('metadata')

                if not image_data_raw:
                    self.logger.warning(f"No image data for {image_id}")
                    continue

                # Handle both base64 string and raw bytes
                if isinstance(image_data_raw, bytes):
                    # Already bytes, use directly
                    image_data = image_data_raw
                else:
                    # Assume base64 string, decode it
                    image_data = base64.b64decode(image_data_raw)

                # Save to image database
                node_id_str = str(node_id)
                self.logger.info(f"💾 Storing image {image_id} with node_id={node_id_str}, map_id={map_id}, metadata={metadata}")
                success = self.image_db.store_image(
                    image_data=image_data,
                    image_id=image_id,
                    node_id=node_id_str,
                    map_id=map_id,
                    content_type=content_type,
                    metadata=metadata
                )

                if success:
                    saved_image_ids.append(image_id)
                    self.stats["images_saved"] += 1
                    self.logger.info(f"✅ Successfully saved image {image_id} for node {node_id_str} in map {map_id}")
                else:
                    self.logger.warning(f"❌ Failed to save image {image_id} for node {node_id_str}")

            except Exception as e:
                self.logger.error(f"Error saving image: {e}")

        return saved_image_ids
    
    def _find_nearby_nodes(self, x: float, y: float, map_id: str) -> List[Dict[str, Any]]:
        """
        Find nearby nodes using radius search.

        Args:
            x: X coordinate
            y: Y coordinate
            map_id: Map ID

        Returns:
            List of nearby nodes in {"node": {...}, "distance": float} format
        """
        try:
            # Query graph database for nodes within radius
            nodes, distances = self.graph_db.nodes_in_range(
                x=x,
                y=y,
                radius=self.radius_threshold,
                map_id=map_id
            )

            return [{"node": node, "distance": dist} for node, dist in zip(nodes, distances)]

        except Exception as e:
            self.logger.error(f"Error finding nearby nodes: {e}")
            return []
    
    def _create_edges(
        self,
        node_id: Union[int, str],
        x: float,
        y: float,
        yaw: float,
        nearby_nodes: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Create edges to nearby nodes based on traversability.

        Args:
            node_id: Current node ID
            x, y, yaw: Current node pose
            nearby_nodes: List of nearby nodes (with 'node' and 'distance' keys)

        Returns:
            List of edge dictionaries
        """
        edges = []

        for nearby_result in nearby_nodes:
            try:
                # Extract node data from result
                nearby_node = nearby_result.get('node', {})
                nearby_node_id = nearby_node.get('node_id')

                # Skip self-connections
                if nearby_node_id == node_id:
                    continue

                # Get nearby node coordinates
                # Check if coordinates are in 'pose' field (ArangoDB format) or directly in node
                pose = nearby_node.get('pose', {})
                nearby_x = pose.get('x') if pose else nearby_node.get('x')
                nearby_y = pose.get('y') if pose else nearby_node.get('y')

                if nearby_x is None or nearby_y is None:
                    continue

                # Compute Euclidean distance and check traversability inline
                dx = nearby_x - x
                dy = nearby_y - y
                distance = math.sqrt(dx**2 + dy**2)

                if distance <= self.distance_threshold:
                    # Create bidirectional edges
                    edge_metadata = {
                        'distance': distance,
                        'created_at': datetime.now().isoformat()
                    }

                    # Edge from new node to nearby node
                    edges.append({
                        'from_node_id': node_id,
                        'to_node_id': nearby_node_id,
                        'metadata': edge_metadata.copy()
                    })

                    # Edge from nearby node to new node (bidirectional)
                    edges.append({
                        'from_node_id': nearby_node_id,
                        'to_node_id': node_id,
                        'metadata': edge_metadata.copy()
                    })

                    self.logger.debug(
                        f"Created edge: {node_id} <-> {nearby_node_id} "
                        f"(distance: {distance:.2f}m)"
                    )

            except Exception as e:
                self.logger.error(f"Error creating edge to node {nearby_node.get('node_id')}: {e}")

        return edges
    
    # ==================== Helper Methods ====================

    def _detect_and_clear_session_reset(self, robot_name: str, session_node_id: int):
        """
        Detect if a new topomap session has started for this robot.

        If a smaller session_node_id arrives than what we have seen before,
        it indicates the robot has restarted its topomap session.
        Clear all old mappings and buffered images for this robot.

        Args:
            robot_name: Name of the robot
            session_node_id: Current session node ID
        """
        # Find the maximum session_node_id we've seen for this robot
        max_session_id = -1
        for (r_name, s_id), _ in self.session_to_global_map.items():
            if r_name == robot_name:
                max_session_id = max(max_session_id, s_id)

        # If current session_node_id is smaller than max, we've detected a session reset
        if max_session_id >= 0 and session_node_id < max_session_id:
            self.logger.warning(
                f"🔄 Session reset detected for {robot_name}: "
                f"session_node_id={session_node_id} < max_seen={max_session_id}. "
                f"Clearing old mappings and buffered images."
            )

            # Clear session mappings for this robot
            keys_to_remove = [
                key for key in self.session_to_global_map.keys()
                if key[0] == robot_name
            ]
            for key in keys_to_remove:
                del self.session_to_global_map[key]
                self.stats["session_mappings"] -= 1

            # Clear buffered images for this robot
            buffer_keys_to_remove = [k for k in self.image_buffer if k[0] == robot_name]
            for key in buffer_keys_to_remove:
                self.stats["buffered_images"] -= len(self.image_buffer.pop(key))

            self.logger.info(
                f"✅ Cleared {len(keys_to_remove)} session mappings and "
                f"{len(buffer_keys_to_remove)} buffered images for {robot_name}"
            )

    def _generate_global_node_id(self) -> str:
        """
        Generate a globally unique node ID.

        Uses UUID4 for distributed uniqueness.

        Returns:
            Global node ID as string
        """
        return str(uuid.uuid4())

    def _get_buffered_images(self, robot_name: str, session_node_id: int) -> List[Dict[str, Any]]:
        """Retrieve and remove buffered images for a specific node (O(1) lookup)."""
        node_key = (robot_name, session_node_id)
        cameras = self.image_buffer.pop(node_key, None)
        if not cameras:
            return []

        now = datetime.now()
        buffered_images = []
        for camera_name, (image_dict, buffer_time) in cameras.items():
            age = (now - buffer_time).total_seconds()
            if age <= self.image_buffer_timeout:
                buffered_images.append(image_dict)
            else:
                self.logger.warning(
                    f"Buffered image timed out: ({robot_name}, {session_node_id}, {camera_name}), age={age:.1f}s"
                )
            self.stats["buffered_images"] -= 1

        return buffered_images

    async def _check_robot_exists(self, robot_name: str) -> bool:
        """
        Check if a robot exists in the Mission Dispatch database.

        Uses a local cache to avoid repeated database queries for known robots.

        Args:
            robot_name: Name of the robot to check

        Returns:
            True if robot exists, False otherwise
        """
        # Check cache first
        if robot_name in self.known_robots:
            return True

        # Query Mission Dispatch database
        try:
            robot = await self.database.get_object(RobotObjectV1, robot_name)
            if robot:
                # Robot exists, add to cache
                self.known_robots.add(robot_name)
                self.logger.debug(f"Robot '{robot_name}' exists in Mission Dispatch database")
                return True
            return False
        except Exception as e:
            self.logger.debug(f"Robot '{robot_name}' check failed: {e}")
            return False

    async def _create_robot(self, robot_name: str) -> bool:
        """
        Create a new robot in the Mission Dispatch database.

        Creates a robot with minimal default configuration.

        Args:
            robot_name: Name of the robot to create

        Returns:
            True if robot was created successfully, False otherwise
        """
        try:
            # Create robot with minimal required fields
            robot = RobotObjectV1(
                name=robot_name,
                lifecycle=ObjectLifecycleV1.ALIVE,
                status=RobotStatusV1(),
                labels=["auto_registered"]
            )

            await self.database.create_object(robot, uuid.uuid4())
            
            # Robot created successfully
            self.known_robots.add(robot_name)
            self.stats["robots_auto_created"] += 1
            self.logger.info(f"🤖 Auto-created robot '{robot_name}' in Mission Dispatch database")
            return True

        except Exception as e:
            self.logger.error(f"Failed to create robot '{robot_name}': {e}")
            return False

    async def _ensure_robot_exists(self, robot_name: str) -> bool:
        """
        Ensure a robot exists in the Mission Dispatch database.

        Checks if the robot exists, and creates it if it does not.

        Args:
            robot_name: Name of the robot

        Returns:
            True if robot exists or was created successfully, False otherwise
        """
        # Check if robot exists
        if await self._check_robot_exists(robot_name):
            return True

        # Robot doesn't exist, try to create it
        self.logger.info(f"Robot '{robot_name}' not found, attempting auto-registration...")
        return await self._create_robot(robot_name)

    def _cleanup_old_mappings(self, threshold_seconds: float = 3600):
        """
        Clean up old session mappings and buffered images.

        Args:
            threshold_seconds: Maximum age in seconds before cleanup
        """
        now = datetime.now()

        # Clean up old session mappings
        old_mappings = []
        for session_key, (global_id, timestamp) in self.session_to_global_map.items():
            age = (now - timestamp).total_seconds()
            if age > threshold_seconds:
                old_mappings.append(session_key)

        for key in old_mappings:
            del self.session_to_global_map[key]
            self.logger.debug(f"Cleaned up old session mapping: {key}")

        # Clean up old buffered images
        old_node_keys = []
        for node_key, cameras in self.image_buffer.items():
            stale = [cam for cam, (_, ts) in cameras.items()
                     if (now - ts).total_seconds() > threshold_seconds]
            for cam in stale:
                del cameras[cam]
                self.stats["buffered_images"] -= 1
                self.logger.debug(f"Cleaned up old buffered image: {node_key + (cam,)}")
            if not cameras:
                old_node_keys.append(node_key)
        for key in old_node_keys:
            del self.image_buffer[key]

    # ==================== Service Management ====================

    def get_stats(self) -> Dict[str, Any]:
        """
        Get service statistics.

        Returns:
            Dictionary with service statistics
        """
        return {
            **self.stats,
            "mqtt_connected": self._mqtt_connected,
            "radius_threshold": self.radius_threshold
        }
    
    def is_healthy(self) -> bool:
        """
        Check if service is healthy.

        Returns:
            True if all dependencies are healthy
        """
        try:
            return self._mqtt_connected and self.topomap_db.is_healthy() and self.database.is_running()
        except Exception:
            return False

    def get_health_details(self) -> Dict[str, Any]:
        try:
            image_db_healthy = False
            graph_db_healthy = False

            try:
                image_db_healthy = self.image_db.is_healthy()
            except Exception:
                pass

            try:
                graph_db_healthy = self.graph_db.is_healthy()
            except Exception:
                pass

            return {
                "mqtt_connected": self._mqtt_connected,
                "image_db": image_db_healthy,
                "graph_db": graph_db_healthy,
            }
        except Exception:
            return {
                "mqtt_connected": self._mqtt_connected,
                "image_db": False,
                "graph_db": False,
            }

