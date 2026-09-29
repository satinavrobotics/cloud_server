"""Robot run-change detection (docs/satinav-maps-redesign.md §14.2, maps U3).

A robot's pose is in its current run frame (odom), which resets at every navstack start. A
session's map_T_session is only valid for the run it was placed in, so a new run unplaces the
robot's open session (mission-dispatch, server.py `_on_run_changed`).

The VDA5050 client numbers its messages per process and per topic, starting again at 0 in a new
process (sati_vda5050_client: `connection_header_id_`, `agv_state_->header_id`). So:

- `state`: a headerId LOWER than the last one seen means a new client process;
- `connection` ONLINE: the client publishes ONLINE on its first connect AND on every MQTT
  reconnect (HandleMqttReconnected), with the next connection headerId. A reconnect of the same
  process therefore has a HIGHER headerId than its previous ONLINE; a new process starts again
  (its ONLINE has headerId 1: 0 went to the last will it sets before connecting). An ONLINE whose
  headerId is not above the last ONLINE's means a new process. The broker-sent last will
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
from typing import Any, Dict, Optional

ONLINE = "ONLINE"


@dataclasses.dataclass
class RunChangeDetector:
    """Per robot; feed it every `connection` and `state` message. Pure (no I/O)."""
    last_online_header: Optional[int] = None
    last_state_header: Optional[int] = None

    def on_connection(self, state: Any, header_id: Any) -> Optional[Dict[str, Any]]:
        """The evidence of a new robot run, or None."""
        state = getattr(state, "value", state)
        if state != ONLINE:
            return None
        try:
            hid = int(header_id)
        except (TypeError, ValueError):
            return None
        last, self.last_online_header = self.last_online_header, hid
        if last is None or hid > last:
            return None
        self.last_state_header = None
        return {"signal": "connection_online", "connection_header_id": hid,
                "last_connection_header_id": last}

    def on_state(self, header_id: Any) -> Optional[Dict[str, Any]]:
        """The evidence of a new robot run, or None."""
        try:
            hid = int(header_id)
        except (TypeError, ValueError):
            return None
        last, self.last_state_header = self.last_state_header, hid
        if last is None or hid >= last:
            return None
        self.last_online_header = None
        return {"signal": "state_header_reset", "state_header_id": hid,
                "last_state_header_id": last}
