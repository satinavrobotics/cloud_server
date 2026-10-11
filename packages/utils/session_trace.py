"""Pure helpers for the trace of a mapping session (GET /api/v1/maps/{id}/sessions/{sid}/trace).

The nodes of a session in the order they were laid down, the per-step derived values between
consecutive nodes and a summary. Nothing here touches a database; see packages/utils/run_legs.py
for the same structure.

A node has two times. `time` is the one graph-builder stores (`timestamp`, else `created_at`): a
naive `datetime.now().isoformat()` of the graph-builder host when it ingested the node, i.e. the
cloud receipt time; `parse_time` reads it as local time of this host when it carries no offset
(the API and graph-builder run on the same host). `capture` (optional) is the robot's capture time
(packages/utils/capture_time.py). A step uses `capture` when BOTH its nodes have one, else the
receipt times; a session's `timestamp_source` says which nodes have a capture time.
"""
import datetime
import math
import statistics
from typing import Any, Dict, List, Mapping, Optional, Sequence


def parse_time(value: Any) -> Optional[datetime.datetime]:
    """A stored time as an aware datetime; None when it is missing or not an ISO string."""
    if isinstance(value, datetime.datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo else parsed.astimezone()


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _round(value: Optional[float], digits: int) -> Optional[float]:
    return None if value is None else round(value, digits)


SOURCE_CAPTURE, SOURCE_INGEST, SOURCE_MIXED = "robot_capture", "cloud_ingest", "mixed"


def timestamp_source(nodes: Sequence[Mapping[str, Any]]) -> str:
    """"robot_capture" when every node has a capture time (and there is at least one),
    "cloud_ingest" when none has (also an empty session), else "mixed"."""
    have = sum(1 for n in nodes if n.get("capture") is not None)
    if nodes and have == len(nodes):
        return SOURCE_CAPTURE
    return SOURCE_INGEST if have == 0 else SOURCE_MIXED


def order(nodes: Sequence[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    """The nodes in laying-down order: by capture time (then session_node_id, then id) when every
    node has one, else unchanged (the caller's receipt-time order)."""
    if not nodes or timestamp_source(nodes) != SOURCE_CAPTURE:
        return list(nodes)

    def key(n: Mapping[str, Any]):
        sid = n.get("session_node_id")
        if _number(sid) is not None:
            rank = (0, float(sid), "")
        elif sid is None:
            rank = (2, 0.0, "")
        else:
            rank = (1, 0.0, str(sid))
        return n["capture"], rank, str(n.get("id", ""))
    return sorted(nodes, key=key)


def step(prev: Mapping[str, Any], cur: Mapping[str, Any]) -> Dict[str, Optional[float]]:
    """{distance_m, dt_s, speed_mps} of the move from `prev` to `cur` (dicts with x, y, time:
    an aware datetime or None, and optionally capture: likewise, used when both have one). distance is None when a pose is missing; dt when a time is
    missing; speed when either is None or dt <= 0 (a duplicate or reversed time)."""
    px, py = _number(prev.get("x")), _number(prev.get("y"))
    cx, cy = _number(cur.get("x")), _number(cur.get("y"))
    distance = None if None in (px, py, cx, cy) else math.hypot(cx - px, cy - py)
    t0, t1 = prev.get("time"), cur.get("time")
    if prev.get("capture") is not None and cur.get("capture") is not None:
        t0, t1 = prev["capture"], cur["capture"]
    dt = (t1 - t0).total_seconds() if t0 is not None and t1 is not None else None
    speed = distance / dt if distance is not None and dt is not None and dt > 0 else None
    return {"distance_m": _round(distance, 4), "dt_s": _round(dt, 3),
            "speed_mps": _round(speed, 4)}


def steps(nodes: Sequence[Mapping[str, Any]]) -> List[Optional[Dict[str, Optional[float]]]]:
    """One entry per node: None for the first, else the step from the node before it."""
    return [None] + [step(a, b) for a, b in zip(nodes, nodes[1:])]


def summarize(nodes: Sequence[Mapping[str, Any]],
              all_steps: Optional[Sequence[Optional[Mapping[str, Any]]]] = None
              ) -> Dict[str, Any]:
    """{node_count, first_time, last_time, duration_s, total_distance_m, median_speed_mps,
    max_speed_mps}. Times are the earliest / latest known (None when no node has one);
    duration is last - first, None below two timed nodes; speeds come from the steps that have
    one (None when none does); the total distance sums the steps that have a distance."""
    all_steps = steps(nodes) if all_steps is None else all_steps
    # One clock for the whole summary: the capture times only when every node has one.
    key = "capture" if timestamp_source(nodes) == SOURCE_CAPTURE else "time"
    times = [n[key] for n in nodes if n.get(key) is not None]
    first, last = (min(times), max(times)) if times else (None, None)
    distances = [s["distance_m"] for s in all_steps if s and s.get("distance_m") is not None]
    speeds = [s["speed_mps"] for s in all_steps if s and s.get("speed_mps") is not None]
    return {
        "node_count": len(nodes),
        "first_time": first.isoformat() if first else None,
        "last_time": last.isoformat() if last else None,
        "duration_s": _round((last - first).total_seconds(), 3) if len(times) >= 2 else None,
        "total_distance_m": _round(sum(distances), 4),
        "median_speed_mps": _round(statistics.median(speeds), 4) if speeds else None,
        "max_speed_mps": _round(max(speeds), 4) if speeds else None,
    }
