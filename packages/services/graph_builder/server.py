#!/usr/bin/env python3
"""
Graph Builder Service

Processes new node updates from MQTT and builds the topological graph.

Maps redesign M2 (docs/satinav-maps-redesign.md §6): nodes and images go to the robot's open
mapping session (ingest.py), never to the old `robot.current_map` (removed in U6) or a
`"default"` map. Data without a session is dropped, counted, and reported as MAP.INGEST_REJECTED (rate-limited). Node poses
are converted into the map frame with the session's map_T_session; the robot-frame pose is kept
as `robot_pose`, and the node carries `session_id`.

A RUNNING mission with `register_map = False` still suppresses ingest (unchanged; the node only
goes to the mission's waypoint log).

3D reconstruction R2 (docs/reconstruction/design.md §5): `robot/depth_upload` carries one
u16-mm depth PNG and its camera parameters per node and camera. It follows the image path
(resolved by session, buffered until its node exists); the PNG goes to MinIO at
`{node}/depth/{camera}.png` and the parameters onto the ArangoDB node as `depth.{camera}`.
`robot/costmap_upload` (one occupancy PNG per node and layer) follows the same path.

Uploads that arrive before their node are buffered (upload_buffer.NodeUploadBuffer). The node is
processed in a worker thread; one lock (`_upload_lock`) makes the handlers' "node there? else
buffer" and the worker's "node there now, take the buffer" atomic, so no upload is lost or
stranded between them. Images need only the node's global id (they go to MinIO before the node
is written); depth and costmap records go onto the ArangoDB node and so wait until `add_node`
succeeded (`_stored_nodes`). The buffers are bounded (config.UPLOAD_BUFFER_MAX_*): an upload
that does not fit is dropped and reported as `buffer_full`.

Resent and buffered robot uploads (phase 1): the uploads are subscribed at QoS 1 (and, opt-in
with GRAPH_BUILDER_MQTT_PERSISTENT_SESSION, over a persistent MQTT session), so a robot may send
them again and the broker may redeliver. The global node id is deterministic,
uuid5(robot, the open cloud session's id, session_node_id[, run]), so the same robot node always
has the same id:
- A node that is stored already (in memory, in the node-id LRU, or found in ArangoDB) is a
  duplicate: first write wins (a resend with other pose / metadata does not update the node),
  no add_node, no edges, no node_count, no WebSocket publish (`_process_topology` returns None,
  stats `nodes_duplicate`). Buffered parts for it are still attached. A node stored but not
  finished (`ingest_complete: false`: edges or count failed) is repaired by its resend; a
  resend that meets the attempt storing the same node waits for it (bounded). The node may
  have been stored with or without `run_id`: `_candidate_ids`.
- An image, depth or costmap upload finds its node by that id in ArangoDB when the memory
  lacks it (a graph-builder restart), and is stored at once; it waits in the buffer only for a
  node that does not exist yet.
- An optional `run_id` in node_update / image / depth / costmap payloads identifies the robot's
  run. A change of the robot's run_id is a session reset (mappings and buffers of the old run are
  dropped, ids of the new run differ because the run is part of the id); a smaller
  session_node_id with the same run_id is only a resend. Without run_id the old heuristic (a
  smaller id than seen before) is the fallback: it cannot tell a resend from a restart, so such
  robots get the old reset behaviour (and, after a graph-builder restart, a restarted counter
  would look like duplicates): robots should send run_id. Once a robot is known to send it
  (also looked up in ArangoDB after a restart) a message without it never triggers that reset.
"""

import logging
import json
import base64
import asyncio
import dataclasses
import functools
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import uuid
from typing import Callable, Dict, Any, Optional, List, Union, Set, Tuple
from datetime import datetime, timezone as _dt_timezone
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
    MINIO_HOST, MINIO_PORT, MINIO_SECURE,
    UPLOAD_BUFFER_MAX_BYTES, UPLOAD_BUFFER_MAX_ENTRIES, NODE_EXISTS_CACHE_SIZE,
    NODE_CLAIM_WAIT_THREADS,
    GRAPH_BUILDER_MQTT_PERSISTENT_SESSION, GRAPH_BUILDER_MQTT_CLIENT_ID,
)
from packages.events.emit import Event, emit
from packages.services.graph_builder import ingest
from packages.utils import capture_time
from packages.services.graph_builder.upload_buffer import DROPPED, NodeIdCache, NodeUploadBuffer

# Namespace of the deterministic global node ids (uuid5): never change it, the ids of stored
# nodes derive from it.
NODE_ID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "nodes.graph-builder.satinavrobotics.com")
# The QoS the upload topics are subscribed at.
UPLOAD_QOS = 1
# How long a resend of a node waits for the attempt that is storing the same node (seconds).
NODE_CLAIM_WAIT_S = 15.0
# After a failed last-run lookup (an unindexed scan) the next one waits this long (seconds).
RUN_PROBE_BACKOFF_S = 30.0


def _run_id_of(payload: Dict[str, Any]) -> Optional[str]:
    """The payload's optional `run_id` (one robot process run / topomap run) as a string, or
    None (absent or empty: an older robot)."""
    run_id = payload.get('run_id')
    return None if run_id is None or run_id == "" else str(run_id)


@dataclasses.dataclass(frozen=True)
class _LayerUpload:
    """What differs between the depth and costmap uploads (one code path for both:
    `_handle_layer_upload`, `_save_layers`). Names derive from `kind`: `{kind}_buffer`,
    `image_db.store_{kind}`, `graph_db.set_node_{kind}` / `set_node_{kind}s`, stats
    `{kind}_saved`, the stamp `{kind}_stamp_ms`."""
    kind: str        # 'depth' | 'costmap'
    sub_key: str     # the entry's sub-key field: camera | layer
    sub_field: str   # the payload field naming it: camera_name | layer
    data_key: str    # the payload's base64 PNG
    check: Callable[[Dict[str, Any]], Dict[str, Any]]
    record: Callable[[Dict[str, Any], Dict[str, float], str], Dict[str, Any]]
    error: type


DEPTH_UPLOAD = _LayerUpload("depth", "camera", "camera_name", "depth_data",
                            ingest.check_depth_payload, ingest.depth_record_from,
                            ingest.DepthPayloadError)
