"""Per-tile and per-region driving statistics of a map: GET /api/v1/maps/{map_id}/drive-stats.

Contract and rules: docs/drive_stats_2026-10-14.md (and docs/API_REFERENCE.md). Read only.

Source: the robots' RECORDED trajectories on the map, in the map frame.
* Track runs: `robot_track_ts` rows with a stored map pose (`x_map`, `y_map`; tracking redesign stage 1) of
  the runs on this map. Each pair of consecutive rows of one run (same session and run epoch) is a movement
  of dt seconds over a straight segment; it is attributed to the 0.1 m cells the segment crosses (the
  segment is sampled every cell/4, each sample carries an equal share of length and time).
* Leg runs: a run on this map WITHOUT any stored map-frame track row falls back to its `run_legs`
  (leaf node to leaf node straight line, the leg's duration / stopped / recovery seconds). A run is never
  counted from both sources.
* Recovery seconds: the intervals of the run's `NAV.RECOVERY_EXITED` events (`ts - duration_s .. ts`), spread
  over the segments they overlap.
* Regions: the scene graph's owner raster of the current version looked up per tile (one cell = one tile).

The numpy work runs in a thread; the answer is cached (map, window, robot, min_speed) for
DRIVE_STATS_CACHE_TTL_S and carries an ETag.
"""
import asyncio
import collections
import datetime
import hashlib
import json
import logging
import math
import uuid
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
from fastapi import HTTPException

from packages import config
from packages.api import fleet_reads, maps, series_axis
from packages.api.result_cache import ResultCache

logger = logging.getLogger("ApiDelegationService.drive_stats")

TILE_GRID_M = 6.4             # scene-graph store tile: raster origins lie on this grid
REGION_CHUNK_CELLS = 1600     # 160 m = 25 store tiles: one owner-plane request per chunk (4M-cell cap is 2000)


# --- pure core: moved to packages/utils/cell_raster.py, re-exported here --------------------------
# (the rasteriser is shared with the per-run cell summaries, packages/api/run_cells.py)
from packages.utils.cell_raster import (  # noqa: E402,F401  pylint: disable=unused-import
    CELL_M, CHUNK_SEGMENTS, MAX_SAMPLES_PER_SEGMENT, SAMPLE_DIV, _OFF, Accumulator, Params, Segments, Tiles,
    cell_key, key_cells, leg_movements, recovery_overlap, track_segments)


# --- regions -------------------------------------------------------------------------------------

def plane_lookup(plane: Dict[str, Any], ax: np.ndarray, ay: np.ndarray) -> np.ndarray:
    """Owner-plane value (0 = no chart, i = keys[i-1]) at absolute cells (ax, ay); 0 outside the plane."""
    rows, cols = plane["shape"]
    index = np.asarray(plane["index"]).reshape(rows, cols)
    step = float(plane["step"])
    cx, cy = int(round(plane["origin"][0] / step)), int(round(plane["origin"][1] / step))
    c, r = ax - cx, ay - cy
    ok = (c >= 0) & (c < cols) & (r >= 0) & (r < rows)
    out = np.zeros(ax.size, np.int64)
    out[ok] = index[r[ok], c[ok]]
    return out


