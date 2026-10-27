"""edge_observations (migrations 20261011_01, 20261027_02) and packages/utils/edge_stats_db.py on a real Postgres.

Opt-in: set EDGE_STATS_TEST_DSN to a THROWAWAY database, e.g.

  docker run -d --name edge-stats-test -p 127.0.0.1:55441:5432 -e POSTGRES_PASSWORD=t \\
      -e POSTGRES_DB=mission postgres:14.5
  EDGE_STATS_TEST_DSN="host=127.0.0.1 port=55441 dbname=mission user=postgres password=t" \\
      pytest tests/integration/test_edge_observations_postgres.py

The migration's SQL is run as written (no Alembic needed: it is plain DDL). The test drops and
recreates edge_observations, so it refuses the default port 5432.
"""
import datetime
import importlib.util
import os
import pathlib
from types import SimpleNamespace

import pytest

from packages.utils import edge_stats as es
from packages.utils import edge_stats_db as store

pytestmark = [pytest.mark.integration, pytest.mark.requires_docker]

VERSIONS = (pathlib.Path(__file__).resolve().parents[2] / "packages" / "api" / "migrations"
            / "versions")
MIGRATIONS = (VERSIONS / "20261011_01_edge_observations.py",
              VERSIONS / "20261027_02_edge_obs_history.py")
UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)


def _test_dsn():
    dsn = os.environ.get("EDGE_STATS_TEST_DSN")
    if not dsn:
        return None
    from psycopg.conninfo import conninfo_to_dict
    if str(conninfo_to_dict(dsn).get("port", "5432")) == "5432":
        return None  # never the production port
    return dsn


DSN = _test_dsn()
skip = pytest.mark.skipif(not DSN, reason="set EDGE_STATS_TEST_DSN to a throwaway Postgres "
                                          "(never the production port 5432)")


def migration_sql(direction, path=MIGRATIONS[0]):
    spec = importlib.util.spec_from_file_location("edge_observations_migration", path)
    module = importlib.util.module_from_spec(spec)
    statements = []
    try:
        import alembic  # noqa: F401  (the module imports `op` from it)
    except ImportError:  # only `op` is needed, and it is replaced below
        import sys
        import types
        stub = types.ModuleType("alembic")
        stub.op = None
        sys.modules["alembic"] = stub
    spec.loader.exec_module(module)
    module.op = SimpleNamespace(execute=statements.append)
    getattr(module, direction)()
    return statements


def obs(i, src="A", dst="B", robot="r1", map_id="yard", source="leg"):
    return es._observation(map_id, src, dst, robot, source, f"k{i:03d}",
                           T0 + datetime.timedelta(seconds=i), 2.0, 1.0)


@pytest.fixture
async def cursor():
    import psycopg
    async with await psycopg.AsyncConnection.connect(DSN, autocommit=True) as conn:
        for sql in (migration_sql("downgrade", MIGRATIONS[0]) + migration_sql("upgrade", MIGRATIONS[0])
                    + migration_sql("upgrade", MIGRATIONS[1])):
            await conn.execute(sql)
        async with conn.cursor() as cur:
            yield cur


async def keys(cur, src="A", dst="B", robot="r1"):
    await cur.execute("SELECT source_key FROM edge_observations WHERE from_node = %s AND "
                      "to_node = %s AND robot_name = %s ORDER BY source_key", (src, dst, robot))
    return [r[0] for r in await cur.fetchall()]


@skip
async def test_store_keeps_everything_fetch_cuts_the_window_purge_removes_old_rows(cursor):
    rows = [obs(i) for i in range(25)] + [obs(i, robot="r2") for i in range(25, 50)]
    assert await store.store(cursor, rows) == 50
    assert await store.store(cursor, rows) == 0                            # idempotent
    assert len(await keys(cursor)) == 25 and len(await keys(cursor, robot="r2")) == 25  # no prune

    fetched = await store.fetch(cursor, "yard")
    for robot, expect in (("r1", range(5, 25)), ("r2", range(30, 50))):
        mine = sorted(r["source_key"] for r in fetched if r["robot_name"] == robot)
        assert mine == [f"k{i:03d}" for i in expect]                       # the newest 20 each
    pooled = await store.fetch(cursor, "yard", pooled=True)
    assert sorted(r["source_key"] for r in pooled) == [f"k{i:03d}" for i in range(30, 50)]
    assert len(await store.fetch(cursor, "yard", "r1", window=3)) == 3
    # the same choice as the pure function, ties included
    tied = [dict(obs(i, dst="T"), observed_at=T0) for i in range(30)]
    await store.store(cursor, tied)
    got = [r for r in await store.fetch(cursor, "yard") if r["to_node"] == "T"]
    assert sorted(r["source_key"] for r in got) == sorted(o["source_key"] for o in es.newest(tied))


@skip
async def test_purge_old_removes_only_rows_older_than_the_retention_in_batches(cursor):
    now = datetime.datetime.now(UTC)
    old = [dict(obs(i), observed_at=now - datetime.timedelta(days=400 + i)) for i in range(7)]
    young = [dict(obs(10 + i), observed_at=now - datetime.timedelta(days=364, hours=i))
             for i in range(3)]
    await store.store(cursor, old + young)
    conn = cursor.connection
    assert await store.purge_old(conn, days=0) == 0                        # 0 = keep forever
    assert await store.purge_old(conn, days=365, batch=3) == 7             # batches of 3, 3, 1
    assert await keys(cursor) == ["k010", "k011", "k012"]
    assert await store.purge_old(conn, days=365) == 0


@skip
async def test_check_constraint_and_history_index_exist(cursor):
    import psycopg
    with pytest.raises(psycopg.errors.CheckViolation):
        await cursor.execute(store.INSERT_SQL,
                             store.insert_params([dict(obs(0, dst="Z"), duration_s=0.0)]))
    await cursor.execute("SELECT indexdef FROM pg_indexes WHERE indexname = "
                         "'edge_observations_history_idx'")
    assert "observed_at DESC" in (await cursor.fetchone())[0]
