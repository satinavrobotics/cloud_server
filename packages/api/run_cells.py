"""Per-run cell summaries: the movement of a finished run in 0.1 m cells, stored once (table run_cells).

Contract and rules: docs/region_stats_2026-10-27.md (sections 1, 6.1, 6.2). This is what GET .../drive-stats
(packages/api/drive_stats.py) computes from the raw track on every read, done once per run after it ended and
stored with the run's blocks added; the rasteriser is packages/utils/cell_raster.py, the storage
packages/utils/run_cells_db.py.

`summarise_rows` (pure) turns one run's rows into one `CellRow` per map:
* Track source: the run's map-frame track rows (`robot_track_ts.x_map/y_map`), grouped by (session, run epoch),
  `track_segments` with the default `Params`, the run's NAV.RECOVERY_EXITED intervals spread over the segments,
  `stopped` = the segment's dt where it moved slower than min_speed. A row belongs to the map of its session
  (`map_sessions.map_name`), else the run's map. A run with a track row never uses its legs.
* Legs source (a run with NO map-frame track row): its `run_legs` with both topomap ids and a positive duration,
  node positions from the topomap graph, `leg_movements`.
* Neither: a row with no cells and source 'empty' (so the worker does not retry the run forever).
* Blocks (NAV.GOAL_BLOCKED, MISSION.EDGE_BLOCKED; one per event) are placed on the segment containing their time
  (`cell_raster.block_cells`) or, for legs, at the leg's end node / along the leg (`leg_block_cells`); an event
  that cannot be placed is counted in `totals.blocks_unattributed`. A run on several maps gives each event to the
  first map (by first track time) that places it, and each map is charged the run's whole unattributed recovery
  seconds (a multi-map run is rare; both are approximations).

`summarise_run` reads the rows and calls it; `RunCellsWorker` summarises every finished run once (one worker per
API deployment, advisory-lock election like packages/api/telemetry.py), and tells region_stats which maps changed.
Numpy work runs in a thread.
"""
import asyncio
import collections
import dataclasses
import datetime
import logging
import time
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from packages import config
from packages.api import fleet_reads
from packages.api.entrypoint import advisory_lock_key
from packages.utils import cell_raster as cr
from packages.utils import robot_class as rclass
from packages.utils import run_cells_db as rdb
from packages.utils.timeutil import utcnow

logger = logging.getLogger("ApiDelegationService.run_cells")

LOCK_NAME = "run_cells_worker"
BLOCK_CODES = ("NAV.GOAL_BLOCKED", "MISSION.EDGE_BLOCKED")
ROW_WINDOW_BEFORE_S = 60.0           # track / event rows of a run are read from started_at - this ...
ROW_WINDOW_AFTER_S = 3600.0          # ... to ended_at + this (bounds the hypertable scan; rows lie inside)

RUN_SQL = "SELECT run_id, robot_name, map_id, started_at, ended_at FROM mission_runs WHERE run_id = %s"
TRACK_SQL = (
    "SELECT extract(epoch FROM t.ts)::float8, t.x_map, t.y_map, t.map_session_id, t.run_epoch, "
    "COALESCE(s.map_name, %s) FROM robot_track_ts t LEFT JOIN map_sessions s ON s.session_id = t.map_session_id "
    "WHERE t.run_id = %s AND t.robot_name = %s AND t.ts >= %s AND t.ts <= %s "
    "AND t.x_map IS NOT NULL AND t.y_map IS NOT NULL ORDER BY t.ts")
EVENTS_SQL = (
    "SELECT code, extract(epoch FROM ts)::float8, (payload->>'duration_s')::float8, (payload->>'leg_seq')::int "
    "FROM fleet_events WHERE run_id = %s AND ts >= %s AND ts <= %s "
    "AND code IN ('NAV.RECOVERY_EXITED', 'NAV.GOAL_BLOCKED', 'MISSION.EDGE_BLOCKED')")
LEGS_SQL = (
    "SELECT seq, from_topomap_node, to_topomap_node, duration_s, stopped_s, recovery_s, "
    "extract(epoch FROM started_at)::float8, extract(epoch FROM ended_at)::float8, COALESCE(map_id, %s) "
    "FROM run_legs WHERE run_id = %s AND from_topomap_node IS NOT NULL AND to_topomap_node IS NOT NULL "
    "AND duration_s > 0 ORDER BY seq")


