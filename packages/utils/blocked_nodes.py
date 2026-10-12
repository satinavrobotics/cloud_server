"""
Graph nodes a robot reported blocked, kept out of new routes for a while (table
blocked_graph_nodes, migrations 20261008_01 and 20261012_01: ship the latter first, the
SQL below names its columns). mission-dispatch writes them, the mission planner
reads them, the API lists and clears them; the SQL and the path search live here so all three
read the table the same way.

A row names a graph node, or, when the waypoint's graph node is not known, a position
("@x,y", map frame): the graph nodes near it are blocked. `row_blocks` is the one matching
rule; the planner (which nodes to avoid) and the API (is a reroute through one) both use it.
The first node of a route is never blocked: the robot is there.

Today only mission-dispatch's edgeBlocked handling writes rows; the "nodeBlocked" and
"operator" sources are reserved. `map_name` holds the waypoint's map_id.
"""
import logging
import math
import os
import re
import time
from collections import deque
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

SOURCES = ("edgeBlocked", "nodeBlocked", "operator")

# A position row ("@x,y") keeps out the graph nodes within this distance of it (metres).
MATCH_RADIUS_M = float(os.getenv("BLOCKED_NODE_MATCH_RADIUS_M", "0.5"))

COLUMNS = ("map_name", "graph_node_id", "edge_from", "edge_to", "source", "robot_name",
           "mission_name", "vda_node_id", "reason", "x", "y", "created_at", "expires_at",
           "robot_x", "robot_y")

