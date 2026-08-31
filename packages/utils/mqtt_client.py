import logging
import asyncio
import threading
import time
import json
from typing import Callable, Dict, Any, Optional
import paho.mqtt.client as mqtt_client

# How long the client can go without a broker connection before the watchdog
# forces a reconnect attempt. Covers cases where paho's built-in automatic
# reconnection silently stalls (observed with the websockets transport).
_RECONNECT_WATCHDOG_INTERVAL_SEC = 15
_RECONNECT_STALL_THRESHOLD_SEC = 30

class MQTTClient:
    """
    A robust, shared wrapper around paho.mqtt.client.Client.
    Provides standard connection setup, automatic reconnection, and
    subscription routing via callbacks.
    """

    def __init__(
        self,
        client_id: str,
        broker: str = "localhost",
        port: int = 1883,
        keepalive: int = 60,
        username: Optional[str] = None,
        password: Optional[str] = None,
        transport: str = "tcp",
        ws_path: Optional[str] = None
    ):
        self.logger = logging.getLogger(f"MQTTClient_{client_id}")
        self.broker = broker
        self.port = port
        self.keepalive = keepalive

        self._connected = False
        self._disconnected_since: Optional[float] = None
        self._callbacks: Dict[str, Callable[[Any, Any, mqtt_client.MQTTMessage], None]] = {}

        self.client = mqtt_client.Client(client_id=client_id, transport=transport, protocol=mqtt_client.MQTTv311)
        # Surface paho's own connection/reconnection logging, which is otherwise
        # silent and was the reason a stalled reconnect went unnoticed.
        self.client.enable_logger(self.logger)

        if username and password:
            self.client.username_pw_set(username=username, password=password)

        if transport == "websockets" and ws_path:
            self.client.ws_set_options(path=ws_path)

        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

        self._watchdog_thread = threading.Thread(
            target=self._reconnect_watchdog, daemon=True)

    @property
    def connected(self) -> bool:
        """Returns True if the client is currently connected to the broker."""
        return self._connected

    def register_callback(self, topic: str, callback: Callable[[Any, Any, mqtt_client.MQTTMessage], None], qos: int = 0):
        """Register a callback for a specific topic."""
        self._callbacks[topic] = callback
        if self._connected:
            self.client.subscribe(topic, qos=qos)
            self.logger.info(f"Subscribed to topic: {topic}")

    def connect(self):
        """Connect to the MQTT broker and start the background thread loop."""
        try:
            self.logger.info(f"Connecting to MQTT broker at {self.broker}:{self.port}...")
            # connect() handles the initial connection blocking call
            self.client.connect(self.broker, self.port, self.keepalive)
            # loop_start() spins up a background thread that automatically handles reconnects
            self.client.loop_start()
            if not self._watchdog_thread.is_alive():
                self._watchdog_thread.start()
        except Exception as e:
            self.logger.error(f"Failed to initiate MQTT connection: {e}")

    def _reconnect_watchdog(self):
        """
        Backstop for paho's automatic reconnection, which has been observed to
        silently stall (e.g. over the websockets transport) leaving the client
        disconnected indefinitely with no further log output. Forces an explicit
        reconnect attempt if the client has been down too long.
        """
        while True:
            time.sleep(_RECONNECT_WATCHDOG_INTERVAL_SEC)
            if self._connected:
                continue
            since = self._disconnected_since
            if since is not None and (time.monotonic() - since) >= _RECONNECT_STALL_THRESHOLD_SEC:
                self.logger.warning(
                    f"MQTT client has been disconnected from {self.broker}:{self.port} for "
                    f"over {_RECONNECT_STALL_THRESHOLD_SEC}s; forcing reconnect attempt.")
                try:
                    self.client.reconnect()
                except Exception as e:
                    self.logger.error(f"Forced MQTT reconnect attempt failed: {e}")

    def publish(self, topic: str, payload: str, qos: int = 0, retain: bool = False):
        """Publish a message to a topic."""
        result = self.client.publish(topic, payload, qos=qos, retain=retain)
        if result.rc != mqtt_client.MQTT_ERR_SUCCESS:
            self.logger.warning(f"Publish to {topic} returned rc={result.rc}")
        return result

    def disconnect(self):
        """Disconnect and stop the background loop."""
        self.client.loop_stop()
        self.client.disconnect()

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self._connected = True
            self._disconnected_since = None
            self.logger.info(f"Successfully connected to MQTT broker at {self.broker}:{self.port}")
            # Re-subscribe to all registered topics upon connection/reconnection
            for topic in self._callbacks.keys():
                client.subscribe(topic)
                self.logger.info(f"Subscribed to topic: {topic}")
        else:
            self._connected = False
            self._disconnected_since = self._disconnected_since or time.monotonic()
            self.logger.error(f"MQTT connection failed with code {rc}")

    def _on_disconnect(self, client, userdata, rc):
        self._connected = False
        self._disconnected_since = time.monotonic()
        if rc != 0:
            self.logger.warning(f"Unexpected disconnect from MQTT broker (code: {rc})")
        else:
            self.logger.info("Disconnected from MQTT broker")

    def _on_message(self, client, userdata, msg):
        """Route incoming messages to registered callbacks."""
        # Find exact match
        if msg.topic in self._callbacks:
            try:
                self._callbacks[msg.topic](client, userdata, msg)
            except Exception as e:
                self.logger.error(f"Error executing callback for topic {msg.topic}: {e}")
            return

        # Handle MQTT wildcard pattern matches (+)
        for registered_topic, callback in self._callbacks.items():
            if self._topic_matches(registered_topic, msg.topic):
                try:
                    callback(client, userdata, msg)
                except Exception as e:
                    self.logger.error(f"Error executing callback for pattern {registered_topic} on topic {msg.topic}: {e}")
                return

        self.logger.debug(f"Received message on topic without specific callback: {msg.topic}")

    def _topic_matches(self, pattern: str, topic: str) -> bool:
        """
        Check if an incoming topic matches a subscribed pattern.
        Supports single-level (+) wildcards. Multi-level (#) wildcards are not fully supported yet.
        """
        pattern_parts = pattern.split('/')
        topic_parts = topic.split('/')

        if len(pattern_parts) != len(topic_parts):
            # Check for multi-level wildcard at the end
            if pattern_parts and pattern_parts[-1] == '#':
                pattern_parts = pattern_parts[:-1]
                if len(topic_parts) < len(pattern_parts):
                    return False
            else:
                return False

        for p_part, t_part in zip(pattern_parts, topic_parts):
            if p_part == '+':
                continue
            if p_part != t_part:
                return False
        return True
