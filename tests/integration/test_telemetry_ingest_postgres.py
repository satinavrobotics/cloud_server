"""packages/telemetry_ingest against a real TimescaleDB with the Alembic migrations applied.

Opt-in: set TELEMETRY_INGEST_TEST_DSN to a THROWAWAY database, e.g.

  docker run -d --name wp5-tsdb-test -p 127.0.0.1:55439:5432 -e POSTGRES_PASSWORD=wp5test \\
      -e POSTGRES_DB=mission timescale/timescaledb-ha:pg17.11-ts2.30.1
  psql ... -c 'CREATE EXTENSION timescaledb'
  POSTGRES_DATABASE_HOST=127.0.0.1 POSTGRES_DATABASE_PORT=55439 ... \\
      alembic -c packages/api/alembic.ini upgrade head
  TELEMETRY_INGEST_TEST_DSN="host=127.0.0.1 port=55439 dbname=mission user=postgres password=wp5test" \\
      pytest tests/integration/test_telemetry_ingest_postgres.py

The tests TRUNCATE the Phase 0 telemetry tables, so they refuse the default port 5432.
"""

import datetime
import os
import uuid

import pytest

from packages.events.codes import EventCode
from packages.events.emit import Event, build_row
from packages.telemetry_ingest import (
    IngestQueue, RecordingLevel, RecordingPolicy, SpillFile, TelemetryWriter, create_pool,
    load_latest,
)

pytestmark = pytest.mark.integration



def _test_dsn():
    dsn = os.environ.get("TELEMETRY_INGEST_TEST_DSN")
    if not dsn:
        return None
    from psycopg.conninfo import conninfo_to_dict
    if str(conninfo_to_dict(dsn).get("port", "5432")) == "5432":
        return None  # never the production port
    return dsn


DSN = _test_dsn()

UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 9, 24, 12, 0, 0, 123456, tzinfo=UTC)

skip = pytest.mark.skipif(not DSN, reason="set TELEMETRY_INGEST_TEST_DSN to a throwaway "
                                          "TimescaleDB (never the production port 5432)")


@pytest.fixture
async def pool():
    pool = await create_pool(DSN)
    await pool.wait(timeout=30)
    async with pool.connection() as conn:
        await conn.execute("TRUNCATE fleet_events, robot_state_ts, diagnostics_ts, robot_latest")
        await conn.execute("""CREATE TABLE IF NOT EXISTS robotobjectv1 (
            name VARCHAR(100) PRIMARY KEY NOT NULL, lifecycle VARCHAR(100) NOT NULL,
            spec jsonb NOT NULL, status jsonb NOT NULL)""")
        await conn.execute("""CREATE TABLE IF NOT EXISTS settingsobjectv1 (
            name VARCHAR(100) PRIMARY KEY NOT NULL, lifecycle VARCHAR(100) NOT NULL,
            spec jsonb NOT NULL, status jsonb NOT NULL)""")
        await conn.execute("TRUNCATE robotobjectv1, settingsobjectv1, robot_site_assignments")
    yield pool
    await pool.close()


def ev(i, robot="r1"):
    return Event(EventCode.ROBOT_STATE_CHANGED, T0 + datetime.timedelta(seconds=i),
                 robot_name=robot, payload={"old": "IDLE", "new": f"S{i}"})


async def count(pool, sql, *params):
    async with pool.connection() as conn:
        cur = await conn.execute(sql, params or None)
        return (await cur.fetchone())[0]