@dataclasses.dataclass
class RunData:
    """What the database holds of one run, before any numpy."""
    run_id: Any
    robot_name: str
    map_id: Optional[str]
    started_at: datetime.datetime
    ended_at: Optional[datetime.datetime]
    track: List[tuple] = dataclasses.field(default_factory=list)       # (ts, x, y, session, run_epoch, map)
    recoveries: List[Tuple[float, float]] = dataclasses.field(default_factory=list)   # (exit ts, duration_s)
    blocks: List[Tuple[float, Optional[int]]] = dataclasses.field(default_factory=list)  # (ts, leg_seq)
    legs: List[tuple] = dataclasses.field(default_factory=list)        # LEGS_SQL rows


async def load_run(cur: Any, run_id: Any) -> Optional[RunData]:
    """The run's rows, or None when the run does not exist."""
    await cur.execute(RUN_SQL, (run_id,))
    head = await cur.fetchone()
    if head is None:
        return None
    data = RunData(*head)
    lo = data.started_at - datetime.timedelta(seconds=ROW_WINDOW_BEFORE_S)
    hi = (data.ended_at or utcnow()) + datetime.timedelta(seconds=ROW_WINDOW_AFTER_S)
    await cur.execute(TRACK_SQL, (data.map_id, run_id, data.robot_name, lo, hi))
    data.track = list(await cur.fetchall())
    await cur.execute(EVENTS_SQL, (run_id, lo, hi))
    for code, ts, dur, seq in await cur.fetchall():
        if code == "NAV.RECOVERY_EXITED":
            if dur is not None and dur > 0:
                data.recoveries.append((ts, dur))
        else:
            data.blocks.append((ts, seq))
    if not data.track:
        await cur.execute(LEGS_SQL, (data.map_id, run_id))
        data.legs = list(await cur.fetchall())
    return data


# --- the pure summary ----------------------------------------------------------------------------

