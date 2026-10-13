"""Migration 20261013_01 (robot_track_ts map-frame columns, mission_runs level columns) on a
real TimescaleDB with the table COMPRESSED, as in production.

Opt-in: set TRACK_TEST_DSN to a THROWAWAY database on the production image, e.g.

  docker run -d --name track-test -p 127.0.0.1:55442:5432 -e POSTGRES_PASSWORD=t \\
      -e POSTGRES_DB=mission timescale/timescaledb-ha:pg17.11-ts2.30.1
  TRACK_TEST_DSN="host=127.0.0.1 port=55442 dbname=mission user=postgres password=t" \\
      pytest tests/integration/test_track_map_frame_postgres.py

The test drops and recreates robot_track_ts / mission_runs (a minimal copy of what migrations
20261009_01 and 20260924_01 create), so it refuses the default port 5432.
"""
import datetime
import importlib.util
import os
import pathlib
import uuid
from types import SimpleNamespace

import pytest

from packages.telemetry_ingest import tables

pytestmark = [pytest.mark.integration, pytest.mark.requires_docker]

VERSIONS = (pathlib.Path(__file__).resolve().parents[2] / "packages" / "api" / "migrations"
            / "versions")
UTC = datetime.timezone.utc
OLD = datetime.datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)   # older than the 3 day compression
NEW = datetime.datetime(2026, 10, 13, 12, 0, 0, tzinfo=UTC)


def _test_dsn():
    dsn = os.environ.get("TRACK_TEST_DSN")
    if not dsn:
        return None
    from psycopg.conninfo import conninfo_to_dict
    if str(conninfo_to_dict(dsn).get("port", "5432")) == "5432":
        return None  # never the production port
    return dsn


DSN = _test_dsn()
skip = pytest.mark.skipif(not DSN, reason="set TRACK_TEST_DSN to a throwaway TimescaleDB "
                                          "(never the production port 5432)")