async def regions_of(reads: Any, map_id: str, ax: np.ndarray, ay: np.ndarray
                     ) -> Tuple[Optional[np.ndarray], List[str], Dict[str, int], Optional[int], str]:
    """Chart of every tile. Returns (region id per tile (0 = none, i = names[i-1]), names, cells per chart key,
    graph version, status "ok" | "none" | "error"). Never raises: a missing graph or a failing read is a status."""
    names: List[str] = []
    cells: Dict[str, int] = {}
    ids = np.zeros(ax.size, np.int64)
    version: Optional[int] = None
    if not ax.size:
        return ids, names, cells, version, "ok"
    from packages.scene_graph.api import BBox
    index_of: Dict[str, int] = {}
    chunk = (np.floor_divide(ax, REGION_CHUNK_CELLS) << 20) + np.floor_divide(ay, REGION_CHUNK_CELLS) + (1 << 19)
    try:
        for c in np.unique(chunk):
            sel = np.nonzero(chunk == c)[0]
            cx, cy = int(np.floor_divide(ax[sel[0]], REGION_CHUNK_CELLS)), int(np.floor_divide(ay[sel[0]], REGION_CHUNK_CELLS))
            span = REGION_CHUNK_CELLS * CELL_M
            box = BBox(round(cx * span, 6), round(cy * span, 6), round((cx + 1) * span, 6), round((cy + 1) * span, 6))
            plane = await reads.owner_plane(map_id, box, 0)
            if plane is None:
                return None, [], {}, None, "none"
            if abs(float(plane["step"]) - CELL_M) > 1e-9:
                logger.warning("Drive stats: owner raster step %s is not %s; no regions", plane["step"], CELL_M)
                return None, [], {}, None, "error"
            version = plane["version"] if version is None else version
            view = await reads.view(map_id, box, 0, raster=False, parents=False)
            for ch in view.get("charts", []):
                cells[ch["key"]] = int(ch.get("cells") or 0)
            vals = await asyncio.to_thread(plane_lookup, plane, ax[sel], ay[sel])
            keys = plane["keys"]
            glob = np.zeros(len(keys) + 1, np.int64)
            for i, name in enumerate(keys, 1):
                if name not in index_of:
                    names.append(name)
                    index_of[name] = len(names)
                glob[i] = index_of[name]
            ids[sel] = glob[np.clip(vals, 0, len(keys))]
    except HTTPException as exc:
        logger.info("Drive stats of %s: no regions (%s %s)", map_id, exc.status_code, exc.detail)
        return None, [], {}, None, "error"
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Drive stats of %s: no regions: %s", map_id, exc)
        return None, [], {}, None, "error"
    return ids, names, cells, version, "ok"


