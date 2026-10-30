#!/usr/bin/env python3
"""Fill run_cells (the per-run cell summaries) for finished runs that have none (or an old algo_version).

    python scripts/backfill_run_cells.py [--apply] [--map M] [--robot NAME] [--since ISO] [--limit N] [--recompute] [--relabel]
                                         [--compose-env docker_compose/.env]
    docker compose ... exec api-delegation-service python scripts/backfill_run_cells.py --apply

The API worker (packages/api/run_cells.py) does this for every run that ends from now on; this script is for the
runs that ended before it existed (and, with --recompute, for a changed rule). It uses the SAME functions
(`run_cells.load_run`, `summarise_rows`, `run_cells_db.insert`), so the rows are identical to the worker's.

Selection: runs with `ended_at` older than the trajectory grace, on a map that exists (not DELETING), `--map` /
`--robot` / `--since` (started_at) narrowing it; without `--recompute` only runs with no row of the current
RUN_CELLS_ALGO_VERSION; with it every selected run is summarised again and its rows replaced.

A run that started in a factsheet period (robot_factsheet_periods, docs/region_stats_2026-10-27.md) is always labelled from
that period (class_basis 'period'), which overrides a stored label. Otherwise a recompute keeps each run's stored
robot_class / speed_max / speed_basis / class_basis (the label it was summarised with, decision D5); --relabel labels it
again with the robot's current factsheet. A run that yields no row (its run or
map vanished meanwhile) is counted in `no_rows` and written nothing.

The default is a DRY RUN: it summarises in memory, prints the counts and the size of the rows, and writes nothing
(the Postgres session is READ ONLY). `--apply` writes, one transaction per run (all its rows, and the stale mark of its maps); it is idempotent (an upsert on
(run_id, map_id)). The region statistics of the touched maps are marked stale in the same transaction
(`region_builds.mark_stale`), so the API worker rebuilds them. Environment as scripts/export_map.py (`--compose-env FILE` reads docker_compose/.env for a
run on the cloud host against localhost).
"""
import argparse
import asyncio
import datetime
import json
import logging
import os
import statistics
import sys
from typing import Any, Dict, List, Mapping, Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from packages import config  # noqa: E402
from packages.api import run_cells as rc  # noqa: E402
from packages.utils import factsheet_periods  # noqa: E402
from packages.utils import region_builds  # noqa: E402
from packages.utils import run_cells_db as rdb  # noqa: E402

logger = logging.getLogger("backfill_run_cells")

SELECT_SQL = (
    "SELECT r.run_id FROM mission_runs r "
    f"JOIN {rdb.MAP_TABLE} m ON m.name = r.map_id AND m.lifecycle NOT IN ('DELETING', 'DELETED') "
    "WHERE r.ended_at IS NOT NULL AND r.map_id IS NOT NULL AND r.ended_at < now() - make_interval(secs => %s)")
SKIP_DONE = " AND NOT EXISTS (SELECT 1 FROM run_cells c WHERE c.run_id = r.run_id AND c.algo_version = %s)"


def select_sql(map_id: Optional[str], since: Optional[datetime.datetime], recompute: bool, limit: Optional[int],
               robot: Optional[str] = None):
    args: List[Any] = [config.RECORDER_TRAJECTORY_GRACE_S + config.RUN_CELLS_EXTRA_GRACE_S]
    sql = SELECT_SQL
    if map_id:
        sql += " AND r.map_id = %s"
        args.append(map_id)
    if robot:
        sql += " AND r.robot_name = %s"
        args.append(robot)
    if since is not None:
        sql += " AND r.started_at >= %s"
        args.append(since)
    if not recompute:
        sql += SKIP_DONE
        args.append(config.RUN_CELLS_ALGO_VERSION)
    sql += " ORDER BY r.ended_at"
    if limit:
        sql += " LIMIT %s"
        args.append(limit)
    return sql, tuple(args)


class ArangoNodes:
    """`graph.get_all_nodes(map_id)` for run_cells.node_lookup, straight from ArangoDB (read only)."""

    def __init__(self, env: Mapping[str, str]) -> None:
        from arango import ArangoClient
        client = ArangoClient(hosts=f"http://{env.get('ARANGO_HOST', 'localhost')}:{env.get('ARANGO_PORT', '8529')}")
        self.db = client.db(env.get("DATABASE_NAME", "topomap_db"), username=env.get("ARANGO_USERNAME", "root"),
                            password=env.get("ARANGO_PASSWORD", ""))

    def get_all_nodes(self, map_id: str) -> List[Dict[str, Any]]:
        name = f"nodes_{map_id}"
        return list(self.db.collection(name).all()) if self.db.has_collection(name) else []