def migration_sql(name, direction):
    spec = importlib.util.spec_from_file_location("m_" + name, VERSIONS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    statements = []
    import alembic  # noqa: F401  (the module imports `op` from it)
    spec.loader.exec_module(module)
    module.op = SimpleNamespace(execute=statements.append)
    getattr(module, direction)()
    return statements


# The part of 20261009_01 that makes the hypertable (the rest touches mission_runs' CHECK).
TRACK_SETUP = """
DROP TABLE IF EXISTS robot_track_ts;
CREATE TABLE robot_track_ts (
  ts timestamptz NOT NULL, robot_name text NOT NULL, run_id uuid, leg_seq int,
  x double precision, y double precision, theta double precision, speed real, omega real,
  map_id text);
SELECT create_hypertable('robot_track_ts', by_range('ts', INTERVAL '1 day'));
CREATE INDEX robot_track_ts_robot_ts_idx ON robot_track_ts (robot_name, ts DESC);
ALTER TABLE robot_track_ts SET (timescaledb.compress, timescaledb.compress_segmentby = 'robot_name',
                                timescaledb.compress_orderby = 'ts DESC');
SELECT add_compression_policy('robot_track_ts', compress_after => INTERVAL '3 days');
SELECT add_retention_policy('robot_track_ts', drop_after => INTERVAL '5 years');
DROP TABLE IF EXISTS mission_runs;
CREATE TABLE mission_runs (run_id uuid PRIMARY KEY, state text NOT NULL DEFAULT 'RUNNING');
INSERT INTO mission_runs (run_id) VALUES ('00000000-0000-0000-0000-000000000001');
"""
OLD_COLUMNS = ("ts", "robot_name", "run_id", "leg_seq", "x", "y", "theta", "speed", "omega",
               "map_id")


@pytest.fixture
async def conn():
    import psycopg
    async with await psycopg.AsyncConnection.connect(DSN, autocommit=True) as c:
        await c.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
        for sql in TRACK_SETUP.split(";\n"):
            if sql.strip():
                await c.execute(sql)
        yield c


async def compress_all(c):
    cur = await c.execute("SELECT compress_chunk(c, if_not_compressed => true) "
                          "FROM show_chunks('robot_track_ts') c")
    return len(await cur.fetchall())


async def compressed_count(c):
    cur = await c.execute("SELECT count(*) FROM timescaledb_information.chunks WHERE "
                          "hypertable_name = 'robot_track_ts' AND is_compressed")
    return (await cur.fetchone())[0]


@skip
async def test_columns_are_added_to_a_compressed_hypertable_and_old_rows_read_null(conn):
    run = uuid.UUID("00000000-0000-0000-0000-000000000001")
    for i in range(5):
        await conn.execute(
            "INSERT INTO robot_track_ts (ts, robot_name, run_id, x, y, theta) "
            "VALUES (%s, 'r1', %s, %s, 2.0, 0.5)", (OLD + datetime.timedelta(seconds=i), run, i))
    assert await compress_all(conn) >= 1 and await compressed_count(conn) >= 1

    for sql in migration_sql("20261013_01_track_map_frame", "upgrade"):
        await conn.execute(sql)

    # the compressed rows are intact and read the new columns as NULL
    cur = await conn.execute("SELECT x, x_map, y_map, theta_map, map_session_id, run_epoch "
                             "FROM robot_track_ts ORDER BY ts")
    rows = await cur.fetchall()
    assert [r[0] for r in rows] == [0.0, 1.0, 2.0, 3.0, 4.0]
    assert all(r[1:] == (None,) * 5 for r in rows)
    assert await compressed_count(conn) >= 1          # nothing was decompressed

    # the policies survived
    cur = await conn.execute("SELECT proc_name FROM timescaledb_information.jobs "
                             "WHERE hypertable_name = 'robot_track_ts' ORDER BY proc_name")
    assert [r[0] for r in await cur.fetchall()] == ["policy_compression", "policy_retention"]

    # new rows (the recorder's COPY with the full column list) land, in a fresh chunk
    session, epoch = uuid.uuid4(), uuid.uuid4()
    async with conn.cursor() as cur:
        async with cur.copy(tables.copy_sql("robot_track_ts")) as copy:
            await copy.write_row((NEW, "r1", run, 1, 1.0, 2.0, 0.5, 0.3, 0.1, "map1",
                                  8.0, 1.0, 2.07, session, epoch))
            await copy.write_row((NEW + datetime.timedelta(seconds=1), "r1", run, 1, 1.5, 2.0,
                                  0.5, 0.3, 0.1, "map1", None, None, None, None, None))
    cur = await conn.execute("SELECT x_map, map_session_id, run_epoch FROM robot_track_ts "
                             "WHERE ts >= %s ORDER BY ts", (NEW,))
    assert await cur.fetchall() == [(8.0, session, epoch), (None, None, None)]

    # compressing again, with old and new shapes mixed, works; so does reading across chunks
    old_chunk_time = OLD + datetime.timedelta(days=1)
    await conn.execute("INSERT INTO robot_track_ts (ts, robot_name, x_map) "
                       "VALUES (%s, 'r1', 5.0)", (old_chunk_time,))
    await compress_all(conn)
    cur = await conn.execute("SELECT count(*), count(x_map) FROM robot_track_ts")
    assert await cur.fetchone() == (8, 2)

    # a run's rows can still be deleted on compressed chunks (run_admin does it)
    await conn.execute("DELETE FROM robot_track_ts WHERE run_id = %s", (run,))
    cur = await conn.execute("SELECT count(*) FROM robot_track_ts")
    assert (await cur.fetchone())[0] == 1


@skip
async def test_mission_runs_columns_and_idempotent_up_down_up(conn):
    for _ in range(2):   # IF NOT EXISTS: running it twice is harmless
        for sql in migration_sql("20261013_01_track_map_frame", "upgrade"):
            await conn.execute(sql)
    cur = await conn.execute("SELECT level_override, level_marks FROM mission_runs")
    assert await cur.fetchall() == [(False, None)]     # existing runs: not an override
    await conn.execute("UPDATE mission_runs SET level_marks = COALESCE(level_marks, '[]'::jsonb) "
                       "|| %s::jsonb", ('[{"ts": "t", "level": "track"}]',))
    for sql in migration_sql("20261013_01_track_map_frame", "downgrade"):
        await conn.execute(sql)
    cur = await conn.execute("SELECT column_name FROM information_schema.columns WHERE "
                             "table_name IN ('robot_track_ts', 'mission_runs') AND "
                             "column_name IN ('x_map', 'level_override', 'level_marks')")
    assert await cur.fetchall() == []
    for sql in migration_sql("20261013_01_track_map_frame", "upgrade"):
        await conn.execute(sql)


@skip
async def test_a_dispatch_started_before_the_migration_still_writes_its_track(conn, tmp_path):
    """The new dispatch's track COPY (full column list) is refused by the pre-migration table
    (UndefinedColumn); the writer retries that batch once with the old columns, so the rows
    are kept and only the map-frame part is lost. (Run inserts and level marks of the new
    dispatch need the new mission_runs columns: the migration must still come first.)"""
    from psycopg import errors
    from packages.telemetry_ingest.queue import IngestQueue, SpillFile
    from packages.telemetry_ingest.writer import TelemetryWriter

    run = uuid.UUID("00000000-0000-0000-0000-000000000001")
    full = tuple(range(len(tables.TRACK_COLUMNS)))
    with pytest.raises(errors.UndefinedColumn):
        async with conn.cursor() as cur:
            async with cur.copy(tables.copy_sql("robot_track_ts")) as copy:
                await copy.write_row((NEW, "r1", run, 1, 1.0, 2.0, 0.5, 0.3, 0.1, "m",
                                      8.0, 1.0, 2.0, None, None))

    writer = TelemetryWriter(None, IngestQueue("dispatch", SpillFile(tmp_path / "spill.jsonl")))
    rows = [(NEW, "r1", run, 1, 1.0, 2.0, 0.5, 0.3, 0.1, "m", 8.0, 1.0, 2.0, None, None)]
    batch = SimpleNamespace(timeseries={tables.TRACK_TABLE: rows}, failed=False)
    assert len(full) == len(rows[0])
    await writer._write_timeseries(conn, batch)
    assert not batch.failed and batch.timeseries == {}
    cur = await conn.execute("SELECT x, y, map_id FROM robot_track_ts")
    assert await cur.fetchall() == [(1.0, 2.0, "m")]
