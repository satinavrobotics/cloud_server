"""Pure helpers for the per-edge traversal statistics of the topomap (groundwork for edge weights).

An observation is one robot traversing one directed topomap edge (from -> to) once, keyed by
(map, from, to, robot, source, source_key). Two sources:

- `leg`: a mission leg (run_legs: one robot move from one topomap node to the next). source_key
  `<run_id>:<seq>`. Carries the recoveries / blocks seen on the leg.
- `mapping`: two consecutive nodes of a mapping session (packages/utils/session_trace.py steps).
  source_key `<session_id>:<step index>` (the index of the later node in the session's order, so
  a second traversal of the same directed pair in one session is its own observation). No
  recoveries / blocks.

Only an observation on a pair that IS a topomap edge counts (`on_edges`): the graph builder joins
a new node to every node within its distance threshold, so consecutive mapping nodes are usually
but not always connected, and a leg between two route nodes of a mission need not be an edge.
Observations with a duration <= 0 (after rounding to milliseconds, as stored) or over
EDGE_STATS_MAX_DURATION_S, a non-finite distance / duration, or a speed over
EDGE_STATS_MAX_SPEED_MPS, are dropped (`reject_reason`). A mapping step slower than
EDGE_STATS_MIN_SPEED_MPS (a session paused or the robot standing still between two nodes) is
dropped too: its time is not a traversal time (legs are not filtered this way). Raw observations are
all kept (up to EDGE_STATS_RETENTION_DAYS, packages/utils/edge_stats_db.py); the window is applied on
read: the statistics (`group_stats`) are computed from the newest EDGE_STATS_WINDOW observations of each
(map, edge, robot), or, pooled, of each (map, edge) across all robots (the database read already cuts
to that window, `group_stats` applies it again so it is right for any caller).
Nothing here touches a database; see packages/utils/run_legs.py for the same structure.
"""
import datetime
import math
import statistics
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from packages import config
from packages.utils import run_legs
from packages.utils import session_trace as trace

SOURCE_LEG, SOURCE_MAPPING = "leg", "mapping"
EdgeKey = Tuple[str, str]                 # (from node id, to node id)


def reject_reason(duration_s: Optional[float], distance_m: Optional[float]) -> Optional[str]:
    """Why an observation is unusable (None when it is fine): no / non-finite / non-positive
    duration (judged after the rounding to milliseconds that is stored), a duration over
    EDGE_STATS_MAX_DURATION_S, a non-finite distance, or a speed (distance / duration, when the
    distance is known) over EDGE_STATS_MAX_SPEED_MPS."""
    if duration_s is None or not math.isfinite(duration_s):
        return "duration"
    duration_s = round(duration_s, 3)
    if not duration_s > 0:
        return "duration"
    if duration_s > config.EDGE_STATS_MAX_DURATION_S:
        return "too_long"
    if distance_m is not None:
        if not math.isfinite(distance_m):
            return "distance"
        if distance_m / duration_s > config.EDGE_STATS_MAX_SPEED_MPS:
            return "too_fast"
    return None


def _observation(map_id: str, src: str, dst: str, robot: str, source: str, source_key: str,
                 observed_at: datetime.datetime, duration_s: float,
                 distance_m: Optional[float], recoveries: int = 0, blocks: int = 0
                 ) -> Dict[str, Any]:
    duration_s = round(duration_s, 3)
    return {"map_id": map_id, "from_node": src, "to_node": dst, "robot_name": robot,
            "source": source, "source_key": source_key, "observed_at": observed_at,
            "duration_s": duration_s,
            "distance_m": None if distance_m is None else round(distance_m, 4),
            "speed_mps": (round(distance_m / duration_s, 4) if distance_m is not None else None),
            "recoveries": int(recoveries), "blocks": int(blocks)}


