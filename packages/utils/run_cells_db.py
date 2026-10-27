"""SQL of the run_cells table (migration 20261028_01_run_cells): one cell summary per (run, map).

A summary is a `CellRow`: the run's identity plus the cells it drove, as parallel numpy arrays over the sorted
absolute cell keys of packages/utils/cell_raster.py (`key`; (ix, iy) = key_cells(key)) with the per-cell length,
time, stopped seconds, recovery seconds and blocks. `encode` / `decode` convert to and from the table's columns;
`insert` upserts (a recompute replaces the row) and `fetch` reads rows back. The functions take a psycopg ASYNC
cursor; no connection handling here (the caller owns the transaction).

float4 columns: a cell's figures are small (metres, seconds), so single precision keeps ~1e-7 relative; the row's
`totals` (float8, in jsonb) hold the exact sums of the run.
"""
import dataclasses
import datetime
import json
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from cloud_common.objects.map import MapObjectV1
from packages.utils import cell_raster as cr

MAP_TABLE = MapObjectV1.table_name()
SOURCES = ("track", "legs", "empty")
ARRAYS = ("len_m", "time_s", "stopped_s", "recovery_s", "blocks")
COLUMNS = ("run_id", "map_id", "robot_name", "robot_class", "speed_max", "started_at", "ended_at", "source",
           "algo_version", "cell_m", "params", "frames", "ix", "iy", "len_m", "time_s", "stopped_s", "recovery_s",
           "blocks", "totals", "created_at")


@dataclasses.dataclass
class CellRow:
    run_id: Any
    map_id: str
    robot_name: str
    robot_class: str
    speed_max: Optional[float]
    started_at: datetime.datetime
    ended_at: Optional[datetime.datetime]
    source: str
    algo_version: int
    cell_m: float
    params: Dict[str, Any]
    frames: List[Any]
    key: np.ndarray                    # sorted absolute cell keys (cell_raster.cell_key)
    len_m: np.ndarray
    time_s: np.ndarray
    stopped_s: np.ndarray
    recovery_s: np.ndarray
    blocks: np.ndarray
    totals: Dict[str, Any]
    created_at: Optional[datetime.datetime] = None

    def __len__(self) -> int:
        return int(self.key.size)


# The insert is skipped (rowcount 0) while the map is gone or being deleted, so a run that ends during a map
# delete cannot bring rows back (packages/api/map_delete.py). The run row must exist (foreign key).
INSERT_SQL = (
    f"INSERT INTO run_cells ({', '.join(COLUMNS[:-1])}) "
    "SELECT %s::uuid, %s::text, %s::text, %s::text, %s::float4, %s::timestamptz, %s::timestamptz, %s::text, "
    "%s::int2, %s::float4, %s::jsonb, %s::jsonb, %s::int4[], %s::int4[], "
    "%s::float4[], %s::float4[], %s::float4[], %s::float4[], %s::int2[], %s::jsonb "
    f"WHERE EXISTS (SELECT 1 FROM {MAP_TABLE} WHERE name = %s::text AND lifecycle NOT IN ('DELETING', 'DELETED')) "
    "ON CONFLICT (run_id, map_id) DO UPDATE SET "
    + ", ".join(f"{c} = EXCLUDED.{c}" for c in COLUMNS[2:-1]) + ", created_at = now()"
)


def encode(row: CellRow) -> tuple:
    """The INSERT_SQL parameters of a row."""
    ax, ay = cr.key_cells(row.key)
    return (row.run_id, row.map_id, row.robot_name, row.robot_class, row.speed_max, row.started_at, row.ended_at,
            row.source, row.algo_version, row.cell_m, json.dumps(row.params), json.dumps(row.frames),
            ax.astype(np.int64).tolist(), ay.astype(np.int64).tolist(),
            np.asarray(row.len_m, float).tolist(), np.asarray(row.time_s, float).tolist(),
            np.asarray(row.stopped_s, float).tolist(), np.asarray(row.recovery_s, float).tolist(),
            np.asarray(row.blocks, np.int64).tolist(), json.dumps(row.totals), row.map_id)


def decode(values: Sequence[Any]) -> CellRow:
    """A CellRow from one SELECT of COLUMNS."""
    d = dict(zip(COLUMNS, values))
    ix = np.asarray(d["ix"], np.int64)
    iy = np.asarray(d["iy"], np.int64)
    return CellRow(
        run_id=d["run_id"], map_id=d["map_id"], robot_name=d["robot_name"], robot_class=d["robot_class"],
        speed_max=None if d["speed_max"] is None else float(d["speed_max"]), started_at=d["started_at"],
        ended_at=d["ended_at"], source=d["source"], algo_version=int(d["algo_version"]), cell_m=float(d["cell_m"]),
        params=_json(d["params"]), frames=_json(d["frames"]), key=cr.cell_key(ix, iy),
        len_m=np.asarray(d["len_m"], float), time_s=np.asarray(d["time_s"], float),
        stopped_s=np.asarray(d["stopped_s"], float), recovery_s=np.asarray(d["recovery_s"], float),
        blocks=np.asarray(d["blocks"], np.int64), totals=_json(d["totals"]), created_at=d["created_at"])


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, (str, bytes)) else value


async def insert(cursor: Any, row: CellRow) -> bool:
    """Store (or replace) the summary; False when nothing was written (the map is gone or being deleted)."""
    await cursor.execute(INSERT_SQL, encode(row))
    return cursor.rowcount > 0


def window_sql(start: Optional[datetime.datetime], end: Optional[datetime.datetime], robot: Optional[str],
               robot_class: Optional[str], args: List[Any], alias: str = "") -> str:
    """The WHERE tail (starting with ` AND`) selecting runs by start time, robot and class; appends to `args`."""
    p = alias + "." if alias else ""
    sql = ""
    if start is not None:
        sql += f" AND {p}started_at >= %s"
        args.append(start)
    if end is not None:
        sql += f" AND {p}started_at < %s"
        args.append(end)
    if robot:
        sql += f" AND {p}robot_name = %s"
        args.append(robot)
    if robot_class is not None:
        sql += f" AND {p}robot_class = %s"
        args.append(robot_class)
    return sql


async def fetch(cursor: Any, map_id: str, start: Optional[datetime.datetime] = None,
                end: Optional[datetime.datetime] = None, robot: Optional[str] = None,
                robot_class: Optional[str] = None, limit: Optional[int] = None,
                algo_version: Optional[int] = None) -> List[CellRow]:
    """The map's summaries with `start <= started_at < end` (open ends allowed), optionally of one robot / one
    class / one algo version, newest first, at most `limit`."""
    args: List[Any] = [map_id]
    sql = f"SELECT {', '.join(COLUMNS)} FROM run_cells WHERE map_id = %s"
    if algo_version is not None:
        sql += " AND algo_version = %s"
        args.append(algo_version)
    sql += window_sql(start, end, robot, robot_class, args) + " ORDER BY started_at DESC, run_id"
    if limit is not None:
        sql += " LIMIT %s"
        args.append(limit)
    await cursor.execute(sql, tuple(args))
    return [decode(r) for r in await cursor.fetchall()]