@skip
async def test_end_to_end(pool, tmp_path):
    run = uuid.uuid4()
    dispatch = IngestQueue("dispatch", SpillFile(tmp_path / "d.jsonl"), maxsize=3)
    api = IngestQueue("api", SpillFile(tmp_path / "a.jsonl"))
    wd = TelemetryWriter(pool, dispatch)
    wa = TelemetryWriter(pool, api)

    for i in range(5):                       # maxsize 3: two go to the spill file
        dispatch.put_event(ev(i))
    assert dispatch.spill.pending
    dispatch.put_latest("r1", state_msg={"orderId": "o1"}, active_run_id=run, site_id="site-a",
                        sw_version="jetson-2026.09.1+gabc", last_seen=T0)
    api.put_diagnostics({"ts": T0, "robot_name": "r1", "cpu": 12.5, "gpu": 3.0, "ram": 40.0,
                         "temp_max": 61.0, "power_w": 15.5, "nodes_down": 0,
                         "gnss_fix": "RTK_FIXED", "gnss_sats": 18, "gnss_h_acc_m": 0.014,
                         "gnss_corr_age_s": 1.0})
    api.put_latest("r1", diagnostics={"gnss": {"fix": "RTK_FIXED"}}, nav_supervisor={"mode": "DRIVE"})

    assert await wd.flush_once()
    assert await wa.flush_once()
    assert await count(pool, "SELECT count(*) FROM fleet_events") == 5
    assert not dispatch.spill.pending
    assert await count(pool, "SELECT count(*) FROM diagnostics_ts WHERE gnss_sats = 18") == 1

    # Replaying the same events (spill replay / duplicate messages) inserts nothing new.
    dispatch.spill.append([build_row(ev(i)) for i in range(5)])
    dispatch.put_event(ev(0))
    assert await wd.flush_once()
    assert await count(pool, "SELECT count(*) FROM fleet_events") == 5

    # Each host touched only its own robot_latest columns.
    latest = (await load_latest(pool, raise_errors=True))["r1"]
    assert latest.state_msg == {"orderId": "o1"}
    assert latest.diagnostics == {"gnss": {"fix": "RTK_FIXED"}}
    assert latest.nav_supervisor == {"mode": "DRIVE"}
    assert latest.active_run_id == run and latest.site_id == "site-a"
    assert latest.last_seen == T0
    api.put_latest("r1", diagnostics={"gnss": {"fix": "FLOAT"}})
    assert await wa.flush_once()
    latest = (await load_latest(pool, raise_errors=True))["r1"]
    assert latest.diagnostics == {"gnss": {"fix": "FLOAT"}}
    assert latest.state_msg == {"orderId": "o1"} and latest.sw_version == "jetson-2026.09.1+gabc"


@skip
async def test_copy_types(pool, tmp_path):
    run = uuid.uuid4()
    queue = IngestQueue("dispatch", SpillFile(tmp_path / "s.jsonl"))
    queue.put_state({"ts": T0, "robot_name": "r1", "run_id": run, "x": 1.5, "y": -2.0,
                     "yaw": 0.1, "map_id": "m1", "battery": 81.5, "state": "DRIVING",
                     "order_id": "o1", "last_node": "n3", "driving": True})
    queue.put_state({"ts": T0 + datetime.timedelta(seconds=5), "robot_name": "r1"})
    assert await TelemetryWriter(pool, queue).flush_once()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT ts, run_id, x, battery, driving, order_id FROM robot_state_ts ORDER BY ts")
        rows = await cur.fetchall()
    assert rows == [(T0, run, 1.5, 81.5, True, "o1"),
                    (T0 + datetime.timedelta(seconds=5), None, None, None, None, None)]


@skip
async def test_bad_event_is_rejected_not_blocking(pool, tmp_path):
    queue = IngestQueue("dispatch", SpillFile(tmp_path / "s.jsonl"))
    bad = build_row(ev(1))
    bad["severity"] = "fatal"                      # violates fleet_events_severity_check
    queue.spill.append([bad])
    queue.put_event(ev(2))
    writer = TelemetryWriter(pool, queue)
    assert await writer.flush_once()
    assert await count(pool, "SELECT count(*) FROM fleet_events") == 1
    assert writer.metrics.events_rejected == 1 and not queue.spill.pending


@skip
async def test_policy_load_sources(pool):
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO robotobjectv1 VALUES ('r1', 'ALIVE', '{\"telemetry_recording\": \"full\"}', '{}'),"
            " ('r2', 'ALIVE', '{}', '{}'), ('r3', 'DELETED', '{\"telemetry_recording\": \"full\"}', '{}')")
        await conn.execute(
            "INSERT INTO settingsobjectv1 VALUES ('global', 'ALIVE', '{\"telemetry_recording\": \"off\"}', '{}')")
        await conn.execute(
            "INSERT INTO robot_site_assignments VALUES ('r2', 'site-a', tstzrange(%s, NULL), 'test')",
            (T0,))
    policy = RecordingPolicy(now=lambda: T0 + datetime.timedelta(days=1))
    assert await policy.refresh(pool)
    assert policy.level_for("r1") is RecordingLevel.FULL
    assert policy.level_for("r2") is RecordingLevel.OFF   # site has no level (no siteobjectv1)
    assert policy.level_for("r3") is RecordingLevel.OFF   # deleted robot's spec ignored
    assert policy.snapshot()["robot_sites"] == {"r2": "site-a"}
