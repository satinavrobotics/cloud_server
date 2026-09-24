"""API container entrypoint: migrate the schema, then exec the real command (uvicorn).

    python -m packages.api.entrypoint python -m packages.api.main --host 0.0.0.0 --port 8000

1. Connect to Postgres (retrying for MIGRATION_DB_WAIT_S seconds, default 120).
2. Take the session-level advisory lock for the name 'migrations' on that connection. Any other
   API container or restart blocks here until the holder finishes; it then finds the database at
   head and does nothing. The lock is released on unlock or when the session ends, so a crashed
   migrator never leaves it held.
3. Run `alembic upgrade head`. Each revision runs in its own
   transaction, so a failure rolls back cleanly; this process then exits non-zero and uvicorn is
   never started (compose `restart: on-failure` retries).
4. Release the lock and exec the command given as arguments (v2 §5.3, api item 1).
"""
import hashlib
import logging
import os
import sys
import time
from pathlib import Path

import psycopg
from psycopg.conninfo import make_conninfo

logger = logging.getLogger("api.entrypoint")
# Explicit: alembic.ini's fileConfig sets the root logger to WARNING mid-run.
logger.setLevel(logging.INFO)

ALEMBIC_INI = Path(__file__).resolve().parent / "alembic.ini"
MIGRATION_LOCK_NAME = "migrations"


def advisory_lock_key(name: str) -> int:
    """A stable signed bigint for a lock name: the first 8 bytes of its SHA-256.

    Computed in Python rather than with hashtext(), whose output Postgres does not promise to
    keep across major versions. 'migrations' -> -3058229681751119483.
    """
    return int.from_bytes(hashlib.sha256(name.encode("utf-8")).digest()[:8], "big", signed=True)


MIGRATION_LOCK_KEY = advisory_lock_key(MIGRATION_LOCK_NAME)


def _conninfo() -> str:
    from packages.config import (
        POSTGRES_DATABASE_HOST, POSTGRES_DATABASE_NAME, POSTGRES_DATABASE_PASSWORD,
        POSTGRES_DATABASE_PORT, POSTGRES_DATABASE_USERNAME)
    return make_conninfo(
        dbname=POSTGRES_DATABASE_NAME, user=POSTGRES_DATABASE_USERNAME,
        password=POSTGRES_DATABASE_PASSWORD, host=POSTGRES_DATABASE_HOST,
        port=POSTGRES_DATABASE_PORT, application_name="api-migrations")


def _connect(conninfo: str, wait_s: float) -> psycopg.Connection:
    deadline = time.monotonic() + wait_s
    while True:
        try:
            return psycopg.connect(conninfo, autocommit=True)
        except psycopg.OperationalError as exc:
            if time.monotonic() >= deadline:
                raise
            logger.warning("Postgres not reachable yet (%s), retrying in 2s", str(exc).strip())
            time.sleep(2)


def run_migrations(conninfo: str, target: str = "head", wait_s: float = 120.0) -> None:
    from alembic import command
    from alembic.config import Config

    with _connect(conninfo, wait_s) as lock_conn:
        logger.info("Waiting for advisory lock %r (key %d)", MIGRATION_LOCK_NAME, MIGRATION_LOCK_KEY)
        lock_conn.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_KEY,))
        logger.info("Holding migration lock; running alembic upgrade %s", target)
        try:
            command.upgrade(Config(str(ALEMBIC_INI)), target)
        finally:
            lock_conn.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK_KEY,))
    logger.info("Migrations done")


def main(argv) -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-5.5s [%(name)s] %(message)s")
    if not argv:
        sys.exit("usage: python -m packages.api.entrypoint <command> [args...]")
    run_migrations(_conninfo(), wait_s=float(os.getenv("MIGRATION_DB_WAIT_S", "120")))
    sys.stdout.flush()
    sys.stderr.flush()
    os.execvp(argv[0], argv)


if __name__ == "__main__":
    main(sys.argv[1:])
