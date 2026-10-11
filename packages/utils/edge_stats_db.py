"""SQL of the edge_observations table (migration 20261011_01_edge_observations), shared by
mission-dispatch (leg observations at run end), the API (mapping sessions, the read route) and
scripts/backfill_edge_observations.py. Takes a psycopg cursor; no connection handling here.
"""
from typing import Any, Dict, List, Optional, Sequence

from packages import config

COLUMNS = ("map_id", "from_node", "to_node", "robot_name", "source", "source_key", "observed_at",
           "duration_s", "distance_m", "speed_mps", "recoveries", "blocks")
INSERT_SQL = (
    f"INSERT INTO edge_observations ({', '.join(COLUMNS)}) "
    f"VALUES ({', '.join(['%s'] * len(COLUMNS))}) "
    "ON CONFLICT (map_id, from_node, to_node, robot_name, source, source_key) DO NOTHING"
)
# Delete all but the newest %s observations of every edge of one map and robot.
PRUNE_SQL = (
    "DELETE FROM edge_observations o USING ("
    "SELECT from_node, to_node, source, source_key, row_number() OVER ("
    "PARTITION BY from_node, to_node ORDER BY observed_at DESC, source DESC, "
    "source_key DESC) AS rn FROM edge_observations WHERE map_id = %s AND robot_name = %s) r "
    "WHERE o.map_id = %s AND o.robot_name = %s AND o.from_node = r.from_node "
    "AND o.to_node = r.to_node AND o.source = r.source AND o.source_key = r.source_key "
    "AND r.rn > %s"
)
SELECT_SQL = (
    f"SELECT {', '.join(COLUMNS)} FROM edge_observations WHERE map_id = %s "
    "AND (%s::text IS NULL OR robot_name = %s)"
)


async def store(cursor: Any, observations: Sequence[Dict[str, Any]],
                window: Optional[int] = None) -> int:
    """Insert the observations (a repeat of one is ignored: the key is the source's own id), then
    prune each touched (map, robot) to the newest `window` per edge. Returns the number of
    rows inserted."""
    window = config.EDGE_STATS_WINDOW if window is None else window
    inserted = 0
    for obs in observations:
        await cursor.execute(INSERT_SQL, tuple(obs[c] for c in COLUMNS))
        inserted += max(0, cursor.rowcount)
    for map_id, robot in sorted({(o["map_id"], o["robot_name"]) for o in observations}):
        await cursor.execute(PRUNE_SQL, (map_id, robot, map_id, robot, window))
    return inserted


async def fetch(cursor: Any, map_id: str, robot: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every stored observation of the map (of one robot when given)."""
    await cursor.execute(SELECT_SQL, (map_id, robot, robot))
    return [dict(zip(COLUMNS, row)) for row in await cursor.fetchall()]
