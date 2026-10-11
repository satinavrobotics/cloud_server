"""edge_observations (migration 20261011_01) and packages/utils/edge_stats_db.py on a real Postgres.

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

MIGRATION = (pathlib.Path(__file__).resolve().parents[2] / "packages" / "api" / "migrations"
             / "versions" / "20261011_01_edge_observations.py")
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


def migration_sql(direction):
    spec = importlib.util.spec_from_file_location("edge_observations_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    statements = []
    import alembic  # noqa: F401  (the module imports `op` from it)
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
        for sql in migration_sql("downgrade") + migration_sql("upgrade"):
            await conn.execute(sql)
        async with conn.cursor() as cur:
            yield cur


async def keys(cur, src="A", dst="B", robot="r1"):
    await cur.execute("SELECT source_key FROM edge_observations WHERE from_node = %s AND "
                      "to_node = %s AND robot_name = %s ORDER BY source_key", (src, dst, robot))
    return [r[0] for r in await cur.fetchall()]


@skip
async def test_migration_store_window_idempotency_and_prune_scope(cursor):
    rows = [obs(i) for i in range(25)]
    assert await store.store(cursor, rows) == 25
    assert await keys(cursor) == [f"k{i:03d}" for i in range(5, 25)]       # newest 20 of 25
    # a repeat: the 5 pruned rows are INSERTed again (store() counts inserts) and pruned again
    assert await store.store(cursor, rows) == 5
    assert await keys(cursor) == [f"k{i:03d}" for i in range(5, 25)]       # nothing changed

    # the prune only touches the edges just written: put A->C over the window by hand
    await cursor.executemany(store.INSERT_SQL, [tuple(o[c] for c in store.COLUMNS)
                                                for o in (obs(i, dst="C") for i in range(23))])
    await store.store(cursor, [obs(100, src="X", dst="Y")])
    assert len(await keys(cursor, dst="C")) == 23                           # untouched edge
    await store.store(cursor, [obs(101, dst="C")])
    assert len(await keys(cursor, dst="C")) == 20                           # now pruned
    assert len(await keys(cursor)) == 20                                    # A->B unaffected

    # robots and maps are separate windows; the CHECK refuses a zero duration
    await store.store(cursor, [obs(i, robot="r2") for i in range(3)])
    assert len(await keys(cursor, robot="r2")) == 3
    import psycopg
    with pytest.raises(psycopg.errors.CheckViolation):
        await cursor.execute(store.INSERT_SQL, tuple(dict(obs(0, dst="Z"), duration_s=0.0)[c]
                                                     for c in store.COLUMNS))
    fetched = await store.fetch(cursor, "yard", "r2")
    assert len(fetched) == 3 and fetched[0]["map_id"] == "yard"
