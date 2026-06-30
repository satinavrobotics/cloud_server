#!/usr/bin/env python3
"""
Fleet triage agent (Phase 1 — summarize).

Wraps the Anthropic SDK. Given a robot's fired events plus its current VDA5050
state, it asks Claude for a plain-language summary, a severity, and a suggested
(advisory) action. Phase 1 takes no real actions — `suggested_action` is text.
Phase 2 will replace the single `messages.create` call with the tool runner and
a gated action surface.

Degraded mode: if no API key is configured the agent still returns a
deterministic summary built from the trigger output, so the service boots and
behaves sensibly in dev without a key.
"""

import json
import logging
from typing import Any, Dict, List, Optional

from .triggers import FiredEvent, max_severity

logger = logging.getLogger("agent_orchestrator.agent")

SYSTEM_PROMPT = """You are the triage agent for an autonomous mobile robot fleet.

Robots report status over the VDA5050 protocol. You are given the notable
state changes that just fired for one robot, plus a compact snapshot of that
robot's current VDA5050 state. Your job is to explain, in one or two plain
sentences an operator can act on, what is happening and why it matters.

VDA5050 quick reference:
- errors[].errorLevel: WARNING (degraded) or FATAL (stopped).
- An error with errorType "edgeBlocked" (WARNING) means a path edge is impassable
  after the robot's local retries; the robot has stopped and is waiting IDLE for a
  new route. It will not reroute itself. The operator should send a new order/route
  that avoids the blocked edge (referenced by errorReferences nodeId/edgeId).
- safetyState.eStop != "NONE" means emergency stop engaged; fieldViolation
  means the robot entered a protective field.
- operatingMode: AUTOMATIC is normal; MANUAL/SERVICE/TEACHIN mean a human or
  maintenance has taken over.
- actionStates[].actionStatus FAILED means a mission step failed.
- batteryState.batteryCharge is state-of-charge in percent.
- navReasoning, when present, is the robot's own plain-language narration of what
  it is currently doing (e.g. heading to / aborting / retrying a waypoint). Use it
  as context to explain *why* a robot stalled; it is advisory, not an error.

Respond with ONLY a JSON object, no prose around it, with exactly these keys:
{
  "severity": "info" | "warning" | "critical",
  "summary": "<one or two sentences>",
  "suggested_action": "<short next step for the operator, or null>"
}
Base severity on the worst fired event. Be concise and specific; name the
robot and the concrete condition. Do not invent data not present in the input.
"""

# Fields worth sending to the model — keeps the prompt small and cache-friendly.
_RELEVANT_FIELDS = (
    "orderId", "lastNodeId", "operatingMode", "paused", "driving",
    "batteryState", "safetyState", "errors", "actionStates", "agvPosition",
)


def _latest_nav_reasoning(state: Dict[str, Any]) -> Optional[str]:
    """Latest navReasoning narration line from the VDA5050 information[] array.

    The robot narrates what it is doing (e.g. "Waypoint 3 aborted - local retry
    2/2") here. It is advisory context, not a trigger, but it explains *why* a
    robot stalled, which is exactly what the agent needs to recommend a reroute.
    """
    info = state.get("information")
    if not isinstance(info, list):
        return None
    line = None
    for entry in info:
        if isinstance(entry, dict) and entry.get("infoType") == "navReasoning":
            line = entry.get("infoDescription")  # last one wins
    return line if isinstance(line, str) and line else None


def _compact_state(state: Dict[str, Any]) -> Dict[str, Any]:
    compact = {k: state[k] for k in _RELEVANT_FIELDS if k in state}
    nav_reasoning = _latest_nav_reasoning(state)
    if nav_reasoning is not None:
        compact["navReasoning"] = nav_reasoning
    return compact


