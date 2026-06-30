#!/usr/bin/env python3
"""
Agent Orchestrator Service.

Subscribes to the VDA5050 robot `state` MQTT stream (the same stream the mission
controller consumes) and routes each robot's messages to a per-robot
``RobotAgentSession``. A session filters its robot's telemetry down to notable
events and asks the shared fleet agent to summarize them. Insights are kept in
each session's history, broadcast to WebSocket subscribers, and logged.

This service is the ingestion router + cross-cutting concerns (global insight id,
fan-out, coordinator seam, counters). Per-robot reasoning context lives in the
sessions. One process, one MQTT subscription, one shared LLM client.

Threading-ownership model (do not violate):

  | State                  | Written by        | Read by                  |
  |------------------------|-------------------|--------------------------|
  | sessions (dict insert) | MQTT thread (lazy)| both (one-time atomic)   |
  | session.last_state     | MQTT thread only  | MQTT thread only         |
  | session.insights       | event loop only   | event loop (FastAPI)     |
  | _insight_seq           | event loop only   | event loop only          |

The blocking LLM call is offloaded with asyncio.to_thread; the insight append
happens back on the event loop, so session.insights stays event-loop-owned.
"""

import re
import json
import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from packages.utils.mqtt_client import MQTTClient
from packages.config import MQTT_KEEPALIVE

from .session import RobotAgentSession
from .agent import FleetAgent

# Per-robot insight history depth (one session each).
SESSION_HISTORY_SIZE = 100


class InsightPublisher:
    """
    Fan-out of new insights to WebSocket clients.

    Two channel kinds: a global feed (``robot=None``) that receives every
    insight, and per-robot channels that receive only their robot's insights.
    Modeled on graph_builder's UpdatePublisher.
    """

    def __init__(self):
        self.logger = logging.getLogger("agent_orchestrator.publisher")
        self.global_subs: Set[asyncio.Queue] = set()
        self.robot_subs: Dict[str, Set[asyncio.Queue]] = {}

    def subscribe(self, queue: asyncio.Queue, robot: Optional[str] = None):
        if robot is None:
            self.global_subs.add(queue)
        else:
            self.robot_subs.setdefault(robot, set()).add(queue)
        self.logger.info("Subscriber added (robot=%s)", robot or "*")

    def unsubscribe(self, queue: asyncio.Queue, robot: Optional[str] = None):
        if robot is None:
            self.global_subs.discard(queue)
        else:
            subs = self.robot_subs.get(robot)
            if subs:
                subs.discard(queue)
                if not subs:
                    del self.robot_subs[robot]

    async def publish(self, insight: Dict[str, Any]):
        targets = set(self.global_subs)
        targets |= self.robot_subs.get(insight["robot"], set())
        for queue in list(targets):
            try:
                await queue.put(insight)
            except Exception as e:
                self.logger.error("Failed to publish to subscriber: %s", e)
                self.global_subs.discard(queue)
                self.robot_subs.get(insight["robot"], set()).discard(queue)


