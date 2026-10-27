"""run_cells / region_stats (migrations 20261028_01, 20261028_02) and packages/utils/run_cells_db.py on a real Postgres.

Opt-in: set RUN_CELLS_TEST_DSN to a THROWAWAY database, e.g.

  docker run -d --name pgtest -p 127.0.0.1:55432:5432 -e POSTGRES_PASSWORD=t timescale/timescaledb-ha:pg17.11-ts2.30.1
  RUN_CELLS_TEST_DSN="host=127.0.0.1 port=55432 dbname=postgres user=postgres password=t" \\
      pytest tests/integration/test_run_cells_postgres.py

The migrations' SQL is run as written (plain DDL, no Alembic needed). The test drops and recreates run_cells,
region_stats, region_stats_builds and two stand-in tables (mission_runs, the map table), so it refuses port 5432.
"""
import datetime
import importlib.util
import os
import pathlib
import uuid
from types import SimpleNamespace

import numpy as np
import pytest

from packages.utils import cell_raster as cr
from packages.utils import run_cells_db as rdb

pytestmark = [pytest.mark.integration, pytest.mark.requires_docker]

VERSIONS = (pathlib.Path(__file__).resolve().parents[2] / "packages" / "api" / "migrations" / "versions")
M1, M2 = VERSIONS / "20261028_01_run_cells.py", VERSIONS / "20261028_02_region_cost.py"
T0 = datetime.datetime(2026, 10, 20, 12, 0, tzinfo=datetime.timezone.utc)


def _test_dsn():
    dsn = os.environ.get("RUN_CELLS_TEST_DSN")
    if not dsn:
        return None
    from psycopg.conninfo import conninfo_to_dict
    if str(conninfo_to_dict(dsn).get("port", "5432")) == "5432":
        return None  # never the production port
    return dsn


DSN = _test_dsn()
skip = pytest.mark.skipif(not DSN, reason="set RUN_CELLS_TEST_DSN to a throwaway Postgres (never port 5432)")


def migration_sql(direction, path):
    spec = importlib.util.spec_from_file_location("migration_" + path.stem, path)
    module = importlib.util.module_from_spec(spec)
    try:
        import alembic  # noqa: F401
    except ImportError:
        import sys
        import types
        stub = types.ModuleType("alembic")
        stub.op = None
        sys.modules["alembic"] = stub
    spec.loader.exec_module(module)
    statements = []
    module.op = SimpleNamespace(execute=statements.append)
    getattr(module, direction)()
    return statements


def a_row(i=1, **kw):
    key = cr.cell_key(np.array([3, 4, 4]), np.array([1, 1, 2]))
    base = dict(run_id=uuid.UUID(int=i), map_id="m1", robot_name="r1", robot_class="CARRIER|0.50x0.50", speed_max=0.24,
                started_at=T0, ended_at=T0, source="track", algo_version=2, cell_m=0.1, params={"min_speed": 0.05},
                frames=[["s", "e"]], key=key, len_m=np.array([0.1, 0.2, 0.3]), time_s=np.array([1.0, 2.0, 3.0]),
                stopped_s=np.zeros(3), recovery_s=np.zeros(3), blocks=np.array([0, 1, 0]), totals={"len_m": 0.6},
                dwell_s=np.array([0.0, 0.5, 0.0]))
    base.update(kw)
    return rdb.CellRow(**base)


async def columns(cur, table):
    await cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = %s", (table,))
    return {r[0] for r in await cur.fetchall()}


@pytest.fixture
async def cursor():
    import psycopg
    async with await psycopg.AsyncConnection.connect(DSN, autocommit=True) as conn:
        await conn.execute(f"DROP TABLE IF EXISTS region_stats_builds, region_stats, run_cells, mission_runs, "
                           f"{rdb.MAP_TABLE} CASCADE")
        await conn.execute("CREATE TABLE mission_runs (run_id uuid PRIMARY KEY)")
        await conn.execute(f"CREATE TABLE {rdb.MAP_TABLE} (name text PRIMARY KEY, lifecycle text)")
        await conn.execute(f"INSERT INTO {rdb.MAP_TABLE} VALUES ('m1', 'ACTIVE')")
        await conn.execute("INSERT INTO mission_runs SELECT ('00000000-0000-0000-0000-00000000000' || i)::uuid "
                           "FROM generate_series(1, 4) i")
        for sql in migration_sql("upgrade", M1):
            await conn.execute(sql)
        async with conn.cursor() as cur:
            yield cur


