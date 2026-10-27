"""The planner's cache of region cost snapshots (docs/planner_region_stats_2026-10-27.md 5, 7, 10).

    snaps = RegionSnapshots(connect)                       # connect() -> async context manager of a Postgres connection
    region = await snaps.get(map_id, graph_version, graph.keys, factsheet)     # RegionPricing, never raises

One `RegionPricing` per (map, graph version, robot class, build stamp, model hash), built by `region_cost.build_snapshot` from
`region_stats_load` rows of that exact version, so a snapshot can only price the charts of the version it was loaded for. The stamp
of a build is only known after reading it, so a (map, version, class, model) is re-read at most every REGION_COST_SNAPSHOT_TTL_S and
the snapshot of the stamp that comes back is reused when the cache already holds it. The planning robot's class is
`robot_class.robot_class(factsheet)`; a factsheet without a real speed_max (`robot_class.speed_basis`) gets the neutral snapshot
"no_speed_max" without a read (decision 5). A read that fails or takes too long is the neutral snapshot "load_error": the plan goes on
unpriced. Nothing here runs unless the caller asks (mode off never gets here).
"""
import asyncio
import logging
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable, Dict, Optional, Sequence, Tuple

from packages import config
from packages.utils import region_cost, region_stats_load, robot_class

LOAD_TIMEOUT_S = 2.0           # the two reads of a snapshot (class and pooled) together
STAMP_RETRIES = 1              # the two reads straddled a rebuild (stamp_mismatch): read again this often

_log = logging.getLogger(__name__)


class RegionSnapshots:
    def __init__(self, connect: Callable[[], Any], clock: Callable[[], float] = time.monotonic,
                 ttl_s: Optional[float] = None, entries: Optional[int] = None):
        self._connect = connect
        self._clock = clock
        self._ttl = config.REGION_COST_SNAPSHOT_TTL_S if ttl_s is None else float(ttl_s)
        self._max = max(1, config.REGION_COST_SNAPSHOT_ENTRIES if entries is None else int(entries))
        self._snaps: "OrderedDict[tuple, region_cost.RegionPricing]" = OrderedDict()      # full key -> snapshot (LRU)
        self._latest: Dict[tuple, Tuple[float, tuple]] = {}      # (map, version, class, model) -> (read at, full key)
        self.loads = 0                                           # database reads made (tests, stats)
        self.builds = 0                                          # snapshots built

    def stats(self) -> Dict[str, Any]:
        return {"entries": len(self._snaps), "loads": self.loads, "builds": self.builds}

    async def get(self, map_id: str, graph_version: int, keys: Sequence[str], factsheet: Any) -> region_cost.RegionPricing:
        model = region_cost.CostModel.from_config()
        cls = robot_class.robot_class(factsheet)
        speed = robot_class.speed_max_of(factsheet)
        basis = robot_class.speed_basis(factsheet)
        if basis != robot_class.SPEED_FACTSHEET or speed is None:
            return region_cost.neutral(map_id, graph_version, cls, keys, model, "no_speed_max")
        short = (map_id, int(graph_version), cls, model.model_hash)
        now = self._clock()
        seen = self._latest.get(short)
        if seen is not None and now - seen[0] < self._ttl and seen[1] in self._snaps:
            self._snaps.move_to_end(seen[1])
            return self._snaps[seen[1]]
        try:
            snap = await asyncio.wait_for(self._read(map_id, int(graph_version), cls, keys, model, speed, basis),
                                          LOAD_TIMEOUT_S)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # pylint: disable=broad-except
            _log.warning("Region statistics of map %s v%s not loaded (%r); planning without the region term",
                         map_id, graph_version, err)
            return region_cost.neutral(map_id, graph_version, cls, keys, model, "load_error")
        key = snap.key
        have = self._snaps.get(key)
        if have is not None and have.keys == snap.keys:
            snap = have                                          # the same build, already built
        else:
            self._snaps[key] = snap
        self._snaps.move_to_end(key)
        while len(self._snaps) > self._max:
            self._snaps.popitem(last=False)
        self._latest[short] = (now, key)
        return snap

    async def _read(self, map_id: str, version: int, cls: str, keys: Sequence[str], model: region_cost.CostModel,
                    speed: float, basis: str) -> region_cost.RegionPricing:
        for attempt in range(STAMP_RETRIES + 1):
            async with self._connect() as conn:
                async with conn.cursor() as cur:
                    stats_class = await region_stats_load.aload(cur, map_id, version, cls)
                    stats_pooled = await region_stats_load.aload(cur, map_id, version, region_stats_load.POOLED)
            self.loads += 1
            key = (map_id, version, cls, stats_class.stamp, model.model_hash)
            have = self._snaps.get(key)
            if have is not None and stats_class.stamp == stats_pooled.stamp and have.keys == tuple(keys):
                return have
            snap = await asyncio.get_running_loop().run_in_executor(     # a python loop over the charts: off the event loop
                None, region_cost.build_snapshot, map_id, version, cls, tuple(keys), stats_class, stats_pooled, model, speed, basis)
            self.builds += 1
            if snap.status != "stamp_mismatch" or attempt == STAMP_RETRIES:
                return snap
        raise AssertionError("unreachable")