class AgentOrchestratorService:
    """MQTT state listener -> per-robot session -> LLM summary -> insights."""

    def __init__(
        self,
        mqtt_host: str,
        mqtt_port: int,
        vda5050_prefix: str,
        api_key: Optional[str],
        model: str,
        battery_low_threshold: float,
        base_url: Optional[str] = None,
    ):
        self.logger = logging.getLogger("agent_orchestrator.service")
        self.mqtt_host = mqtt_host
        self.mqtt_port = mqtt_port
        self.vda5050_prefix = vda5050_prefix
        self.battery_low_threshold = battery_low_threshold

        self.state_topic = f"{vda5050_prefix}/+/state"
        self._state_re = re.compile(rf"{re.escape(vda5050_prefix)}/(.+)/state")

        self.agent = FleetAgent(api_key=api_key, model=model, base_url=base_url)
        self.publisher = InsightPublisher()

        self.mqtt_client: Optional[MQTTClient] = None
        self._event_loop: Optional[asyncio.AbstractEventLoop] = None

        # Per-robot reasoning context, lazily created, keyed by robot name.
        self.sessions: Dict[str, RobotAgentSession] = {}
        # Global monotonic insight id for stable cross-robot ordering.
        self._insight_seq = 0

        self.stats = {
            "messages_seen": 0,
            "events_fired": 0,
            "insights_generated": 0,
            "llm_calls": 0,
            "llm_errors": 0,
        }

    # ==================== Lifecycle ====================

    def set_event_loop(self, loop: asyncio.AbstractEventLoop):
        self._event_loop = loop

    def connect_mqtt(self) -> bool:
        try:
            client_id = f"agent_orchestrator_{datetime.now().timestamp()}"
            self.mqtt_client = MQTTClient(
                client_id=client_id,
                broker=self.mqtt_host,
                port=self.mqtt_port,
                keepalive=MQTT_KEEPALIVE,
            )
            self.mqtt_client.register_callback(self.state_topic, self._on_state_message)
            self.mqtt_client.connect()
            self.logger.info("Subscribed to VDA5050 state stream: %s", self.state_topic)
            return True
        except Exception as e:
            self.logger.error("Failed to set up MQTT client: %s", e)
            return False

    def disconnect_mqtt(self):
        if self.mqtt_client:
            self.mqtt_client.disconnect()
            self.logger.info("Disconnected from MQTT broker")

    # ==================== Session registry ====================

    def _session(self, robot_name: str) -> RobotAgentSession:
        """Get-or-create the session for a robot. Called on the MQTT thread."""
        session = self.sessions.get(robot_name)
        if session is None:
            session = RobotAgentSession(
                robot_name=robot_name,
                agent=self.agent,
                battery_low_threshold=self.battery_low_threshold,
                history_size=SESSION_HISTORY_SIZE,
            )
            self.sessions[robot_name] = session
        return session

    # ==================== MQTT handling (paho thread) ====================

    def _on_state_message(self, client, userdata, msg):
        """Route the message to its robot's session and schedule work on events."""
        self.stats["messages_seen"] += 1
        try:
            match = self._state_re.match(msg.topic)
            if not match:
                return
            robot_name = match.group(1)
            curr = json.loads(msg.payload.decode("utf-8"))
        except Exception as e:
            self.logger.error("Failed to decode state message on %s: %s", msg.topic, e)
            return

        session = self._session(robot_name)
        events = session.detect(curr)  # updates the session baseline (MQTT thread)
        if not events:
            return  # quiet telemetry — the common case, no LLM call

        self.stats["events_fired"] += len(events)
        if self._event_loop is not None:
            asyncio.run_coroutine_threadsafe(
                self._handle_events(session, events, curr),
                self._event_loop,
            )

    # ==================== Async handling (event loop) ====================

    async def _handle_events(self, session: RobotAgentSession, events, curr: Dict[str, Any]):
        try:
            self.stats["llm_calls"] += 1
            # Offload the blocking LLM call; the body comes back without an id.
            body = await asyncio.to_thread(session.summarize, events, curr)
            if body.get("degraded") and body.get("model") is not None:
                # model set + degraded == the LLM call was attempted and raised
                self.stats["llm_errors"] += 1

            self._insight_seq += 1
            insight = {
                "id": self._insight_seq,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                **body,
            }
            session.record(insight)  # event-loop-owned append
            self.stats["insights_generated"] += 1
            self.logger.info(
                "[%s] %s: %s", insight["severity"], session.robot_name, insight["summary"]
            )
            await self.publisher.publish(insight)
            self._notify_coordinator(insight)
        except Exception as e:
            self.logger.error("Failed to handle events for %s: %s", session.robot_name, e)

    def _notify_coordinator(self, insight: Dict[str, Any]) -> None:
        """
        Seam for the fleet coordinator (future).

        The coordinator will consume per-robot insights to make cross-robot
        decisions (charging contention, traffic/deadlock, escalation priority,
        reassigning a mission off a dead robot). No-op in Phase 1.
        """
        return

    # ==================== Read API (FastAPI handlers, event loop) ====================

    def get_insights(self, robot: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        """Recent insights, newest first. Per-robot from its session, else merged."""
        if robot:
            session = self.sessions.get(robot)
            return session.recent_insights(limit) if session else []
        merged = [i for s in self.sessions.values() for i in s.insights]
        merged.sort(key=lambda i: i["id"], reverse=True)
        return merged[:limit]

    def get_stats(self) -> Dict[str, Any]:
        return {
            **self.stats,
            "robots_tracked": len(self.sessions),
            "insights_buffered": sum(len(s.insights) for s in self.sessions.values()),
            "mqtt_connected": bool(self.mqtt_client and self.mqtt_client.connected),
            "degraded": self.agent.degraded,
            "model": self.agent.model,
        }

    def get_health_details(self) -> Dict[str, Any]:
        return {
            "mqtt_connected": bool(self.mqtt_client and self.mqtt_client.connected),
            "agent_ready": not self.agent.degraded,
        }
