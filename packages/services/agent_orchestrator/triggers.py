#!/usr/bin/env python3
"""
Trigger detection for the Agent Orchestrator.

Pure, dependency-free functions that decide WHICH VDA5050 state messages are
worth handing to the LLM. State messages are high-frequency telemetry, so the
orchestrator must only react to *meaningful changes* — this module is that
filter.

Detection is edge-triggered: it compares the previous state snapshot for a
robot against the current one and returns only the conditions that newly
appeared. A persistent error / e-stop / low battery fires once, not on every
subsequent message.

Kept free of any third-party imports so it can be unit-tested without the
Anthropic SDK, paho-mqtt, or a broker.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional


# Severity ordering (low -> high). Used to roll several fired events up into a
# single insight severity.
SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}

DEFAULT_BATTERY_LOW_THRESHOLD = 20.0


@dataclass
class FiredEvent:
    """A single notable condition detected on a robot's state."""
    type: str          # machine-readable event kind, e.g. "new_error"
    severity: str      # "info" | "warning" | "critical"
    detail: str        # short human-readable description

    def to_dict(self) -> Dict[str, str]:
        return asdict(self)


def max_severity(events: List[FiredEvent]) -> str:
    """Return the highest severity among ``events`` ("info" if empty)."""
    if not events:
        return "info"
    return max(events, key=lambda e: SEVERITY_ORDER.get(e.severity, 0)).severity


# ---------------------------------------------------------------------------
# Field accessors (defensive — VDA5050 payloads are decoded JSON dicts)
# ---------------------------------------------------------------------------

def _errors(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    errors = state.get("errors")
    return errors if isinstance(errors, list) else []


def _error_key(err: Dict[str, Any]) -> tuple:
    """Stable identity for an error so we can tell new ones from carried-over."""
    return (
        err.get("errorType", ""),
        err.get("errorLevel", ""),
        err.get("errorDescription", ""),
    )


def _edge_blocked_detail(err: Dict[str, Any]) -> str:
    """Human-readable detail for an edgeBlocked error, including node/edge refs."""
    desc = err.get("errorDescription") or "Edge blocked"
    node = edge = None
    refs = err.get("errorReferences")
    if isinstance(refs, list):
        for r in refs:
            if not isinstance(r, dict):
                continue
            key = r.get("referenceKey")
            if key in ("nodeId", "node_id"):
                node = r.get("referenceValue")
            elif key in ("edgeId", "edge_id"):
                edge = r.get("referenceValue")
    extra = []
    if node:
        extra.append(f"node {node}")
    if edge:
        extra.append(f"edge {edge}")
    return f"{desc} ({', '.join(extra)})" if extra else desc


def _failed_action_ids(state: Dict[str, Any]) -> set:
    out = set()
    for a in state.get("actionStates", []) or []:
        if isinstance(a, dict) and a.get("actionStatus") == "FAILED":
            aid = a.get("actionId")
            if aid is not None:
                out.add(aid)
    return out


def _safety_flags(state: Dict[str, Any]) -> tuple:
    """Return (estop_engaged, field_violation) as a comparable tuple."""
    ss = state.get("safetyState") or {}
    if not isinstance(ss, dict):
        return (False, False)
    estop = ss.get("eStop")
    estop_engaged = estop is not None and str(estop).upper() != "NONE"
    field_violation = bool(ss.get("fieldViolation", False))
    return (estop_engaged, field_violation)


def _battery(state: Dict[str, Any]) -> Optional[float]:
    bs = state.get("batteryState") or {}
    if not isinstance(bs, dict):
        return None
    charge = bs.get("batteryCharge")
    try:
        return float(charge)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def detect_events(
    prev: Optional[Dict[str, Any]],
    curr: Dict[str, Any],
    battery_low_threshold: float = DEFAULT_BATTERY_LOW_THRESHOLD,
) -> List[FiredEvent]:
    """
    Compare two VDA5050 ``state`` payloads and return the notable changes.

    ``prev`` is ``None`` the first time a robot is seen. In that case any
    *standing* bad condition (active error, engaged e-stop, failed action, low
    battery) fires once so a robot that boots into a bad state is surfaced.
    Operating-mode changes are skipped on first sight (no baseline to compare).

    Returns an empty list for quiet telemetry — that is the common case and the
    whole point: no LLM call is made unless this returns something.
    """
    events: List[FiredEvent] = []
    first_sight = prev is None
    prev = prev or {}

    # --- New errors -------------------------------------------------------
    prev_error_keys = {_error_key(e) for e in _errors(prev)}
    for err in _errors(curr):
        key = _error_key(err)
        if key in prev_error_keys:
            continue
        # edgeBlocked is a distinct, recoverable condition: the robot stopped at an
        # impassable edge and is waiting IDLE for a new route. Surface it as its own
        # event type (not a generic error) so the agent can recommend a reroute.
        if str(err.get("errorType", "")) == "edgeBlocked":
            events.append(FiredEvent("edge_blocked", "warning", _edge_blocked_detail(err)))
            continue
        level = str(err.get("errorLevel", "")).upper()
        severity = "critical" if level == "FATAL" else "warning"
        desc = err.get("errorDescription") or err.get("errorType") or "unknown error"
        events.append(FiredEvent("new_error", severity, f"{level or 'ERROR'}: {desc}"))

    # --- Safety state (e-stop / field violation) --------------------------
    prev_estop, prev_field = _safety_flags(prev)
    curr_estop, curr_field = _safety_flags(curr)
    if curr_estop and not prev_estop:
        events.append(FiredEvent("estop_engaged", "critical", "Emergency stop engaged"))
    if curr_field and not prev_field:
        events.append(FiredEvent("field_violation", "critical", "Safety field violation"))

    # --- Operating mode change (needs a baseline) -------------------------
    if not first_sight:
        prev_mode = prev.get("operatingMode")
        curr_mode = curr.get("operatingMode")
        if curr_mode is not None and prev_mode is not None and curr_mode != prev_mode:
            events.append(FiredEvent(
                "operating_mode_change", "warning",
                f"Operating mode {prev_mode} -> {curr_mode}",
            ))

    # --- Newly failed actions --------------------------------------------
    new_failures = _failed_action_ids(curr) - _failed_action_ids(prev)
    for aid in sorted(new_failures, key=str):
        events.append(FiredEvent("action_failed", "critical", f"Action '{aid}' FAILED"))

    # --- Battery low (downward crossing) ----------------------------------
    curr_batt = _battery(curr)
    if curr_batt is not None and curr_batt <= battery_low_threshold:
        prev_batt = _battery(prev)
        # Fire only when crossing the threshold downward, or on first sight.
        if prev_batt is None or prev_batt > battery_low_threshold:
            events.append(FiredEvent(
                "battery_low", "warning",
                f"Battery at {curr_batt:.0f}% (<= {battery_low_threshold:.0f}%)",
            ))

    return events
