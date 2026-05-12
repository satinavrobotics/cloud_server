#!/usr/bin/env python3
"""
API Delegation Service

Central API gateway that provides a unified interface for clients.
Delegates requests to appropriate microservices and manages WebSocket connections.
"""

import logging
import json
from typing import Dict, Any, Optional, List, Set
from datetime import datetime
import asyncio
import threading
import time

from fastapi import WebSocket, WebSocketDisconnect, HTTPException

from packages.topomap_dbs.client import TopomapDatabaseClient
from packages.services.mission_planner.client import MissionPlannerClient
from packages.services.livekit.client import LiveKitClient
from packages.database.postgres import PostgresDatabase
from packages.topomap_dbs.graph_db.server import GraphDatabaseService
from cloud_common.objects.robot import RobotObjectV1
from cloud_common.objects.map import MapObjectV1, MapSpecV1, MapStatusV1
from cloud_common.objects.mission import MissionObjectV1
from cloud_common.objects.object import ObjectLifecycleV1
from packages.config import (
    ARANGO_HOST, ARANGO_PORT, ARANGO_USERNAME, ARANGO_PASSWORD, DATA_BASE_NAME,
    URL_MISSION_PLANNER, URL_LIVEKIT, URL_MISSION_DISPATCH,
    MINIO_HOST, MINIO_PORT, MINIO_ACCESS_KEY, MINIO_SECRET_KEY, MINIO_SECURE,
    MQTT_HOST, MQTT_PORT, MQTT_KEEPALIVE, DEFAULT_MAP_ID,
)

try:
    import websockets
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False




class WebSocketManager:
    """
    Manages WebSocket connections for real-time updates.
    
    Supports multiple connection types:
    - Map updates (graph builder notifications)
    - Mission status updates
    - Navigation status updates
    """
    
    def __init__(self):
        self.logger = logging.getLogger("WebSocketManager")
        
        # Active connections by type and identifier
        # Format: {connection_type: {identifier: Set[WebSocket]}}
        self.connections: Dict[str, Dict[str, Set[WebSocket]]] = {
            "map_updates": {},      # map_id -> Set[WebSocket]
            "mission_status": {},   # mission_name -> Set[WebSocket]
            "robot_status": {},     # robot_name -> Set[WebSocket]
        }
    
    async def connect(self, websocket: WebSocket, connection_type: str, identifier: str):
        """
        Register a new WebSocket connection.
        
        Args:
            websocket: WebSocket connection
            connection_type: Type of connection (map_updates, mission_status, robot_status)
            identifier: Unique identifier (map_id, mission_name, robot_name)
        """
        await websocket.accept()
        
        if connection_type not in self.connections:
            self.logger.warning(f"Unknown connection type: {connection_type}")
            return
        
        if identifier not in self.connections[connection_type]:
            self.connections[connection_type][identifier] = set()
        
        self.connections[connection_type][identifier].add(websocket)
        self.logger.info(f"WebSocket connected: {connection_type}/{identifier}")
    
    def disconnect(self, websocket: WebSocket, connection_type: str, identifier: str):
        """
        Unregister a WebSocket connection.
        
        Args:
            websocket: WebSocket connection
            connection_type: Type of connection
            identifier: Unique identifier
        """
        if connection_type in self.connections:
            if identifier in self.connections[connection_type]:
                self.connections[connection_type][identifier].discard(websocket)
                
                # Clean up empty sets
                if not self.connections[connection_type][identifier]:
                    del self.connections[connection_type][identifier]
        
        self.logger.info(f"WebSocket disconnected: {connection_type}/{identifier}")
    
    async def broadcast(self, connection_type: str, identifier: str, message: Dict[str, Any]):
        """
        Broadcast a message to all connections of a specific type and identifier.
        
        Args:
            connection_type: Type of connection
            identifier: Unique identifier
            message: Message to broadcast
        """
        if connection_type not in self.connections:
            return
        
        if identifier not in self.connections[connection_type]:
            return
        
        # Create list to avoid modification during iteration
        connections = list(self.connections[connection_type][identifier])
        
        for websocket in connections:
            try:
                await websocket.send_json(message)
            except Exception as e:
                self.logger.error(f"Failed to send message to WebSocket: {e}")
                # Remove failed connection
                self.disconnect(websocket, connection_type, identifier)
    
    async def broadcast_to_all(self, connection_type: str, message: Dict[str, Any]):
        """
        Broadcast a message to all connections of a specific type.
        
        Args:
            connection_type: Type of connection
            message: Message to broadcast
        """
        if connection_type not in self.connections:
            return
        
        for identifier in list(self.connections[connection_type].keys()):
            await self.broadcast(connection_type, identifier, message)