# A new report on a node already blocked extends its expiry (never shortens it) and keeps
# the newest report's details; on a row that had expired it starts a new block (created_at).
# A report without the robot's pose (NULL) never erases an earlier valid one. The table's
# prev_x/prev_y columns (migration 20261012_01) are no longer written: nothing reads them.
UPSERT_SQL = (
    "INSERT INTO blocked_graph_nodes (map_name, graph_node_id, edge_from, edge_to, source, "
    "robot_name, mission_name, vda_node_id, reason, x, y, robot_x, robot_y, "
    "expires_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
    "now() + make_interval(secs => %s)) "
    "ON CONFLICT (map_name, graph_node_id) DO UPDATE SET "
    "edge_from = EXCLUDED.edge_from, edge_to = EXCLUDED.edge_to, source = EXCLUDED.source, "
    "robot_name = EXCLUDED.robot_name, mission_name = EXCLUDED.mission_name, "
    "vda_node_id = EXCLUDED.vda_node_id, reason = EXCLUDED.reason, x = EXCLUDED.x, "
    "y = EXCLUDED.y, "
    "robot_x = COALESCE(EXCLUDED.robot_x, blocked_graph_nodes.robot_x), "
    "robot_y = COALESCE(EXCLUDED.robot_y, blocked_graph_nodes.robot_y), "
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

# Rows expired for longer than this are dropped (nothing reads an expired row; the API's
# clear tolerates its absence). A map delete removes the rest (map_delete finish).
PURGE_SQL = ("DELETE FROM blocked_graph_nodes "
             "WHERE expires_at < now() - make_interval(secs => %s)")
PURGE_AFTER_S = 86400.0
PURGE_EVERY_S = 3600.0
_last_purge = float("-inf")


async def purge_expired(database: Any, now: Optional[float] = None) -> None:
    """Drop rows expired for more than PURGE_AFTER_S, at most once per PURGE_EVERY_S per
    process (cheap; called from the upsert and list paths). Best effort: own connection
    (so a failure cannot abort the caller's transaction), errors are only logged."""
    global _last_purge  # pylint: disable=global-statement
    now = time.monotonic() if now is None else now
    if now - _last_purge < PURGE_EVERY_S:
        return
    _last_purge = now
    try:
        async with database.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(PURGE_SQL, (PURGE_AFTER_S,))
    except Exception as err:  # pylint: disable=broad-except
        logging.getLogger(__name__).warning("Could not purge expired blocked nodes: %s", err)


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


# "{name}-r{run_id}" or "{name}-r{run_id}v{order_rev}": the prefix of the order ids the dispatcher
# generates (controllers/mission/order_ids.run_prefix); run_id is uuid4().hex[:8].
_RUN_PREFIX = re.compile(r"-r([0-9a-f]{8})(?:v\d+)?$")


def run_id_of(row: Mapping[str, Any]) -> Optional[str]:
    """The `mission_runs.run_id` (uuid string) of the dispatcher run a blocked report was made
    in, derived from the row, never stored: `vda_node_id` is "{prefix}-n{idx}-s{seq}" and its
    prefix carries the dispatcher's run id (order_ids), which with `mission_name` is the key of
    fleet_recorder.run_uuid. None for a row without them (written before the dispatcher
    numbered runs, a manual row). Stable across restarts and reroutes (order_rev is not part of
    the key).

    This is the uuid of the DISPATCHER run id of the pass the report was made in. It equals
    mission_runs.run_id for the first pass only: a repeat pass starts a new dispatcher run id
    while the recorder keeps the first pass's mission_runs row, so for later passes (and for a
    run adopted after a restart) use `resolve_run_ids`, which looks the run up."""
    node, mission = row.get("vda_node_id"), row.get("mission_name")
    if not node or not mission:
        return None
    from packages.controllers.mission import fleet_recorder, order_ids  # not at the top: heavy
    order = order_ids.order_of_node(str(node))
    prefix = order_ids.order_prefix(order) if order else None
    match = _RUN_PREFIX.search(prefix) if prefix else None
    if match is None or prefix[:match.start()] != str(mission):
        return None
    return str(fleet_recorder.run_uuid(str(mission), match.group(1)))


# The mission_runs rows a report's mission and robot may belong to. A repeat pass gets a new
# dispatcher run id but the recorder keeps ONE mission_runs row for the mission on the robot
# (fleet_recorder.run_started returns early when the mission's run is open), keyed by the FIRST
# pass's id (or, after a dispatcher restart, adopted by mission and robot): so for a later pass
# `run_id_of` names a uuid no mission_runs row has.
RUNS_OF_MISSIONS_SQL = (
    "SELECT run_id, mission_name, robot_name, started_at FROM mission_runs "
    "WHERE mission_name = ANY(%s) ORDER BY started_at")


async def resolve_run_ids(database: Any, rows: List[Dict[str, Any]]) -> None:
    """Replace `run_id` of `rows` (in place) by the mission_runs row they belong to, where
    `run_id_of`'s uuid is not one: the run of the same mission and robot that had started when
    the report was made (latest `started_at` not after `created_at`, which a re-block resets, so
    the run may be a later one only if the report is newer than it). One query for all rows;
    rows without a derived id are not looked at; any failure leaves the derived ids."""
    todo = [r for r in rows if r.get("run_id") and r.get("mission_name")]
    if not todo:
        return
    try:
        async with database.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(RUNS_OF_MISSIONS_SQL,
                                     (sorted({str(r["mission_name"]) for r in todo}),))
                runs = [(str(a), b, c, d) for a, b, c, d in await cursor.fetchall()]
    except Exception as err:  # pylint: disable=broad-except
        logging.getLogger(__name__).warning("Could not resolve run ids of blocked nodes: %s", err)
        return
    known = {run[0] for run in runs}
    for row in todo:
        if row["run_id"] in known:
            continue
        created = _as_datetime(row.get("created_at"))
        best = None
        for run_id, mission, robot, started in runs:
            if mission != row["mission_name"] or (row.get("robot_name") and robot != row["robot_name"]):
                continue
            if created is not None and started is not None and started > created:
                continue
            best = run_id          # runs are ordered by started_at: the last one wins
        if best is not None:
            row["run_id"] = best


def _as_datetime(value: Any) -> Any:
    import datetime
    if isinstance(value, str):
        try:
            value = datetime.datetime.fromisoformat(value)
        except ValueError:
            return None
    if isinstance(value, datetime.datetime) and value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return value if isinstance(value, datetime.datetime) else None


def row_dict(row: Sequence[Any]) -> Dict[str, Any]:
    out = dict(zip(COLUMNS, row))
    for key in ("created_at", "expires_at"):
        if out.get(key) is not None and hasattr(out[key], "isoformat"):
            out[key] = out[key].isoformat()
    out["run_id"] = run_id_of(out)
    return out


def upsert_params(map_name: str, graph_node_id: str, source: str, expires_in_s: float,
                  edge_from: Optional[str] = None, edge_to: Optional[str] = None,
                  robot_name: Optional[str] = None, mission_name: Optional[str] = None,
                  vda_node_id: Optional[str] = None, reason: Optional[str] = None,
                  x: Optional[float] = None, y: Optional[float] = None,
                  robot_x: Optional[float] = None, robot_y: Optional[float] = None
                  ) -> Tuple[Any, ...]:
    if source not in SOURCES:
        raise ValueError(f"unknown source {source!r}")
    return (map_name, graph_node_id, edge_from, edge_to, source, robot_name, mission_name,
            vda_node_id, reason, x, y, robot_x, robot_y, float(expires_in_s))


async def fetch_active(database: Any, map_id: str) -> List[Dict[str, Any]]:
    """The map's rows whose exclusion has not expired (`database` has psycopg's
    connection())."""
    async with database.connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(ACTIVE_FOR_MAP_SQL, (map_id,))
            rows = [row_dict(r) for r in await cursor.fetchall()]
    await purge_expired(database)
    return rows


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
             radius_m: Optional[float] = None,
             polyline: Optional[Sequence[Tuple[float, float]]] = None,
             half_width_m: Optional[float] = None) -> List[Dict[str, Any]]:
    """The `rows` that block any of `points` ((node_id, xy) pairs), in row order.

    With a `polyline` (the route's positions in order, the robot's start included), a row that
    has the robot's pose at the block (`robot_x`, `robot_y`) also blocks a route whose legs pass
    the obstacle segment (blocked_geometry.route_hits_row); this is in addition to the point
    rule. Rows without a robot pose, and calls without a polyline, are judged as before.
    `half_width_m`: the passing robot's half width (blocked_geometry.half_width_for); None keeps
    the constant."""
    points = list(points)
    poly = [(float(x), float(y)) for x, y in polyline] if polyline else []
    geometry = _geometry() if poly else None
    out = []
    for row in rows:
        if any(row_blocks(row, node_id, xy, radius_m) for node_id, xy in points) or \
                (poly and geometry.route_hits_row(
                    row, poly, *geometry.widths(half_width_m))):
            out.append(dict(row))
    return out