async def run(env: Mapping[str, str], args: argparse.Namespace) -> Dict[str, Any]:
    import psycopg
    conninfo = (f"host={env.get('POSTGRES_DATABASE_HOST', 'localhost')} "
                f"port={env.get('POSTGRES_DATABASE_PORT', '5432')} dbname={env.get('POSTGRES_DATABASE_NAME', 'mission')} "
                f"user={env.get('POSTGRES_DATABASE_USERNAME', 'postgres')} "
                f"password={env.get('POSTGRES_DATABASE_PASSWORD', '')} connect_timeout=5")
    graph = ArangoNodes(env)
    nodes = rc.node_lookup(graph)
    counts: Dict[str, Any] = {"runs": 0, "rows": 0, "written": 0, "failed": 0, "no_rows": 0, "by_source": {}}
    sizes: List[int] = []
    since = datetime.datetime.fromisoformat(args.since) if args.since else None
    async with await psycopg.AsyncConnection.connect(conninfo, autocommit=True) as read, \
            await psycopg.AsyncConnection.connect(conninfo) as write:
        await read.execute("SET default_transaction_read_only = on")
        sql, params = select_sql(args.map, since, args.recompute, args.limit, args.robot)
        run_ids = [r[0] for r in await (await read.execute(sql, params)).fetchall()]
        robots = await _factsheets(read)
        for run_id in run_ids:
            try:
                async with read.cursor() as cur:
                    data = await rc.load_run(cur, run_id)
                    period = await factsheet_periods.period_at(cur, data.robot_name, data.started_at) if data else None
                    labels = {} if (args.relabel or period is not None) else await rdb.fetch_labels(cur, run_id)
                rows = rc.summarise_rows(data, nodes, period if period is not None else robots.get(data.robot_name),
                                         class_basis="current" if period is None else "period") if data else []
                rc.keep_labels(rows, labels)
            except Exception as exc:  # pylint: disable=broad-except
                counts["failed"] += 1
                logger.warning("run %s: %s: %s", run_id, type(exc).__name__, exc)
                continue
            counts["runs"] += 1
            if not rows:
                counts["no_rows"] += 1
                continue
            for row in rows:
                counts["rows"] += 1
                counts["by_source"][row.source] = counts["by_source"].get(row.source, 0) + 1
                sizes.append(len(row) * (4 * 4 + 2) + 64)       # the arrays' payload in bytes, before TOAST compression
            if args.apply:
                try:
                    async with write.cursor() as cur:        # one transaction per run: a multi-map run is never half written
                        written = [row.map_id for row in rows if await rdb.insert(cur, row)]
                        await region_builds.mark_stale(cur, map_ids=written)
                    await write.commit()
                    counts["written"] += len(written)
                except Exception as exc:  # pylint: disable=broad-except
                    await write.rollback()
                    counts["failed"] += 1
                    logger.warning("run %s: write failed: %s: %s", run_id, type(exc).__name__, exc)
    if sizes:
        counts["row_bytes_median"] = int(statistics.median(sizes))
        counts["row_bytes_max"] = max(sizes)
    return counts


async def _factsheets(conn: Any) -> Dict[str, Any]:
    """Robot name -> factsheet (RobotTypeIdentifierV1 fields as a dict) from the robot objects."""
    from cloud_common.objects.robot import RobotObjectV1
    out: Dict[str, Any] = {}
    cur = await conn.execute(f"SELECT name, status FROM {RobotObjectV1.table_name()} WHERE lifecycle != 'DELETED'")
    for name, status in await cur.fetchall():
        status = json.loads(status) if isinstance(status, (str, bytes)) else (status or {})
        out[name] = status.get("factsheet")
    return out


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--apply", action="store_true", help="write (default: dry run)")
    p.add_argument("--map", help="only runs on this map")
    p.add_argument("--robot", help="only runs of this robot (e.g. to relabel after seeding its factsheet periods)")
    p.add_argument("--since", help="only runs started at or after this ISO-8601 time")
    p.add_argument("--limit", type=int, help="at most this many runs (oldest ended first)")
    p.add_argument("--recompute", action="store_true", help="also runs that already have a current summary")
    p.add_argument("--relabel", action="store_true",
                   help="label recomputed runs with the robot's current factsheet (default: keep the stored label)")
    p.add_argument("--compose-env", help="read credentials from docker_compose/.env")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    env = dict(os.environ)
    if args.compose_env:
        from scripts import export_map
        env.update(export_map.compose_env(args.compose_env))
    print(json.dumps(asyncio.run(run(env, args)), indent=1))
    if not args.apply:
        print("dry run: nothing written (pass --apply)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
