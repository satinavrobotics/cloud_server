"""Migration 20261027_01 (recordings + views) and the ingest upsert on a real PostgreSQL.

Opt-in: set RECORDINGS_TEST_DSN to a THROWAWAY database (never port 5432), e.g.

  docker run -d --name rec-test -p 127.0.0.1:55444:5432 -e POSTGRES_PASSWORD=t \\
      -e POSTGRES_DB=mission timescale/timescaledb-ha:pg17.11-ts2.30.1
  RECORDINGS_TEST_DSN="host=127.0.0.1 port=55444 dbname=mission user=postgres password=t" \\
      pytest tests/integration/test_recordings_postgres.py

The test drops and recreates minimal copies of mission_runs, run_legs and fleet_events (plain tables:
the views do not depend on the hypertable) and the recordings objects.
"""
import datetime
import importlib.util
import json
import os
import pathlib
import uuid
from types import SimpleNamespace

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.requires_docker]

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

VERSIONS = (pathlib.Path(__file__).resolve().parents[2] / "packages" / "api" / "migrations"
            / "versions")
UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 10, 18, 17, 0, tzinfo=UTC)


def _dsn():
    dsn = os.environ.get("RECORDINGS_TEST_DSN")
    if not dsn:
        return None
    from psycopg.conninfo import conninfo_to_dict
    return None if str(conninfo_to_dict(dsn).get("port", "5432")) == "5432" else dsn


DSN = _dsn()
skip = pytest.mark.skipif(not DSN, reason="set RECORDINGS_TEST_DSN to a throwaway PostgreSQL "
                                          "(never port 5432)")

SCHEMA = """
DROP VIEW IF EXISTS recording_events, recording_legs, recording_runs;
DROP TABLE IF EXISTS recordings, run_legs, fleet_events, mission_runs CASCADE;
CREATE TABLE mission_runs (run_id uuid PRIMARY KEY, mission_name text, robot_name text NOT NULL,
  started_at timestamptz NOT NULL, ended_at timestamptz);
CREATE TABLE run_legs (run_id uuid REFERENCES mission_runs(run_id), seq int, robot_name text NOT NULL,
  to_vda_node text NOT NULL, started_at timestamptz NOT NULL, ended_at timestamptz NOT NULL,
  PRIMARY KEY (run_id, seq));
CREATE TABLE fleet_events (ts timestamptz NOT NULL, event_id uuid, robot_name text, run_id uuid,
  code text NOT NULL, severity text NOT NULL DEFAULT 'info', payload jsonb NOT NULL DEFAULT '{}');
"""


def migration_sql(direction):
    spec = importlib.util.spec_from_file_location("m_recordings", VERSIONS / "20261027_01_recordings.py")
    module = importlib.util.module_from_spec(spec)
    statements = []
    spec.loader.exec_module(module)
    module.op = SimpleNamespace(execute=statements.append)
    getattr(module, direction)()
    return statements


def at(seconds):
    return T0 + datetime.timedelta(seconds=seconds)


async def _col(conn, sql, *params):
    return [r[0] for r in await (await conn.execute(sql, params)).fetchall()]