@skip
async def test_upgrade_replaces_the_algo_1_cache_and_adds_the_columns(cursor):
    await cursor.execute(
        "INSERT INTO run_cells (run_id, map_id, robot_name, robot_class, speed_max, started_at, source, algo_version, "
        "cell_m, params, ix, iy, len_m, time_s, stopped_s, recovery_s, blocks, totals) VALUES "
        "('00000000-0000-0000-0000-000000000001', 'm1', 'r1', 'AMR|0.80x0.50|1.20', 1.2, now(), 'track', 1, 0.1, '{}', "
        "'{}', '{}', '{}', '{}', '{}', '{}', '{}', '{}')")
    await cursor.execute("INSERT INTO region_stats_builds (map_id, graph_version, stamp, runs, status) "
                         "VALUES ('m1', 3, 'x', 1, 'ok')")
    for sql in migration_sql("upgrade", M2):
        await cursor.execute(sql)
    await cursor.execute("SELECT count(*) FROM run_cells")
    assert (await cursor.fetchone())[0] == 0                        # algo 1 rows (old class string) are gone
    await cursor.execute("SELECT count(*) FROM region_stats_builds")
    assert (await cursor.fetchone())[0] == 0
    assert {"speed_basis", "class_basis", "dwell_s"} <= await columns(cursor, "run_cells")
    assert {"dwell_s", "norm_runs", "norm_len_m", "norm_moving_m", "norm_stopped_m"} <= await columns(cursor, "region_stats")


@skip
async def test_a_row_with_dwell_and_bases_round_trips_and_the_checks_hold(cursor):
    for sql in migration_sql("upgrade", M2):
        await cursor.execute(sql)
    assert await rdb.insert(cursor, a_row(1, speed_basis="placeholder", class_basis="run", speed_max=1.0))
    [back] = await rdb.fetch(cursor, "m1", algo_version=2)
    assert (back.speed_basis, back.class_basis, back.robot_class) == ("placeholder", "run", "CARRIER|0.50x0.50")
    assert np.allclose(back.dwell_s, [0.0, 0.5, 0.0]) and np.allclose(back.stopped_all_s, [0.0, 0.5, 0.0])
    # an upsert replaces
    assert await rdb.insert(cursor, a_row(1, dwell_s=np.array([1.0, 0.0, 0.0])))
    [back] = await rdb.fetch(cursor, "m1", algo_version=2)
    assert np.allclose(back.dwell_s, [1.0, 0.0, 0.0]) and back.speed_basis == "factsheet"
    import psycopg
    with pytest.raises(psycopg.errors.CheckViolation):
        await rdb.insert(cursor, a_row(2, speed_basis="bogus"))
    with pytest.raises(psycopg.errors.CheckViolation):
        await rdb.insert(cursor, a_row(3, class_basis="bogus"))


@skip
async def test_downgrade_and_upgrade_again(cursor):
    for sql in migration_sql("upgrade", M2):
        await cursor.execute(sql)
    assert await rdb.insert(cursor, a_row(1))
    for sql in migration_sql("downgrade", M2):
        await cursor.execute(sql)
    assert not ({"speed_basis", "class_basis", "dwell_s"} & await columns(cursor, "run_cells"))
    assert not ({"dwell_s", "norm_runs", "norm_len_m"} & await columns(cursor, "region_stats"))
    await cursor.execute("SELECT count(*) FROM run_cells")
    assert (await cursor.fetchone())[0] == 0                        # the algo 2 rows went with the columns
    for sql in migration_sql("upgrade", M2):
        await cursor.execute(sql)
    assert {"speed_basis", "class_basis", "dwell_s"} <= await columns(cursor, "run_cells")
