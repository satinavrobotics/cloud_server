"""Per-region driving statistics from the stored per-run cell summaries: table region_stats, GET
/api/v1/maps/{map_id}/region-stats. Contract: docs/region_stats_2026-10-27.md (sections 1, 6.3, 6.4).

Source of truth is `run_cells` (one cell summary per run, packages/api/run_cells.py). This module only GROUPS those
cells by the owner raster of a graph version into charts, so it never depends on how charts split or merge: a
version's rows are a rebuildable cache, dropped and recomputed from `run_cells` at any time.

`collect` (read side, also used by the route for a robot filter) maps every summarised cell of a map to its chart
with the same owner-raster lookup drive-stats uses (`drive_stats.regions_of`), and `aggregate` (pure) turns that into
rows:
* A (run, chart) pair is a SAMPLE when the run drove at least REGION_STATS_MIN_LEN_M in the chart (drops boundary
  clips). Per (chart, robot_class) the newest REGION_STATS_MAX_RUNS samples by the run's start are kept; the
  robot_class '*' pools all classes and keeps the newest MAX_RUNS samples of the chart over all of them.
* Per (chart, class): sums over the kept samples; pace = time_s / len_m PER SAMPLE and its median / p90 (linear
  percentile) over the samples; moving pace the same with time_s - stopped_s; ratios from the sums (NULL on a zero
  denominator); `runs` = the kept samples; `visited` = distinct 0.1 m cells of the chart with movement in them;
  `speed_max` = the class's factsheet speed_max when all its samples agree (NULL for '*' or unknown).
* `stamp` = sha1 (16 hex) of (algo_version, window, min length, max runs, sorted (run_id, created_at) of the inputs).
  Day decay, priors and shrinkage are the planner's job (it has `runs`).

`build_map` writes one (map, graph version) in one transaction (delete + insert + its region_stats_builds row) and
keeps the newest REGION_STATS_KEEP_VERSIONS versions. `maps_needing_build` names the maps whose newest build is not
of the current graph version, or is marked stale (a run was deleted); the run_cells worker rebuilds them.
Only the CURRENT graph version can be built: the owner raster of an older one is not served.
"""
import asyncio
import dataclasses
import datetime
import hashlib
import json
import logging
import uuid
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
from fastapi import HTTPException

from cloud_common.objects.map import MapObjectV1
from packages import config
from packages.api import drive_stats, fleet_reads, maps
from packages.api.result_cache import ResultCache
from packages.utils import cell_raster as cr
from packages.utils import run_cells_db as rdb
from packages.utils.timeutil import utcnow

logger = logging.getLogger("ApiDelegationService.region_stats")

POOLED = "*"
NO_GRAPH_VERSION = 0                     # graph_version of the build row of a map without a scene graph
COLUMNS = ("map_id", "graph_version", "chart_key", "robot_class", "runs", "len_m", "time_s", "stopped_s",
           "recovery_s", "blocks", "pace_median_s_per_m", "pace_p90_s_per_m", "moving_pace_median_s_per_m",
           "moving_speed_mps", "stopped_share", "recovery_s_per_100m", "blocks_per_run", "speed_max", "cells",
           "visited", "first_at", "last_at", "stamp")
INSERT_SQL = (f"INSERT INTO region_stats ({', '.join(COLUMNS)}) VALUES ({', '.join(['%s'] * len(COLUMNS))})")
LIVE_MAP_SQL = (f"SELECT 1 FROM {MapObjectV1.table_name()} WHERE name = %s "
                "AND lifecycle NOT IN ('DELETING', 'DELETED') FOR SHARE")
CACHE = ResultCache(config.REGION_STATS_CACHE_ENTRIES, config.REGION_STATS_CACHE_TTL_S)


@dataclasses.dataclass
class Collected:
    """The grouped input of a build: `status` "ok" | "none" (no scene graph); `rows` the summaries."""
    status: str
    version: Optional[int]
    rows: List[rdb.CellRow]
    stamp: str
    stats: List[Dict[str, Any]]