class WebSocketProxyManager:
    """
    Manages WebSocket proxying between clients and backend services.

    This class establishes connections to backend services and forwards
    messages bidirectionally between clients and backends.
    """

    def __init__(
        self,
        graph_builder_ws_url: Optional[str] = None,
        mission_dispatcher_ws_url: Optional[str] = None
    ):
        """
        Initialize the WebSocket proxy manager.

        Args:
            graph_builder_ws_url: Base WebSocket URL for graph builder service
            mission_dispatcher_ws_url: Base WebSocket URL for mission dispatcher
        """
        self.logger = logging.getLogger("WebSocketProxyManager")

        # Backend service URLs
        self.graph_builder_ws_url = graph_builder_ws_url
        self.mission_dispatcher_ws_url = mission_dispatcher_ws_url

        # Active proxy connections
        # Format: {connection_key: {"backend": websocket, "clients": Set[WebSocket], "task": asyncio.Task}}
        self.proxy_connections: Dict[str, Dict[str, Any]] = {}

        # Lock for managing proxy connections
        self._lock = asyncio.Lock()

        if not WEBSOCKETS_AVAILABLE:
            self.logger.warning("websockets library not available - proxy functionality disabled")

    async def proxy_map_updates(self, client_ws: WebSocket, map_id: str):
        """
        Proxy map updates from Graph Builder Service to client.

        Args:
            client_ws: Client WebSocket connection
            map_id: Map identifier
        """
        if not WEBSOCKETS_AVAILABLE:
            await client_ws.close(code=1011, reason="WebSocket proxy not available")
            return

        await client_ws.accept()
        connection_key = f"map_updates:{map_id}"

        try:
            # Add client to proxy connection
            # Note: If backend service is not configured, this will create a stub connection
            # that keeps the client connected but doesn't forward messages
            await self._add_client_to_proxy(connection_key, client_ws, map_id, "map_updates")

            # Keep connection alive and handle client messages
            while True:
                try:
                    # Receive messages from client (if any)
                    data = await client_ws.receive_text()
                    # For now, just acknowledge (can be extended for client commands)
                    await client_ws.send_json({
                        "type": "ack",
                        "message": "Message received",
                        "timestamp": datetime.now().isoformat()
                    })
                except WebSocketDisconnect:
                    break

        except Exception as e:
            self.logger.error(f"Error in map updates proxy for {map_id}: {e}")
        finally:
            await self._remove_client_from_proxy(connection_key, client_ws)

    async def _add_client_to_proxy(
        self,
        connection_key: str,
        client_ws: WebSocket,
        identifier: str,
        connection_type: str
    ):
        """
        Add a client to an existing proxy connection or create a new one.

        Args:
            connection_key: Unique key for this proxy connection
            client_ws: Client WebSocket
            identifier: Resource identifier (map_id, mission_name, etc.)
            connection_type: Type of connection (map_updates, mission_status, etc.)
        """
        async with self._lock:
            if connection_key not in self.proxy_connections:
                # Create new proxy connection to backend
                self.logger.info(f"Creating new proxy connection: {connection_key}")

                backend_ws = await self._connect_to_backend(connection_type, identifier)

                # If backend connection fails, create a stub connection that keeps the client connected
                # but doesn't forward any messages from a backend
                if backend_ws is None:
                    self.logger.warning(
                        f"Backend connection failed for {connection_key}, "
                        f"creating stub connection (no backend forwarding)"
                    )
                    forward_task = asyncio.create_task(
                        self._hold_until_cancelled(connection_key)
                    )
                else:
                    # Create task to forward messages from backend to clients
                    forward_task = asyncio.create_task(
                        self._forward_from_backend(connection_key, backend_ws)
                    )

                self.proxy_connections[connection_key] = {
                    "backend": backend_ws,
                    "clients": set(),
                    "task": forward_task
                }

            # Add client to the set
            self.proxy_connections[connection_key]["clients"].add(client_ws)
            self.logger.info(
                f"Client added to {connection_key} "
                f"(total clients: {len(self.proxy_connections[connection_key]['clients'])})"
            )

    async def _remove_client_from_proxy(self, connection_key: str, client_ws: WebSocket):
        """
        Remove a client from a proxy connection and clean up if no clients remain.

        Args:
            connection_key: Unique key for this proxy connection
            client_ws: Client WebSocket to remove
        """
        async with self._lock:
            if connection_key in self.proxy_connections:
                self.proxy_connections[connection_key]["clients"].discard(client_ws)

                remaining_clients = len(self.proxy_connections[connection_key]["clients"])
                self.logger.info(f"Client removed from {connection_key} (remaining: {remaining_clients})")

                # If no clients remain, close backend connection
                if remaining_clients == 0:
                    self.logger.info(f"No clients remaining, closing backend connection: {connection_key}")

                    # Cancel forwarding task
                    self.proxy_connections[connection_key]["task"].cancel()

                    # Close backend WebSocket
                    try:
                        await self.proxy_connections[connection_key]["backend"].close()
                    except Exception as e:
                        self.logger.error(f"Error closing backend connection: {e}")

                    # Remove from tracking
                    del self.proxy_connections[connection_key]

    async def _connect_to_backend(
        self,
        connection_type: str,
        identifier: str
    ) -> Optional[Any]:
        """
        Connect to backend service WebSocket.

        Args:
            connection_type: Type of connection (map_updates, mission_status, etc.)
            identifier: Resource identifier

        Returns:
            WebSocket connection to backend or None if failed
        """
        try:
            if connection_type == "map_updates":
                if not self.graph_builder_ws_url:
                    self.logger.error("Graph builder WebSocket URL not configured")
                    return None

                backend_url = f"{self.graph_builder_ws_url}/ws/updates/{identifier}"
                self.logger.info(f"Connecting to backend: {backend_url}")

                backend_ws = await websockets.connect(backend_url)
                self.logger.info(f"Connected to backend: {backend_url}")
                return backend_ws

            elif connection_type == "mission_status":
                if not self.mission_dispatcher_ws_url:
                    self.logger.error("Mission dispatcher WebSocket URL not configured")
                    return None

                backend_url = f"{self.mission_dispatcher_ws_url}/ws/mission/{identifier}"
                self.logger.info(f"Connecting to backend: {backend_url}")

                backend_ws = await websockets.connect(backend_url)
                self.logger.info(f"Connected to backend: {backend_url}")
                return backend_ws

            elif connection_type == "robot_status":
                if not self.mission_dispatcher_ws_url:
                    self.logger.error("Mission dispatcher WebSocket URL not configured")
                    return None

                backend_url = f"{self.mission_dispatcher_ws_url}/ws/robot/{identifier}"
                self.logger.info(f"Connecting to backend: {backend_url}")

                backend_ws = await websockets.connect(backend_url)
                self.logger.info(f"Connected to backend: {backend_url}")
                return backend_ws

            else:
                self.logger.error(f"Unknown connection type: {connection_type}")
                return None

        except Exception as e:
            self.logger.error(f"Failed to connect to backend for {connection_type}/{identifier}: {e}")
            return None

    async def _forward_from_backend(self, connection_key: str, backend_ws: Any):
        """
        Forward messages from backend to all connected clients.
        Automatically reconnects if the backend connection is lost.

        Args:
            connection_key: Unique key for this proxy connection
            backend_ws: Backend WebSocket connection
        """
        current_backend_ws = backend_ws
        max_reconnect_attempts = 5
        reconnect_delay = 2  # seconds

        while True:
            try:
                async for message in current_backend_ws:
                    # Parse message
                    try:
                        data = json.loads(message)
                    except json.JSONDecodeError:
                        data = {"raw": message}

                    # Forward to all clients
                    if connection_key in self.proxy_connections:
                        clients = list(self.proxy_connections[connection_key]["clients"])

                        self.logger.debug(f"Forwarding message to {len(clients)} clients for {connection_key}")

                        for client_ws in clients:
                            try:
                                await client_ws.send_json(data)
                            except Exception as e:
                                self.logger.error(f"Failed to forward message to client: {e}")
                                # Client will be removed when their connection handler exits
                    else:
                        # Connection key no longer exists, exit
                        self.logger.info(f"Connection key {connection_key} no longer exists, stopping forwarding")
                        return

                # If the loop finishes normally, it means the connection was closed
                raise Exception("Backend connection closed normally")
                
            except asyncio.CancelledError:
                self.logger.info(f"Forwarding task cancelled for {connection_key}")
                return
            except Exception as e:
                self.logger.error(f"Error forwarding from backend for {connection_key}: {e}")

                # Check if we should attempt reconnection
                if connection_key not in self.proxy_connections:
                    self.logger.info(f"Connection key {connection_key} no longer exists, not reconnecting")
                    return

                # Check if there are still clients connected
                if len(self.proxy_connections[connection_key]["clients"]) == 0:
                    self.logger.info(f"No clients remaining for {connection_key}, not reconnecting")
                    return

                # Attempt to reconnect
                for attempt in range(max_reconnect_attempts):
                    self.logger.info(f"Attempting to reconnect to backend for {connection_key} (attempt {attempt + 1}/{max_reconnect_attempts})")

                    await asyncio.sleep(reconnect_delay)

                    # Parse connection_key to get connection_type and identifier
                    # Format: "connection_type:identifier"
                    parts = connection_key.split(":", 1)
                    if len(parts) != 2:
                        self.logger.error(f"Invalid connection_key format: {connection_key}")
                        return

                    connection_type, identifier = parts

                    # Try to reconnect
                    new_backend_ws = await self._connect_to_backend(connection_type, identifier)

                    if new_backend_ws is not None:
                        self.logger.info(f"Successfully reconnected to backend for {connection_key}")

                        # Update the backend connection in proxy_connections
                        async with self._lock:
                            if connection_key in self.proxy_connections:
                                self.proxy_connections[connection_key]["backend"] = new_backend_ws

                        current_backend_ws = new_backend_ws
                        break
                    else:
                        self.logger.warning(f"Failed to reconnect to backend for {connection_key} (attempt {attempt + 1}/{max_reconnect_attempts})")
                else:
                    # Max reconnection attempts reached
                    self.logger.error(f"Max reconnection attempts reached for {connection_key}, giving up")
                    return

    async def _hold_until_cancelled(self, connection_key: str):
        """
        Keeps a proxy slot alive when no backend connection is available.

        Sleeps indefinitely until cancelled by the last-client-disconnect cleanup path.

        Args:
            connection_key: Unique key for this proxy connection
        """
        try:
            await asyncio.sleep(float('inf'))
        except asyncio.CancelledError:
            self.logger.info(f"Idle hold task cancelled for {connection_key}")


