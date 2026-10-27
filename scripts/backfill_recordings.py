#!/usr/bin/env python3
"""Fill the `recordings` registry from the rosbag uploads in MinIO.

    python scripts/backfill_recordings.py [--apply] [--compose-env docker_compose/.env] [--rows]
    docker compose ... exec api-delegation-service python scripts/backfill_recordings.py

Reads every sidecar `rosbags/<robot>/<bag_id>.json` and its bag object. The true UTC interval comes
from the rosbag2 metadata.yaml INSIDE the tar, read with ranged GETs on the tar headers (the object is
never downloaded); without it, from the sidecar's `recorded_at` (Europe/Budapest) as a zero-length
range. Uploads of the same recording (same robot, run prefix and part) collapse to one row. Rules and
fallbacks: packages/topomap_dbs/rosbag_db/recordings.py, table: migration 20261027_01_recordings.

The default is a DRY RUN: MinIO and Postgres are only read (the session is READ ONLY), nothing is
written; it prints the counts, and how many rows already exist and how many overlap mission_runs.
`--rows` also prints every row. `--apply` upserts, idempotently (re-running adds nothing); the migration
must have run. Environment as scripts/export_map.py (`--compose-env FILE` for the cloud host).
"""
import argparse
import json
import logging
import os
import sys
from typing import Any, List, Mapping, Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from packages.topomap_dbs.rosbag_db import recordings as rec  # noqa: E402

logger = logging.getLogger("backfill_recordings")


def connect(env: Mapping[str, str], read_only: bool):
    import psycopg
    return psycopg.connect(
        host=env.get("POSTGRES_DATABASE_HOST", "localhost"),
        port=int(env.get("POSTGRES_DATABASE_PORT", "5432")),
        dbname=env.get("POSTGRES_DATABASE_NAME", "mission"),
        user=env.get("POSTGRES_DATABASE_USERNAME", "postgres"),
        password=env.get("POSTGRES_DATABASE_PASSWORD", ""),
        options="-c default_transaction_read_only=on" if read_only else None,
        connect_timeout=5)


def minio_client(env: Mapping[str, str]):
    from minio import Minio
    return Minio(f"{env.get('MINIO_HOST', 'localhost')}:{env.get('MINIO_PORT', '9000')}",
                 access_key=env.get("MINIO_ACCESS_KEY", ""), secret_key=env.get("MINIO_SECRET_KEY", ""),
                 secure=env.get("MINIO_SECURE", "false").lower() in ("true", "1", "yes"))


def dry_run_report(env: Mapping[str, str], rows: List[dict]) -> dict:
    """Read-only look at Postgres: rows that already exist, overlap with mission_runs."""
    with connect(env, read_only=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('recordings') IS NOT NULL")
        has_table = cur.fetchone()[0]
        existing = set()
        if has_table:
            cur.execute("SELECT robot_name, group_key, part FROM recordings")
            existing = set(cur.fetchall())
        cur.execute("SELECT robot_name, started_at, ended_at FROM mission_runs")
        runs = [dict(zip(("robot_name", "started_at", "ended_at"), r)) for r in cur.fetchall()]
    out = {"table_exists": has_table, "mission_runs": len(runs)}
    out["would_update"] = sum(1 for r in rows if (r["robot_name"], r["group_key"], r["part"]) in existing)
    out["would_insert"] = len(rows) - out["would_update"]
    out.update(rec.overlap_counts(rows, runs))
    return out


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--apply", action="store_true", help="write (default: dry run)")
    p.add_argument("--rows", action="store_true", help="print every row")
    p.add_argument("--compose-env", help="read credentials from docker_compose/.env")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    env = dict(os.environ)
    if args.compose_env:
        from scripts import export_map
        env.update(export_map.compose_env(args.compose_env))
    rows, counts = rec.collect(rec.MinioSources(minio_client(env)))
    if args.rows:
        for r in rows:
            print(json.dumps({k: v for k, v in r.items() if k != "source"}
                             | {"interval_source": r["source"]["interval_source"],
                                "duplicates": r["source"]["duplicates"]}, default=str))
    if args.apply:
        conn = connect(env, read_only=False)
        try:
            with conn.cursor() as cur:
                counts["apply"] = rec.write(cur, rows)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    else:
        counts["dry_run"] = dry_run_report(env, rows)
    print(json.dumps(counts, indent=1, default=str))
    if not args.apply:
        print("dry run: nothing written (pass --apply)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
