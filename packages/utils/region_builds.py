"""Marking region statistics stale, and dropping the read caches of the maps a change touched.

Every writer that changes the inputs of a region build (run delete, archive / restore, import undo, robot delete, a
new or recomputed run_cells row) calls `mark_stale` in the SAME transaction as the change and BEFORE it deletes
rows (the maps are looked up in run_cells). It does two things per affected map:
* bumps `region_stats_inputs.gen` (upsert, +1): the builder (packages/api/region_stats.py) reads the generation when it
  collects and refuses to write status 'ok' when it moved since, so a delete that lands during a build is not lost;
* sets `region_stats_builds.status = 'stale'` where it was 'ok', so the worker rebuilds the map.
After the commit the caller drops the in-process read caches of those maps with `discard_caches`.
The functions take a psycopg ASYNC cursor; the caller owns the transaction.
"""
import importlib
import logging
from typing import Any, Iterable, List, Optional, Sequence

logger = logging.getLogger("ApiDelegationService.region_builds")

MAPS_OF_RUNS_SQL = "SELECT DISTINCT map_id FROM run_cells WHERE run_id = ANY(%s::uuid[])"
MAPS_OF_ROBOT_SQL = "SELECT DISTINCT map_id FROM run_cells WHERE robot_name = %s"
BUMP_SQL = ("INSERT INTO region_stats_inputs (map_id, gen) SELECT m, 1 FROM unnest(%s::text[]) AS m "
            "ON CONFLICT (map_id) DO UPDATE SET gen = region_stats_inputs.gen + 1")
STALE_SQL = "UPDATE region_stats_builds SET status = 'stale' WHERE status = 'ok' AND map_id = ANY(%s::text[])"

# The modules holding a ResultCache named CACHE keyed by map id (packages/api/result_cache.py: discard_map) whose
# figures come from runs or their summaries. Imported lazily (they import the API stack); one that is missing is skipped.
CACHE_MODULES = ("drive_stats", "mapping_stats", "map_series", "map_events", "path_efficiency", "run_stats",
                 "region_stats")


async def mark_stale(cur: Any, *, run_ids: Optional[Sequence[Any]] = None, robot_name: Optional[str] = None,
                     map_ids: Optional[Iterable[str]] = None) -> List[str]:
    """Bump the inputs generation of, and mark stale the builds of, the maps of `run_ids` / `robot_name` (their
    run_cells rows) plus `map_ids`. Returns the sorted map ids. Call it BEFORE the rows are deleted."""
    maps = {m for m in (map_ids or ()) if m}
    if run_ids:
        await cur.execute(MAPS_OF_RUNS_SQL, (list(run_ids),))
        maps.update(r[0] for r in await cur.fetchall())
    if robot_name:
        await cur.execute(MAPS_OF_ROBOT_SQL, (robot_name,))
        maps.update(r[0] for r in await cur.fetchall())
    ordered = sorted(maps)          # a fixed order, so two writers cannot deadlock on the generation rows
    if ordered:
        await cur.execute(BUMP_SQL, (ordered,))
        await cur.execute(STALE_SQL, (ordered,))
    return ordered


def discard_caches(map_ids: Iterable[str]) -> None:
    """Drop the stats read caches of these maps (call after the commit). Never raises."""
    maps = sorted(set(map_ids))
    if not maps:
        return
    for name in CACHE_MODULES:
        try:
            cache = getattr(importlib.import_module(f"packages.api.{name}"), "CACHE", None)
            for map_id in maps:
                if cache is not None:
                    cache.discard_map(map_id)
        except Exception:  # noqa: BLE001 - a cache that cannot be reached expires by its TTL
            logger.warning("Could not discard the %s cache of %s", name, maps, exc_info=True)
