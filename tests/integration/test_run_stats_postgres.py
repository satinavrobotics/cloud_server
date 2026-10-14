"""GET /api/v1/maps/{id}/run-stats SQL and migration 20261014_01 on a real PostgreSQL.

Opt-in: set RUN_STATS_TEST_DSN to a THROWAWAY database (never port 5432), e.g.

  docker run -d --name stats-test -p 127.0.0.1:55443:5432 -e POSTGRES_PASSWORD=t \\
      -e POSTGRES_DB=mission timescale/timescaledb-ha:pg17.11-ts2.30.1
  RUN_STATS_TEST_DSN="host=127.0.0.1 port=55443 dbname=mission user=postgres password=t" \\
      pytest tests/integration/test_run_stats_postgres.py

The test drops and recreates minimal copies of cause_codes, mission_runs, run_legs and
fleet_events (plain tables: the statements do not depend on the hypertable).
"""
import datetime
import importlib.util
import os
import pathlib
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.requires_docker]

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

VERSIONS = (pathlib.Path(__file__).resolve().parents[2] / "packages" / "api" / "migrations"
            / "versions")
UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 10, 1, 8, 0, tzinfo=UTC)


def _dsn():
    dsn = os.environ.get("RUN_STATS_TEST_DSN")
    if not dsn:
        return None
    from psycopg.conninfo import conninfo_to_dict
    return None if str(conninfo_to_dict(dsn).get("port", "5432")) == "5432" else dsn


DSN = _dsn()
skip = pytest.mark.skipif(not DSN, reason="set RUN_STATS_TEST_DSN to a throwaway PostgreSQL "
                                          "(never port 5432)")

SCHEMA = """
DROP TABLE IF EXISTS run_legs, fleet_events, mission_runs, cause_codes CASCADE;
CREATE TABLE cause_codes (code text PRIMARY KEY, category text NOT NULL, title text NOT NULL,
                          description text);
INSERT INTO cause_codes VALUES ('UNKNOWN', 'UNKNOWN', 'Unknown', NULL),
  ('OPERATOR.CANCELED', 'OPERATOR', 'Canceled', NULL);
CREATE TABLE mission_runs (run_id uuid PRIMARY KEY, mission_name text, robot_name text NOT NULL,
  map_id text, state text NOT NULL, abort_cause text REFERENCES cause_codes(code),
  started_at timestamptz NOT NULL, ended_at timestamptz, archived_at timestamptz);
CREATE TABLE run_legs (run_id uuid REFERENCES mission_runs(run_id), seq int, duration_s double
  precision NOT NULL, recoveries int NOT NULL DEFAULT 0, recovery_s double precision NOT NULL
  DEFAULT 0, PRIMARY KEY (run_id, seq));
CREATE TABLE fleet_events (ts timestamptz NOT NULL, event_id uuid, robot_name text, run_id uuid,
  code text NOT NULL, payload jsonb NOT NULL DEFAULT '{}');
"""


def migration_sql(direction):
    spec = importlib.util.spec_from_file_location("m_run_cause", VERSIONS /
                                                  "20261014_01_run_cause_codes.py")
    module = importlib.util.module_from_spec(spec)
    statements = []
    spec.loader.exec_module(module)
    module.op = SimpleNamespace(execute=statements.append)
    getattr(module, direction)()
    return statements


class Db:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def connection(self):
        yield self.conn