def _pack(keys: np.ndarray, sums: np.ndarray, block_keys: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(sorted union of the cells, the 4 sums (len, time, recovery, stopped) on it, blocks per cell)."""
    bk, bn = np.unique(block_keys, return_counts=True)
    key = np.union1d(keys, bk)
    full = np.zeros((4, key.size))
    full[:, np.searchsorted(key, keys)] = sums
    blocks = np.zeros(key.size, np.int64)
    blocks[np.searchsorted(key, bk)] = bn
    return key, full, blocks


def _params_json(p: cr.Params) -> Dict[str, float]:
    return {"min_speed": p.min_speed, "max_gap_s": p.max_gap_s, "max_speed": p.max_speed, "leg_max_m": p.leg_max_m}


def _empty_totals() -> Dict[str, Any]:
    return {"len_m": 0.0, "time_s": 0.0, "stopped_s": 0.0, "recovery_s": 0.0, "blocks": 0, "segments": 0,
            "rejected": 0, "recovery_unattributed_s": 0.0, "blocks_unattributed": 0, "legs_skipped": 0}


def _track_map(rows: List[tuple], events_rec: Sequence[Tuple[float, float]], blocks: Sequence[Tuple[float, Any]],
               p: cr.Params) -> Tuple[Dict[str, Any], np.ndarray, np.ndarray, np.ndarray, List[bool]]:
    """One map's track rows -> (totals, keys, sums, block keys, placed flag per block event)."""
    grp_codes: Dict[Any, int] = {}
    grp = np.array([grp_codes.setdefault((r[3], r[4]), len(grp_codes)) for r in rows], np.int64)
    run = np.zeros(len(rows), np.int64)
    ts = np.array([r[0] for r in rows], float)
    x = np.array([r[1] for r in rows], float)
    y = np.array([r[2] for r in rows], float)
    order = np.lexsort((ts, run))
    seg, rejected = cr.track_segments(run[order], grp[order], ts[order], x[order], y[order], p)
    intervals = {0: [(t - d, t) for t, d in events_rec]}
    rec, lost = cr.recovery_overlap(seg, intervals)
    dt = seg.t1 - seg.t0
    dist = np.hypot(seg.x1 - seg.x0, seg.y1 - seg.y0)
    stopped = np.where(dist < p.min_speed * dt, dt, 0.0)
    acc = cr.Accumulator(1)
    acc.add(seg.run, seg.x0, seg.y0, seg.x1, seg.y1, dt, rec, stopped)
    tiles = acc.tiles()
    placed: List[bool] = []
    bkeys: List[np.ndarray] = []
    for bt, _seq in blocks:       # one at a time: block_cells says only how many were unplaced
        k, lost_one = cr.block_cells(seg, np.zeros(1, np.int64), np.array([bt]), p.max_gap_s)
        placed.append(not lost_one)
        if k.size:
            bkeys.append(k)
    totals = _empty_totals()
    totals.update(len_m=float(dist.sum()), time_s=float(dt.sum()), stopped_s=float(stopped.sum()),
                  recovery_s=float(rec.sum()), segments=len(seg), rejected=rejected,
                  recovery_unattributed_s=float(lost))
    return totals, tiles.key, tiles.sums, (np.concatenate(bkeys) if bkeys else np.empty(0, np.int64)), placed


def _legs_map(legs: List[tuple], blocks: Sequence[Tuple[float, Optional[int]]],
              pos: Callable[[str], Optional[Tuple[float, float]]], p: cr.Params
              ) -> Tuple[Dict[str, Any], np.ndarray, np.ndarray, np.ndarray, List[bool]]:
    used, parts, skipped = [], [], 0
    for seq, a_id, b_id, dur, stopped, rec, t0, t1, _map in legs:
        mv, sk = cr.leg_movements([{"run": 0, "from": str(a_id), "to": str(b_id), "duration_s": dur,
                                    "stopped_s": stopped, "recovery_s": rec}], pos, p)
        if not mv:
            skipped += sk
            continue
        parts.append(mv)
        used.append({"seq": seq, "t0": t0, "t1": t1, "a": pos(str(a_id)), "b": pos(str(b_id))})
    acc = cr.Accumulator(1)
    totals = _empty_totals()
    totals["legs_skipped"] = skipped
    if parts:
        mv = {k: np.concatenate([m[k] for m in parts]) for k in parts[0]}
        acc.add(mv["run"], mv["x0"], mv["y0"], mv["x1"], mv["y1"], mv["time"], mv["rec"], mv["stopped"])
        totals.update(len_m=float(np.hypot(mv["x1"] - mv["x0"], mv["y1"] - mv["y0"]).sum()),
                      time_s=float(mv["time"].sum()), stopped_s=float(mv["stopped"].sum()),
                      recovery_s=float(mv["rec"].sum()), segments=len(used))
    tiles = acc.tiles()
    placed: List[bool] = []
    bkeys: List[np.ndarray] = []
    for bt, seq in blocks:
        k, lost_one = cr.leg_block_cells([(bt, seq)], used)
        placed.append(not lost_one)
        if k.size:
            bkeys.append(k)
    return totals, tiles.key, tiles.sums, (np.concatenate(bkeys) if bkeys else np.empty(0, np.int64)), placed


def summarise_rows(data: RunData, node_xy: Callable[[str], Dict[str, Tuple[float, float]]], factsheet: Any,
                   p: Optional[cr.Params] = None) -> List[rdb.CellRow]:
    """One CellRow per map of the run (pure; `node_xy(map_id)` -> {topomap node: (x, y)} is called only for the
    legs source). A run on no map gives no row."""
    p = p or cr.Params()
    cls, vmax = rclass.robot_class(factsheet), rclass.speed_max_of(factsheet)
    rows: List[rdb.CellRow] = []

    def make(map_id: str, source: str, totals: Dict[str, Any], keys, sums, bkeys, frames) -> rdb.CellRow:
        key, full, blocks = _pack(keys, sums, bkeys)
        totals["blocks"] = int(blocks.sum())
        return rdb.CellRow(
            run_id=data.run_id, map_id=map_id, robot_name=data.robot_name, robot_class=cls, speed_max=vmax,
            started_at=data.started_at, ended_at=data.ended_at, source=source,
            algo_version=config.RUN_CELLS_ALGO_VERSION, cell_m=cr.CELL_M, params=_params_json(p), frames=frames,
            key=key, len_m=full[0], time_s=full[1], stopped_s=full[3], recovery_s=full[2], blocks=blocks,
            totals=totals)

    remaining = list(data.blocks)
    if data.track:
        by_map: Dict[str, List[tuple]] = collections.OrderedDict()
        for r in sorted(data.track, key=lambda r: r[0]):
            by_map.setdefault(r[5], []).append(r)
        for map_id, mrows in by_map.items():
            totals, keys, sums, bkeys, placed = _track_map(mrows, data.recoveries, remaining, p)
            unplaced = [b for b, ok in zip(remaining, placed) if not ok]
            totals["blocks_unattributed"] = len(unplaced) if len(by_map) == 1 else 0
            remaining = unplaced
            frames = sorted({(str(r[3]) if r[3] is not None else None, str(r[4]) if r[4] is not None else None)
                             for r in mrows}, key=str)
            rows.append(make(map_id, "track", totals, keys, sums, bkeys, [list(f) for f in frames]))
        if len(by_map) > 1:           # events no map placed
            rows[-1].totals["blocks_unattributed"] = len(remaining)
        return rows
    if data.legs:
        by_map_legs: Dict[str, List[tuple]] = collections.OrderedDict()
        for leg in data.legs:
            by_map_legs.setdefault(leg[8], []).append(leg)
        for map_id, legs in by_map_legs.items():
            nodes = node_xy(map_id)
            totals, keys, sums, bkeys, placed = _legs_map(legs, remaining, nodes.get, p)
            unplaced = [b for b, ok in zip(remaining, placed) if not ok]
            totals["blocks_unattributed"] = len(unplaced) if len(by_map_legs) == 1 else 0
            remaining = unplaced
            rows.append(make(map_id, "legs", totals, keys, sums, bkeys, []))
        if len(by_map_legs) > 1:
            rows[-1].totals["blocks_unattributed"] = len(remaining)
        return rows
    if data.map_id:
        totals = _empty_totals()
        totals["blocks_unattributed"] = len(remaining)
        rows.append(make(data.map_id, "empty", totals, np.empty(0, np.int64), np.empty((4, 0)),
                         np.empty(0, np.int64), []))
    return rows


# --- reading and writing -------------------------------------------------------------------------

def node_lookup(graph: Any) -> Callable[[str], Dict[str, Tuple[float, float]]]:
    """`node_xy(map_id)` over the topomap graph service (get_all_nodes), cached per call of the lookup."""
    cache: Dict[str, Dict[str, Tuple[float, float]]] = {}

    def nodes(map_id: str) -> Dict[str, Tuple[float, float]]:
        if map_id not in cache:
            out = {}
            for doc in graph.get_all_nodes(map_id):
                pose = doc.get("pose") or {}
                if pose.get("x") is not None and pose.get("y") is not None and doc.get("_key") is not None:
                    out[str(doc["_key"])] = (float(pose["x"]), float(pose["y"]))
            cache[map_id] = out
        return cache[map_id]
    return nodes


async def summarise_run(db: Any, graph: Any, robots: Mapping[str, Any], run_id: Any) -> List[rdb.CellRow]:
    """The run's summaries (one per map). `robots` maps a robot name to its factsheet (unknown robot: class '')."""
    async with fleet_reads.read_cursor(db) as cur:
        data = await load_run(cur, run_id)
    if data is None:
        return []
    return await asyncio.to_thread(summarise_rows, data, node_lookup(graph), robots.get(data.robot_name))


async def store(db: Any, rows: Sequence[rdb.CellRow]) -> int:
    """Insert the rows of one run in one transaction; the number actually written."""
    written = 0
    async with db.connection() as conn:
        async with conn.cursor() as cur:
            for row in rows:
                written += 1 if await rdb.insert(cur, row) else 0
    return written


PENDING_SQL = (
    "SELECT r.run_id, r.map_id FROM mission_runs r "
    f"JOIN {rdb.MAP_TABLE} m ON m.name = r.map_id AND m.lifecycle NOT IN ('DELETING', 'DELETED') "
    "WHERE r.ended_at IS NOT NULL AND r.map_id IS NOT NULL AND r.ended_at < now() - make_interval(secs => %s) "
    "AND NOT EXISTS (SELECT 1 FROM run_cells c WHERE c.run_id = r.run_id AND c.algo_version = %s) "
    "AND r.run_id <> ALL(%s::uuid[]) ORDER BY r.ended_at LIMIT %s")


async def factsheets(db: Any) -> Dict[str, Any]:
    from cloud_common.objects.robot import RobotObjectV1
    return {r.name: r.status.factsheet for r in await db.list_objects(RobotObjectV1)}


class RunCellsWorker:
    """Summarises finished runs, and rebuilds the region statistics of the maps they touched.

    One instance per API process; `step()` is one pass (the election calls it every RUN_CELLS_POLL_S while this
    worker holds the lock `run_cells_worker`). `build(map_id)` (region_stats.build_map, optional) is called for a
    map REGION_STATS_DEBOUNCE_S after the first run summarised on it since its last build; `stale_maps()`
    (optional) names maps whose statistics need a build for another reason (a new graph version, a deleted
    run); they are treated the same."""

    def __init__(self, db: Any, graph: Any, *, build: Optional[Callable[[str], Awaitable[Any]]] = None,
                 stale_maps: Optional[Callable[[], Awaitable[Iterable[str]]]] = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.db, self.graph, self.build, self.stale_maps, self.clock = db, graph, build, stale_maps, clock
        self.failed: Dict[Any, float] = {}              # run_id -> retry not before (clock)
        self.dirty: Dict[str, float] = {}               # map_id -> clock of the first change since its last build
        self.summarised = 0
        self.errors = 0
        self.election: Any = None

    # -- lifecycle
    def start(self) -> None:
        if not config.RUN_CELLS_ENABLED or self.election is not None:
            return
        from packages.api.telemetry import WriterElection

        async def nothing(*_a: Any) -> None:
            return None
        self.election = WriterElection(
            self.db.dedicated_connection, nothing, nothing, key=advisory_lock_key(LOCK_NAME),
            retry_s=config.RUN_CELLS_POLL_S, check_s=config.RUN_CELLS_POLL_S, on_tick=self._tick)
        self.election.start()

    async def stop(self) -> None:
        election, self.election = self.election, None
        if election is not None:
            await election.stop()

    async def _tick(self, _conn: Any) -> None:
        await self.step()

    # -- one pass
    def retry_blocked(self) -> List[Any]:
        """Runs that failed and are not due for another try yet."""
        now = self.clock()
        self.failed = {k: v for k, v in self.failed.items() if v > now}
        return list(self.failed)

    async def pending(self, limit: int) -> List[Tuple[Any, str]]:
        blocked = self.retry_blocked()
        grace = config.RECORDER_TRAJECTORY_GRACE_S + config.RUN_CELLS_EXTRA_GRACE_S
        async with fleet_reads.read_cursor(self.db) as cur:
            await cur.execute(PENDING_SQL, (grace, config.RUN_CELLS_ALGO_VERSION, blocked, limit))
            return [(r[0], r[1]) for r in await cur.fetchall()]

    async def step(self) -> Dict[str, int]:
        """Summarise up to RUN_CELLS_BATCH runs, then rebuild the due maps. Never raises."""
        done = failed = built = 0
        try:
            todo = await self.pending(config.RUN_CELLS_BATCH)
            robots = await factsheets(self.db) if todo else {}
            for run_id, map_id in todo:
                try:
                    rows = await summarise_run(self.db, self.graph, robots, run_id)
                    if not rows:
                        raise LookupError("run has no summary rows (gone, or no map)")
                    if not await store(self.db, rows):
                        raise LookupError("map is gone or being deleted")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # pylint: disable=broad-except
                    failed += 1
                    self.errors += 1
                    self.failed[run_id] = self.clock() + config.RUN_CELLS_RETRY_S
                    logger.warning("Run %s: no cell summary (%s: %s); retry in %.0f s", run_id,
                                   type(exc).__name__, exc, config.RUN_CELLS_RETRY_S)
                    continue
                done += 1
                self.summarised += 1
                for row in rows:
                    self.dirty.setdefault(row.map_id, self.clock())
            if self.stale_maps is not None:
                for map_id in await self.stale_maps():
                    self.dirty.setdefault(map_id, self.clock())
        except asyncio.CancelledError:
            raise
        except Exception:  # pylint: disable=broad-except
            self.errors += 1
            logger.exception("Run cells pass failed")
        if self.build is not None:
            built = await self.build_due()
        if done or failed or built:
            logger.info("Run cells: %d summarised, %d failed, %d maps rebuilt", done, failed, built)
        return {"summarised": done, "failed": failed, "built": built}

    async def build_due(self) -> int:
        built = 0
        now = self.clock()
        for map_id, since in sorted(self.dirty.items()):
            if now - since < config.REGION_STATS_DEBOUNCE_S:
                continue
            self.dirty.pop(map_id, None)
            try:
                await self.build(map_id)
                built += 1
            except asyncio.CancelledError:
                raise
            except Exception:  # pylint: disable=broad-except
                self.errors += 1
                self.dirty[map_id] = self.clock()              # try again after another debounce
                logger.exception("Region stats build of %s failed", map_id)
        return built
