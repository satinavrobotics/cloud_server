#!/usr/bin/env python3
"""
RobotAgentSession — one robot's reasoning context.

Holds the per-robot baseline state (for edge-triggered detection), the recent
insight history, and a placeholder conversation list for the future interactive
mode (Option B). A single shared FleetAgent does the actual LLM call; the session
owns the *context*, not an Anthropic client.

This is the foundation for the hierarchical design: per-robot sessions now, a
fleet coordinator on top later (it will consume sessions' insights, not raw
telemetry).

Threading ownership (must be respected — see server.py for the full table):
  - last_state:  written/read ONLY on the MQTT (paho) thread, via detect().
  - insights:    written/read ONLY on the asyncio event loop, via record() /
                 recent_insights(). The blocking LLM call (summarize) is offloaded
                 to a worker thread by the service and returns a body WITHOUT
                 touching the deque, so the deque stays event-loop-owned.
  - conversation: event loop only (unused in Phase 1).
"""

from collections import deque
from typing import Any, Deque, Dict, List, Optional

from .triggers import detect_events, FiredEvent
from .agent import FleetAgent

DEFAULT_HISTORY_SIZE = 100


class RobotAgentSession:
    """One robot's reasoning context (state baseline + insight history)."""

    def __init__(
        self,
        robot_name: str,
        agent: FleetAgent,
        battery_low_threshold: float,
        history_size: int = DEFAULT_HISTORY_SIZE,
    ):
        self.robot_name = robot_name
        self._agent = agent
        self._battery_low_threshold = battery_low_threshold

        # MQTT-thread-owned:
        self.last_state: Optional[Dict[str, Any]] = None
        # event-loop-owned:
        self.insights: Deque[Dict[str, Any]] = deque(maxlen=history_size)
        # Option B seam — per-robot conversation, created empty and unused in Phase 1.
        self.conversation: List[Dict[str, Any]] = []

    # ==================== MQTT thread ====================

    def detect(self, curr: Dict[str, Any]) -> List[FiredEvent]:
        """Run edge-triggered detection and advance the baseline. MQTT thread only."""
        events = detect_events(self.last_state, curr, self._battery_low_threshold)
        self.last_state = curr
        return events

    # ==================== Worker thread (offloaded by service) ====================

    def summarize(self, events: List[FiredEvent], curr: Dict[str, Any]) -> Dict[str, Any]:
        """
        Call the shared agent and return the insight body.

        Blocking (synchronous HTTP) — the service runs this via asyncio.to_thread.
        Deliberately does NOT append to ``insights`` (that would put a write on the
        worker thread); the service assigns the global id/timestamp and calls
        ``record()`` on the event loop.
        """
        result = self._agent.summarize(self.robot_name, events, curr)
        return {
            "robot": self.robot_name,
            "severity": result["severity"],
            "summary": result["summary"],
            "suggested_action": result.get("suggested_action"),
            "fired_events": [e.to_dict() for e in events],
            "model": result.get("model"),
            "degraded": result.get("degraded", False),
        }

    # ==================== Event loop ====================

    def record(self, insight: Dict[str, Any]) -> None:
        """Append a finished insight (with id/timestamp) to history. Event loop only."""
        self.insights.append(insight)

    def recent_insights(self, limit: int = 50) -> List[Dict[str, Any]]:
        """This robot's insights, newest first. Event loop only."""
        return list(reversed(self.insights))[:limit]

    def stats_snapshot(self) -> Dict[str, Any]:
        return {
            "robot": self.robot_name,
            "insights": len(self.insights),
            "has_state": self.last_state is not None,
        }
