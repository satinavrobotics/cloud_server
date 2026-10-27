"""The planner's side of the region statistics: a pure loader of one (map, graph version, robot class).

    stats = load(cur, map_id, graph_version, robot_class)      # sync cursor
    stats = await aload(cur, map_id, graph_version, robot_class)  # async cursor
    stats.charts["<chart_key>"].pace_median_s_per_m ...

No API imports (packages/api/region_stats.py writes the table, docs/region_stats_2026-10-27.md describes it); the
planner calls it once per (map, graph version, `stamp`) and keeps the result, so pricing a route costs a dict lookup
per chart. `robot_class` comes from packages.utils.robot_class.robot_class(factsheet) of the planning robot; '*'
(the default) pools all classes. A chart without a row has no statistics for that class (fall back to '*' or a
prior: the planner's choice). `stamp` and `built_at` identify the build; `runs` is the number of samples behind a
chart's figures so the planner can shrink thin ones. Day decay, priors and shrinkage are not stored.

The `norm_*` figures are what the planner prices with (packages/utils/region_cost.py): sums over the samples whose run
had a factsheet `speed_max` (speed_basis "factsheet"), each time multiplied by that run's OWN speed_max, so runs of
different speeds add up: `norm_len_m` the metres, `norm_moving_m` the moving seconds x speed_max (moving = time - stopped
- dwell), `norm_stopped_m` the stopped seconds x speed_max, `norm_runs` the samples. norm_moving_m / norm_len_m is 1 for a
robot that drives at its speed_max throughout; the moving excess of a chart is that ratio minus 1. A pooled ('*') row
holds the same sums over all classes.
"""
import dataclasses
import datetime
from typing import Any, Dict, Optional, Sequence

POOLED = "*"
FIELDS = ("chart_key", "robot_class", "runs", "len_m", "time_s", "stopped_s", "recovery_s", "blocks",
          "pace_median_s_per_m", "pace_p90_s_per_m", "moving_pace_median_s_per_m", "moving_speed_mps",
          "stopped_share", "recovery_s_per_100m", "blocks_per_run", "speed_max", "cells", "visited", "first_at",
          "last_at", "stamp", "dwell_s", "norm_runs", "norm_len_m", "norm_moving_m", "norm_stopped_m")
SQL = (f"SELECT {', '.join(FIELDS)} FROM region_stats WHERE map_id = %s AND graph_version = %s AND robot_class = %s")
BUILD_SQL = "SELECT stamp, status, built_at FROM region_stats_builds WHERE map_id = %s AND graph_version = %s"


@dataclasses.dataclass(frozen=True)
class RegionStat:
    chart_key: str
    robot_class: str
    runs: int
    len_m: float
    time_s: float
    stopped_s: float
    recovery_s: float
    blocks: int
    pace_median_s_per_m: Optional[float]
    pace_p90_s_per_m: Optional[float]
    moving_pace_median_s_per_m: Optional[float]
    moving_speed_mps: Optional[float]
    stopped_share: Optional[float]
    recovery_s_per_100m: Optional[float]
    blocks_per_run: Optional[float]
    speed_max: Optional[float]
    cells: int
    visited: int
    first_at: Optional[datetime.datetime]
    last_at: Optional[datetime.datetime]
    stamp: str
    # waiting near the start, goal and action nodes (not in stopped_s) and the speed-normalised sums (below)
    dwell_s: float = 0.0
    norm_runs: int = 0
    norm_len_m: float = 0.0
    norm_moving_m: float = 0.0
    norm_stopped_m: float = 0.0


@dataclasses.dataclass(frozen=True)
class RegionStats:
    """`charts` by chart key; `stamp` / `status` / `built_at` of the build (None when the version was never built)."""
    map_id: str
    graph_version: int
    robot_class: str
    charts: Dict[str, RegionStat]
    stamp: Optional[str] = None
    status: Optional[str] = None
    built_at: Optional[datetime.datetime] = None


def parse(map_id: str, graph_version: int, robot_class: str, rows: Sequence[Sequence[Any]],
          build: Optional[Sequence[Any]]) -> RegionStats:
    charts = {r[0]: RegionStat(**dict(zip(FIELDS, r))) for r in rows}
    stamp, status, built_at = build if build else (None, None, None)
    return RegionStats(map_id, graph_version, robot_class, charts, stamp, status, built_at)


def load(cur: Any, map_id: str, graph_version: int, robot_class: str = POOLED) -> RegionStats:
    cur.execute(SQL, (map_id, graph_version, robot_class))
    rows = cur.fetchall()
    cur.execute(BUILD_SQL, (map_id, graph_version))
    return parse(map_id, graph_version, robot_class, rows, cur.fetchone())


async def aload(cur: Any, map_id: str, graph_version: int, robot_class: str = POOLED) -> RegionStats:
    await cur.execute(SQL, (map_id, graph_version, robot_class))
    rows = await cur.fetchall()
    await cur.execute(BUILD_SQL, (map_id, graph_version))
    return parse(map_id, graph_version, robot_class, rows, await cur.fetchone())