def stamp_of(rows: Sequence[rdb.CellRow]) -> str:
    ident = [config.RUN_CELLS_ALGO_VERSION, config.REGION_STATS_WINDOW_DAYS, config.REGION_STATS_MIN_LEN_M,
             config.REGION_STATS_MAX_RUNS, sorted((str(r.run_id), r.created_at.isoformat() if r.created_at else "")
                                                  for r in rows)]
    return hashlib.sha1(json.dumps(ident, separators=(",", ":")).encode()).hexdigest()[:16]


def _percentiles(values: np.ndarray) -> Tuple[Optional[float], Optional[float]]:
    if not values.size:
        return None, None
    p50, p90 = np.percentile(values, [50, 90])
    return float(p50), float(p90)


def _ratio(num: float, den: float) -> Optional[float]:
    return float(num / den) if den > 0 else None


def aggregate(map_id: str, version: int, rows: Sequence[rdb.CellRow], ids_per_row: Sequence[np.ndarray],
              names: Sequence[str], cells: Dict[str, int], stamp: str) -> List[Dict[str, Any]]:
    """The region_stats rows (dicts of COLUMNS) of `rows`; `ids_per_row[i]` = the chart id (0 = none, k =
    names[k-1]) of every cell of rows[i]."""
    samples: Dict[str, List[Dict[str, Any]]] = {}
    visited: Dict[Tuple[str, int], np.ndarray] = {}
    for i, (row, ids) in enumerate(zip(rows, ids_per_row)):
        if not len(row):
            continue
        n = len(names) + 1
        sums = {f: np.bincount(ids, weights=getattr(row, f), minlength=n)
                for f in ("len_m", "time_s", "stopped_s", "recovery_s", "blocks")}
        for k in np.nonzero(sums["len_m"][1:] >= config.REGION_STATS_MIN_LEN_M)[0] + 1:
            samples.setdefault(names[k - 1], []).append({
                "row": row, "len_m": float(sums["len_m"][k]), "time_s": float(sums["time_s"][k]),
                "stopped_s": float(sums["stopped_s"][k]), "recovery_s": float(sums["recovery_s"][k]),
                "blocks": int(round(sums["blocks"][k])), "i": i, "k": int(k)})
    out: List[Dict[str, Any]] = []
    for chart in sorted(samples):
        pool = sorted(samples[chart], key=lambda s: (s["row"].started_at, str(s["row"].run_id)), reverse=True)
        by_class: Dict[str, List[Dict[str, Any]]] = {}
        for s in pool:
            by_class.setdefault(s["row"].robot_class, []).append(s)
        groups = [(cls, v[:config.REGION_STATS_MAX_RUNS]) for cls, v in sorted(by_class.items())]
        groups.append((POOLED, pool[:config.REGION_STATS_MAX_RUNS]))
        for cls, kept in groups:
            out.append(_stat_row(map_id, version, chart, cls, kept, rows, ids_per_row, cells.get(chart, 0), stamp))
    return out