def _first_balanced_object(text: str) -> Optional[Dict[str, Any]]:
    """Return the first complete, string-aware balanced {...} object, or None."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _parse_json_object(text: str) -> Optional[Dict[str, Any]]:
    """
    Extract the first JSON object from model text, defensively.

    Models (especially non-Claude ones behind the proxy) wrap the object in
    ```json fences or add prose around it. We scan for the first *balanced*
    object so trailing prose — including stray braces — doesn't poison the parse,
    then fall back to a greedy first-{ ... last-} slice.
    """
    if not text:
        return None
    obj = _first_balanced_object(text)
    if obj is not None:
        return obj
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


class FleetAgent:
    """Summarizes a robot's notable state changes via Claude."""

    def __init__(self, api_key: Optional[str], model: str, base_url: Optional[str] = None):
        self.model = model
        self._client = None
        if api_key:
            try:
                import anthropic  # imported lazily so tests/dev don't require it
                # base_url lets us target an Anthropic-compatible proxy (e.g. a
                # local LiteLLM proxy fronting a free model). Unset == real API.
                kwargs = {"api_key": api_key}
                if base_url:
                    kwargs["base_url"] = base_url
                self._client = anthropic.Anthropic(**kwargs)
                if base_url:
                    logger.info("Anthropic client pointed at base_url=%s", base_url)
            except Exception as e:  # pragma: no cover - import/setup failure
                logger.warning("Anthropic client unavailable, running degraded: %s", e)
        else:
            logger.warning(
                "ANTHROPIC_API_KEY not set — agent running in degraded (non-LLM) mode"
            )

    @property
    def degraded(self) -> bool:
        return self._client is None

    def summarize(
        self,
        robot_name: str,
        events: List[FiredEvent],
        curr_state: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Return {severity, summary, suggested_action, model, degraded}.

        Blocking (synchronous HTTP). The server offloads this to a thread so it
        never blocks the event loop.
        """
        floor = max_severity(events)
        if self.degraded:
            return self._degraded_summary(robot_name, events, floor)

        event_lines = "\n".join(f"- [{e.severity}] {e.type}: {e.detail}" for e in events)
        user_text = (
            f"Robot: {robot_name}\n\n"
            f"Fired events:\n{event_lines}\n\n"
            f"Current state (relevant fields):\n"
            f"{json.dumps(_compact_state(curr_state), default=str)}"
        )

        try:
            resp = self._client.messages.create(
                model=self.model,
                max_tokens=1024,
                system=[{
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{"role": "user", "content": user_text}],
            )
            text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
            data = _parse_json_object(text) or {}
            severity = self._clamp_severity(data.get("severity"), floor)
            summary = str(data.get("summary") or "").strip()
            if not summary:
                summary = self._fallback_summary(robot_name, events)
            return {
                "severity": severity,
                "summary": summary,
                "suggested_action": data.get("suggested_action") or None,
                "model": self.model,
                "degraded": False,
            }
        except Exception as e:
            logger.error("LLM summarization failed for %s: %s", robot_name, e)
            return self._degraded_summary(robot_name, events, floor, llm_error=True)

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _clamp_severity(model_severity: Any, floor: str) -> str:
        from .triggers import SEVERITY_ORDER
        ms = str(model_severity).lower() if model_severity else floor
        if ms not in SEVERITY_ORDER:
            ms = floor
        # Never report below the worst fired event.
        return ms if SEVERITY_ORDER[ms] >= SEVERITY_ORDER[floor] else floor

    @staticmethod
    def _fallback_summary(robot_name: str, events: List[FiredEvent]) -> str:
        details = "; ".join(e.detail for e in events) or "state change"
        return f"{robot_name}: {details}."

    def _degraded_summary(
        self,
        robot_name: str,
        events: List[FiredEvent],
        floor: str,
        llm_error: bool = False,
    ) -> Dict[str, Any]:
        return {
            "severity": floor,
            "summary": self._fallback_summary(robot_name, events),
            "suggested_action": None,
            "model": self.model if llm_error else None,
            "degraded": True,
        }
