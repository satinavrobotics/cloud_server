"""SQL of the edge_observations table (migration 20261011_01_edge_observations, history index
20261027_02_edge_obs_history, moving / stopped / recovery columns 20261027_04_leg_moving), shared by
mission-dispatch (leg observations at run end, the retention purge), the API (mapping sessions, the read route) and
scripts/backfill_edge_observations.py.

Every raw observation is kept: store() only inserts. The window (newest EDGE_STATS_WINDOW per edge
and robot, or per edge when pooled) is applied on read, in SQL (fetch); observations older than
EDGE_STATS_RETENTION_DAYS are deleted by purge_old() (0 = never). The functions take a psycopg
cursor (purge_old a connection, it commits per batch); no other connection handling here.
"""
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from packages import config

COLUMNS = ("map_id", "from_node", "to_node", "robot_name", "source", "source_key", "observed_at",
           "duration_s", "distance_m", "speed_mps", "recoveries", "blocks", "moving_s", "stopped_s",
           "recovery_s")
# Column types of edge_observations, for the unnest of INSERT_SQL.
COLUMN_TYPES = ("text", "text", "text", "text", "text", "text", "timestamptz", "float8", "float8",
                "float8", "int", "int", "float8", "float8", "float8")
# ONE statement inserts the whole batch: every column is a parallel array (insert_params), so the
# round trips do not grow with the batch and the cursor's rowcount is the number of rows inserted
# (executemany would give a per-statement count). A row that already exists, also one repeated in
# the batch, is skipped.
INSERT_SQL = (
    f"INSERT INTO edge_observations ({', '.join(COLUMNS)}) "
    f"SELECT * FROM unnest({', '.join(f'%s::{t}[]' for t in COLUMN_TYPES)}) "
    "ON CONFLICT (map_id, from_node, to_node, robot_name, source, source_key) DO NOTHING"
)
# The newest %s observations per partition. The order is edge_stats.newest()'s: observed_at, then
# source, then source_key, all DESC (COLLATE "C" = Python's code point order, so the choice of
# the rows at the window's edge does not depend on the database collation). Per (from, to, robot)
# by default, per (from, to) across robots when pooled. Parameters: map, robot, robot, window.
_SELECT_TEMPLATE = (
    f"SELECT {', '.join(COLUMNS)} FROM ("
    f"SELECT {', '.join(COLUMNS)}, row_number() OVER (PARTITION BY from_node, to_node{{part}} "
    'ORDER BY observed_at DESC, source COLLATE "C" DESC, source_key COLLATE "C" DESC) AS rn '
    "FROM edge_observations WHERE map_id = %s AND (%s::text IS NULL OR robot_name = %s)) w "
    "WHERE rn <= %s"
)
SELECT_SQL = _SELECT_TEMPLATE.format(part=", robot_name")
SELECT_POOLED_SQL = _SELECT_TEMPLATE.format(part="")
# Retention: delete one batch of observations observed more than `days` ago. Parameters: days,
# batch (PURGE_BATCH rows per transaction; PURGE_EVERY_S = how often the recorder runs it).
PURGE_SQL = (
    "DELETE FROM edge_observations WHERE ctid IN (SELECT ctid FROM edge_observations "
    "WHERE observed_at < now() - make_interval(days => %s) LIMIT %s)"
)
PURGE_BATCH = 5000
PURGE_EVERY_S = 3600.0


def insert_params(observations: Iterable[Dict[str, Any]]) -> Tuple[List[Any], ...]:
    """The INSERT_SQL parameters: one list per column of COLUMNS, one entry per observation."""
    rows = list(observations)
    return tuple([o[c] for o in rows] for c in COLUMNS)


async def store(cursor: Any, observations: Sequence[Dict[str, Any]]) -> int:
    """Insert the observations (a repeat of one is ignored: the key is the source's own id).
    Append-only: nothing is pruned here, the window is applied by fetch() and the age limit by
    purge_old(). Returns the number of rows inserted."""
    if not observations:
        return 0
    await cursor.execute(INSERT_SQL, insert_params(observations))
    return max(0, cursor.rowcount)


async def fetch(cursor: Any, map_id: str, robot: Optional[str] = None, pooled: bool = False,
                window: Optional[int] = None) -> List[Dict[str, Any]]:
    """The newest `window` (default EDGE_STATS_WINDOW) observations of every (from, to, robot)
    of the map, or of every (from, to) across the robots when `pooled`; of one robot when
    `robot` is given. Cut in SQL, so the history does not travel."""
    window = config.EDGE_STATS_WINDOW if window is None else window
    await cursor.execute(SELECT_POOLED_SQL if pooled else SELECT_SQL,
                         (map_id, robot, robot, window))
    return [dict(zip(COLUMNS, row)) for row in await cursor.fetchall()]


async def purge_old(conn: Any, days: Optional[int] = None, batch: int = PURGE_BATCH) -> int:
    """Delete the observations observed more than `days` ago (default
    EDGE_STATS_RETENTION_DAYS; <= 0 = keep forever, nothing is done), `batch` rows per
    transaction, so no long lock and no huge transaction. `conn` is a psycopg async connection.
    Returns the number of rows deleted."""
    days = config.EDGE_STATS_RETENTION_DAYS if days is None else days
    if days <= 0:
        return 0
    total = 0
    while True:
        async with conn.transaction():
            async with conn.cursor() as cursor:
                await cursor.execute(PURGE_SQL, (days, batch))
                deleted = max(0, cursor.rowcount)
        total += deleted
        if deleted < batch:
            return total