def _stat_row(map_id: str, version: int, chart: str, cls: str, kept: List[Dict[str, Any]],
              rows: Sequence[rdb.CellRow], ids_per_row: Sequence[np.ndarray], chart_cells: int, stamp: str
              ) -> Dict[str, Any]:
    len_m, time_s = sum(s["len_m"] for s in kept), sum(s["time_s"] for s in kept)
    stopped, rec = sum(s["stopped_s"] for s in kept), sum(s["recovery_s"] for s in kept)
    blocks = sum(s["blocks"] for s in kept)
    pace = np.array([s["time_s"] / s["len_m"] for s in kept])
    moving = np.array([(s["time_s"] - s["stopped_s"]) / s["len_m"] for s in kept if s["time_s"] - s["stopped_s"] > 0])
    p50, p90 = _percentiles(pace)
    m50, _ = _percentiles(moving)
    cell_keys = [rows[s["i"]].key[(ids_per_row[s["i"]] == s["k"]) & ((rows[s["i"]].len_m > 0) | (rows[s["i"]].time_s > 0))]
                 for s in kept]
    speeds = {s["row"].speed_max for s in kept}
    return {"map_id": map_id, "graph_version": version, "chart_key": chart, "robot_class": cls, "runs": len(kept),
            "len_m": len_m, "time_s": time_s, "stopped_s": stopped, "recovery_s": rec, "blocks": blocks,
            "pace_median_s_per_m": p50, "pace_p90_s_per_m": p90, "moving_pace_median_s_per_m": m50,
            "moving_speed_mps": _ratio(len_m, time_s - stopped), "stopped_share": _ratio(stopped, time_s),
            "recovery_s_per_100m": _ratio(rec * 100.0, len_m), "blocks_per_run": blocks / len(kept),
            "speed_max": (None if cls == POOLED or len(speeds) != 1 or None in speeds else float(speeds.pop())),
            "cells": int(chart_cells), "visited": int(np.unique(np.concatenate(cell_keys)).size),
            "first_at": min(s["row"].started_at for s in kept), "last_at": max(s["row"].started_at for s in kept),
            "stamp": stamp}


async def current_version(reads: Any, map_id: str) -> Optional[int]:
    """The map's current scene-graph version from the stored manifest (no tile read); None when there is no graph
    or the read side cannot tell."""
    if reads is None:
        return None
    try:
        topo = await reads.topology(map_id)
    except Exception as exc:  # pylint: disable=broad-except
        logger.info("Region stats of %s: no graph version (%s)", map_id, exc)
        return None
    return None if topo is None else int(topo["version"])


async def collect(db: Any, reads: Any, map_id: str, robot: Optional[str] = None) -> Collected:
    """Read the map's summaries of the window and group them by the current owner raster. Raises when the
    scene-graph read fails (a build must not store a wrong version)."""
    start = utcnow() - datetime.timedelta(days=config.REGION_STATS_WINDOW_DAYS)
    async with fleet_reads.read_cursor(db) as cur:
        rows = await rdb.fetch(cur, map_id, start=start, robot=robot, algo_version=config.RUN_CELLS_ALGO_VERSION)
    stamp = stamp_of(rows)
    if reads is None:
        return Collected("none", None, rows, stamp, [])
    keys = [r.key for r in rows if len(r)]
    uniq = np.unique(np.concatenate(keys)) if keys else np.empty(0, np.int64)
    if uniq.size:
        ids, names, cells, version, status = await drive_stats.regions_of(reads, map_id, *cr.key_cells(uniq))
        if status == "error":
            raise RuntimeError(f"scene graph read of {map_id} failed")
        if status == "none" or ids is None:
            return Collected("none", None, rows, stamp, [])
    else:
        version = await current_version(reads, map_id)
        if version is None:
            return Collected("none", None, rows, stamp, [])
        ids, names, cells = np.empty(0, np.int64), [], {}
    per_row = [ids[np.searchsorted(uniq, r.key)] if len(r) else np.empty(0, np.int64) for r in rows]
    stats = await asyncio.to_thread(aggregate, map_id, int(version), rows, per_row, names, cells, stamp)
    return Collected("ok", int(version), rows, stamp, stats)


def _params(stat: Dict[str, Any]) -> tuple:
    return tuple(stat[c] for c in COLUMNS)


