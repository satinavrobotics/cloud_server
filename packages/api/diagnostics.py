"""Robot system diagnostics ingestion for the API Delegation Service.

Subscribes directly to `<robot_name>/diagnostics` MQTT messages (JSON mirror of a
ROS DiagnosticArray covering jtop/host_stats/ros_health/topic_availability
collectors), keeps an in-memory latest-value cache per robot, and rebroadcasts
to WebSocket clients
already connected to `/ws/robot/{robot_name}` (bucket "robot_status"). Cache-only:
this is live telemetry, not persisted to Postgres.

Also covers the two nav2 behavior-tree topics published by the same robot-side
diagnostics_reporter node, on the same MQTT client and cache/broadcast pattern:
`<robot_name>/nav2_bt_tree` (retained, published once at startup then only on an
XML change) and `<robot_name>/nav2_bt_state` (live, coalesced to 5Hz by the
publisher).
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from packages.utils.mqtt_client import MQTTClient

logger = logging.getLogger("ApiDelegationService.diagnostics")

DIAGNOSTICS_TOPIC = "+/diagnostics"
_TOPIC_RE = re.compile(r"^(.+)/diagnostics$")

BT_TREE_TOPIC = "+/nav2_bt_tree"
_BT_TREE_TOPIC_RE = re.compile(r"^(.+)/nav2_bt_tree$")

BT_STATE_TOPIC = "+/nav2_bt_state"
_BT_STATE_TOPIC_RE = re.compile(r"^(.+)/nav2_bt_state$")

COLLECTOR_NAMES = ("jtop", "host_stats", "ros_health", "topic_availability")
STALE_SOURCES = ("esp32", "gps", "sati_pose")

LEVEL_OK = 0
LEVEL_WARN = 1
LEVEL_ERROR = 2


class DiagnosticsService:
    def __init__(self, mqtt_host: str, mqtt_port: int, mqtt_keepalive: int, ws_manager: Any):
        self.logger = logger
        self.mqtt_host = mqtt_host
        self.mqtt_port = mqtt_port
        self.mqtt_keepalive = mqtt_keepalive
        self.ws_manager = ws_manager

        self.mqtt_client: Optional[MQTTClient] = None
        self._event_loop: Optional[asyncio.AbstractEventLoop] = None
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._bt_tree_cache: Dict[str, Dict[str, Any]] = {}
        self._bt_state_cache: Dict[str, Dict[str, Any]] = {}

    def set_event_loop(self, loop: asyncio.AbstractEventLoop):
        self._event_loop = loop

    def connect_mqtt(self) -> bool:
        try:
            client_id = f"api_delegation_diagnostics_{datetime.now(timezone.utc).timestamp()}"
            self.mqtt_client = MQTTClient(
                client_id=client_id,
                broker=self.mqtt_host,
                port=self.mqtt_port,
                keepalive=self.mqtt_keepalive,
            )
            self.mqtt_client.register_callback(DIAGNOSTICS_TOPIC, self._on_diagnostics_message)
            self.mqtt_client.register_callback(BT_TREE_TOPIC, self._on_bt_tree_message)
            self.mqtt_client.register_callback(BT_STATE_TOPIC, self._on_bt_state_message)
            self.mqtt_client.connect()
            self.logger.info(
                f"[Diagnostics] Subscribed to diagnostics streams: "
                f"{DIAGNOSTICS_TOPIC}, {BT_TREE_TOPIC}, {BT_STATE_TOPIC}"
            )
            return True
        except Exception as e:
            self.logger.error(f"[Diagnostics] Failed to set up MQTT client: {e}")
            return False

    def disconnect_mqtt(self):
        if self.mqtt_client:
            self.mqtt_client.disconnect()
            self.logger.info("[Diagnostics] Disconnected from MQTT broker")

    def get_cached(self, robot_name: str) -> Optional[Dict[str, Any]]:
        return self._cache.get(robot_name)

    def get_cached_bt_tree(self, robot_name: str) -> Optional[Dict[str, Any]]:
        return self._bt_tree_cache.get(robot_name)

    def get_cached_bt_state(self, robot_name: str) -> Optional[Dict[str, Any]]:
        return self._bt_state_cache.get(robot_name)

    def _decode_mqtt_json(self, msg, topic_re: "re.Pattern"):
        """
        Runs on the MQTT client's own thread. Matches `msg.topic` against `topic_re`
        and decodes its JSON payload. Returns `(robot_name, payload)`, or `None` if
        the topic doesn't match or the payload isn't valid JSON.
        """
        match = topic_re.match(msg.topic)
        if not match:
            return None
        robot_name = match.group(1)

        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception as e:
            self.logger.error(f"[Diagnostics] Failed to decode payload on {msg.topic}: {e}")
            return None

        return robot_name, payload

    def _schedule(self, coro_factory, *args):
        """Hand a coroutine off to the event loop, if one is set. Builds the coroutine
        lazily so nothing is created (and left unawaited) when there's no loop to run it."""
        if self._event_loop is not None:
            asyncio.run_coroutine_threadsafe(coro_factory(*args), self._event_loop)

    def _on_diagnostics_message(self, client, userdata, msg):
        decoded = self._decode_mqtt_json(msg, _TOPIC_RE)
        if decoded is None:
            return
        robot_name, payload = decoded
        diagnostics = self._parse_diagnostics(payload)
        self._schedule(self._handle_diagnostics, robot_name, payload.get("timestamp"), diagnostics)

    def _on_bt_tree_message(self, client, userdata, msg):
        decoded = self._decode_mqtt_json(msg, _BT_TREE_TOPIC_RE)
        if decoded is None:
            return
        robot_name, payload = decoded
        self._schedule(self._handle_bt_tree, robot_name, payload.get("trees"))

    def _on_bt_state_message(self, client, userdata, msg):
        decoded = self._decode_mqtt_json(msg, _BT_STATE_TOPIC_RE)
        if decoded is None:
            return
        robot_name, payload = decoded
        self._schedule(self._handle_bt_state, robot_name, payload.get("nodes"), payload.get("stamp"))

    @staticmethod
    def _parse_diagnostics(payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Extract the 4 fixed collectors by key, tolerant of any being absent (a
        failed collector read simply omits its block) or containing null values.
        """
        diagnostics = {}
        for name in COLLECTOR_NAMES:
            block = payload.get(name)
            if not isinstance(block, dict):
                continue
            diagnostics[name] = {
                "level": DiagnosticsService._derive_level(name, block),
                "values": block,
            }
        return diagnostics

    @staticmethod
    def _derive_level(name: str, block: Dict[str, Any]) -> int:
        """
        jtop/host_stats carry no explicit level in the JSON mirror: the block
        being present at all means the collector's read succeeded (OK); a failed
        read omits the block entirely. This module makes no judgment about that
        absence — downstream consumers (e.g. the client's severity rollup) may
        treat a missing block as ERROR, but that's their convention, not this
        one's. ros_health has no single level field either, so it's derived from
        its staleness/health sub-fields. topic_availability
        is keyed by topic name; per the sati_system_diagnostics contract it's
        WARN if any monitored topic is missing or not publishing, OK otherwise,
        and deliberately never ERROR (that's reserved for the collector itself
        throwing, not a topic being slow to start).
        """
        if name == "topic_availability":
            unhealthy = any(
                not isinstance(entry, dict) or not entry.get("exists") or not entry.get("publishing")
                for entry in block.values()
            )
            return LEVEL_WARN if unhealthy else LEVEL_OK

        if name != "ros_health":
            return LEVEL_OK

        stale = any(block.get(f"{source}_stale") for source in STALE_SOURCES)
        unhealthy_pose = block.get("sati_pose_is_healthy") is False
        worst = LEVEL_WARN if (stale or unhealthy_pose) else LEVEL_OK

        esp32_level = block.get("esp32_level")
        if isinstance(esp32_level, int):
            worst = max(worst, esp32_level)
        return worst

    async def _cache_and_broadcast(
        self, cache: Dict[str, Dict[str, Any]], robot_name: str, envelope: Dict[str, Any], kind: str
    ):
        cache[robot_name] = envelope
        try:
            await self.ws_manager.broadcast("robot_status", robot_name, envelope)
        except Exception as e:
            self.logger.error(f"[Diagnostics] Failed to broadcast {kind} for {robot_name}: {e}")

    async def _handle_diagnostics(self, robot_name: str, robot_timestamp: Any, diagnostics: Dict[str, Any]):
        envelope = {
            "type": "diagnostics_update",
            "robot_name": robot_name,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "robot_timestamp": robot_timestamp,
            "diagnostics": diagnostics,
        }
        await self._cache_and_broadcast(self._cache, robot_name, envelope, "diagnostics")

    async def _handle_bt_tree(self, robot_name: str, trees: Optional[Dict[str, Any]]):
        envelope = {
            "type": "nav2_bt_tree_update",
            "robot_name": robot_name,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "trees": trees,
        }
        await self._cache_and_broadcast(self._bt_tree_cache, robot_name, envelope, "BT tree")

    async def _handle_bt_state(self, robot_name: str, nodes: Optional[Any], robot_stamp: Any):
        envelope = {
            "type": "nav2_bt_state_update",
            "robot_name": robot_name,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "robot_stamp": robot_stamp,
            "nodes": nodes,
        }
        await self._cache_and_broadcast(self._bt_state_cache, robot_name, envelope, "BT state")