@skip
async def test_recordings_migration_views_and_upsert():
    import psycopg
    from packages.topomap_dbs.rosbag_db import recordings as rec
    async with await psycopg.AsyncConnection.connect(DSN, autocommit=True) as conn:
        await conn.execute(SCHEMA)
        for direction in ("upgrade", "downgrade", "upgrade"):
            for sql in migration_sql(direction):
                await conn.execute(sql)
            present = await _col(conn, "SELECT to_regclass('recordings') IS NOT NULL")
            assert present == [direction == "upgrade"]
        assert await _col(conn, "SELECT indexdef FROM pg_indexes WHERE indexname = "
                                "'recordings_robot_during_idx'") != []
        assert "gist" in (await _col(conn, "SELECT indexdef FROM pg_indexes WHERE indexname = "
                                           "'recordings_robot_during_idx'"))[0]

        # --- mission data: robot r runs A [0, 100], B [100, 200], C open from 300; q has Q [0, 100]
        runs = {}
        for key, robot, start, end in (("A", "r", 0, 100), ("B", "r", 100, 200), ("C", "r", 300, None),
                                       ("Q", "q", 0, 100)):
            runs[key] = uuid.uuid4()
            await conn.execute("INSERT INTO mission_runs VALUES (%s, %s, %s, %s, %s)",
                               (runs[key], key, robot, at(start), None if end is None else at(end)))
        for key, seq, robot, start, end in (("A", 1, "r", 10, 40), ("A", 2, "r", 40, 90),
                                            ("B", 1, "r", 110, 150), ("Q", 1, "q", 10, 40)):
            await conn.execute("INSERT INTO run_legs VALUES (%s, %s, %s, 'n', %s, %s)",
                               (runs[key], seq, robot, at(start), at(end)))
        for sec, robot in ((50, "r"), (95, "r"), (120, "r"), (50, "q"), (1000, "r")):
            await conn.execute("INSERT INTO fleet_events (ts, event_id, robot_name, code) "
                               "VALUES (%s, %s, %s, 'E')", (at(sec), uuid.uuid4(), robot))

        def row(group, start, end, robot="r", part="nav", **kw):
            r = rec.build_row({"bag_id": group, "robot_name": robot, "map_id": "m",
                               "description": f"{group}_navstack_sati_odin"}, None, None)
            r.update(group_key=group, part=part, start=None if start is None else at(start),
                     end=None if end is None else at(end), id=str(uuid.uuid4()), **kw)
            r["source"]["interval_source"] = "metadata"
            return r

        rows = [row("span", 50, 250),                 # spans A, B (and the gap), not C
                row("inside", 20, 30),                # inside A only
                row("near_before", -4, -1),           # 4 s before A: within the 5 s tolerance
                row("too_early", -20, -6),            # 6 s before A: outside it
                row("tail", 294, 296),                # 4 s before open run C: tolerance
                row("open", 1000, 1010),              # inside open run C (ended_at NULL)
                row("point", 55, 55),                 # sidecar-start-only: a point inside A
                row("nothing", None, None),           # empty range
                row("other_robot", 20, 30, robot="z"),
                row("boundary", 200, 205)]            # touches B's end exactly
        def write(batch):                                     # write() is the scripts' sync cursor API
            with psycopg.connect(DSN) as sync, sync.cursor() as cur:
                return rec.write(cur, batch)
        counts = write(rows)
        assert counts == {"inserted": 10, "updated": 0, "kept": 0}
        assert (await _col(conn, "SELECT during FROM recordings WHERE group_key = 'nothing'"))[0].isempty

        async def links(sql):
            return {tuple(r) for r in await (await conn.execute(sql)).fetchall()}

        pairs = await links("SELECT g.group_key, v.mission_name, round(v.overlap_s::numeric), v.confirmed "
                            "FROM recording_runs v JOIN recordings g ON g.id = v.recording_id")
        assert pairs == {("span", "A", 50, False), ("span", "B", 100, False),
                         ("inside", "A", 10, False), ("near_before", "A", 0, False),
                         ("tail", "C", 0, False), ("open", "C", 10, False), ("point", "A", 0, False),
                         ("boundary", "B", 0, False)}                       # one bag, two runs (and v.v.)
        legs = await links("SELECT g.group_key, v.run_id::text = %s, v.seq, round(v.overlap_s::numeric) "
                           "FROM recording_legs v JOIN recordings g ON g.id = v.recording_id"
                           % ("'" + str(runs["A"]) + "'"))
        assert legs == {("span", True, 2, 40), ("span", False, 1, 40),
                        ("inside", True, 1, 10), ("point", True, 2, 0)}
        events = await _col(conn, "SELECT g.group_key || ':' || extract(epoch from v.ts - %s)::int "
                                  "FROM recording_events v JOIN recordings g ON g.id = v.recording_id "
                                  "ORDER BY 1", T0)
        assert events == ["open:1000", "span:120", "span:50", "span:95"]

        # --- confirmed: the robot-recorded run_id
        await conn.execute("UPDATE recordings SET run_id = %s WHERE group_key = 'inside'", (runs["A"],))
        assert await _col(conn, "SELECT confirmed FROM recording_runs v JOIN recordings g ON "
                                "g.id = v.recording_id WHERE g.group_key = 'inside'") == [True]

        # --- upsert: idempotent, dedupe on (robot, group, part), robot context kept, no downgrade
        again = [dict(r, id=str(uuid.uuid4())) for r in rows]
        with_obj = row("span", 50, 250, size_bytes=77, content_hash="h2")
        without = row("span", 60, 70, size_bytes=None)
        assert write(again) == {"inserted": 0, "updated": 10, "kept": 0}
        assert write([with_obj]) == {"inserted": 0, "updated": 1, "kept": 0}
        assert write([without]) == {"inserted": 0, "updated": 0, "kept": 1}
        assert await _col(conn, "SELECT count(*) FROM recordings") == [10]
        assert await _col(conn, "SELECT size_bytes FROM recordings WHERE group_key = 'span'") == [77]
        assert await _col(conn, "SELECT run_id FROM recordings WHERE group_key = 'inside'") == [runs["A"]]
        assert await _col(conn, "SELECT count(*) FROM recordings WHERE group_key = 'span'") == [1]
        raw = row("span", 50, 250, part="raw")                      # nav and raw of a run: two rows
        assert write([raw])["inserted"] == 1

        # --- the constraints
        with pytest.raises(psycopg.errors.CheckViolation):
            await conn.execute("INSERT INTO recordings (kind, robot_name, during, group_key, part, "
                               "storage_bucket, storage_key) VALUES ('x', 'r', 'empty', 'g', 'p', 'b', 'k')")

        for sql in migration_sql("downgrade"):
            await conn.execute(sql)
        assert await _col(conn, "SELECT count(*) FROM pg_views WHERE viewname LIKE 'recording_%%'") == [0]