def region_rows(tiles: Tiles, ids: np.ndarray, names: List[str], cells: Dict[str, int]) -> List[Dict[str, Any]]:
    """Per chart: distinct runs through it, summed length / time / recovery / stopped, the chart's cells and
    how many of its 0.1 m cells were driven (`visited`)."""
    sel = np.nonzero(ids > 0)[0]
    if not sel.size:
        return []
    rid = ids[sel]
    order = np.unique(rid)
    out = []
    pair_region = ids[tiles.pair_tile]
    pk = np.unique(pair_region.astype(np.int64) * (int(tiles.pair_run.max()) + 1 if tiles.pair_run.size else 1)
                   + tiles.pair_run)
    runs_max = (int(tiles.pair_run.max()) + 1) if tiles.pair_run.size else 1
    n_by_region = np.bincount(pk // runs_max, minlength=int(ids.max()) + 1)
    for r in order:
        m = rid == r
        s = tiles.sums[:, sel[m]].sum(axis=1)
        name = names[int(r) - 1]
        out.append({"key": name, "n": int(n_by_region[r]), "len_m": round(float(s[0]), 2),
                    "time_s": round(float(s[1]), 1), "recovery_s": round(float(s[2]), 1),
                    "stopped_s": round(float(s[3]), 1), "cells": cells.get(name, 0), "visited": int(m.sum())})
    out.sort(key=lambda d: d["key"])
    return out


# --- data access ---------------------------------------------------------------------------------

def _epoch(ts: datetime.datetime) -> float:
    return ts.timestamp()


def _window_sql(column: str, start: Optional[datetime.datetime], end: Optional[datetime.datetime],
                robot: Optional[str], robot_column: str, args: List[Any]) -> str:
    sql = ""
    if start is not None:
        sql += f" AND {column} >= %s"
        args.append(start)
    if end is not None:
        sql += f" AND {column} < %s"
        args.append(end)
    if robot:
        sql += f" AND {robot_column} = %s"
        args.append(robot)
    return sql


async def fetch_track(cur: Any, map_id: str, start, end, robot, limit: int) -> Tuple[List[tuple], bool]:
    """Newest `limit` rows (run_id, epoch s, x_map, y_map, session, run_epoch) of the runs on `map_id` with a
    stored map pose, and whether the cap cut older rows."""
    args: List[Any] = [map_id]
    sql = ("SELECT t.run_id, extract(epoch FROM t.ts)::float8, t.x_map, t.y_map, t.map_session_id, t.run_epoch "
           "FROM robot_track_ts t JOIN mission_runs r ON r.run_id = t.run_id "
           "WHERE r.map_id = %s AND t.x_map IS NOT NULL AND t.y_map IS NOT NULL"
           + _window_sql("t.ts", start, end, robot, "t.robot_name", args)
           + " ORDER BY t.ts DESC LIMIT %s")
    args.append(limit + 1)
    await cur.execute(sql, tuple(args))
    rows = await cur.fetchall()
    return list(rows[:limit]), len(rows) > limit


async def fetch_recoveries(cur: Any, run_ids: Sequence[Any], t_min: float, t_max: float) -> List[tuple]:
    """(run_id, exit epoch s, duration_s) of NAV.RECOVERY_EXITED events of the runs around the rows' span."""
    if not run_ids:
        return []
    lo = datetime.datetime.fromtimestamp(t_min - 3600, datetime.timezone.utc)
    hi = datetime.datetime.fromtimestamp(t_max + 3600, datetime.timezone.utc)
    await cur.execute(
        "SELECT run_id, extract(epoch FROM ts)::float8, (payload->>'duration_s')::float8 FROM fleet_events "
        "WHERE code = 'NAV.RECOVERY_EXITED' AND run_id = ANY(%s) AND ts >= %s AND ts < %s",
        (list(run_ids), lo, hi))
    return list(await cur.fetchall())


async def fetch_legs(cur: Any, map_id: str, start, end, robot) -> List[tuple]:
    args: List[Any] = [map_id]
    sql = ("SELECT run_id, from_topomap_node, to_topomap_node, duration_s, stopped_s, recovery_s "
           "FROM run_legs WHERE map_id = %s AND from_topomap_node IS NOT NULL AND to_topomap_node IS NOT NULL "
           "AND duration_s > 0" + _window_sql("started_at", start, end, robot, "robot_name", args)
           + " ORDER BY started_at DESC LIMIT %s")
    args.append(config.DRIVE_STATS_MAX_ROWS)
    await cur.execute(sql, tuple(args))
    return list(await cur.fetchall())


async def runs_with_track(cur: Any, run_ids: Sequence[Any]) -> set:
    """Runs (of `run_ids`) that have ANY stored map-frame track row, in or out of the window."""
    if not run_ids:
        return set()
    await cur.execute("SELECT DISTINCT run_id FROM robot_track_ts WHERE run_id = ANY(%s) AND x_map IS NOT NULL",
                      (list(run_ids),))
    return {r[0] for r in await cur.fetchall()}


# --- the computation -----------------------------------------------------------------------------

class Loaded:
    """What the database gave, before any numpy."""

    def __init__(self) -> None:
        self.track_rows: List[tuple] = []
        self.partial = False
        self.recoveries: List[tuple] = []
        self.legs: List[tuple] = []
        self.legs_dropped_for_track = 0


async def load(db: Any, map_id: str, start, end, robot) -> Loaded:
    out = Loaded()
    async with fleet_reads.read_cursor(db) as cur:
        out.track_rows, out.partial = await fetch_track(cur, map_id, start, end, robot, config.DRIVE_STATS_MAX_ROWS)
        track_runs = {r[0] for r in out.track_rows}
        if track_runs:
            ts = [r[1] for r in out.track_rows]
            out.recoveries = await fetch_recoveries(cur, sorted(track_runs, key=str), min(ts), max(ts))
        legs = await fetch_legs(cur, map_id, start, end, robot)
        leg_runs = {l[0] for l in legs} - track_runs
        has_track = await runs_with_track(cur, sorted(leg_runs, key=str))
        out.legs = [l for l in legs if l[0] not in track_runs and l[0] not in has_track]
        out.legs_dropped_for_track = len(legs) - len(out.legs)
    return out


def compute(loaded: Loaded, node_xy: Callable[[], Dict[str, Tuple[float, float]]], p: Params
            ) -> Tuple[Tiles, Dict[str, Any], Tiles, Tiles]:
    """Tiles of both sources merged, the source counts, and the two sources' tiles (for the cell shares)."""
    codes: Dict[Any, int] = {}

    def code(run: Any) -> int:
        return codes.setdefault(run, len(codes))

    rows = loaded.track_rows
    track_run_codes = [code(r[0]) for r in rows]
    for leg in loaded.legs:
        code(leg[0])
    n_runs = len(codes)
    src: Dict[str, Any] = {"track_runs": len({r[0] for r in rows}), "track_rows": len(rows), "track_segments": 0,
                           "track_pairs_rejected": 0, "leg_runs": 0, "leg_rows": 0, "legs_skipped": 0,
                           "legs_dropped_run_has_track": loaded.legs_dropped_for_track,
                           "recovery_unattributed_s": 0.0, "partial": loaded.partial}
    acc_t, acc_l = Accumulator(n_runs), Accumulator(n_runs)
    if rows:
        run = np.array(track_run_codes, np.int64)
        grp_codes: Dict[Any, int] = {}
        grp = np.array([grp_codes.setdefault((r[4], r[5]), len(grp_codes)) for r in rows], np.int64)
        ts = np.array([r[1] for r in rows], float)
        x = np.array([r[2] for r in rows], float)
        y = np.array([r[3] for r in rows], float)
        order = np.lexsort((ts, run))
        seg, rejected = track_segments(run[order], grp[order], ts[order], x[order], y[order], p)
        intervals: Dict[int, List[Tuple[float, float]]] = collections.defaultdict(list)
        for rid, t_exit, dur in loaded.recoveries:
            if rid in codes and dur is not None and dur > 0:
                intervals[codes[rid]].append((t_exit - dur, t_exit))
        rec, lost = recovery_overlap(seg, intervals)
        dt = seg.t1 - seg.t0
        dist = np.hypot(seg.x1 - seg.x0, seg.y1 - seg.y0)
        stopped = np.where(dist < p.min_speed * dt, dt, 0.0)
        acc_t.add(seg.run, seg.x0, seg.y0, seg.x1, seg.y1, dt, rec, stopped)
        src.update(track_segments=len(seg), track_pairs_rejected=rejected, recovery_unattributed_s=round(lost, 1))
    if loaded.legs:
        xy = node_xy()
        mv, skipped = leg_movements(
            [{"run": codes[l[0]], "from": str(l[1]), "to": str(l[2]), "duration_s": l[3], "stopped_s": l[4],
              "recovery_s": l[5]} for l in loaded.legs], xy.get, p)
        src.update(leg_rows=len(loaded.legs) - skipped, legs_skipped=skipped, leg_runs=len({l[0] for l in loaded.legs}))
        if mv:
            acc_l.add(mv["run"], mv["x0"], mv["y0"], mv["x1"], mv["y1"], mv["time"], mv["rec"], mv["stopped"])
    tt, tl = acc_t.tiles(), acc_l.tiles()
    merged = acc_t.merged_with(acc_l).tiles()
    src["tiles_track"] = len(tt)
    src["tiles_leg_only"] = int(np.count_nonzero(~np.isin(tl.key, tt.key)))
    return merged, src, tt, tl


def series_day(loaded: Loaded, p: Params, axis: Sequence[datetime.datetime]) -> Dict[str, Any]:
    """The `series` block (docs/map_series_2026-10-15.md): per UTC day of `axis`, from the stored map-frame tracks
    only (legs of runs without a track carry no timestamps). A movement belongs to the day it STARTED.
    `new_tiles` = 0.1 m cells visited that day and on no earlier day OF THE SERIES WINDOW ("first visited" is
    relative to the window, not to all time); `cumulative_tiles` is their running sum (cells visited so far in the
    window). Days without movement: 0 for len_m / time_s / new_tiles, null for the speed and the shares."""
    n = len(axis)
    out: Dict[str, Any] = {"bucket": "day", "t": [t.isoformat() for t in axis], "len_m": [0.0] * n,
                           "time_s": [0.0] * n, "moving_speed_mps": [None] * n, "recovery_share": [None] * n,
                           "stopped_share": [None] * n, "new_tiles": [0] * n, "cumulative_tiles": [0] * n,
                           "source": {"basis": "track", "track_rows": len(loaded.track_rows),
                                      "partial": loaded.partial,
                                      "leg_only_runs_not_in_series": len({l[0] for l in loaded.legs})}}
    rows = loaded.track_rows
    if rows:
        codes: Dict[Any, int] = {}
        run = np.array([codes.setdefault(r[0], len(codes)) for r in rows], np.int64)
        grp_codes: Dict[Any, int] = {}
        grp = np.array([grp_codes.setdefault((r[4], r[5]), len(grp_codes)) for r in rows], np.int64)
        ts = np.array([r[1] for r in rows], float)
        x = np.array([r[2] for r in rows], float)
        y = np.array([r[3] for r in rows], float)
        order = np.lexsort((ts, run))
        seg, _ = track_segments(run[order], grp[order], ts[order], x[order], y[order], p)
        intervals: Dict[int, List[Tuple[float, float]]] = collections.defaultdict(list)
        for rid, t_exit, dur in loaded.recoveries:
            if rid in codes and dur is not None and dur > 0:
                intervals[codes[rid]].append((t_exit - dur, t_exit))
        rec, _lost = recovery_overlap(seg, intervals)
        dt = seg.t1 - seg.t0
        dist = np.hypot(seg.x1 - seg.x0, seg.y1 - seg.y0)
        moving = dist >= p.min_speed * dt
        stopped = np.where(moving, 0.0, dt)
        idx = np.floor((seg.t0 - axis[0].timestamp()) / 86400.0).astype(np.int64)
        ok = (idx >= 0) & (idx < n)
        seen = np.empty(0, np.int64)
        for i in range(n):
            sel = np.nonzero(ok & (idx == i))[0]
            if not sel.size:
                out["cumulative_tiles"][i] = int(seen.size)
                continue
            time_s = float(dt[sel].sum())
            mov = sel[moving[sel]]
            out["len_m"][i] = round(float(dist[sel].sum()), 2)
            out["time_s"][i] = round(time_s, 1)
            if mov.size and dt[mov].sum() > 0:
                out["moving_speed_mps"][i] = round(float(dist[mov].sum() / dt[mov].sum()), 3)
            if time_s > 0:
                out["recovery_share"][i] = round(float(rec[sel].sum()) / time_s, 4)
                out["stopped_share"][i] = round(float(stopped[sel].sum()) / time_s, 4)
            acc = Accumulator(1)
            z = np.zeros(sel.size)
            acc.add(np.zeros(sel.size, np.int64), seg.x0[sel], seg.y0[sel], seg.x1[sel], seg.y1[sel], dt[sel], z, z)
            day_keys = acc.tiles().key
            out["new_tiles"][i] = int(np.setdiff1d(day_keys, seen, assume_unique=True).size)
            seen = np.union1d(seen, day_keys)
            out["cumulative_tiles"][i] = int(seen.size)
    return out


def origin_cells(key: np.ndarray, scene_graph: bool) -> Tuple[int, int]:
    """Origin of the tile indices, in absolute cells: (0, 0) without a scene graph, else the lower-left of the
    data on the scene-graph tile grid (multiples of 64 cells = 6.4 m)."""
    if not scene_graph or not key.size:
        return 0, 0
    ax, ay = key_cells(key)
    per = int(round(TILE_GRID_M / CELL_M))
    return int(np.floor_divide(ax.min(), per)) * per, int(np.floor_divide(ay.min(), per)) * per


def build_body(map_id: str, tiles: Tiles, src: Dict[str, Any], regions: List[Dict[str, Any]],
               origin: Tuple[int, int], version: Optional[int], window: Dict[str, Any]) -> Dict[str, Any]:
    ax, ay = key_cells(tiles.key)
    ix, iy = ax - origin[0], ay - origin[1]
    rows = [[int(a), int(b), int(n), round(float(s0), 3), round(float(s1), 2), round(float(s2), 2), round(float(s3), 2)]
            for a, b, n, s0, s1, s2, s3 in zip(ix, iy, tiles.n, *tiles.sums)]
    return {"map_id": map_id, "graph_version": version, "cell_m": CELL_M,
            "origin": [round(origin[0] * CELL_M, 6), round(origin[1] * CELL_M, 6)], "step": CELL_M,
            "tile_fields": ["ix", "iy", "n", "len_m", "time_s", "recovery_s", "stopped_s"],
            "tiles": rows, "regions": regions, "source": src, "window": window}


# --- cache ---------------------------------------------------------------------------------------

CACHE = ResultCache(config.DRIVE_STATS_CACHE_ENTRIES, config.DRIVE_STATS_CACHE_TTL_S)


# --- the endpoint --------------------------------------------------------------------------------

def parse_min_speed(value: Optional[float]) -> float:
    if value is None:
        return config.DRIVE_STATS_MIN_SPEED_MS
    if not (math.isfinite(value) and 0.0 <= value <= 1.0):
        raise HTTPException(422, "min_speed must be between 0 and 1 (m/s)")
    return float(value)


async def drive_stats(db: Any, graph: Any, reads: Any, map_id: str, start: Optional[datetime.datetime] = None,
                      end: Optional[datetime.datetime] = None, robot: Optional[str] = None,
                      min_speed: Optional[float] = None, cache: Optional[ResultCache] = None,
                      series: Optional[str] = None) -> Tuple[bytes, str]:
    """(JSON body, ETag) of GET /api/v1/maps/{map_id}/drive-stats. 404 unknown map; 422 bad query.
    `series="day"` adds the per-day `series` block over the window (default the last RUN_STATS_DEFAULT_DAYS days)."""
    p = Params(min_speed=parse_min_speed(min_speed))
    if start is not None and end is not None and end <= start:
        raise HTTPException(422, "to must be after from")
    cache = CACHE if cache is None else cache
    key = (map_id, start, end, robot or None, p.min_speed) + ((series,) if series else ())
    s_start, s_end = start, end
    if series:
        s_end = end or datetime.datetime.now(datetime.timezone.utc)
        s_start = start or s_end - datetime.timedelta(days=config.RUN_STATS_DEFAULT_DAYS)
        axis = series_axis.day_axis(s_start, s_end)     # 422 for a window over 120 days, before any query

    async def run() -> Tuple[bytes, str]:
        try:
            async with maps.open_store(db, uuid.uuid4()) as store:
                if await store.get_map(map_id) is None:
                    raise HTTPException(404, f"Did not find \"map\" with name \"{map_id}\"")
        except maps._SCHEMA_ERRORS as exc:
            raise maps._undefined_table(exc) from exc
        loaded = await load(db, map_id, start, end, robot)

        def nodes() -> Dict[str, Tuple[float, float]]:
            out = {}
            for doc in graph.get_all_nodes(map_id):
                pose = doc.get("pose") or {}
                if pose.get("x") is not None and pose.get("y") is not None and doc.get("_key") is not None:
                    out[str(doc["_key"])] = (float(pose["x"]), float(pose["y"]))
            return out

        tiles, src, _, _ = await asyncio.to_thread(compute, loaded, nodes, p)
        if not len(tiles):
            ids, names, cells, version, status = np.zeros(0, np.int64), [], {}, None, "ok"
        elif reads is None:
            ids, names, cells, version, status = None, [], {}, None, "none"
        else:
            ids, names, cells, version, status = await regions_of(reads, map_id, *key_cells(tiles.key))
        src["scene_graph"] = status
        regions = region_rows(tiles, ids, names, cells) if ids is not None else []
        origin = origin_cells(tiles.key, status == "ok" and version is not None)
        window = {"from": start.isoformat() if start else None, "to": end.isoformat() if end else None,
                  "robot": robot or None, "min_speed": p.min_speed, "max_gap_s": p.max_gap_s,
                  "max_speed": p.max_speed}
        body = build_body(map_id, tiles, src, regions, origin, version, window)
        if series:
            sloaded = loaded if (s_start, s_end) == (start, end) else await load(db, map_id, s_start, s_end, robot)
            body["series"] = await asyncio.to_thread(series_day, sloaded, p, axis)
            body["series"]["window"] = {"from": s_start.isoformat(), "to": s_end.isoformat()}
        raw = json.dumps(body, separators=(",", ":")).encode()
        return raw, '"' + hashlib.sha1(raw).hexdigest()[:20] + '"'
    return await cache.get(key, run)

