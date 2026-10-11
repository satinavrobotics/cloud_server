"""Robot run-change detection (docs/satinav-maps-redesign.md §14.2, maps U3).

A robot's pose is in its current run frame (odom), which resets at every navstack start. A
session's map_T_session is only valid for the run it was placed in, so a new run unplaces the
robot's open session (mission-dispatch, server.py `_on_run_changed`).

The VDA5050 client numbers its messages per process and per topic, starting again at 0 in a new
process (sati_vda5050_client: `connection_header_id_`, `agv_state_->header_id`). So:

- `state`: a headerId LOWER than the last one seen, confirmed by the next state also being
  lower than it, means a new client process (one reordered/duplicate message is not);
- `connection` ONLINE: the client publishes ONLINE on its first connect AND on every MQTT
  reconnect (HandleMqttReconnected), with the next connection headerId. A reconnect of the same
  process therefore has a HIGHER headerId than its previous ONLINE; a new process starts again
  (its ONLINE has headerId 1: 0 went to the last will it sets before connecting). An ONLINE whose
  headerId is not above the last ONLINE's means a new process, unless the broker delivered it
  from its retained store (the dispatcher's own reconnect) with the same headerId. The broker-sent last will
  (CONNECTIONBROKEN, headerId 0 of the dead process) and OFFLINE are ignored: a network blip
  also produces the will, and the process (and its odom frame) may well survive it.

After one signal both baselines are forgotten, so the other topic's first message of the new
process (state headerId 0 after the new ONLINE, or the new ONLINE after a decreasing state) does
not count the same restart twice. After a dispatcher restart nothing is known: the first message
of each kind only sets the baseline (a restart missed while dispatch was down is not detected;
Limits in §14.2: an odom reset inside a running navstack is missed too, and a restart of only the
VDA5050 client is a false positive that costs one placement).
"""

import dataclasses
from typing import Any, Dict, Optional, Tuple

ONLINE = "ONLINE"


@dataclasses.dataclass
class RunChangeDetector:
    """Per robot; feed it every `connection` and `state` message. Pure (no I/O)."""
    last_online_header: Optional[int] = None
    last_state_header: Optional[int] = None
    # A state headerId below the last one, awaiting confirmation: (new id, the old last id).
    pending_state_drop: Optional[Tuple[int, int]] = None

    def on_connection(self, state: Any, header_id: Any,
                      retained: bool = False) -> Optional[Dict[str, Any]]:
        """The evidence of a new robot run, or None. `retained`: the broker delivered the
        message from its retained store (a new subscription: the dispatcher's own MQTT
        reconnect), not as a live publish. The robot's connection message is retained, so
        such a delivery repeats the last ONLINE as it was, with the same headerId: that is
        not a restart (a live equal ONLINE is: a restarted client can repeat headerId 1)."""
        state = getattr(state, "value", state)
        if state != ONLINE:
            return None
        try:
            hid = int(header_id)
        except (TypeError, ValueError):
            return None
        last, self.last_online_header = self.last_online_header, hid
        if last is None or hid > last or (retained and hid == last):
            return None
        self.last_state_header = None
        self.pending_state_drop = None
        return {"signal": "connection_online", "connection_header_id": hid,
                "last_connection_header_id": last}

    def on_state(self, header_id: Any) -> Optional[Dict[str, Any]]:
        """The evidence of a new robot run, or None. A drop is only a run change once the
        NEXT state is below the old last id too (a new process counts up from 0, so its
        second message is still below any old id of 2 or more); one reordered or redelivered
        older message is followed by the old sequence continuing and is dropped."""
        try:
            hid = int(header_id)
        except (TypeError, ValueError):
            return None
        pending, self.pending_state_drop = self.pending_state_drop, None
        if pending is not None:
            first, old_last = pending
            if hid < old_last:
                self.last_state_header = hid
                self.last_online_header = None
                return {"signal": "state_header_reset", "state_header_id": first,
                        "last_state_header_id": old_last}
            # The old sequence went on: the drop was a stray message.
            self.last_state_header = max(old_last, hid)
            return None
        last = self.last_state_header
        if last is None or hid >= last:
            self.last_state_header = hid
            return None
        self.pending_state_drop = (hid, last)
        self.last_state_header = last   # the baseline stays the old run's until confirmed
        return None


@dataclasses.dataclass
class FrameWatcher:
    """Per robot; debounces the frame witness check (map_sessions.frame_verdict): a placed
    session whose frame witness differs from the robot's DEFINITIVE reading is unplaced only
    after the mismatch was seen in `confirm_states` consecutive definitive states AND for
    `confirm_s` seconds, with the same (session, expected, observed). A matching state resets it;
    an `unknown` state (no definitive reading, SLAM save under way, no witness) neither confirms
    nor resets it. Pure (the caller passes the monotonic clock and the wall time)."""
    confirm_s: float = 30.0
    confirm_states: int = 3
    key: Optional[Tuple[Any, Any, Any]] = None
    first_seen: Optional[float] = None
    first_seen_at: Optional[str] = None
    states: int = 0

    def reset(self) -> None:
        self.key = self.first_seen = self.first_seen_at = None
        self.states = 0

    def on_verdict(self, verdict: str, session_id: Any, expected: Any, observed: Any,
                   now: float, wall: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """The evidence {expected_map_id, observed_map_id, first_seen_at, states} of a confirmed
        mismatch (the watcher is reset), else None."""
        if verdict == "unknown":
            return None
        if verdict != "mismatch":
            self.reset()
            return None
        key = (str(session_id), expected, observed)
        if key != self.key:
            self.key, self.first_seen, self.first_seen_at, self.states = key, now, wall, 0
        self.states += 1
        if self.states < self.confirm_states or now - self.first_seen < self.confirm_s:
            return None
        evidence = {"expected_map_id": expected, "observed_map_id": observed,
                    "first_seen_at": self.first_seen_at, "states": self.states}
        self.reset()
        return evidence
