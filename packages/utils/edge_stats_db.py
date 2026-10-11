"""SQL of the edge_observations table (migration 20261011_01_edge_observations), shared by
mission-dispatch (leg observations at run end), the API (mapping sessions, the read route) and
scripts/backfill_edge_observations.py. Takes a psycopg cursor; no connection handling here.
"""
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from packages import config

COLUMNS = ("map_id", "from_node", "to_node", "robot_name", "source", "source_key", "observed_at",
           "duration_s", "distance_m", "speed_mps", "recoveries", "blocks")
INSERT_SQL = (
    f"INSERT INTO edge_observations ({', '.join(COLUMNS)}) "
    f"VALUES ({', '.join(['%s'] * len(COLUMNS))}) "
    "ON CONFLICT (map_id, from_node, to_node, robot_name, source, source_key) DO NOTHING"
)
# Delete all but the newest %s observations of every edge of one map and robot that is in the
# given (from, to) pairs (two parallel arrays): the cost follows the touched edges, not the map.
# Parameters: map, robot, from_nodes, to_nodes, map, robot, window.
PRUNE_SQL = (
    "DELETE FROM edge_observations o USING ("
    "SELECT e.from_node, e.to_node, e.source, e.source_key, row_number() OVER ("
    "PARTITION BY e.from_node, e.to_node ORDER BY e.observed_at DESC, e.source DESC, "
    "e.source_key DESC) AS rn FROM edge_observations e WHERE e.map_id = %s AND e.robot_name = %s "
    "AND (e.from_node, e.to_node) IN (SELECT * FROM unnest(%s::text[], %s::text[]))) r "
    "WHERE o.map_id = %s AND o.robot_name = %s AND o.from_node = r.from_node "
    "AND o.to_node = r.to_node AND o.source = r.source AND o.source_key = r.source_key "
    "AND r.rn > %s"
)
SELECT_SQL = (
    f"SELECT {', '.join(COLUMNS)} FROM edge_observations WHERE map_id = %s "
    "AND (%s::text IS NULL OR robot_name = %s)"
)


def prune_params(observations: Iterable[Dict[str, Any]], window: Optional[int] = None
                 ) -> List[Tuple[Any, ...]]:
    """The PRUNE_SQL parameters, one tuple per (map, robot) of `observations`, restricted to the
    (from, to) pairs they touch."""
    window = config.EDGE_STATS_WINDOW if window is None else window
    pairs: Dict[Tuple[str, str], set] = {}
    for o in observations:
        pairs.setdefault((o["map_id"], o["robot_name"]), set()).add((o["from_node"], o["to_node"]))
    out = []
    for (map_id, robot), edges in sorted(pairs.items()):
        ordered = sorted(edges)
        out.append((map_id, robot, [f for f, _ in ordered], [t for _, t in ordered],
                    map_id, robot, window))
    return out


async def store(cursor: Any, observations: Sequence[Dict[str, Any]],
                window: Optional[int] = None) -> int:
    """Insert the observations (a repeat of one is ignored: the key is the source's own id), then
    prune the touched edges of each (map, robot) to the newest `window` observations. Returns
    the number of rows INSERTED, which counts a new row that the prune removed at once (one
    older than the window of its edge) -- it is not the number of rows that remain."""
    inserted = 0
    for obs in observations:
        await cursor.execute(INSERT_SQL, tuple(obs[c] for c in COLUMNS))
        inserted += max(0, cursor.rowcount)
    for params in prune_params(observations, window):
        await cursor.execute(PRUNE_SQL, params)
    return inserted


async def fetch(cursor: Any, map_id: str, robot: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every stored observation of the map (of one robot when given)."""
    await cursor.execute(SELECT_SQL, (map_id, robot, robot))
    return [dict(zip(COLUMNS, row)) for row in await cursor.fetchall()]