async def build_map(db: Any, reads: Any, map_id: str, version: Optional[int] = None) -> Dict[str, Any]:
    """Rebuild the region statistics of `map_id`'s current graph version from run_cells and store them. `version`
    (optional) must equal the current one (older owner rasters are not served): LookupError otherwise.
    Returns {graph_version, status, runs, charts, stamp}."""
    got = await collect(db, reads, map_id)
    if version is not None and got.version is not None and version != got.version:
        raise LookupError(f"graph version {version} is not the current one ({got.version})")
    ver = NO_GRAPH_VERSION if got.version is None else got.version
    status = "ok" if got.status == "ok" else "no_scene_graph"
    runs = len({str(r.run_id) for r in got.rows})
    async with db.connection() as conn:
        async with conn.cursor() as cur:
            # A map deleted (or being deleted) while the build ran must not get rows back: map_delete removes them
            # once it holds the row; FOR SHARE makes a marking that starts now wait for this transaction.
            await cur.execute(LIVE_MAP_SQL, (map_id,))
            if await cur.fetchone() is None:
                logger.info("Region stats of %s: map is gone or being deleted, nothing stored", map_id)
                return {"graph_version": ver, "status": "map_gone", "runs": runs, "charts": 0, "stamp": got.stamp}
            await cur.execute("DELETE FROM region_stats WHERE map_id = %s AND graph_version = %s", (map_id, ver))
            if got.stats:
                await cur.executemany(INSERT_SQL, [_params(s) for s in got.stats])
            await cur.execute(
                "INSERT INTO region_stats_builds (map_id, graph_version, stamp, runs, status, detail, built_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, now()) ON CONFLICT (map_id, graph_version) DO UPDATE SET "
                "stamp = EXCLUDED.stamp, runs = EXCLUDED.runs, status = EXCLUDED.status, detail = EXCLUDED.detail, "
                "built_at = now()", (map_id, ver, got.stamp, runs, status, None))
            await cur.execute(
                "DELETE FROM region_stats WHERE map_id = %s AND graph_version NOT IN "
                "(SELECT graph_version FROM region_stats_builds WHERE map_id = %s "
                "ORDER BY graph_version DESC LIMIT %s)", (map_id, map_id, config.REGION_STATS_KEEP_VERSIONS))
            await cur.execute(
                "DELETE FROM region_stats_builds WHERE map_id = %s AND graph_version NOT IN "
                "(SELECT graph_version FROM region_stats_builds WHERE map_id = %s "
                "ORDER BY graph_version DESC LIMIT %s)", (map_id, map_id, config.REGION_STATS_KEEP_VERSIONS))
    CACHE.discard_map(map_id)
    logger.info("Region stats of %s: version %s, %d runs, %d rows (%s)", map_id, ver, runs, len(got.stats), status)
    return {"graph_version": ver, "status": status, "runs": runs, "charts": len(got.stats), "stamp": got.stamp}


async def maps_needing_build(db: Any, reads: Any) -> List[str]:
    """Maps with summaries in the window whose newest build is not of the current graph version or is stale."""
    start = utcnow() - datetime.timedelta(days=config.REGION_STATS_WINDOW_DAYS)
    async with fleet_reads.read_cursor(db) as cur:
        await cur.execute("SELECT DISTINCT map_id FROM run_cells WHERE started_at >= %s AND algo_version = %s",
                          (start, config.RUN_CELLS_ALGO_VERSION))
        wanted = {r[0] for r in await cur.fetchall()}
        await cur.execute("SELECT map_id, graph_version, status FROM region_stats_builds")
        builds: Dict[str, Dict[int, str]] = {}
        for map_id, ver, status in await cur.fetchall():
            builds.setdefault(map_id, {})[int(ver)] = status
            if status == "stale":           # its last runs may be deleted: the rebuild empties the stored rows
                wanted.add(map_id)
    wanted = sorted(wanted)
    out = []
    for map_id in wanted:
        have = builds.get(map_id, {})
        ver = await current_version(reads, map_id)
        if ver is None:
            if not have or "stale" in have.values():
                out.append(map_id)
        elif have.get(ver) != "ok":
            out.append(map_id)
    return out


# --- read route ----------------------------------------------------------------------------------

def _row_json(values: Sequence[Any]) -> Dict[str, Any]:
    d = dict(zip(COLUMNS, values))
    for k in ("first_at", "last_at"):
        d[k] = d[k].isoformat() if d[k] is not None else None
    return d


def _etag(raw: bytes) -> str:
    return '"' + hashlib.sha1(raw).hexdigest()[:20] + '"'


