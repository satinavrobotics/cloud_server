"""The robot's capture time of a topomap node, read from a robot/node_update payload.

The robot (sati_topo_mapping/topomap_node.py) does not send a dedicated capture-time field.
What it does send, and what is read here, in this order:

1. `capture_time` (reserved: a future explicit field), then top-level `stamp`, then top-level
   `timestamp` (the simulator sends an ISO 8601 UTC string there). Each may be an ISO 8601 string
   WITH an offset (`Z` accepted) or a number of epoch seconds / milliseconds / microseconds /
   nanoseconds (the unit is told by magnitude).
2. `camera_metadata[].timestamp`: integer epoch **milliseconds** of the TF stamp (map -> base_link,
   taken at the camera frame's header stamp) of the node; the earliest sane one wins. Only nodes
   that carry images have it.

NOT read: `metadata.timestamp`. It is a naive `datetime.now().isoformat()` of the robot (local
time, no offset, taken at publish) and cannot be placed on the UTC axis.

A value is sane when it is finite, parses, falls in [2020-01-01, now + MAX_FUTURE_S]. Anything else
(non-numeric, naive ISO string, year < 2020 such as ROS sim time, far future from a wrong robot
clock) is ignored and the next source tried; no sane source = no capture time (None). A robot
whose clock is merely offset (but plausible) keeps its capture times: the offset cancels out in
the trace's dt / speed, which is what the field is for. Do not use them as wall-clock proof.
"""
import datetime
import math
from typing import Any, Mapping, Optional

MIN_YEAR = 2020
MAX_FUTURE_S = 300.0
_UTC = datetime.timezone.utc
_MIN = datetime.datetime(MIN_YEAR, 1, 1, tzinfo=_UTC)

FIELD = "capture_time"
_TOP_LEVEL_KEYS = ("capture_time", "stamp", "timestamp")


def _epoch_seconds(value: float) -> float:
    """Seconds from an epoch number of unknown unit: s, ms, us or ns, by magnitude."""
    mag = abs(value)
    for limit, div in ((1e11, 1.0), (1e14, 1e3), (1e17, 1e6)):
        if mag < limit:
            return value / div
    return value / 1e9


def parse(value: Any, now: Optional[datetime.datetime] = None) -> Optional[datetime.datetime]:
    """One raw value as a sane aware UTC datetime, else None."""
    parsed = None
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            if not math.isfinite(value):
                return None
            parsed = datetime.datetime.fromtimestamp(_epoch_seconds(float(value)), _UTC)
        elif isinstance(value, str) and value.strip():
            parsed = datetime.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return None
            parsed = parsed.astimezone(_UTC)
        else:
            return None
    except (ValueError, OverflowError, OSError):
        return None
    now = now or datetime.datetime.now(_UTC)
    if parsed < _MIN or (parsed - now).total_seconds() > MAX_FUTURE_S:
        return None
    return parsed


def from_camera_metadata(entries: Any, now: Optional[datetime.datetime] = None
                         ) -> Optional[datetime.datetime]:
    """The earliest sane `timestamp` of a camera_metadata list (epoch ms), else None."""
    if not isinstance(entries, (list, tuple)):
        return None
    found = [parse(e.get("timestamp"), now) for e in entries if isinstance(e, Mapping)]
    found = [f for f in found if f is not None]
    return min(found) if found else None


def extract(payload: Mapping[str, Any], now: Optional[datetime.datetime] = None
            ) -> Optional[str]:
    """The node's capture time as ISO 8601 UTC with offset (`...+00:00`), or None."""
    for key in _TOP_LEVEL_KEYS:
        got = parse(payload.get(key), now)
        if got is not None:
            return got.isoformat()
    got = from_camera_metadata(payload.get("camera_metadata"), now)
    return got.isoformat() if got is not None else None