def _geometry() -> Any:
    from packages.utils import blocked_geometry  # imports this module: not at the top
    return blocked_geometry


def leg_crosser(rows: Iterable[Mapping[str, Any]], half_width_m: Optional[float] = None
                ) -> Callable[[Tuple[float, float], Tuple[float, float]], bool]:
    """`crosses(a, b)`: whether the straight leg a -> b passes the obstacle segment of any of
    `rows` (rows without a robot pose never match). The segments are computed once, for
    path searches that test many legs against the same rows."""
    geometry = _geometry()
    half, lead = geometry.widths(half_width_m)
    segs = [s for s in (geometry.row_segment(r, lead) for r in rows) if s is not None]
    return lambda a, b: any(geometry.segment_hits(s, [a, b], half) for s in segs)


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
                           excluded: Set[str],
                           edge_blocked: Optional[Callable[[str, str], bool]] = None
                           ) -> Optional[List[str]]:
    """The fewest-hops path from `start` to `end` along the directed `edges` (graph_db's
    {from, to}) that passes no `excluded` node; None if there is none. The start node is
    never excluded (the robot is there). `edge_blocked(a, b)`, when given, also rules
    out single edges (a leg across an obstacle segment). Same metric as graph_db.shortest_path (hop count,
    OUTBOUND)."""
    start, end = str(start), str(end)
    if end in excluded and end != start:
        return None
    adjacency: Dict[str, List[str]] = {}
    for edge in edges:
        a, b = str(edge.get("from")), str(edge.get("to"))
        if edge_blocked is not None and edge_blocked(a, b):
            continue
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