async def region_stats(db: Any, reads: Any, map_id: str, robot_class: Optional[str] = None,
                       version: Optional[int] = None, robot: Optional[str] = None,
                       cache: Optional[ResultCache] = None) -> Tuple[bytes, str]:
    """(JSON body, ETag) of GET /api/v1/maps/{map_id}/region-stats. 404 unknown map or a version that is neither
    stored nor current. Default: the stored rows of the newest built version; `robot` (display) or a current
    version not stored yet is computed on read, cached per (map, version, robot, stamp of the inputs)."""
    cls = POOLED if robot_class is None else robot_class
    try:
        async with maps.open_store(db, uuid.uuid4()) as store:
            if await store.get_map(map_id) is None:
                raise HTTPException(404, f"Did not find \"map\" with name \"{map_id}\"")
    except maps._SCHEMA_ERRORS as exc:
        raise maps._undefined_table(exc) from exc
    current = await current_version(reads, map_id)
    meta = {"window_days": config.REGION_STATS_WINDOW_DAYS, "min_len_m": config.REGION_STATS_MIN_LEN_M,
            "max_runs": config.REGION_STATS_MAX_RUNS, "current_graph_version": current}
    stored: Optional[tuple] = None
    if robot is None:
        async with fleet_reads.read_cursor(db) as cur:
            sql = "SELECT graph_version, stamp, built_at, status, runs FROM region_stats_builds WHERE map_id = %s"
            args: List[Any] = [map_id]
            if version is not None:
                sql += " AND graph_version = %s"
                args.append(version)
            await cur.execute(sql + " ORDER BY graph_version DESC LIMIT 1", tuple(args))
            stored = await cur.fetchone()
            if stored is not None:
                await cur.execute(f"SELECT {', '.join(COLUMNS)} FROM region_stats WHERE map_id = %s AND graph_version = %s "
                                  "AND robot_class = %s ORDER BY chart_key", (map_id, stored[0], cls))
                regions = [_row_json(r) for r in await cur.fetchall()]
        if stored is not None:
            ver, stamp, built_at, status, runs = stored
            body = dict(meta, map_id=map_id, graph_version=int(ver), stamp=stamp, built_at=built_at.isoformat(),
                        stale=bool(status == "stale" or (current is not None and int(ver) != current)),
                        status=status, robot_class=cls, robot=None, source="stored", runs=int(runs), regions=regions)
            raw = json.dumps(body, separators=(",", ":")).encode()
            return raw, _etag(raw)
    if version is not None and version != current:
        raise HTTPException(404, f"Region statistics of graph version {version} of \"{map_id}\" are not stored")
    cache = CACHE if cache is None else cache

    async def run() -> Tuple[bytes, str]:
        got = await collect(db, reads, map_id, robot=robot)
        regions = [_row_json(_params(s)) for s in got.stats if s["robot_class"] == cls]
        body = dict(meta, map_id=map_id, graph_version=got.version, stamp=got.stamp,
                    built_at=utcnow().isoformat(), stale=False,
                    status="ok" if got.status == "ok" else "no_scene_graph", robot_class=cls, robot=robot,
                    source="computed", runs=len({str(r.run_id) for r in got.rows}), regions=regions)
        raw = json.dumps(body, separators=(",", ":")).encode()
        return raw, _etag(raw)
    # the stamp of the inputs is part of the key: a new summary changes it, so it needs one cheap read first
    async with fleet_reads.read_cursor(db) as cur:
        start = utcnow() - datetime.timedelta(days=config.REGION_STATS_WINDOW_DAYS)
        args2: List[Any] = [map_id, config.RUN_CELLS_ALGO_VERSION, start]
        sql2 = ("SELECT run_id, created_at FROM run_cells WHERE map_id = %s AND algo_version = %s AND started_at >= %s")
        if robot:
            sql2 += " AND robot_name = %s"
            args2.append(robot)
        await cur.execute(sql2, tuple(args2))
        ident = sorted((str(r[0]), r[1].isoformat()) for r in await cur.fetchall())
    key = (map_id, current, robot, cls, hashlib.sha1(json.dumps(ident).encode()).hexdigest()[:16])
    return await cache.get(key, run)
