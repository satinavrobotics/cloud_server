"""
Graph nodes a robot reported blocked, kept out of new routes for a while (table
blocked_graph_nodes, migration 20261008_01). mission-dispatch writes them, the mission planner
reads them, the API lists and clears them; the SQL and the path search live here so all three
read the table the same way.

A row names a graph node, or, when the waypoint's graph node is not known, a position
("@x,y", map frame): the graph nodes near it are blocked. `row_blocks` is the one matching
rule; the planner (which nodes to avoid) and the API (is a reroute through one) both use it.
The first node of a route is never blocked: the robot is there.

Today only mission-dispatch's edgeBlocked handling writes rows; the "nodeBlocked" and
"operator" sources are reserved. `map_name` holds the waypoint's map_id.
"""
import math
import os
from collections import deque
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

SOURCES = ("edgeBlocked", "nodeBlocked", "operator")

# A position row ("@x,y") keeps out the graph nodes within this distance of it (metres).
MATCH_RADIUS_M = float(os.getenv("BLOCKED_NODE_MATCH_RADIUS_M", "0.5"))

COLUMNS = ("map_name", "graph_node_id", "edge_from", "edge_to", "source", "robot_name",
           "mission_name", "vda_node_id", "reason", "x", "y", "created_at", "expires_at")

# A new report on a node already blocked extends its expiry (never shortens it) and keeps
# the newest report's details; on a row that had expired it starts a new block (created_at).
UPSERT_SQL = (
    "INSERT INTO blocked_graph_nodes (map_name, graph_node_id, edge_from, edge_to, source, "
    "robot_name, mission_name, vda_node_id, reason, x, y, expires_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now() + make_interval(secs => %s)) "
    "ON CONFLICT (map_name, graph_node_id) DO UPDATE SET "
    "edge_from = EXCLUDED.edge_from, edge_to = EXCLUDED.edge_to, source = EXCLUDED.source, "
    "robot_name = EXCLUDED.robot_name, mission_name = EXCLUDED.mission_name, "
    "vda_node_id = EXCLUDED.vda_node_id, reason = EXCLUDED.reason, x = EXCLUDED.x, "
    "y = EXCLUDED.y, "
    "created_at = CASE WHEN blocked_graph_nodes.expires_at <= now() THEN now() "
    "ELSE blocked_graph_nodes.created_at END, "
    "expires_at = GREATEST(blocked_graph_nodes.expires_at, EXCLUDED.expires_at)")

ACTIVE_FOR_MAP_SQL = (
    f"SELECT {', '.join(COLUMNS)} FROM blocked_graph_nodes "
    "WHERE map_name = %s AND expires_at > now() ORDER BY created_at")

# Removes the row, expired or not; returns whether it was still active (an expired row is
# not blocked: cleared quietly, reported as not found).
DELETE_SQL = ("DELETE FROM blocked_graph_nodes WHERE map_name = %s AND graph_node_id = %s "
              "RETURNING expires_at > now()")


def synthetic_id(x: float, y: float) -> str:
    """The id of a blocked position whose graph node is not known."""
    return f"@{x:.2f},{y:.2f}"


def position_of(graph_node_id: str) -> Optional[Tuple[float, float]]:
    """The position a synthetic id names, else None (a real graph node id)."""
    if not graph_node_id.startswith("@"):
        return None
    try:
        x, y = graph_node_id[1:].split(",")
        return float(x), float(y)
    except ValueError:
        return None


def row_dict(row: Sequence[Any]) -> Dict[str, Any]:
    out = dict(zip(COLUMNS, row))
    for key in ("created_at", "expires_at"):
        if out.get(key) is not None and hasattr(out[key], "isoformat"):
            out[key] = out[key].isoformat()
    return out


def upsert_params(map_name: str, graph_node_id: str, source: str, expires_in_s: float,
                  edge_from: Optional[str] = None, edge_to: Optional[str] = None,
                  robot_name: Optional[str] = None, mission_name: Optional[str] = None,
                  vda_node_id: Optional[str] = None, reason: Optional[str] = None,
                  x: Optional[float] = None, y: Optional[float] = None) -> Tuple[Any, ...]:
    if source not in SOURCES:
        raise ValueError(f"unknown source {source!r}")
    return (map_name, graph_node_id, edge_from, edge_to, source, robot_name, mission_name,
            vda_node_id, reason, x, y, float(expires_in_s))