def leg_observation(leg: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The observation of a run_legs row (run_id, seq, robot_name, map_id, from_topomap_node,
    to_topomap_node, ended_at, duration_s, straight_m, recoveries, blocks); None for a leg
    without both topomap nodes (or on one node) or a map, or an unusable one. The distance is the
    straight line between the leg's waypoints (the leg follows planned_path)."""
    src, dst = leg.get("from_topomap_node"), leg.get("to_topomap_node")
    if not src or not dst or src == dst or not leg.get("map_id") or not leg.get("robot_name"):
        return None
    duration, distance = leg.get("duration_s"), leg.get("straight_m")
    if reject_reason(duration, distance) is not None:
        return None
    return _observation(str(leg["map_id"]), str(src), str(dst), str(leg["robot_name"]),
                        SOURCE_LEG, f"{leg['run_id']}:{leg['seq']}", leg["ended_at"],
                        float(duration), None if distance is None else float(distance),
                        leg.get("recoveries") or 0, leg.get("blocks") or 0)


def session_observations(map_id: str, session_id: str, robot_name: str,
                         nodes: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """The observations of a mapping session: `nodes` as session_trace.order() returns them
    (dicts with id, x, y, time, capture). One per consecutive pair whose step has a distance and
    a usable duration and a speed of at least EDGE_STATS_MIN_SPEED_MPS; observed_at = the later
    node's time on the clock the step used; source_key `<session_id>:<index of the later node>`."""
    out = []
    for index, (prev, cur, st) in enumerate(zip(nodes, nodes[1:], trace.steps(nodes)[1:]), 1):
        if not st or st["distance_m"] is None or reject_reason(st["dt_s"], st["distance_m"]):
            continue
        if st["distance_m"] / round(st["dt_s"], 3) < config.EDGE_STATS_MIN_SPEED_MPS:
            continue
        if str(prev["id"]) == str(cur["id"]):
            continue
        both = prev.get("capture") is not None and cur.get("capture") is not None
        when = cur["capture"] if both else cur.get("time")
        out.append(_observation(map_id, str(prev["id"]), str(cur["id"]), robot_name,
                                SOURCE_MAPPING, f"{session_id}:{index}", when, st["dt_s"],
                                st["distance_m"]))
    return out


def edge_set(edges: Iterable[Mapping[str, Any]]) -> Set[EdgeKey]:
    """{(from, to)} of GraphDatabaseService.get_edges() rows (directed, as stored)."""
    return {(str(e["from_node_id"]), str(e["to_node_id"])) for e in edges
            if e.get("from_node_id") is not None and e.get("to_node_id") is not None}


def on_edges(observations: Iterable[Mapping[str, Any]], edges: Set[EdgeKey]
             ) -> List[Dict[str, Any]]:
    """The observations whose (from, to) is an existing edge; the others are ignored."""
    return [dict(o) for o in observations if (o["from_node"], o["to_node"]) in edges]


def newest(observations: Iterable[Mapping[str, Any]], window: Optional[int] = None
           ) -> List[Mapping[str, Any]]:
    """The newest `window` observations (default EDGE_STATS_WINDOW), newest first; the tie
    break (source, source_key) makes the choice deterministic."""
    window = config.EDGE_STATS_WINDOW if window is None else window
    return sorted(observations, key=lambda o: (o["observed_at"], o["source"], o["source_key"]),
                  reverse=True)[:window]


def _round(value: Optional[float], digits: int = 3) -> Optional[float]:
    return None if value is None else round(value, digits)


def stats(observations: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """The statistics of one (map, edge, robot)'s observations (already the window): count,
    median_s / p90_s of the duration, median_speed_mps (null when none has a speed),
    last_observed_at, the recoveries / blocks seen (mission legs only) and the count per source."""
    durations = [float(o["duration_s"]) for o in observations]
    speeds = [float(o["speed_mps"]) for o in observations if o.get("speed_mps") is not None]
    return {
        "count": len(observations),
        "median_s": _round(statistics.median(durations)) if durations else None,
        "p90_s": _round(run_legs.percentile(durations, 90)),
        "median_speed_mps": _round(statistics.median(speeds), 4) if speeds else None,
        "last_observed_at": (max(o["observed_at"] for o in observations).isoformat()
                             if observations else None),
        "recoveries": sum(int(o.get("recoveries") or 0) for o in observations),
        "blocks": sum(int(o.get("blocks") or 0) for o in observations),
        "sources": {s: sum(1 for o in observations if o["source"] == s)
                    for s in (SOURCE_LEG, SOURCE_MAPPING)},
    }


def group_stats(observations: Iterable[Mapping[str, Any]], window: Optional[int] = None,
                pooled: bool = False) -> List[Dict[str, Any]]:
    """Per (map, from, to, robot): the stats of its newest `window` observations, sorted by
    (map, from, to, robot). Each item: map_id, from_node_id, to_node_id, robot + `stats`.

    `pooled`: per (map, from, to) across the robots instead, from the newest `window`
    observations of the edge of ALL robots together; every item then has `robot: None` and its
    `sources` / counts are the pool's."""
    groups: Dict[Tuple[str, str, str, Optional[str]], List[Mapping[str, Any]]] = {}
    for o in observations:
        groups.setdefault((o["map_id"], o["from_node"], o["to_node"],
                           None if pooled else o["robot_name"]), []).append(o)
    items = []
    for (map_id, src, dst, robot), group in sorted(groups.items()):
        items.append({"map_id": map_id, "from_node_id": src, "to_node_id": dst, "robot": robot,
                      **stats(newest(group, window))})
    return items