COSTMAP_UPLOAD = _LayerUpload("costmap", "layer", "layer", ingest.COSTMAP_DATA_KEY,
                              ingest.check_costmap_payload, ingest.costmap_record_from,
                              ingest.CostmapPayloadError)


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
        image_buffer_timeout: float = 30.0,
        session_cache_ttl: float = ingest.SESSION_CACHE_TTL_S,
        reject_event_interval: float = ingest.REJECT_EVENT_INTERVAL_S,
        mqtt_depth_topic: str = "robot/depth_upload",
        mqtt_costmap_topic: str = "robot/costmap_upload",
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
            image_buffer_timeout: Timeout in seconds for buffering images
            session_cache_ttl: How long a robot's open-session lookup is reused (s)
            reject_event_interval: Minimum time between MAP.INGEST_REJECTED events per robot
                and reason (s)
        """
        self.logger = logging.getLogger("GraphBuilderService")

        # Configuration
        self.mqtt_host = mqtt_host
        self.mqtt_port = mqtt_port
        self.mqtt_topic = mqtt_topic
        self.mqtt_image_topic = mqtt_image_topic
        self.mqtt_depth_topic = mqtt_depth_topic
        self.mqtt_costmap_topic = mqtt_costmap_topic
        self.radius_threshold = radius_threshold
        self.distance_threshold = distance_threshold
        self.image_buffer_timeout = image_buffer_timeout

        # Initialize PostgreSQL connection. map_sessions and fleet_events come from the API's
        # migrations: startup waits for them.
        self.database = PostgresDatabase(
            dbname=postgres_db,
            user=postgres_user,
            password=postgres_password,
            host=postgres_host,
            port=postgres_port,
            required_tables=("map_sessions", "fleet_events"),
        )
        # Where a robot's data goes (its open mapping session), and the drop reports.
        self.sessions = ingest.SessionResolver(self._fetch_open_session, ttl=session_cache_ttl,
                                               realign=self._realign_session)
        self.rejects = ingest.RejectLimiter(interval=reject_event_interval)

        # Initialize service clients
        from packages.config import require_secret
        self.topomap_db = TopomapDatabaseClient(
            arango_host=arango_host,
            arango_port=arango_port,
            arango_username=arango_username,
            arango_password=arango_password or require_secret("ARANGO_PASSWORD"),
            arango_database=arango_database,
            minio_host=minio_host,
            minio_port=minio_port,
            minio_access_key=minio_access_key,
            minio_secret_key=minio_secret_key,
            minio_secure=minio_secure,
        )
        self.graph_db = self.topomap_db.graph
        self.image_db = self.topomap_db.image

        # MQTT client (its live `connected` state is the health's mqtt_connected)
        self.mqtt_client: Optional[MQTTClient] = None

        # Robot node counter to global node ID (set before the node is written: enough for
        # images, which go to MinIO only)
        # Key: (robot_name, session_node_id) -> (global_id, timestamp, map_name, session_id)
        self.session_to_global_map: Dict[Tuple[str, int], Tuple[str, datetime, str, str]] = {}
        # Nodes whose ArangoDB document exists (add_node succeeded): depth and costmap records
        # can be set on them. (robot_name, session_node_id) ->
        # (global_id, map_name, session_id, the map_T_session the node was stored with)
        self._stored_nodes: Dict[Tuple[str, Any],
                                 Tuple[str, str, Optional[str], Dict[str, float]]] = {}

        # Nodes known to exist in ArangoDB, (map, global id) (LRU): a resent node is a no-op, an
        # upload of a node stored before a restart is stored directly. Positives only.
        self._node_cache = NodeIdCache(NODE_EXISTS_CACHE_SIZE)
        # Nodes being stored right now, (map, global id): a concurrent resend is a duplicate.
        self._processing: Set[Tuple[str, str]] = set()
        # robot -> the run_id of its last node_update (reset detection). Under _upload_lock.
        self._robot_runs: Dict[str, str] = {}
        # robot -> a token set when the fallback heuristic (no run_id, a smaller session_node_id)
        # saw a reset: it goes into the robot's node ids so the new run's nodes do not collide
        # with the old run's. In memory only: robots should send run_id.
        self._legacy_epochs: Dict[str, str] = {}
        # Nodes whose document is written but whose edges / session count are not done yet
        # (`ingest_complete: false` in ArangoDB), (map, global id). Under _upload_lock.
        self._incomplete: Set[Tuple[str, str]] = set()
        # Nodes known to be complete (LRU; the node-id LRU only says "exists").
        self._complete_cache = NodeIdCache(NODE_EXISTS_CACHE_SIZE)
        # claim -> event set when the claim is released (a resend waits on it). _upload_lock.
        self._claim_events: Dict[Tuple[str, str], threading.Event] = {}
        self.claim_wait_s = NODE_CLAIM_WAIT_S
        # Resends waiting for a claim: at most NODE_CLAIM_WAIT_THREADS at once, the rest are
        # dropped as duplicates (the robot resends). `_process_topology` runs on its own pool,
        # with threads to spare beyond the waiters, so a burst of waiters never starves the
        # work of the attempts they wait for (that work also uses the default pool).
        self._claim_waiters = threading.BoundedSemaphore(NODE_CLAIM_WAIT_THREADS)
        self._topology_executor = ThreadPoolExecutor(
            max_workers=NODE_CLAIM_WAIT_THREADS + 4, thread_name_prefix="gb-topology")
        # Robots whose last run_id was looked up in ArangoDB already (once per process), and
        # robot -> time.monotonic() before which a failed lookup is not retried.
        self._run_probed: Set[str] = set()
        self._run_probe_retry: Dict[str, float] = {}
        # Nodes whose WebSocket update / mission waypoint went out (LRU): a repair after a
        # failed count does not publish them a second time. In memory: after a restart a
        # repaired node is published (and logged) again.
        self._published = NodeIdCache(NODE_EXISTS_CACHE_SIZE)

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
            "robots_auto_created": 0,
            "nodes_rejected": 0,
            "nodes_duplicate": 0,
            "images_rejected": 0,
            "reject_events_written": 0,
            "reject_events_failed": 0,
            "depth_saved": 0,
            "depth_rejected": 0,
            "buffered_depth": 0,
            "costmap_saved": 0,
            "costmap_rejected": 0,
            "buffered_costmap": 0,
        }

        # Uploads that arrived before their node (module docstring). One lock for the three
        # buffers, the mapping writes and _stored_nodes.
        self._upload_lock = threading.RLock()
        # camera -> image dict
        caps = {"max_bytes": UPLOAD_BUFFER_MAX_BYTES, "max_entries": UPLOAD_BUFFER_MAX_ENTRIES}
        self.image_buffer = NodeUploadBuffer("image", self.stats, "buffered_images",
                                             lock=self._upload_lock, log=self.logger, **caps)
        # camera -> {camera, data, parsed, session_id} (3D reconstruction R2)
        self.depth_buffer = NodeUploadBuffer("depth", self.stats, "buffered_depth",
                                             lock=self._upload_lock, log=self.logger, **caps)
        # layer -> {layer, data, parsed, session_id} (robot/costmap_upload)
        self.costmap_buffer = NodeUploadBuffer("costmap", self.stats, "buffered_costmap",
                                               lock=self._upload_lock, log=self.logger, **caps)

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
        self.logger.info(f"   Depth topic: {mqtt_depth_topic}")
        self.logger.info(f"   Costmap topic: {mqtt_costmap_topic}")
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
            # A persistent session (opt-in) needs a stable client id; otherwise a fresh one
            # and a clean session, as before.
            persistent = GRAPH_BUILDER_MQTT_PERSISTENT_SESSION
            client_id = (GRAPH_BUILDER_MQTT_CLIENT_ID if persistent
                         else f"graph_builder_{datetime.now().timestamp()}")
            self.mqtt_client = MQTTClient(
                client_id=client_id,
                broker=self.mqtt_host,
                port=self.mqtt_port,
                keepalive=MQTT_KEEPALIVE,
                clean_session=not persistent,
            )

            # Register callbacks. All uploads are subscribed at QoS 1: a robot publishing at
            # QoS 1 gets the broker's redelivery while this client is connected (the granted
            # QoS is the lower of the two); the handlers are idempotent for the duplicates.
            self.mqtt_client.register_callback(self.mqtt_topic, self._on_node_update_message,
                                               qos=UPLOAD_QOS)
            self.mqtt_client.register_callback(self.mqtt_image_topic,
                                               self._on_image_upload_message, qos=UPLOAD_QOS)
            if self.mqtt_depth_topic:
                self.mqtt_client.register_callback(self.mqtt_depth_topic,
                                                   self._on_depth_upload_message, qos=UPLOAD_QOS)
            if self.mqtt_costmap_topic:
                self.mqtt_client.register_callback(self.mqtt_costmap_topic,
                                                   self._on_costmap_upload_message,
                                                   qos=UPLOAD_QOS)

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

    def close(self):
        """Stop the node-topology pool (running calls finish; waiting resends time out)."""
        self._topology_executor.shutdown(wait=False, cancel_futures=True)
            
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

    # ==================== Sessions and rejections (M2) ====================

    async def _fetch_open_session(self, robot_name: str) -> Optional[Tuple]:
        """ingest.OPEN_SESSION_SQL for one robot (SessionResolver's fetch)."""
        async with self.database.connection() as conn:
            cursor = await conn.execute(ingest.OPEN_SESSION_SQL, (robot_name,))
            return await cursor.fetchone()

    async def _realign_session(self, robot_name: str, session: "ingest.OpenSession",
                               plan: "ingest.Realign") -> bool:
        """Store the session's new datum and map_T_session (compare-and-set on the old datum)
        and record MAP.SESSION_REALIGNED in the same transaction. True: this call won."""
        async with self.database.connection() as conn:
            cursor = await conn.execute(ingest.REALIGN_SQL,
                                        ingest.realign_params(session, plan))
            if cursor.rowcount != 1:
                return False
            await emit(conn, ingest.realign_event(
                robot_name, session, plan, datetime.now(_dt_timezone.utc)))
        self.stats["realigned_sessions"] = self.stats.get("realigned_sessions", 0) + 1
        return True

    async def _count_nodes(self, session_id: str, count: int = 1) -> bool:
        """map_sessions.node_count += count. A failure is logged (the node is stored already)
        and returns False: the node is then not marked complete, a resend counts it."""
        try:
            async with self.database.connection() as conn:
                await conn.execute(ingest.COUNT_SQL, (count, uuid.UUID(str(session_id))))
            return True
        except Exception as e:
            self.logger.warning(f"Could not count a node for session {session_id}: {e}")
            return False

    async def _write_event(self, event: Event) -> None:
        try:
            async with self.database.connection() as conn:
                await emit(conn, event)
            self.stats["reject_events_written"] += 1
        except Exception as e:  # noqa: BLE001 - a lost report must not stop ingest
            self.stats["reject_events_failed"] += 1
            self.logger.warning(f"Could not write {event.code.value}: {e}")

    async def _reject(self, resolution: "ingest.Resolution", kind: str, count: int = 1,
                      log: bool = True) -> None:
        """Drop `count` nodes, images, depth images or costmaps ('node' | 'image' | 'depth' |
        'costmap'): count them and report when due. `log` False: the caller logs (the full
        buffers log once per window)."""
        stat = {"node": "nodes_rejected", "depth": "depth_rejected",
                "costmap": "costmap_rejected"}.get(kind, "images_rejected")
        self.stats[stat] += count
        if count and log:
            self.logger.info(
                f"Dropped {count} {kind}(s) from {resolution.robot_name}: {resolution.reason}"
                + (f" (map {resolution.map_name})" if resolution.map_name else ""))
        for _ in range(count):
            event = self.rejects.record(resolution, kind)
            if event is not None:
                await self._write_event(event)

    async def flush_rejects(self) -> None:
        """Report drops no later drop has carried yet (periodic, main.py)."""
        for event in self.rejects.due():
            await self._write_event(event)

    async def manual_target_state(self, map_id: str) -> str:
        """POST /node's guard: the map's effective state ('mapping' accepts), or 'missing' /
        'deleting'."""
        async with self.database.connection() as conn:
            cursor = await conn.execute(
                "SELECT lifecycle, status->>'state' FROM mapobjectv1 WHERE name = %s "
                "AND lifecycle <> 'DELETED'", (map_id,))
            row = await cursor.fetchone()
        if row is None:
            return "missing"
        if row[0] == "DELETING":
            return "deleting"
        return row[1] or "ready"

    async def _active_mission(self, robot_name: str):
        """(RUNNING mission or None, register_map). Unchanged rule: no mission -> ingest."""
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
                return missions[0], missions[0].register_map
        except Exception as e:
            self.logger.error(f"Failed to look up active mission for '{robot_name}': {e}")
        return None, True

    def _mapped_node(self, node_key: Tuple[str, Any], session_id: Optional[str],
                     run_id: Optional[str] = None) -> Optional[Tuple[str, str]]:
        """(global id, map) of a node already mapped in this session (its images can be
        stored), else None. A `run_id` that differs from the robot's current run: None (the
        mapping is from an earlier run). Call under _upload_lock."""
        if self._stale_run(node_key[0], run_id):
            return None
        mapping = self.session_to_global_map.get(node_key)
        if mapping and mapping[3] == session_id:
            return mapping[0], mapping[2]
        return None

    def _stale_run(self, robot_name: str, run_id: Optional[str]) -> bool:
        """The upload's `run_id` is not the robot's current run (call under _upload_lock)."""
        current = self._robot_runs.get(robot_name)
        return run_id is not None and current is not None and run_id != current

    def _node_global_id(self, robot_name: str, session_id: Optional[str],
                        session_node_id: Any, run_id: Optional[str] = None) -> str:
        """The global id of a robot's node: uuid5 of robot, the cloud session's id and the
        robot's node id, so a resend (or an upload after a restart) names the same node. A run
        (the payload's `run_id`, else the token of a fallback-detected reset) is part of the key,
        so a restarted robot's nodes do not collide with the old run's. No session: a fresh
        UUID4 (only the manual / test paths)."""
        if session_id is None:
            return self._generate_global_node_id()
        name = f"{robot_name}:{session_id}:{session_node_id}"
        run = run_id or self._legacy_epochs.get(robot_name)
        if run:
            name += f":{run}"
        return str(uuid.uuid5(NODE_ID_NAMESPACE, name))

    def _candidate_ids(self, robot_name: str, session_id: str, session_node_id: Any,
                       run_id: Optional[str], parts: bool = False) -> List[str]:
        """The global ids a robot's node may be stored under, most likely first. Robots may
        send `run_id` on some messages only (or start to mid-stream, or after a graph-builder
        restart), so the same node can have been stored with or without it:
        - a node message with a `run_id`: that one (`parts` False);
        - an upload with a `run_id`: that one, then the robot's last-known run, then none --
          unless the `run_id` is not the robot's current run (an upload of an earlier run:
          only its own);
        - no `run_id`: the robot's last-known run (`_robot_runs`), then none.
        "None" is the plain id (with the legacy epoch token, if any)."""
        current = self._robot_runs.get(robot_name)
        if run_id is not None:
            runs: List[Optional[str]] = [run_id]
            if parts and (current is None or current == run_id):
                runs.append(None)
        else:
            runs = [current, None] if current is not None else [None]
        ids: List[str] = []
        for run in runs:
            gid = self._node_global_id(robot_name, session_id, session_node_id, run)
            if gid not in ids:
                ids.append(gid)
        return ids

    def _probe_run(self, robot_name: str, session_id: str, map_id: str) -> None:
        """After a restart the robot's last-known run is gone. For a message without `run_id`
        look it up once per robot (the newest node of the robot in this cloud session that
        stored one), so that node and its parts are found under the id they were stored with
        and the legacy reset heuristic stays off for a robot that sends run_id. Blocking."""
        with self._upload_lock:
            if robot_name in self._robot_runs or robot_name in self._run_probed:
                return
            if time.monotonic() < self._run_probe_retry.get(robot_name, 0.0):
                return
        try:
            run = self.graph_db.last_run_id(map_id, robot_name, session_id)
        except Exception as e:  # unknown: ask again, but not for every message
            self.logger.warning(f"Could not look up the last run of {robot_name}: {e}")
            with self._upload_lock:
                self._run_probe_retry[robot_name] = time.monotonic() + RUN_PROBE_BACKOFF_S
            return
        with self._upload_lock:
            self._run_probed.add(robot_name)
            self._run_probe_retry.pop(robot_name, None)
            if isinstance(run, str) and run and robot_name not in self._robot_runs:
                self._robot_runs[robot_name] = run

    async def _find_part_node(self, robot_name: str, session: "ingest.OpenSession",
                              session_node_id: Any, run_id: Optional[str]
                              ) -> Tuple[Optional[str], bool]:
        """(global id, unknown) of the stored node an image / depth / costmap belongs to: the
        node-id LRU for every candidate id first, then ArangoDB per candidate (stopping at the
        first hit). `unknown` True: no candidate was found and a lookup failed, so the node may
        exist (the caller buffers the part; the resend or the periodic cleanup settles it)."""
        if session.session_id is None:
            return None, False
        if (run_id is None and robot_name not in self._robot_runs
                and robot_name not in self._run_probed):
            await asyncio.to_thread(self._probe_run, robot_name, session.session_id,
                                    session.map_name)
        ids = self._candidate_ids(robot_name, session.session_id, session_node_id, run_id,
                                  parts=True)
        for gid in ids:
            if (session.map_name, gid) in self._node_cache:
                return gid, False
        unknown = False
        for gid in ids:
            try:
                if await asyncio.to_thread(self.graph_db.node_exists, session.map_name, gid):
                    self._node_cache.add((session.map_name, gid))
                    return gid, False
            except Exception:  # logged by node_exists
                unknown = True
        if unknown:
            self.stats["errors"] += 1
            self.logger.error(f"Could not tell whether node ({robot_name}, {session_node_id}) "
                              "exists; its upload is buffered")
        return None, unknown

    def _known_complete(self, session_key: Tuple[str, Any], map_id: str,
                        session_id: Optional[str], ids: List[str]) -> Optional[str]:
        """The candidate id of a node known (in memory) to be stored and complete, else None.
        Call under _upload_lock."""
        stored = self._stored_nodes.get(session_key)
        for gid in ids:
            claim = (map_id, gid)
            if claim in self._incomplete:
                continue
            if stored is not None and stored[0] == gid and stored[2] == session_id:
                return gid
            if claim in self._complete_cache:
                return gid
        return None

    def _locate_node(self, map_id: str, ids: List[str]) -> Tuple[Optional[str], str]:
        """(global id, state) of the node in ArangoDB: state 'complete', 'incomplete' (written
        but `ingest_complete: false`; a node without the marker, stored before it existed, is
        complete), 'absent' (no candidate exists) or 'unknown' (a lookup failed and none was
        found: neither absent nor present)."""
        unknown = False
        for gid in ids:
            try:
                if not self.graph_db.node_exists(map_id, gid):
                    continue
            except Exception:  # logged by node_exists
                unknown = True
                continue
            if (map_id, gid) in self._incomplete:
                return gid, 'incomplete'
            try:
                if self.graph_db.node_ingest_incomplete(map_id, gid) is True:
                    return gid, 'incomplete'
            except Exception:  # logged by the graph db
                return gid, 'unknown'
            return gid, 'complete'
        return None, ('unknown' if unknown else 'absent')

    def _wait_claim(self, claim: Tuple[str, str], deadline: float) -> bool:
        """Block (a worker thread; no lock is held while waiting) until the attempt that holds
        `claim` has released it; False when `deadline` (time.monotonic) passed first."""
        with self._upload_lock:
            if claim not in self._processing:
                return True
            event = self._claim_events.setdefault(claim, threading.Event())
        return event.wait(max(0.0, deadline - time.monotonic()))

    def _release_claim(self, claim: Tuple[str, str]) -> None:
        """Release a node's claim and wake the resends waiting for it. Idempotent."""
        with self._upload_lock:
            self._processing.discard(claim)
            event = self._claim_events.pop(claim, None)
        if event is not None:
            event.set()

    def _release_claims(self, claims: List[Tuple[str, str]]) -> None:
        for claim in list(claims):
            self._release_claim(claim)

    def _complete_node(self, map_id: str, global_id: str) -> None:
        """The node's edges are in and it is counted: mark the document complete and remember
        it. A failed marker is logged; in this process the node is complete regardless (so it
        is not repaired and counted twice), after a restart a resend would repair it."""
        if self.graph_db.mark_node_complete(map_id, global_id) is False:
            self.logger.warning(f"Could not mark node {global_id} complete in {map_id}")
        with self._upload_lock:
            self._incomplete.discard((map_id, global_id))
        self._complete_cache.add((map_id, global_id))

    def _stored_node(self, node_key: Tuple[str, Any], session_id: Optional[str],
                     run_id: Optional[str] = None
                     ) -> Optional[Tuple[str, str, Dict[str, float]]]:
        """(global id, map, map_T_session the node was stored with) of a node of this session
        whose ArangoDB document exists (depth and costmap records can be set on it), else None
        (also for a `run_id` that is not the robot's current run). Call under _upload_lock."""
        if self._stale_run(node_key[0], run_id):
            return None
        stored = self._stored_nodes.get(node_key)
        if stored and stored[2] == session_id:
            return stored[0], stored[1], stored[3]
        return None

    async def _handle_node_update(self, payload: Dict[str, Any]):
        """
        Async handler for MQTT node update messages.

        1. A RUNNING mission with register_map=False: no topology, the node only goes to the
           mission's waypoint log (robot frame). Unchanged from before M2.
        2. Otherwise the robot's open mapping session decides (ingest.py): accepted -> the
           node is stored in the session's map, in the map frame; dropped -> counted and
           reported (MAP.INGEST_REJECTED), and so are the images buffered for it. A running
           mission still gets its waypoint.

        The robot comes from the payload's robot_name; a map_id in the payload is ignored.
        """
        robot_name = payload.get('robot_name')
        session_node_id = payload.get('session_node_id')
        x, y = payload.get('x'), payload.get('y')
        yaw = payload.get('yaw', payload.get('theta', 0.0))
        if session_node_id is None or robot_name is None or x is None or y is None:
            self.logger.error("Missing required fields: session_node_id, robot_name, x, y")
            self.stats["errors"] += 1
            return

        active_mission, register_map = await self._active_mission(robot_name)
        if not register_map:
            await self._log_mission_waypoint(
                robot_name, str(session_node_id), session_node_id, x, y, yaw, '',
                _mission=active_mission,
            )
            return

        resolution = await self.sessions.resolve(robot_name, payload.get('session_id'))
        if not resolution.accepted:
            await self._reject(resolution, "node")
            node_key = (robot_name, session_node_id)
            await self._reject(resolution, "image", self.image_buffer.pop(node_key))
            await self._reject(resolution, "depth", self.depth_buffer.pop(node_key))
            await self._reject(resolution, "costmap", self.costmap_buffer.pop(node_key))
            if active_mission is not None:
                await self._log_mission_waypoint(
                    robot_name, str(session_node_id), session_node_id, x, y, yaw, '',
                    _mission=active_mission,
                )
            return

        session = resolution.session
        if not await self._ensure_robot_exists(robot_name):
            self.logger.error(f"Failed to ensure robot '{robot_name}' exists in Mission Dispatch database")
        # `_process_topology` reports the claim it holds in `claims` (not the returned node's
        # id: a repair may resolve to another candidate id than the claimed one). If this
        # handler is cancelled meanwhile the worker thread still finishes: its claim is
        # released when it does.
        claims: List[Tuple[str, str]] = []
        future = asyncio.get_running_loop().run_in_executor(
            self._topology_executor,
            functools.partial(
                self._process_topology, payload, map_id=session.map_name,
                transform=session.map_t_session, session_id=session.session_id,
                hold_claim=True, claim_out=claims))
        try:
            result = await asyncio.shield(future)
        except asyncio.CancelledError:
            future.add_done_callback(lambda _f: self._release_claims(claims))
            raise
        if result is None:
            return
        global_node_id, mx, my, myaw, map_id, edges, session_node_id, robot_name = result
        published_key = (map_id, global_node_id)
        try:
            # The claim is held until the node is counted and marked complete: a resend that
            # waited for it then sees a finished node, not one to repair (and count again).
            if await self._count_nodes(session.session_id) is not False:
                await asyncio.to_thread(self._complete_node, map_id, global_node_id)
            else:
                self.logger.warning(f"Node {global_node_id} stays incomplete (not counted): "
                                    "a resend repairs it")
            # Published / logged once per node (in this process): a repair after a failed
            # count must not do it again. Decided under the claim.
            already_published = published_key in self._published
            self._published.add(published_key)
        finally:
            self._release_claims(claims)
        if already_published:
            return
        await self._publish_node_update(map_id, global_node_id, mx, my, myaw, edges, image_ids=None)
        if active_mission is not None:
            await self._log_mission_waypoint(
                robot_name, global_node_id, session_node_id, mx, my, myaw, map_id,
                _mission=active_mission,
            )

    def _process_topology(self, payload: Dict[str, Any], map_id: Optional[str] = None,
                          transform: Optional[Dict[str, float]] = None,
                          session_id: Optional[str] = None,
                          hold_claim: bool = False,
                          claim_out: Optional[List[Tuple[str, str]]] = None
                          ) -> Optional[Tuple]:
        """
        Build topological graph from a node-update payload.

        Validates fields, derives the global node id (deterministic: `_node_global_id`),
        creates edges to nearby nodes, and writes the node and edges to the graph database.

        A node that is already stored and complete (the robot resent it, or an MQTT
        redelivery) is a duplicate: first write wins, so nothing is written (no add_node over a
        node the scene workers may have enriched, no new edges), nothing is counted in the
        session and None is returned (`_attach_duplicate`). Known nodes are found in memory, in
        the node-id LRU, or by an ArangoDB lookup (after a restart); the node may have been
        stored with or without the robot's `run_id` (`_candidate_ids`). A node that is stored
        but not complete (`ingest_complete: false`: its edges or its count failed) is repaired
        by the resend (`_repair_node`) and returned like a new node. A resend that meets a
        claim (the same node is being stored by another thread) waits for it, bounded by
        `claim_wait_s`, and then looks again. When ArangoDB cannot tell whether the node exists
        nothing is written (counted in `errors`): the robot's resend settles it.

        Args:
            payload: The incoming MQTT node-update dict (robot-frame x, y, yaw; optional
                    `run_id`, which tells a restarted robot run from a resend).
            map_id: The session's map (required; there is no default map any more). Any
                    ``map_id`` key present in the payload is ignored.
            transform: The session's map_T_session; the stored `pose` is the payload pose in
                    the map frame. None: identity.
            session_id: The cloud session (of the open mapping session, not the payload's);
                    stored on the node with `robot_pose` (the payload pose).
            hold_claim: for a returned node, keep its claim (`_processing`) until the caller
                    releases it (after counting and `_complete_node`).
            claim_out: with `hold_claim`, the held claim is appended here (before the call
                    returns) -- the caller releases exactly it (`_release_claims`), whatever
                    candidate id the returned node resolved to.

        Returns (global_node_id, x, y, yaw, map_id, edges, session_node_id, robot_name), with
        x, y, yaw in the map frame, or None on failure or for a duplicate.
        """
        session_node_id = payload.get('session_node_id')
        robot_name = payload.get('robot_name')
        rx = payload.get('x')
        ry = payload.get('y')
        ryaw = payload.get('yaw', payload.get('theta', 0.0))

        if session_node_id is None or robot_name is None or rx is None or ry is None:
            self.logger.error("Missing required fields: session_node_id, robot_name, x, y")
            self.stats["errors"] += 1
            return None
        if not map_id:
            self.logger.error("No map for node update (no default map since maps M2)")
            self.stats["errors"] += 1
            return None

        transform = transform or ingest.map_geo.IDENTITY
        x, y, yaw = ingest.map_pose(transform, rx, ry, ryaw)

        self.logger.info(f"📨 Received node update from {robot_name}, session_node_id={session_node_id}")

        session_key = (robot_name, session_node_id)
        run_id = _run_id_of(payload)
        if session_id is not None and run_id is None:
            self._probe_run(robot_name, session_id, map_id)
        deadline = time.monotonic() + self.claim_wait_s
        with self._upload_lock:
            self._detect_and_clear_session_reset(robot_name, session_node_id, run_id)
            global_node_id = self._node_global_id(robot_name, session_id, session_node_id,
                                                  run_id)
        if session_id is None:  # manual / test path: a fresh id, nothing to deduplicate
            return self._store_node(payload, map_id, transform, session_id, x, y, yaw,
                                    global_node_id)

        claim = (map_id, global_node_id)
        while True:
            with self._upload_lock:
                ids = self._candidate_ids(robot_name, session_id, session_node_id, run_id)
                known = self._known_complete(session_key, map_id, session_id, ids)
                busy = next(((map_id, g) for g in ids if (map_id, g) in self._processing), None)
                if known is None and busy is None:
                    self._processing.add(claim)
            if known is not None:
                return self._attach_duplicate(session_key, known, map_id, transform, session_id,
                                              run_id)
            if busy is None:
                break
            # The same node is being stored by another worker thread: wait for it (no lock
            # held), then look again -- it may have failed, or left the node to repair.
            if not self._claim_waiters.acquire(blocking=False):
                self.stats["nodes_duplicate"] += 1
                self.logger.warning(f"Node {busy[1]} is being stored and too many resends "
                                    "wait; resend ignored")
                return None
            try:
                waited = self._wait_claim(busy, deadline)
            finally:
                self._claim_waiters.release()
            if not waited:
                self.stats["nodes_duplicate"] += 1
                self.logger.warning(f"Node {busy[1]} is still being stored after "
                                    f"{self.claim_wait_s:.0f}s; resend ignored")
                return None

        held = False
        try:
            found, state = self._locate_node(map_id, ids)
            if state == 'unknown':
                self.stats["errors"] += 1
                self.logger.error(f"Cannot tell whether node {global_node_id} {session_key} "
                                  "exists (ArangoDB lookup failed); nothing written, "
                                  "a resend settles it")
                return None
            if state == 'complete':
                self._complete_cache.add((map_id, found))
                return self._attach_duplicate(session_key, found, map_id, transform,
                                              session_id, run_id)
            if state == 'incomplete':
                result = self._repair_node(payload, map_id, transform, session_id, x, y, yaw,
                                           found)
            else:
                result = self._store_node(payload, map_id, transform, session_id, x, y, yaw,
                                          global_node_id)
            held = result is not None and hold_claim
            if held and claim_out is not None:
                claim_out.append(claim)
            return result
        finally:
            if not held:
                self._release_claim(claim)

    def _restore_node(self, session_key: Tuple[str, Any], global_node_id: str, map_id: str,
                      transform: Dict[str, float], session_id: str, run_id: Optional[str]
                      ) -> Tuple[Dict[str, float], List[Any], List[Any], List[Any]]:
        """Restore in memory what a later upload needs for a node stored in ArangoDB (mapping,
        `_stored_nodes`: e.g. after a restart) and take what is buffered for it. Returns the
        node's transform and the buffered images, depth and costmap entries."""
        with self._upload_lock:
            mapping = self.session_to_global_map.get(session_key)
            if mapping is None or mapping[0] != global_node_id or mapping[3] != session_id:
                self.session_to_global_map[session_key] = (global_node_id, datetime.now(),
                                                           map_id, session_id)
                self.stats["session_mappings"] += 1
            stored = self._stored_nodes.get(session_key)
            if stored is None or stored[0] != global_node_id or stored[2] != session_id:
                self._stored_nodes[session_key] = (global_node_id, map_id, session_id, transform)
            else:
                transform = stored[3]
            timeout = self.image_buffer_timeout
            return (transform,
                    self.image_buffer.take(session_key, timeout, session_id, run_id),
                    self.depth_buffer.take(session_key, timeout, session_id, run_id),
                    self.costmap_buffer.take(session_key, timeout, session_id, run_id))

    def _save_buffered(self, global_node_id: str, map_id: str, transform: Dict[str, float],
                       session_id: Optional[str], images: List[Any], depth: List[Any],
                       costmap: List[Any]) -> None:
        if images:
            self._save_images(global_node_id, map_id, images)
        if depth:
            self._save_depth(global_node_id, map_id, depth, transform, session_id)
        if costmap:
            self._save_costmap(global_node_id, map_id, costmap, transform, session_id)

    def _attach_duplicate(self, session_key: Tuple[str, Any], global_node_id: str, map_id: str,
                          transform: Dict[str, float], session_id: str,
                          run_id: Optional[str] = None) -> None:
        """A resent node that is stored already: no write, no edges, no count, no WebSocket
        update. Only what a later upload needs is restored in memory (mapping, `_stored_nodes`:
        e.g. after a restart) and anything buffered for the node is saved. Always None."""
        transform, images, depth, costmap = self._restore_node(
            session_key, global_node_id, map_id, transform, session_id, run_id)
        self._node_cache.add((map_id, global_node_id))
        self.stats["nodes_duplicate"] += 1
        self.logger.debug(f"Node {global_node_id} {session_key} is stored already; resend "
                          "ignored (first write wins)")
        self._save_buffered(global_node_id, map_id, transform, session_id, images, depth,
                            costmap)
        return None

    def _repair_node(self, payload: Dict[str, Any], map_id: str, transform: Dict[str, float],
                     session_id: str, x: float, y: float, yaw: float, global_node_id: str
                     ) -> Optional[Tuple]:
        """The resend of a node whose document is written but not complete: create its
        missing edges (deterministic keys: the ones that exist are left alone) and return it
        like a new node, so the caller counts it (once: it was not counted) and marks it
        complete. No add_node (first write wins). None when the edges fail again."""
        session_node_id = payload.get('session_node_id')
        robot_name = payload.get('robot_name')
        session_key = (robot_name, session_node_id)
        with self._upload_lock:
            self._incomplete.add((map_id, global_node_id))
        transform, images, depth, costmap = self._restore_node(
            session_key, global_node_id, map_id, transform, session_id, _run_id_of(payload))
        self._node_cache.add((map_id, global_node_id))
        self.logger.warning(f"Repairing node {global_node_id} {session_key}: stored but not "
                            "complete")
        edges = self._create_edges(global_node_id, x, y, yaw,
                                   self._find_nearby_nodes(x, y, map_id))
        try:
            inserted = self.graph_db.add_edges_bulk(edges, map_id=map_id,
                                                    deterministic_keys=True,
                                                    raise_on_error=True)
        except Exception as e:
            self.logger.error(f"Repair of node {global_node_id}: edges failed: {e}")
            self.stats["errors"] += 1
            inserted = None
        self._save_buffered(global_node_id, map_id, transform, session_id, images, depth,
                            costmap)
        if inserted is None:
            return None
        self.stats["edges_created"] += inserted
        self.stats["nodes_repaired"] = self.stats.get("nodes_repaired", 0) + 1
        return global_node_id, x, y, yaw, map_id, edges, session_node_id, robot_name

    def _store_node(self, payload: Dict[str, Any], map_id: str, transform: Dict[str, float],
                    session_id: Optional[str], x: float, y: float, yaw: float,
                    global_node_id: str) -> Optional[Tuple]:
        """The write path of `_process_topology` for a node that is not stored yet: map it,
        take the images buffered for it, find its edges, add the node, then take the depth and
        costmap records and insert the edges."""
        session_node_id = payload.get('session_node_id')
        robot_name = payload.get('robot_name')
        rx = payload.get('x')
        ry = payload.get('y')
        ryaw = payload.get('yaw', payload.get('theta', 0.0))
        camera_metadata = payload.get('camera_metadata', [])
        metadata = dict(payload.get('metadata') or {})

        session_key = (robot_name, session_node_id)
        run_id = _run_id_of(payload)
        with self._upload_lock:
            # A re-sent node: its records wait for this node's document.
            self._stored_nodes.pop(session_key, None)
            self.session_to_global_map[session_key] = (global_node_id, datetime.now(), map_id,
                                                       session_id)
            buffered_images = self.image_buffer.take(session_key, self.image_buffer_timeout,
                                                     session_id, run_id)
        self.stats["session_mappings"] += 1

        self.logger.info(f"🔑 Mapped ({robot_name}, {session_node_id}) -> {global_node_id}")

        metadata['robot_name'] = robot_name
        metadata['session_node_id'] = session_node_id
        metadata['camera_metadata'] = camera_metadata
        if session_id is not None:
            metadata['session_id'] = session_id
            metadata['robot_pose'] = {'x': rx, 'y': ry, 'yaw': ryaw}
        if run_id is not None:
            metadata['run_id'] = run_id
        # Complete only when the edges are in and the node is counted (`_complete_node`).
        # A node document without this field was stored before the marker existed: complete.
        # (The manual / test path, no session, has no count to wait for: no marker.)
        if session_id is not None:
            metadata['ingest_complete'] = False

        if buffered_images:
            self.logger.info(f"Found {len(buffered_images)} buffered images for node {global_node_id}")
            self._save_images(global_node_id, map_id, buffered_images)

        nearby_nodes = self._find_nearby_nodes(x, y, map_id)
        edges = self._create_edges(global_node_id, x, y, yaw, nearby_nodes)

        metadata['map_id'] = map_id
        metadata['timestamp'] = datetime.now().isoformat()
        # The robot's capture time (a separate field; `timestamp`/`created_at` stay the cloud's
        # receipt time). Never taken from payload.metadata: only the sanitised value or absent.
        metadata.pop(capture_time.FIELD, None)
        captured = capture_time.extract(payload)
        if captured is not None:
            metadata[capture_time.FIELD] = captured

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

        # Depth and costmap records go onto the node document: from now on they are stored
        # directly; what was buffered until now is taken under the same lock.
        with self._upload_lock:
            self._stored_nodes[session_key] = (global_node_id, map_id, session_id, transform)
            if session_id is not None:
                self._incomplete.add((map_id, global_node_id))
            buffered_depth = self.depth_buffer.take(session_key, self.image_buffer_timeout,
                                                    session_id, run_id)
            buffered_costmap = self.costmap_buffer.take(session_key, self.image_buffer_timeout,
                                                        session_id, run_id)
        if session_id is not None:
            self._node_cache.add((map_id, global_node_id))  # exists (parts can attach)

        try:
            inserted = self.graph_db.add_edges_bulk(edges, map_id=map_id,
                                                    deterministic_keys=True,
                                                    raise_on_error=True)
        except Exception as e:
            # The node stays incomplete (not counted, not published): the robot's resend
            # repairs it. The records taken above are still saved.
            self.logger.error(f"Edges of node {global_node_id} failed: {e}")
            self.stats["errors"] += 1
            inserted = None

        # The records are made now, with the transform the node itself got (an upload that
        # waited may predate a re-placement of the session).
        if buffered_depth:
            self._save_depth(global_node_id, map_id, buffered_depth, transform, session_id)
        if buffered_costmap:
            self._save_costmap(global_node_id, map_id, buffered_costmap, transform, session_id)
        if inserted is None:
            return None
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

    def _on_upload_message(self, name: str, handler: Callable, msg) -> None:
        """An image / depth / costmap upload message from MQTT (paho thread): parse it and hand
        it to `handler(payload)` on the event loop, which needs the session lookup."""
        try:
            payload = json.loads(msg.payload.decode('utf-8'))
            if self._event_loop is None:
                self.logger.error(f"{name.capitalize()} upload before the event loop was set; "
                                  "dropped")
                self.stats["errors"] += 1
                return
            asyncio.run_coroutine_threadsafe(handler(payload), self._event_loop)
        except Exception as e:
            self.logger.error(f"Error processing {name} upload message: {e}")
            self.stats["errors"] += 1

    def _on_image_upload_message(self, client, userdata, msg):
        """robot/image_upload (paho thread)."""
        self._on_upload_message("image", self._handle_image_upload, msg)

    async def _reject_buffer_full(self, resolution: "ingest.Resolution", kind: str) -> None:
        """Count an upload dropped because its buffer is at its cap (the buffer logs it once
        per window) as `buffer_full` in MAP.INGEST_REJECTED."""
        full = dataclasses.replace(resolution, reason=ingest.BUFFER_FULL)
        await self._reject(full, kind, log=False)

    async def _handle_image_upload(self, payload: Dict[str, Any]):
        """
        Save one camera image of a node, or buffer it until the node arrives.

        The image goes where its node goes: the robot's open mapping session decides
        (ingest.py; a dropped image is counted and reported like a node). A map_id in the
        payload is ignored (before M2 it was used, so images landed in "default" while their
        nodes went to the robot's old current_map).

        Payload: session_node_id, robot_name, camera_name, image_data (base64), and optionally
        content_type, timestamp, yaw_offset, session_id (M3 robots), run_id (the robot's run: an
        upload of an earlier run does not match this run's node).

        The node is found by its deterministic id (see `_node_global_id`): in memory, in the
        node-id LRU or in ArangoDB (after a restart, or a node another worker stored); only a
        node that does not exist yet makes the image wait. An image resent is stored again
        under the same MinIO key (idempotent).
        """
        try:
            session_node_id = payload.get('session_node_id')
            robot_name = payload.get('robot_name')
            camera_name = payload.get('camera_name')
            image_data_b64 = payload.get('image_data')
            content_type = payload.get('content_type', 'image/jpeg')
            timestamp = payload.get('timestamp')
            yaw_offset = payload.get('yaw_offset', 0.0)
            run_id = _run_id_of(payload)

            if session_node_id is None or robot_name is None or camera_name is None or image_data_b64 is None:
                self.logger.error("Missing required fields in image upload: session_node_id, robot_name, camera_name, image_data")
                self.stats["errors"] += 1
                return

            self.logger.info(f"📸 Image upload: camera={camera_name}, yaw_offset={yaw_offset}, robot={robot_name}, session_node={session_node_id}")

            resolution = await self.sessions.resolve(robot_name, payload.get('session_id'))
            if not resolution.accepted:
                await self._reject(resolution, "image")
                return
            session = resolution.session

            image_dict = {
                'image_id': camera_name,
                'data': image_data_b64,
                'content_type': content_type,
                'session_id': session.session_id,  # NodeUploadBuffer.take checks it
                'run_id': run_id,  # a reset keeps the new run's buffered uploads
                'metadata': {
                    'camera_name': camera_name,
                    'timestamp': timestamp,
                    'robot_name': robot_name,
                    'session_node_id': session_node_id,
                    'yaw_offset': yaw_offset,
                    'session_id': session.session_id,
                }
            }

            # Node update already received in this session: save to the node's map. Else
            # (the robot sends images first) buffer it until the node arrives. A node stored
            # before a restart is not in memory: it is found in ArangoDB by its deterministic
            # id (MinIO needs only the id), so the image is stored without waiting.
            node_key = (robot_name, session_node_id)
            with self._upload_lock:
                known = self._mapped_node(node_key, session.session_id, run_id)
            if known is None:
                gid, _unknown = await self._find_part_node(robot_name, session, session_node_id,
                                                           run_id)
                if gid is not None:
                    known = (gid, session.map_name)
            target = self.image_buffer.put_unless(
                node_key, camera_name, image_dict,
                lambda: self._mapped_node(node_key, session.session_id, run_id) or known)
            if target is DROPPED:
                await self._reject_buffer_full(resolution, "image")
            elif target is not None:
                global_node_id, map_id = target
                saved_image_ids = await asyncio.to_thread(
                    self._save_images, global_node_id, map_id, [image_dict])
                if saved_image_ids:
                    await self._publish_image_update(map_id, global_node_id, saved_image_ids)
            else:
                self.logger.debug(f"Buffered image for ({robot_name}, {session_node_id}, {camera_name})")

        except Exception as e:
            self.logger.error(f"Error processing image upload message: {e}")
            self.stats["errors"] += 1

    # ==================== Depth (3D reconstruction R2) and costmap ====================

    def _on_depth_upload_message(self, client, userdata, msg):
        """robot/depth_upload (paho thread)."""
        self._on_upload_message("depth", self._handle_depth_upload, msg)

    def _on_costmap_upload_message(self, client, userdata, msg):
        """robot/costmap_upload (paho thread)."""
        self._on_upload_message("costmap", self._handle_costmap_upload, msg)

    async def _handle_depth_upload(self, payload: Dict[str, Any]):
        """
        Store one camera's depth image of a node, or buffer it until the node arrives
        (docs/reconstruction/design.md §5). Resolved like an image: a dropped depth image is
        counted as `depth` in MAP.INGEST_REJECTED. The node's `depth.{camera}` carries the
        camera block, scale, stamps, `robot_pose3d` and its map-frame `pose3d_map`, converted
        with the transform the node was stored with (the same the node pose got).
        """
        await self._handle_layer_upload(DEPTH_UPLOAD, payload)

    async def _handle_costmap_upload(self, payload: Dict[str, Any]):
        """
        Store one layer's occupancy costmap of a node, or buffer it until the node arrives
        (same session gating and buffering as depth). A dropped one is counted as `costmap` in
        MAP.INGEST_REJECTED. The node's `costmap.{layer}` is ingest.costmap_record_from (the
        known fields, parsed, plus `origin_map` / `origin_pose3d_map` in the map frame). A
        malformed message is only logged and counted in stats["errors"]: it is validated once,
        before the session lookup.
        """
        await self._handle_layer_upload(COSTMAP_UPLOAD, payload)

    async def _handle_layer_upload(self, spec: _LayerUpload, payload: Dict[str, Any]):
        """The depth / costmap upload: validate (once, parsed), resolve the session, then store
        against the node's document or buffer the parsed fields until the node is stored. The
        record itself (with its map-frame fields) is made when it is stored."""
        try:
            try:
                parsed = spec.check(payload)
            except spec.error as e:
                self.logger.error(f"Invalid {spec.kind} upload: {e}")
                self.stats["errors"] += 1
                return
            robot_name = payload['robot_name']
            session_node_id = payload['session_node_id']
            sub = str(payload[spec.sub_field])

            resolution = await self.sessions.resolve(robot_name, payload.get('session_id'))
            if not resolution.accepted:
                await self._reject(resolution, spec.kind)
                return
            session = resolution.session
            run_id = _run_id_of(payload)
            entry = {spec.sub_key: sub, "data": payload[spec.data_key], "parsed": parsed,
                     "session_id": session.session_id, "run_id": run_id}

            # The record goes onto the node's document: a node stored before a restart is not
            # in memory, so its document is looked up (by the deterministic id) before buffering.
            node_key = (robot_name, session_node_id)
            with self._upload_lock:
                known = self._stored_node(node_key, session.session_id, run_id)
            if known is None:
                gid, _unknown = await self._find_part_node(robot_name, session, session_node_id,
                                                           run_id)
                if gid is not None:
                    known = (gid, session.map_name, session.map_t_session)
            target = getattr(self, f"{spec.kind}_buffer").put_unless(
                node_key, sub, entry,
                lambda: self._stored_node(node_key, session.session_id, run_id) or known)
            if target is DROPPED:
                await self._reject_buffer_full(resolution, spec.kind)
            elif target is not None:
                global_node_id, map_id, transform = target
                await asyncio.to_thread(self._save_layers, spec, global_node_id, map_id,
                                        [entry], transform, session.session_id)
        except Exception as e:
            self.logger.error(f"Error processing {spec.kind} upload message: {e}")
            self.stats["errors"] += 1

    def _save_depth(self, node_id: str, map_id: str, entries: List[Dict[str, Any]],
                    transform: Optional[Dict[str, float]] = None,
                    session_id: Optional[str] = None) -> int:
        """`_save_layers` for depth entries (`transform` None: identity)."""
        return self._save_layers(DEPTH_UPLOAD, node_id, map_id, entries,
                                 transform or ingest.map_geo.IDENTITY, session_id)

    def _save_costmap(self, node_id: str, map_id: str, entries: List[Dict[str, Any]],
                      transform: Optional[Dict[str, float]] = None,
                      session_id: Optional[str] = None) -> int:
        """`_save_layers` for costmap entries (`transform` None: identity)."""
        return self._save_layers(COSTMAP_UPLOAD, node_id, map_id, entries,
                                 transform or ingest.map_geo.IDENTITY, session_id)

    def _save_layers(self, spec: _LayerUpload, node_id: str, map_id: str,
                     entries: List[Dict[str, Any]], transform: Dict[str, float],
                     session_id: Optional[str]) -> int:
        """Store each entry's PNG in MinIO, then the records of the ones that stored on the node
        in one update. How many were stored (the PNG first: a node never names an image that
        does not exist; each failed entry counts one error). The records are built here, from
        the entries' parsed fields, with `transform` (the node's map_T_session)."""
        store = getattr(self.image_db, f"store_{spec.kind}")
        stamp = f"{spec.kind}_stamp_ms"
        stored: Dict[str, Dict[str, Any]] = {}
        for entry in entries:
            sub = entry[spec.sub_key]
            try:
                record = spec.record(entry["parsed"], transform, session_id)
                png = base64.b64decode(entry["data"], validate=True)
                if not store(png, str(node_id), sub, map_id,
                             metadata={spec.sub_field: sub, stamp: record.get(stamp),
                                       "session_id": record.get("session_id")}):
                    self.stats["errors"] += 1
                    continue
                stored[sub] = record
            except Exception as e:
                self.logger.error(f"Error saving {spec.kind} {sub} of node {node_id}: {e}")
                self.stats["errors"] += 1
        if not stored:
            return 0
        try:
            if len(stored) == 1:
                [(sub, record)] = stored.items()
                ok = getattr(self.graph_db, f"set_node_{spec.kind}")(
                    map_id, str(node_id), sub, record)
            else:
                ok = getattr(self.graph_db, f"set_node_{spec.kind}s")(
                    map_id, str(node_id), stored)
        except Exception as e:
            self.logger.error(f"Error saving {spec.kind} {', '.join(stored)} of node "
                              f"{node_id}: {e}")
            ok = False
        if not ok:
            self.stats["errors"] += len(stored)
            return 0
        self.stats[f"{spec.kind}_saved"] += len(stored)
        return len(stored)

    # ==================== Node Processing ====================

    def process_node_update(self, node_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Process a manual node update (POST /node, a debug/test hook; robots use MQTT).

        Writes straight into `map_id`, which is required (no default map since maps M2) and
        must be a map in the `mapping` state (checked by the route, main.py). The pose is
        taken as map-frame; there is no session transform.

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
                - map_id: Map identifier (required)
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
            map_id = node_data.get('map_id')
            metadata = node_data.get('metadata', {})

            # Validate required fields
            if node_id is None or x is None or y is None or not map_id:
                self.logger.error("Missing required fields: node_id, x, y, map_id")
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

    def _detect_and_clear_session_reset(self, robot_name: str, session_node_id: int,
                                        run_id: Optional[str] = None):
        """
        Detect if a new topomap run has started for this robot.

        With a `run_id` (robots that send it) a reset is a change of the robot's run_id: a
        lower session_node_id with the same run_id is a resend or a redelivery and wipes
        nothing. Without one, the fallback heuristic applies: a smaller session_node_id than
        any seen before for the robot means the robot restarted its counter (a resend of an
        older node looks the same, so it also resets, as before); the robot's later node ids
        then carry a new epoch token.
        On a reset all old mappings and buffered uploads for this robot are cleared (the new
        run's own uploads that already arrived stay). Call under _upload_lock.

        Args:
            robot_name: Name of the robot
            session_node_id: Current session node ID
            run_id: The payload's run_id, if any
        """
        keep = None
        if run_id is not None:
            previous = self._robot_runs.get(robot_name)
            self._robot_runs[robot_name] = run_id
            if previous is None or previous == run_id:
                return
            reason = f"run_id {previous} -> {run_id}"
            keep = lambda entry: isinstance(entry, dict) and entry.get("run_id") == run_id  # noqa: E731
        else:
            if robot_name in self._robot_runs:
                # The robot sends run_id (it is only missing on this message): a lower id is
                # a resend, never evidence of a restart. Do not wipe its state.
                return
            # (A lower id may be a resend, but without a run_id it cannot be told from a
            # restart, and ignoring a restart would drop the new run's nodes as duplicates.)
            # Find the maximum session_node_id we've seen for this robot
            max_session_id = -1
            for (r_name, s_id), _ in self.session_to_global_map.items():
                if r_name == robot_name:
                    max_session_id = max(max_session_id, s_id)

            # If current session_node_id is smaller than max, we've detected a session reset
            if not (max_session_id >= 0 and session_node_id < max_session_id):
                return
            reason = f"session_node_id={session_node_id} < max_seen={max_session_id}"
            self._legacy_epochs[robot_name] = uuid.uuid4().hex[:8]

        self.logger.warning(
            f"🔄 Session reset detected for {robot_name}: {reason}. "
            f"Clearing old mappings and buffered images."
        )

        # Clear session mappings for this robot
        keys_to_remove = [
            key for key in self.session_to_global_map.keys()
            if key[0] == robot_name
        ]
        for key in keys_to_remove:
            del self.session_to_global_map[key]
            self._stored_nodes.pop(key, None)
            self.stats["session_mappings"] -= 1

        # Clear buffered uploads for this robot
        image_nodes = self.image_buffer.clear_robot(robot_name, keep)
        self.depth_buffer.clear_robot(robot_name, keep)
        self.costmap_buffer.clear_robot(robot_name, keep)

        self.logger.info(
            f"✅ Cleared {len(keys_to_remove)} session mappings and "
            f"{image_nodes} buffered images for {robot_name}"
        )

    def _generate_global_node_id(self) -> str:
        """
        Generate a globally unique node ID.

        Uses UUID4 for distributed uniqueness.

        Returns:
            Global node ID as string
        """
        return str(uuid.uuid4())

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

        with self._upload_lock:
            # Clean up old session mappings
            old_mappings = []
            for session_key, (global_id, timestamp, *_rest) in self.session_to_global_map.items():
                age = (now - timestamp).total_seconds()
                if age > threshold_seconds:
                    old_mappings.append(session_key)

            for key in old_mappings:
                del self.session_to_global_map[key]
                self._stored_nodes.pop(key, None)
                self.logger.debug(f"Cleaned up old session mapping: {key}")

            # Clean up old buffered uploads
            for buffer in (self.image_buffer, self.depth_buffer, self.costmap_buffer):
                buffer.cleanup(threshold_seconds)

    # ==================== Service Management ====================

    def get_stats(self) -> Dict[str, Any]:
        """
        Get service statistics.

        Returns:
            Dictionary with service statistics
        """
        return {
            **self.stats,
            "mqtt_connected": self.mqtt_connected,
            "radius_threshold": self.radius_threshold
        }

    @property
    def mqtt_connected(self) -> bool:
        """The MQTT client's live connection state (paho's on_connect / on_disconnect)."""
        return bool(self.mqtt_client is not None and self.mqtt_client.connected)
    
    def is_healthy(self) -> bool:
        """
        Check if service is healthy.

        Returns:
            True if all dependencies are healthy
        """
        try:
            return self.mqtt_connected and self.topomap_db.is_healthy() and self.database.is_running()
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
                "mqtt_connected": self.mqtt_connected,
                "image_db": image_db_healthy,
                "graph_db": graph_db_healthy,
            }
        except Exception:
            return {
                "mqtt_connected": self.mqtt_connected,
                "image_db": False,
                "graph_db": False,
            }