@skip
async def test_run_stats_on_postgres():
    import psycopg
    from packages.api import run_stats
    async with await psycopg.AsyncConnection.connect(DSN, autocommit=True) as conn:
        await conn.execute(SCHEMA)
        for sql in migration_sql("upgrade"):                      # seeds + index
            await conn.execute(sql)
        for sql in migration_sql("upgrade"):                      # idempotent
            await conn.execute(sql)
        codes = [r[0] for r in await (await conn.execute("SELECT code FROM cause_codes")).fetchall()]
        assert {"SAFETY.REFLEX_LATCHED", "MAP.NOT_PLACED"} <= set(codes)

        def at(minutes):
            return T0 + datetime.timedelta(minutes=minutes)

        runs = {}

        async def run(key, robot, state, start, minutes, map_id="m", cause=None, archived=False):
            rid = runs[key] = uuid.uuid4()
            end = None if state == "RUNNING" else at(start + minutes)
            await conn.execute(
                "INSERT INTO mission_runs VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (rid, key, robot, map_id, state, cause, at(start), end,
                 at(0) if archived else None))

        await run("ok1", "r1", "COMPLETED", 0, 10)
        await run("ok2", "r1", "COMPLETED", 5, 10)               # overlaps ok1 by 5 min
        await run("ok3", "r1", "COMPLETED", 60, 20)              # has a teleop session
        await run("fail", "r2", "FAILED", 0, 5, cause="UNKNOWN")
        await run("cancel", "r2", "CANCELED", 30, 5, cause="OPERATOR.CANCELED")
        await run("take", "r2", "CANCELED", 40, 5, cause="OPERATOR.CANCELED")
        await run("hung", "r2", "CANCELED", 100, 600, cause="OPERATOR.CANCELED")  # 10 h: excluded
        await run("open", "r1", "RUNNING", 200, 0)
        await run("other", "r1", "COMPLETED", 300, 10, map_id="elsewhere")
        await run("nomap", "r1", "COMPLETED", 400, 10, map_id=None)
        await run("hidden", "r1", "FAILED", 500, 10, cause="UNKNOWN", archived=True)

        async def leg(key, seq, duration, recoveries=0, recovery_s=0.0):
            await conn.execute("INSERT INTO run_legs VALUES (%s, %s, %s, %s, %s)",
                               (runs[key], seq, duration, recoveries, recovery_s))
        await leg("ok1", 0, 100.0, 1, 10.0)
        await leg("ok1", 1, 100.0)
        await leg("ok3", 0, 200.0, 2, 30.0)
        await leg("other", 0, 999.0, 1, 999.0)

        async def event(minutes, code, robot, run_key=None, **payload):
            import json
            await conn.execute(
                "INSERT INTO fleet_events VALUES (%s, %s, %s, %s, %s, %s::jsonb)",
                (at(minutes), uuid.uuid4(), robot, runs.get(run_key), code, json.dumps(payload)))
        await event(42, "ROBOT.ERROR_RAISED", "r2", "take", error_type="operatorTakeover")
        await event(70, "TELEOP.SESSION_STARTED", "r1", "ok3", map_id="m", role="robot")
        await event(1, "ROBOT.ERROR_RAISED", "r1", "ok1", error_type="safetyReflexError")
        await event(2, "ROBOT.ERROR_RAISED", "r1", "ok1", error_type="motorFault")  # not safety
        await event(3, "MISSION.EDGE_BLOCKED", "r1", None)       # run-less, inside ok1's window
        await event(4, "NAV.GOAL_BLOCKED", "r1", "other")        # another map's run
        await event(150, "MISSION.NODE_SKIPPED", "r1", None)     # outside any run
        await event(2, "ROBOT.HEARTBEAT_LOST", "r2", "fail")

        out = await run_stats.run_stats(Db(conn), "m", at(-10), at(1000))
        assert out["runs"] == {"finished": 7, "completed": 3, "failed": 1, "timeout": 0,
                               "aborted": 0, "canceled": 3, "running": 1}
        assert out["success_rate"] == 0.75
        assert out["intervention"]["takeovers"] == 1 and out["intervention"]["cancels"] == 2
        assert out["intervention"]["teleop"] == 1 and out["intervention"]["count"] == 4
        assert out["intervention"]["free_runs"] == 2                 # ok3 had a teleop session
        # r1: ok1+ok2 union 15 min + ok3 20 = 35; r2: fail 5 + cancel 5 + take 5 = 15;
        # the 10 h hung run is excluded -> 50 min
        assert out["hours"] == round(50 / 60, 3)
        assert out["data_quality"]["hours_excluded_runs"] == 1
        assert out["duration_s"]["n"] == 3 and out["duration_s"]["median"] == 600.0
        assert out["recovery"]["legs"] == 3 and out["recovery"]["legs_with_recovery"] == 2
        assert out["recovery"]["median_s"] == 20.0 and out["recovery"]["total_s"] == 40.0
        assert out["safety"]["by_class"] == {"safety_reflex": 1, "blocked": 1, "skipped": 0,
                                             "link_loss": 1}
        assert out["by_cause"] == {"OPERATOR.CANCELED": 3, "UNKNOWN": 1}
        assert out["data_quality"]["runs_without_map"] == 1
        # the first teleop event (minute 70) is after the window start: not fully recorded
        assert out["data_quality"]["teleop_recorded"] is False
        late = await run_stats.run_stats(Db(conn), "m", at(75), at(1000))
        assert late["data_quality"]["teleop_recorded"] is True

        only_r1 = await run_stats.run_stats(Db(conn), "m", at(-10), at(1000), robot="r1")
        assert only_r1["runs"]["finished"] == 3 and only_r1["intervention"]["teleop"] == 1

        for sql in migration_sql("downgrade"):
            await conn.execute(sql)
        assert (await (await conn.execute(
            "SELECT count(*) FROM cause_codes WHERE code = 'MAP.NOT_PLACED'")).fetchone())[0] == 0