async def fetch_active(database: Any, map_id: str) -> List[Dict[str, Any]]:
    """The map's rows whose exclusion has not expired (`database` has psycopg's
    connection())."""
    async with database.connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(ACTIVE_FOR_MAP_SQL, (map_id,))
            return [row_dict(r) for r in await cursor.fetchall()]


def row_blocks(row: Mapping[str, Any], node_id: Optional[Any] = None,
               xy: Optional[Tuple[float, float]] = None,
               radius_m: Optional[float] = None) -> bool:
    """Whether `row` blocks a point of a route: graph node `node_id` at position `xy`.
    A graph node row blocks that node; a position row ("@x,y") blocks what is within
    `radius_m` of it. A point whose graph node is not known (a hand-placed waypoint) is
    matched by position against where the row's node was reported."""
    radius_m = MATCH_RADIUS_M if radius_m is None else radius_m
    graph_node_id = str(row["graph_node_id"])
    pos = position_of(graph_node_id)
    if pos is None:
        if node_id is not None:
            return str(node_id) == graph_node_id
        if row.get("x") is None or row.get("y") is None:
            return False
        pos = (float(row["x"]), float(row["y"]))
    return xy is not None and math.hypot(xy[0] - pos[0], xy[1] - pos[1]) <= radius_m


def rows_hit(rows: Iterable[Mapping[str, Any]],
             points: Iterable[Tuple[Optional[Any], Optional[Tuple[float, float]]]],
             radius_m: Optional[float] = None) -> List[Dict[str, Any]]:
    """The `rows` that block any of `points` ((node_id, xy) pairs), in row order."""
    points = list(points)
    return [dict(row) for row in rows
            if any(row_blocks(row, node_id, xy, radius_m) for node_id, xy in points)]


def route_points(waypoints: Sequence[Any], skip_first: bool = True
                 ) -> List[Tuple[Optional[str], Optional[Tuple[float, float]]]]:
    """A route's points to check, (node_id, xy) per waypoint (dict or Pose2D); the first
    waypoint is left out unless `skip_first` is false (the robot starts there)."""
    out = []
    for wp in list(waypoints)[1 if skip_first else 0:]:
        wp = wp if isinstance(wp, Mapping) else wp.dict()
        node_id = wp.get("node_id")
        out.append((None if node_id is None else str(node_id), node_xy(wp)))
    return out


def excluded_node_ids(rows: Iterable[Mapping[str, Any]],
                      nodes: Optional[Iterable[Mapping[str, Any]]] = None,
                      radius_m: Optional[float] = None) -> Set[str]:
    """The graph node ids the active `rows` keep out: each graph node row's node, and for a
    position row every graph node within `radius_m` of it (needs `nodes`, as graph_db
    returns them). Same rule as `row_blocks`."""
    rows = list(rows)
    out = {str(row["graph_node_id"]) for row in rows
           if position_of(str(row["graph_node_id"])) is None}
    positions = [row for row in rows if position_of(str(row["graph_node_id"])) is not None]
    if positions and nodes is not None:
        for node in nodes:
            node_id = str(node.get("node_id", node.get("_key")))
            xy = node_xy(node)
            if any(row_blocks(row, node_id, xy, radius_m) for row in positions):
                out.add(node_id)
    return out


def node_xy(node: Mapping[str, Any]) -> Optional[Tuple[float, float]]:
    pose = node.get("pose") if isinstance(node.get("pose"), Mapping) else node
    try:
        return float(pose["x"]), float(pose["y"])
    except (KeyError, TypeError, ValueError):
        return None


def shortest_path_avoiding(edges: Iterable[Mapping[str, Any]], start: str, end: str,
                           excluded: Set[str]) -> Optional[List[str]]:
    """The fewest-hops path from `start` to `end` along the directed `edges` (graph_db's
    {from, to}) that passes no `excluded` node; None if there is none. The start node is
    never excluded (the robot is there). Same metric as graph_db.shortest_path (hop count,
    OUTBOUND)."""
    start, end = str(start), str(end)
    if end in excluded and end != start:
        return None
    adjacency: Dict[str, List[str]] = {}
    for edge in edges:
        a, b = str(edge.get("from")), str(edge.get("to"))
        adjacency.setdefault(a, []).append(b)
    previous: Dict[str, Optional[str]] = {start: None}
    queue = deque([start])
    while queue:
        node = queue.popleft()
        if node == end:
            path = [node]
            while previous[path[-1]] is not None:
                path.append(previous[path[-1]])
            return path[::-1]
        for nxt in adjacency.get(node, []):
            if nxt not in previous and nxt not in excluded:
                previous[nxt] = node
                queue.append(nxt)
    return None