class ApiDelegationService:
    """
    API Delegation Service - Central gateway for client requests.
    
    Provides:
    - REST endpoints for map loading, image retrieval, navigation, status queries
    - WebSocket endpoints for real-time updates
    - Proxy functionality to underlying microservices
    """
    
    def __init__(
        self,
        arango_host: str = ARANGO_HOST,
        arango_port: int = ARANGO_PORT,
        arango_username: str = ARANGO_USERNAME,
        arango_password: Optional[str] = None,
        arango_database: str = DATA_BASE_NAME,
        minio_host: str = MINIO_HOST,
        minio_port: int = MINIO_PORT,
        minio_access_key: Optional[str] = None,
        minio_secret_key: Optional[str] = None,
        minio_secure: bool = MINIO_SECURE,
        mission_planner_url: str = URL_MISSION_PLANNER,
        livekit_url: str = URL_LIVEKIT,
        postgres_db: str = "mission",
        postgres_user: str = "postgres",
        postgres_password: str = "postgres",
        postgres_host: str = "localhost",
        postgres_port: int = 5432,
        default_map_id: str = DEFAULT_MAP_ID,
        graph_builder_ws_url: Optional[str] = None,
        mission_dispatcher_ws_url: Optional[str] = None,
        mqtt_enabled: bool = True,
        mqtt_broker: str = MQTT_HOST,
        mqtt_port: int = MQTT_PORT,
        mqtt_keepalive: int = MQTT_KEEPALIVE
    ):
        """
        Initialize API Delegation Service.

        Args:
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
            mission_planner_url: URL of the mission planner service
            livekit_url: URL of the LiveKit service
            postgres_db: PostgreSQL database name
            postgres_user: PostgreSQL database user
            postgres_password: PostgreSQL database password
            postgres_host: PostgreSQL database host
            postgres_port: PostgreSQL database port
            default_map_id: Default map ID to use
            graph_builder_ws_url: WebSocket URL for graph builder service
            mission_dispatcher_ws_url: WebSocket URL for mission dispatcher service
            mqtt_enabled: Enable MQTT mission progress subscription
            mqtt_broker: MQTT broker address
            mqtt_port: MQTT broker port
            mqtt_keepalive: MQTT keepalive interval in seconds
        """
        self.logger = logging.getLogger("ApiDelegationService")

        # Initialize service clients
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
        self.rosbag_db = self.topomap_db.rosbag
        self.model_db = self.topomap_db.model
        self.mission_planner = MissionPlannerClient(url=mission_planner_url)
        self.livekit = LiveKitClient(url=livekit_url)
        self.database = PostgresDatabase(
            dbname=postgres_db,
            user=postgres_user,
            password=postgres_password,
            host=postgres_host,
            port=postgres_port
        )
        import uuid
        self._publisher_id = uuid.uuid4()

        # Configuration
        self.default_map_id = default_map_id

        self.ws_manager = WebSocketManager()

        # WebSocket proxy manager (new implementation)
        self.ws_proxy = WebSocketProxyManager(
            graph_builder_ws_url=graph_builder_ws_url,
            mission_dispatcher_ws_url=mission_dispatcher_ws_url
        )

        # Service URLs / endpoints for reference
        self.arango_host = arango_host
        self.arango_port = arango_port
        self.minio_host = minio_host
        self.mission_planner_url = mission_planner_url
        self.livekit_url = livekit_url
        self.database_url = f"postgresql://{postgres_host}:{postgres_port}/{postgres_db}"

        # MQTT configuration
        self.mqtt_enabled = mqtt_enabled
        self.mqtt_broker = mqtt_broker
        self.mqtt_port = mqtt_port
        self.mqtt_keepalive = mqtt_keepalive
        self.mqtt_client = None

        # Background task management
        self._event_loop = None
        self._robot_changes: asyncio.Queue[RobotObjectV1] = None
        self._mission_changes: asyncio.Queue[MissionObjectV1] = None
        self._robot_watcher_thread = None
        self._mission_watcher_thread = None
        self._watcher_tasks = []
        self._running = False

        self.logger.info("✅ API Delegation Service initialized")
        self.logger.info(f"   ArangoDB: {arango_host}:{arango_port}")
        self.logger.info(f"   MinIO: {minio_host}:{minio_port}")
        self.logger.info(f"   Mission Planner: {mission_planner_url}")
        self.logger.info(f"   LiveKit: {livekit_url}")
        self.logger.info(f"   Database: {self.database_url}")
        if graph_builder_ws_url:
            self.logger.info(f"   Graph Builder WS: {graph_builder_ws_url}")
        if mission_dispatcher_ws_url:
            self.logger.info(f"   Mission Dispatcher WS: {mission_dispatcher_ws_url}")



    # ==================== Health Check ====================
    
    def is_healthy(self) -> bool:
        """
        Check if the service and all dependencies are healthy.

        Returns:
            True if all dependencies are healthy
        """
        try:
            database_healthy = self.database.is_running()
            return self.topomap_db.is_healthy() and database_healthy
        except Exception as e:
            self.logger.error(f"Health check failed: {e}")
            return False
    
    def get_stats(self) -> Dict[str, Any]:
        """
        Get service statistics.

        Returns:
            Dictionary with service statistics
        """
        return {
            "service": "api_delegation",
            "graph_db_url": f"arangodb://{self.arango_host}:{self.arango_port}",
            "minio_host": self.minio_host,
            "mission_planner_url": self.mission_planner_url,
            "livekit_url": self.livekit_url,
            "database_url": self.database_url,
            "default_map_id": self.default_map_id,
            "websocket_connections": {
                "map_updates": sum(len(conns) for conns in self.ws_manager.connections["map_updates"].values()),
                "mission_status": sum(len(conns) for conns in self.ws_manager.connections["mission_status"].values()),
                "robot_status": sum(len(conns) for conns in self.ws_manager.connections["robot_status"].values()),
            }
        }
    
    # ==================== Map Operations ====================

    async def load_map(
        self,
        map_data: Optional[Dict[str, Any]] = None,
        map_id: Optional[str] = None,
        description: Optional[str] = None,
        datum_latitude: Optional[float] = None,
        datum_longitude: Optional[float] = None,
        datum_bearing_deg: float = 0.0,
    ) -> Dict[str, Any]:
        """
        Load a map into the graph and image databases and register it in Postgres.

        Can be called in two ways:
        1. load_map(map_data) - Load complete map with nodes and edges
        2. load_map(map_id) - Load existing map from database
        """
        import uuid as _uuid

        # Handle both calling conventions
        if isinstance(map_data, dict) and "map_id" in map_data:
            actual_map_id = map_data["map_id"]
            nodes = map_data.get("nodes", [])
            edges = map_data.get("edges", [])
        else:
            actual_map_id = map_data if isinstance(map_data, str) else (map_id or self.default_map_id)
            nodes = []
            edges = []

        self.logger.info(f"Loading map: {actual_map_id}")

        try:
            # Create map in ArangoDB
            result = self.graph_db.create_map(actual_map_id)
            if not result:
                return {
                    "success": False,
                    "map_id": actual_map_id,
                    "error": "Failed to create map"
                }

            # Add nodes and images
            for node in nodes:
                self.graph_db.add_node(
                    map_id=actual_map_id,
                    node_id=node["id"],
                    x=node["x"],
                    y=node["y"],
                    yaw=node.get("theta", 0.0)
                )
                if "image" in node:
                    import base64
                    try:
                        image_data = base64.b64decode(node["image"])
                        import uuid as _uuid2
                        _image_id = str(_uuid2.uuid4())
                        success = self.image_db.store_image(
                            image_data=image_data,
                            image_id=_image_id,
                            node_id=node["id"],
                            map_id=actual_map_id,
                        )
                        if not success:
                            self.logger.warning(f"Failed to save image for node {node['id']}")
                    except Exception as e:
                        self.logger.warning(f"Failed to save image for node {node['id']}: {e}")

            # Add edges
            for edge in edges:
                self.graph_db.add_edge(
                    from_node_id=edge["from"],
                    to_node_id=edge["to"],
                    map_id=actual_map_id,
                    metadata={"weight": edge.get("weight", 1.0)}
                )

            # Register map in Postgres (create or update if already exists)
            try:
                stats = self.graph_db.get_map_stats(actual_map_id)
            except Exception:
                stats = {"node_count": len(nodes), "edge_count": len(edges)}

            map_obj = MapObjectV1(
                name=actual_map_id,
                lifecycle=ObjectLifecycleV1.ALIVE,
                description=description,
                datum_latitude=datum_latitude,
                datum_longitude=datum_longitude,
                datum_bearing_deg=datum_bearing_deg,
                status=MapStatusV1(
                    node_count=stats.get("node_count", len(nodes)),
                    edge_count=stats.get("edge_count", len(edges)),
                ),
            )
            try:
                await self.database.create_object(map_obj, _uuid.uuid4())
                self.logger.info(f"Registered map '{actual_map_id}' in Postgres")
            except Exception:
                # Map already registered — update spec to pick up any datum changes
                await self.database.update_spec(
                    MapObjectV1, actual_map_id, map_obj.spec, _uuid.uuid4()
                )
                self.logger.info(f"Updated existing map '{actual_map_id}' in Postgres")

            # Fetch existing nodes and edges from the database
            try:
                self.logger.info(f"Fetching nodes and edges for map: {actual_map_id}")
                all_nodes_response = self.graph_db.get_all_nodes(actual_map_id)
                all_edges_response = self.graph_db.get_edges(actual_map_id)
                self.logger.info(f"Fetched {len(all_nodes_response)} nodes and {len(all_edges_response)} edges for map {actual_map_id}")

                nodes_data = []
                for node in all_nodes_response:
                    pose = node.get("pose", {})
                    x = pose.get("x") if pose else node.get("x", 0)
                    y = pose.get("y") if pose else node.get("y", 0)
                    yaw = pose.get("yaw") if pose else (node.get("theta") or node.get("yaw", 0))
                    node_id = str(node.get("node_id") or node.get("_key"))
                    nodes_data.append({
                        "id": node_id,
                        "x": x,
                        "y": y,
                        "theta": yaw,
                        "timestamp": node.get("created_at"),
                        "metadata": node.get("metadata", {})
                    })

                edges_data = []
                for edge in all_edges_response:
                    edges_data.append({
                        "from": str(edge.get("from") or edge.get("from_node_id")),
                        "to": str(edge.get("to") or edge.get("to_node_id")),
                        "weight": edge.get("weight"),
                        "metadata": edge.get("metadata", {})
                    })
            except Exception as e:
                self.logger.error(f"Failed to fetch nodes/edges for map {actual_map_id}: {e}", exc_info=True)
                nodes_data = []
                edges_data = []

            # Notify graph builder
            self._notify_graph_builder(actual_map_id, "map_loaded")

            return {
                "success": True,
                "map_id": actual_map_id,
                "message": f"Map {actual_map_id} loaded successfully",
                "stats": stats,
                "nodes": nodes_data,
                "edges": edges_data,
            }
        except Exception as e:
            self.logger.error(f"Failed to load map {actual_map_id}: {e}")
            return {
                "success": False,
                "map_id": actual_map_id,
                "error": str(e)
            }

    async def get_map_status(self, map_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Get status/statistics for a map.

        Args:
            map_id: Map ID (uses default if not provided)

        Returns:
            Dictionary with map status and statistics
        """
        if map_id is None:
            map_id = self.default_map_id

        self.logger.info(f"Getting status for map: {map_id}")

        try:
            # Get map statistics from graph database
            stats = self.graph_db.get_map_stats(map_id)

            if "error" in stats:
                return {
                    "success": False,
                    "map_id": map_id,
                    "error": stats["error"]
                }

            # Return stats directly with map_id and status
            return {
                "success": True,
                "map_id": map_id,
                "node_count": stats.get("node_count", 0),
                "edge_count": stats.get("edge_count", 0),
                "status": "ready"
            }
        except Exception as e:
            error_msg = str(e)
            self.logger.error(f"Failed to get map status for {map_id}: {error_msg}")

            # Ensure error message indicates map not found
            if "not found" not in error_msg.lower() and "404" in error_msg:
                error_msg = f"Map {map_id} not found"

            return {
                "success": False,
                "map_id": map_id,
                "error": error_msg
            }

    async def get_map(self, map_id: str) -> Dict[str, Any]:
        """Get a single map from Postgres, enriched with live ArangoDB node/edge counts."""
        import uuid as _uuid
        try:
            map_obj = await self.database.get_object(MapObjectV1, map_id)
        except Exception:
            return {"success": False, "error": f"Map '{map_id}' not found"}
        stats = self.graph_db.get_map_stats(map_id)
        return {
            "success": True,
            "map_id": map_id,
            **map_obj.spec.dict(),
            "node_count": stats.get("node_count", 0),
            "edge_count": stats.get("edge_count", 0),
        }

    async def update_map_datum(
        self,
        map_id: str,
        datum_latitude: float,
        datum_longitude: float,
        datum_bearing_deg: float = 0.0,
    ) -> Dict[str, Any]:
        """Register or update the GPS datum for an existing map."""
        import uuid as _uuid
        try:
            map_obj = await self.database.get_object(MapObjectV1, map_id)
        except Exception:
            return {"success": False, "error": f"Map '{map_id}' not found"}
        map_obj.datum_latitude = datum_latitude
        map_obj.datum_longitude = datum_longitude
        map_obj.datum_bearing_deg = datum_bearing_deg
        await self.database.update_spec(MapObjectV1, map_id, map_obj.spec, _uuid.uuid4())
        self.logger.info(
            f"Updated datum for map '{map_id}': "
            f"({datum_latitude}, {datum_longitude}, bearing={datum_bearing_deg}°)"
        )
        return {
            "success": True,
            "map_id": map_id,
            "datum_latitude": datum_latitude,
            "datum_longitude": datum_longitude,
            "datum_bearing_deg": datum_bearing_deg,
        }

    async def update_node(
        self,
        map_id: str,
        node_id: str,
        x: Optional[float] = None,
        y: Optional[float] = None,
        theta: Optional[float] = None,
        yaw: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Update a node in the graph.

        Args:
            map_id: Map ID
            node_id: Node ID
            x: New x coordinate (optional)
            y: New y coordinate (optional)
            theta: New orientation (optional)
            yaw: New orientation (optional)
            metadata: New metadata (optional)

        Returns:
            Dictionary with success status
        """
        self.logger.info(f"Updating node {node_id} in map {map_id}")

        try:
            result = self.graph_db.update_node(
                map_id=map_id,
                node_id=node_id,
                x=x,
                y=y,
                theta=theta,
                yaw=yaw,
                metadata=metadata
            )

            # Notify graph builder about the node update
            if result:
                self._notify_graph_builder(map_id, "node_updated")

            return {"success": bool(result)}
        except Exception as e:
            self.logger.error(f"Failed to update node {node_id}: {e}")
            return {
                "success": False,
                "error": str(e)
            }

    async def delete_map(self, map_id: str) -> Dict[str, Any]:
        """Delete a map from ArangoDB, MinIO, and Postgres."""
        import uuid as _uuid

        self.logger.info(f"Deleting map: {map_id}")
        graph_result = self.graph_db.delete_map(map_id)
        image_result = self.image_db.delete_map(map_id)
        graph_ok = graph_result.get("success", False) if isinstance(graph_result, dict) else bool(graph_result)
        image_ok = image_result.get("success", False) if isinstance(image_result, dict) else bool(image_result)

        # Remove Postgres record regardless of ArangoDB/MinIO outcome
        try:
            await self.database.set_lifecycle(
                MapObjectV1, map_id, ObjectLifecycleV1.DELETED, _uuid.uuid4()
            )
        except Exception as e:
            self.logger.warning(f"Could not delete Postgres map record for '{map_id}': {e}")

        if graph_ok and image_ok:
            return {"success": True, "map_id": map_id, "message": f"Map {map_id} deleted successfully"}
        errors = []
        if not graph_ok:
            errors.append(f"graph_db: {graph_result.get('error', 'unknown') if isinstance(graph_result, dict) else 'failed'}")
        if not image_ok:
            errors.append(f"image_db: {image_result.get('error', 'unknown') if isinstance(image_result, dict) else 'failed'}")
        return {"success": False, "map_id": map_id, "error": "; ".join(errors)}

    def _notify_graph_builder(self, map_id: str, event_type: str = "map_updated") -> None:
        """
        Notify graph builder service about map events via WebSocket.

        Args:
            map_id: Map ID
            event_type: Type of event (e.g., "map_loaded", "map_updated")
        """
        try:
            # Broadcast to all connected graph builder WebSocket clients
            message = {
                "map_id": map_id,
                "event_type": event_type,
                "timestamp": str(datetime.now())
            }

            # Note: In a real async context, this would be awaited
            # For now, we just log the notification
            self.logger.debug(f"Notified graph builder: {event_type} for map {map_id}")

        except Exception as e:
            self.logger.warning(f"Failed to notify graph builder: {e}")
    
    # ==================== Image Operations ====================

    async def load_image(
        self,
        map_id: str,
        node_id: str,
        image_data: bytes,
        image_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Load (save) an image to the image database.

        Args:
            map_id: Map ID
            node_id: Node ID
            image_data: Image data as bytes
            image_id: Optional image ID (auto-generated if not provided)

        Returns:
            Dictionary with success status and image_id
        """
        if map_id is None:
            map_id = self.default_map_id

        self.logger.info(f"Loading image for node {node_id} in map {map_id}")

        import uuid as _uuid
        if image_id is None:
            image_id = str(_uuid.uuid4())
        success = self.image_db.store_image(
            image_data=image_data,
            image_id=image_id,
            node_id=node_id,
            map_id=map_id,
        )
        return {"success": success, "image_id": image_id}

    async def list_node_images(
        self,
        map_id: str,
        node_id: str
    ) -> List[str]:
        """
        List all images for a specific node.

        Args:
            map_id: Map ID
            node_id: Node ID

        Returns:
            List of image IDs
        """
        if map_id is None:
            map_id = self.default_map_id

        self.logger.info(f"Listing images for node {node_id} in map {map_id}")

        try:
            result = self.image_db.list_node_images(
                map_id=map_id,
                node_id=node_id
            )
            self.logger.info(f"Found {len(result)} images for node {node_id}")
            return result
        except Exception as e:
            self.logger.error(f"Error listing images for node {node_id} in map {map_id}: {e}", exc_info=True)
            return []

    async def get_image_metadata(
        self,
        map_id: str,
        node_id: str,
        image_id: str
    ) -> Optional[Dict[str, Any]]:
        """
        Get metadata for a specific image.

        Args:
            map_id: Map ID
            node_id: Node ID
            image_id: Image ID

        Returns:
            Dictionary with image metadata, or None if not found
        """
        if map_id is None:
            map_id = self.default_map_id

        self.logger.info(f"Getting metadata for image {image_id} of node {node_id} in map {map_id}")

        try:
            result = self.image_db.get_image_metadata(
                map_id=map_id,
                node_id=node_id,
                image_id=image_id
            )
            return result
        except Exception as e:
            self.logger.error(f"Error getting metadata for image {image_id}: {e}", exc_info=True)
            return None

    async def get_image(
        self,
        map_id: str,
        node_id: str,
        image_id: Optional[str] = None
    ) -> Optional[bytes]:
        """
        Retrieve an image from the image database.

        Args:
            map_id: Map ID
            node_id: Node ID
            image_id: Image ID (optional - gets first image if not provided)

        Returns:
            Image data as bytes, or None if not found
        """
        if map_id is None:
            map_id = self.default_map_id

        self.logger.info(f"Retrieving image for node {node_id} in map {map_id}, image_id={image_id}")

        try:
            result = self.image_db.get_image(
                map_id=map_id,
                node_id=node_id,
                image_id=image_id
            )
            if result:
                self.logger.info(f"Successfully retrieved image for node {node_id}, size={len(result)} bytes")
            else:
                self.logger.info(f"No image found for node {node_id}")
            return result
        except Exception as e:
            self.logger.error(f"Error retrieving image for node {node_id} in map {map_id}: {e}", exc_info=True)
            return None

    # ==================== ROS Bag Operations ====================

    async def create_bag_upload_url(
        self,
        map_id: str,
        robot_name: str,
        description: Optional[str] = None,
        recorded_at: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Generate a presigned PUT URL for direct robot upload.

        Returns dict with bag_id, upload_url, expires_in, map_id, robot_name
        or None on error.
        """
        import uuid as _uuid
        bag_id = str(_uuid.uuid4())

        metadata: Dict[str, str] = {}
        if description:
            metadata["description"] = description
        if recorded_at:
            metadata["recorded_at"] = recorded_at

        result = self.rosbag_db.create_upload_url(
            map_id=map_id,
            robot_name=robot_name,
            bag_id=bag_id,
            metadata=metadata or None,
        )
        if result is None:
            return None

        return {
            "bag_id": bag_id,
            "upload_url": result["upload_url"],
            "expires_in": result["expires_in"],
            "map_id": map_id,
            "robot_name": robot_name,
        }

    async def get_bag_metadata(
        self,
        map_id: str,
        robot_name: str,
        bag_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Get metadata and a presigned download URL for a bag."""
        try:
            meta = self.rosbag_db.get_bag_metadata(
                map_id=map_id,
                robot_name=robot_name,
                bag_id=bag_id,
            )
            if meta is None:
                return None

            download_url = self.rosbag_db.get_download_url(
                map_id=map_id,
                robot_name=robot_name,
                bag_id=bag_id,
            )
            meta["download_url"] = download_url
            return meta
        except Exception as e:
            self.logger.error(f"Error getting metadata for bag {bag_id}: {e}", exc_info=True)
            return None

    async def list_bags(
        self,
        map_id: str,
        robot_name: Optional[str] = None,
    ) -> List[Dict[str, str]]:
        """List bags for a map, optionally filtered by robot."""
        try:
            return self.rosbag_db.list_bags(map_id=map_id, robot_name=robot_name)
        except Exception as e:
            self.logger.error(f"Error listing bags in map {map_id}: {e}", exc_info=True)
            return []

    async def delete_bag(
        self,
        map_id: str,
        robot_name: str,
        bag_id: str,
    ) -> Dict[str, Any]:
        """Delete a single bag."""
        try:
            success = self.rosbag_db.delete_bag(
                map_id=map_id,
                robot_name=robot_name,
                bag_id=bag_id,
            )
            return {"success": success}
        except Exception as e:
            self.logger.error(f"Error deleting bag {bag_id}: {e}", exc_info=True)
            return {"success": False}

    async def delete_robot_bags(
        self,
        map_id: str,
        robot_name: str,
    ) -> Dict[str, Any]:
        """Delete all bags for a robot+map combination."""
        try:
            bags_before = self.rosbag_db.list_bags(map_id=map_id, robot_name=robot_name)
            count = len(bags_before)
            success = self.rosbag_db.delete_robot_bags(map_id=map_id, robot_name=robot_name)
            return {"success": success, "count_deleted": count if success else 0}
        except Exception as e:
            self.logger.error(
                f"Error deleting bags for robot {robot_name} in map {map_id}: {e}",
                exc_info=True,
            )
            return {"success": False, "count_deleted": 0}

    # ==================== Base Model Operations ====================

    async def create_model_upload_url(
        self,
        name: str,
        description: Optional[str] = None,
        format: Optional[str] = "onnx",
    ) -> Optional[Dict[str, Any]]:
        """
        Register a new base model and return a presigned PUT URL for binary upload.

        Returns dict with model_id, upload_url, expires_in, name, format — or None on error.
        """
        import uuid as _uuid
        model_id = str(_uuid.uuid4())
        result = self.model_db.create_model_entry(
            model_id=model_id,
            name=name,
            description=description,
            format=format,
        )
        if result is None:
            return None
        result["name"] = name
        result["format"] = format
        return result

    async def get_model_metadata(self, model_id: str) -> Optional[Dict[str, Any]]:
        """Return metadata and a presigned download URL for a base model."""
        try:
            meta = self.model_db.get_model_metadata(model_id)
            if meta is None:
                return None
            meta["download_url"] = self.model_db.get_download_url(model_id)
            return meta
        except Exception as e:
            self.logger.error(f"Error getting metadata for model {model_id}: {e}", exc_info=True)
            return None

    async def list_models(self) -> List[Dict[str, Any]]:
        """List all base models with metadata and presigned download URLs."""
        try:
            models = self.model_db.list_models()
            for m in models:
                if m.get("uploaded"):
                    m["download_url"] = self.model_db.get_download_url(m["model_id"])
                else:
                    m["download_url"] = None
            return models
        except Exception as e:
            self.logger.error(f"Error listing models: {e}", exc_info=True)
            return []

    async def delete_model(self, model_id: str) -> Dict[str, Any]:
        """Delete a base model (binary + metadata sidecar)."""
        try:
            success = self.model_db.delete_model(model_id)
            return {"success": success, "model_id": model_id}
        except Exception as e:
            self.logger.error(f"Error deleting model {model_id}: {e}", exc_info=True)
            return {"success": False, "model_id": model_id}

    # ==================== Navigation Operations ====================

    async def _call_mission_planner(
        self,
        robot_id: str,
        goal_x: float,
        goal_y: float,
        map_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Internal method to call mission planner service.

        Args:
            robot_id: Robot ID
            goal_x: Goal x coordinate
            goal_y: Goal y coordinate
            map_id: Map ID

        Returns:
            Mission planner response
        """
        try:
            # Call mission planner via client
            result = await self.mission_planner.navigate(
                robot_name=robot_id,
                target_x=goal_x,
                target_y=goal_y
            )
            return result
        except Exception as e:
            self.logger.error(f"Mission planner call failed: {e}")
            return {
                "success": False,
                "error": str(e)
            }

    async def plan_mission(
        self,
        robot_id: str,
        goal_x: float,
        goal_y: float,
        map_id: Optional[str] = None,
        mission_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Plan a mission for a robot.

        Args:
            robot_id: Robot ID
            goal_x: Goal x coordinate
            goal_y: Goal y coordinate
            map_id: Map ID
            mission_name: Optional mission name

        Returns:
            Mission planning result
        """
        self.logger.info(f"Planning mission for robot '{robot_id}' to ({goal_x}, {goal_y})")

        return await self._call_mission_planner(robot_id, goal_x, goal_y, map_id)

    async def navigate(
        self,
        robot_name: str,
        target_x: Optional[float] = None,
        target_y: Optional[float] = None,
        target_lat: Optional[float] = None,
        target_lon: Optional[float] = None,
        map_id: Optional[str] = None,
        mission_name: Optional[str] = None,
        timeout_seconds: int = 300,
    ) -> Dict[str, Any]:
        """Request navigation (proxy to mission planner).

        Accepts either local Cartesian (target_x, target_y) or GPS
        (target_lat, target_lon) coordinates. GPS requires a datum on the map.
        """
        has_gps = target_lat is not None and target_lon is not None
        coord_desc = (
            f"GPS ({target_lat}, {target_lon})" if has_gps
            else f"({target_x}, {target_y})"
        )
        self.logger.info(
            f"Navigation request for robot '{robot_name}' to {coord_desc} "
            f"on map '{map_id or 'default'}'"
        )

        try:
            await self.database.get_object(RobotObjectV1, robot_name)
        except Exception as e:
            self.logger.error(f"Robot '{robot_name}' not found: {e}")
            raise HTTPException(status_code=404, detail=f"Robot '{robot_name}' not found")

        try:
            result = await self.mission_planner.navigate(
                robot_name=robot_name,
                target_x=target_x,
                target_y=target_y,
                target_lat=target_lat,
                target_lon=target_lon,
                map_id=map_id,
                mission_name=mission_name,
                timeout_seconds=timeout_seconds,
            )
            return result
        except Exception as e:
            self.logger.error(f"Navigation request failed: {e}")
            return {"success": False, "error": str(e)}

    async def direct_waypoints(
        self,
        robot_name: str,
        waypoints: list,
        mission_name=None,
        timeout_seconds: int = 300,
    ) -> dict:
        """
        Mapless mode: build and submit a waypoint mission directly to the database.

        No graph lookup or planner service hop. The robot follows the waypoints in order.
        """
        from cloud_common.objects.mission import (
            MissionNodeV1, MissionRouteNodeV1, MissionStateV1, MissionMode,
        )
        from cloud_common.objects.common import Pose2D
        from datetime import timedelta
        import uuid

        self.logger.info(
            f"Mapless waypoint mission for robot '{robot_name}' "
            f"with {len(waypoints)} waypoints"
        )

        try:
            await self.database.get_object(RobotObjectV1, robot_name)
        except Exception as e:
            self.logger.error(f"Robot '{robot_name}' not found: {e}")
            raise HTTPException(status_code=404, detail=f"Robot '{robot_name}' not found")

        if not mission_name:
            ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            mission_name = f"nav_{robot_name}_{ts}"

        try:
            parsed = [Pose2D(**wp) for wp in waypoints]
            mission_tree = [
                MissionNodeV1(
                    name="navigate_to_target",
                    parent="root",
                    route=MissionRouteNodeV1(waypoints=parsed),
                )
            ]
            node_names = ["root", "navigate_to_target"]
            mission = MissionObjectV1(
                name=mission_name,
                lifecycle=ObjectLifecycleV1.ALIVE,
                robot=robot_name,
                mission_tree=mission_tree,
                timeout=timedelta(seconds=timeout_seconds),
                needs_canceled=False,
                mode=MissionMode.MAPLESS,
                status={
                    "state": MissionStateV1.PENDING,
                    "current_node": 0,
                    "node_status": {n: {"state": MissionStateV1.PENDING, "error_msg": None}
                                    for n in node_names},
                    "task_status": {},
                    "start_timestamp": None,
                    "end_timestamp": None,
                    "failure_reason": None,
                    "failure_category": None,
                },
            )
            await self.database.create_object(mission, self._publisher_id)
            self.logger.info(f"Mapless mission '{mission_name}' submitted for robot '{robot_name}'")
            return {
                "success": True,
                "robot_name": robot_name,
                "mission_name": mission_name,
                "waypoints_count": len(parsed),
                "message": f"Mission '{mission_name}' submitted successfully (mapless)",
            }
        except Exception as e:
            self.logger.error(f"Mapless waypoint mission failed: {e}")
            return {"success": False, "error": str(e)}

    async def get_mission_plan(self, mission_name: str, map_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Get mission plan from mission planner service.

        Args:
            mission_name: Name of the mission
            map_id: Map ID to use for node lookup (uses default if not provided)

        Returns:
            Mission plan with path node IDs
        """
        self.logger.info(f"Getting mission plan for '{mission_name}' with map_id '{map_id or 'default'}'")

        try:
            result = await self.mission_planner.get_mission_plan(mission_name, map_id=map_id)
            return result
        except Exception as e:
            self.logger.error(f"Failed to get mission plan: {e}")
            raise HTTPException(status_code=404, detail=f"Failed to get mission plan: {str(e)}")

    async def invoke_custom_action(
        self,
        robot_name: str,
        action_type: str,
        action_parameters: Dict[str, str],
        blocking_type: str = "HARD"
    ) -> Dict[str, Any]:
        """
        Invoke a custom VDA5050 action on a robot.

        Args:
            robot_name: Name of the robot
            action_type: Type of action to invoke
            action_parameters: Action parameters as key-value pairs
            blocking_type: Blocking type (NONE, SOFT, or HARD)

        Returns:
            Dictionary with action invocation result
        """
        from cloud_common.objects.mission import MissionActionNodeV1, MissionNodeV1, MissionStateV1
        from datetime import timedelta
        import uuid

        self.logger.info(f"Invoking custom action '{action_type}' on robot '{robot_name}'")

        try:
            # Validate robot exists
            try:
                robot = await self.database.get_object(RobotObjectV1, robot_name)
            except Exception as e:
                self.logger.error(f"Robot '{robot_name}' not found: {e}")
                raise HTTPException(status_code=404, detail=f"Robot '{robot_name}' not found")

            # Validate action is available on this robot
            available_actions = robot.status.factsheet.custom_actions
            action_found = any(action.action_type == action_type for action in available_actions)

            if not action_found and available_actions:
                self.logger.warning(
                    f"Action '{action_type}' not found in robot's available actions. "
                    f"Available: {[a.action_type for a in available_actions]}"
                )

            # Generate unique action ID
            action_id = f"custom_action_{action_type}_{uuid.uuid4().hex[:8]}"
            mission_name = f"action_{robot_name}_{action_type}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

            # Create action node
            custom_action = MissionActionNodeV1(
                action_type=action_type,
                action_parameters=action_parameters
            )

            # Create mission tree with custom action
            mission_tree = [
                MissionNodeV1(
                    name=f"custom_action_{action_type}",
                    parent="root",
                    action=custom_action
                )
            ]

            # Create mission object
            mission = MissionObjectV1(
                name=mission_name,
                lifecycle=ObjectLifecycleV1.ALIVE,
                robot=robot_name,
                mission_tree=mission_tree,
                timeout=timedelta(seconds=60),  # Default 60 second timeout for actions
                needs_canceled=False,
                status={
                    "state": MissionStateV1.PENDING,
                    "current_node": 0,
                    "node_status": {
                        "root": {"state": MissionStateV1.PENDING, "error_msg": None},
                        f"custom_action_{action_type}": {"state": MissionStateV1.PENDING, "error_msg": None}
                    },
                    "task_status": {},
                    "start_timestamp": None,
                    "end_timestamp": None,
                    "failure_reason": None,
                    "failure_category": None
                }
            )

            # Submit mission to database (mission dispatcher will pick it up)
            await self.database.create_object(mission, self._publisher_id)

            self.logger.info(
                f"Custom action mission '{mission_name}' created for robot '{robot_name}' "
                f"with action '{action_type}'"
            )

            return {
                "success": True,
                "action_id": action_id,
                "robot_name": robot_name,
                "action_type": action_type
            }

        except HTTPException:
            raise
        except Exception as e:
            self.logger.error(f"Custom action invocation failed: {e}")
            return {
                "success": False,
                "error": str(e)
            }

    # ==================== LiveKit Operations ====================

    async def create_livekit_token(
        self,
        participant_name: str,
        room_name: str = "quickstart-room",
        ttl: Optional[int] = None,
        metadata: Optional[str] = None,
        can_publish: bool = True,
        can_subscribe: bool = True,
        can_publish_data: bool = True
    ) -> Dict[str, Any]:
        """
        Create a LiveKit access token (proxy to LiveKit service).

        Args:
            participant_name: Unique identifier for the participant
            room_name: Name of the room to join
            ttl: Token time-to-live in seconds (uses service default if not provided)
            metadata: Optional metadata for the participant
            can_publish: Whether participant can publish tracks
            can_subscribe: Whether participant can subscribe to tracks
            can_publish_data: Whether participant can publish data messages

        Returns:
            Dictionary with token details:
                - token: JWT access token
                - ttl: Token time-to-live in seconds
                - server_url: LiveKit server WebSocket URL
                - participant_name: Participant identifier
                - room_name: Room name
        """
        self.logger.info(f"Creating LiveKit token for participant '{participant_name}' in room '{room_name}'")

        try:
            result = await self.livekit.create_token(
                participant_name=participant_name,
                room_name=room_name,
                ttl=ttl,
                metadata=metadata,
                can_publish=can_publish,
                can_subscribe=can_subscribe,
                can_publish_data=can_publish_data
            )
            return result
        except Exception as e:
            self.logger.error(f"LiveKit token creation failed: {e}")
            raise HTTPException(
                status_code=500,
                detail=f"LiveKit token creation failed: {str(e)}"
            )

    # ==================== Database Watchers for WebSocket Broadcasting ====================

    def start_watchers(self, event_loop: asyncio.AbstractEventLoop):
        """
        Start background threads that watch the database for robot and mission updates.
        These watchers broadcast updates to WebSocket clients.

        Args:
            event_loop: The asyncio event loop to use for async operations
        """
        if self._running:
            self.logger.warning("Watchers already running")
            return

        self._running = True
        self._event_loop = event_loop

        # Create queues for robot and mission updates
        self._robot_changes = asyncio.Queue()
        self._mission_changes = asyncio.Queue()

        # Create queue for MQTT mission progress updates
        self._mission_progress_queue = asyncio.Queue(maxsize=100)

        # Start async tasks to watch database changes and handle updates
        self._watcher_tasks = [
            event_loop.create_task(self._watch_robot_changes()),
            event_loop.create_task(self._watch_mission_changes()),
            event_loop.create_task(self._handle_robot_updates()),
            event_loop.create_task(self._handle_mission_updates()),
            event_loop.create_task(self._handle_mission_progress_updates())
        ]

        self.logger.info("✅ Database watchers started for WebSocket broadcasting")

    def stop_watchers(self):
        """Stop all background watchers."""
        self._running = False

        # Cancel async tasks
        for task in self._watcher_tasks:
            task.cancel()

        # Disconnect MQTT client
        if self.mqtt_enabled and self.mqtt_client:
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()
            self.logger.info("[MQTT] Disconnected from broker")

        self.logger.info("Database watchers stopped")

    async def _watch_robot_changes(self):
        """
        Async task that watches for robot object changes in the database.
        """
        reconnect_delay = 5  # seconds

        while self._running:
            try:
                self.logger.info("Starting robot database watcher...")
                watcher = await self.database.get_watcher(RobotObjectV1, self._publisher_id)
                async for robot in watcher.watch():
                    if not self._running:
                        break

                    # Enqueue the robot update to be processed by async handler
                    await self._robot_changes.put(robot)

            except Exception as e:
                if self._running:
                    self.logger.error(f"Robot watcher error: {e}, reconnecting in {reconnect_delay}s...")
                    await asyncio.sleep(reconnect_delay)

    async def _watch_mission_changes(self):
        """
        Async task that watches for mission object changes in the database.
        """
        reconnect_delay = 5  # seconds

        while self._running:
            try:
                self.logger.info("Starting mission database watcher...")
                watcher = await self.database.get_watcher(MissionObjectV1, self._publisher_id)
                async for mission in watcher.watch():
                    if not self._running:
                        break

                    # Enqueue the mission update to be processed by async handler
                    await self._mission_changes.put(mission)

            except Exception as e:
                if self._running:
                    self.logger.error(f"Mission watcher error: {e}, reconnecting in {reconnect_delay}s...")
                    await asyncio.sleep(reconnect_delay)

    async def _handle_robot_updates(self):
        """
        Async task that processes robot updates from the queue and broadcasts them
        to WebSocket clients subscribed to that robot.
        """
        while self._running:
            try:
                robot = await self._robot_changes.get()

                # Ignore deleted robots
                if robot.lifecycle == ObjectLifecycleV1.DELETED:
                    continue

                # Build WebSocket message
                message = {
                    "type": "robot_update",
                    "robot_name": robot.name,
                    "timestamp": datetime.now().isoformat(),
                    "status": {
                        "online": robot.status.online if hasattr(robot.status, 'online') else False,
                        "battery_level": robot.status.battery_level if hasattr(robot.status, 'battery_level') else 0,
                        "state": robot.status.state.value if hasattr(robot.status, 'state') else "IDLE",
                        "pose": {
                            "x": robot.status.pose.x if hasattr(robot.status, 'pose') else 0,
                            "y": robot.status.pose.y if hasattr(robot.status, 'pose') else 0,
                            "theta": robot.status.pose.theta if hasattr(robot.status, 'pose') else 0
                        } if hasattr(robot.status, 'pose') else None,
                        "recording_state": robot.status.recording_state if hasattr(robot.status, 'recording_state') else None,
                    }
                }

                # Broadcast to all WebSocket clients subscribed to this robot
                await self.ws_manager.broadcast("robot_status", robot.name, message)

                self.logger.debug(f"Broadcasted robot update for {robot.name}: online={message['status']['online']}, battery={message['status']['battery_level']}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Error handling robot update: {e}")

    async def _handle_mission_updates(self):
        """
        Async task that processes mission updates from the queue and broadcasts them
        to WebSocket clients subscribed to that mission.
        """
        while self._running:
            try:
                mission = await self._mission_changes.get()

                # Ignore deleted missions
                if mission.lifecycle == ObjectLifecycleV1.DELETED:
                    continue

                # Build WebSocket message
                message = {
                    "type": "mission_update",
                    "mission_name": mission.name,
                    "robot_name": mission.robot,
                    "timestamp": datetime.now().isoformat(),
                    "status": {
                        "state": mission.status.state.value if hasattr(mission.status, 'state') else "PENDING",
                        "current_node": mission.status.current_node if hasattr(mission.status, 'current_node') else 0,
                        "failure_reason": mission.status.failure_reason if hasattr(mission.status, 'failure_reason') else None
                    }
                }

                # Broadcast to all WebSocket clients subscribed to this mission
                await self.ws_manager.broadcast("mission_status", mission.name, message)

                self.logger.debug(f"Broadcasted mission update for {mission.name}: state={message['status']['state']}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Error handling mission update: {e}")

    async def _handle_mission_progress_updates(self):
        """
        Async task that processes MQTT mission progress updates from the queue and broadcasts them
        to WebSocket clients subscribed to that mission.
        """
        while self._running:
            try:
                progress = await self._mission_progress_queue.get()

                mission_id = progress.get('mission_id')
                robot_name = progress.get('robot_name')
                next_waypoint_index = progress.get('next_waypoint_index')
                current_waypoint_index = progress.get('current_waypoint_index')
                timestamp = progress.get('timestamp')
                status = progress.get('status')

                # Build WebSocket message for waypoint progress
                message = {
                    "type": "mission_progress",
                    "mission_name": mission_id,
                    "robot_name": robot_name,
                    "timestamp": timestamp or datetime.now().isoformat(),
                    "progress": {
                        "next_waypoint_index": next_waypoint_index,
                        "current_waypoint_index": current_waypoint_index,
                        "status": status
                    }
                }

                # Broadcast to all WebSocket clients subscribed to this mission
                await self.ws_manager.broadcast("mission_status", mission_id, message)

                self.logger.debug(
                    f"Broadcasted mission progress for {mission_id}: "
                    f"current_waypoint={current_waypoint_index}, next_waypoint={next_waypoint_index}"
                )

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Error handling mission progress update: {e}")

