"""Migration 20261027_01 (navstack_deployments, the one-running partial unique index, robot_software_periods)
and the DeploymentStore / deploy job on a real PostgreSQL.

Opt-in: set NAVSTACK_TEST_DSN to a SCRATCH database (its name must contain "scratch": the test creates and
drops tables in it, so never the live `mission` database), e.g.

  psql ... -c 'CREATE DATABASE navstack_scratch'
  NAVSTACK_TEST_DSN="host=127.0.0.1 port=5432 dbname=navstack_scratch user=postgres password=..." \\
      pytest tests/integration/test_navstack_deployments_postgres.py
  psql ... -c 'DROP DATABASE navstack_scratch'
"""
import asyncio
import datetime
import importlib.util
import os
import pathlib
import uuid
from types import SimpleNamespace

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.requires_docker]

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

VERSIONS = (pathlib.Path(__file__).resolve().parents[2] / "packages" / "api" / "migrations" / "versions")
UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


def _dsn():
    dsn = os.environ.get("NAVSTACK_TEST_DSN")
    if not dsn:
        return None
    from psycopg.conninfo import conninfo_to_dict
    return dsn if "scratch" in str(conninfo_to_dict(dsn).get("dbname", "")) else None


DSN = _dsn()
skip = pytest.mark.skipif(not DSN, reason="set NAVSTACK_TEST_DSN to a scratch database (name contains 'scratch')")


def migration_sql(direction):
    try:
        import alembic  # noqa: F401
    except ImportError:                      # only the module-level `from alembic import op` is needed
        import sys
        import types
        fake = types.ModuleType("alembic")
        fake.op = SimpleNamespace(execute=lambda sql: None)
        sys.modules["alembic"] = fake
    spec = importlib.util.spec_from_file_location("m_navstack", VERSIONS / "20261027_01_navstack_deployments.py")
    module = importlib.util.module_from_spec(spec)
    statements = []
    spec.loader.exec_module(module)
    module.op = SimpleNamespace(execute=statements.append)
    getattr(module, direction)()
    return statements


class Db:
    """The two members of PostgresDatabase the store uses, over a pool on the scratch database."""

    def __init__(self, pool):
        self._pool = pool

    def connection(self):
        return self._pool.connection()

    async def dedicated_connection(self):
        import psycopg
        return await psycopg.AsyncConnection.connect(DSN, autocommit=True)


def row(robot, state="queued", kind="deploy", to_tag="humble-1-abcdef0", at=0, **kw):
    r = {"id": str(uuid.uuid4()), "robot_name": robot, "kind": kind, "mode": "image", "to_tag": to_tag,
         "from_tag": None, "from_sw_version": None, "to_git_sha": "abcdef0", "state": state,
         "steps": [{"name": "preflight", "state": "pending", "started_at": None, "finished_at": None,
                    "detail": None}], "error": None, "requested_by": "t",
         "requested_at": T0 + datetime.timedelta(minutes=at)}
    r.update(kw)
    return r


@pytest.fixture
async def db():
    import psycopg
    from psycopg_pool import AsyncConnectionPool
    async with await psycopg.AsyncConnection.connect(DSN, autocommit=True) as conn:
        await conn.execute("DROP VIEW IF EXISTS robot_software_periods; "
                           "DROP TABLE IF EXISTS navstack_deployments")
        for sql in migration_sql("upgrade"):
            await conn.execute(sql)
    pool = AsyncConnectionPool(DSN, min_size=1, max_size=4, open=False)
    await pool.open(wait=True)
    try:
        yield Db(pool)
    finally:
        await pool.close()
        async with await psycopg.AsyncConnection.connect(DSN, autocommit=True) as conn:
            await conn.execute("DROP VIEW IF EXISTS robot_software_periods; "
                               "DROP TABLE IF EXISTS navstack_deployments")


@skip
async def test_migration_up_down_up_and_objects():
    import psycopg
    async with await psycopg.AsyncConnection.connect(DSN, autocommit=True) as conn:
        await conn.execute("DROP VIEW IF EXISTS robot_software_periods; DROP TABLE IF EXISTS navstack_deployments")
        for direction in ("upgrade", "downgrade", "upgrade"):
            for sql in migration_sql(direction):
                await conn.execute(sql)
            cur = await conn.execute("SELECT to_regclass('navstack_deployments') IS NOT NULL, "
                                     "to_regclass('robot_software_periods') IS NOT NULL")
            assert await cur.fetchone() == (direction == "upgrade",) * 2
        cur = await conn.execute("SELECT indexdef FROM pg_indexes WHERE indexname = 'navstack_deployments_one_running'")
        definition = (await cur.fetchone())[0]
        assert "UNIQUE" in definition and "(robot_name)" in definition
        assert "'done'" in definition and "'cancelled'" in definition
        cur = await conn.execute("SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                                 "WHERE table_name = 'navstack_deployments' ORDER BY ordinal_position")
        cols = {r[0]: (r[1], r[2]) for r in await cur.fetchall()}
        assert cols["steps"] == ("jsonb", "NO") and cols["requested_at"][0] == "timestamp with time zone"
        assert cols["id"][0] == "uuid" and cols["finished_at"][1] == "YES"
        await conn.execute("DROP VIEW robot_software_periods; DROP TABLE navstack_deployments")


@skip
async def test_partial_unique_index_allows_one_non_final_row_per_robot(db):
    import psycopg
    from packages.api import navstack_deploy as nd
    store = nd.DeploymentStore(db)
    first = row("r1", state="pull")
    await store.insert(first)
    with pytest.raises(nd.AnotherRunning):
        await store.insert(row("r1", state="queued"))
    await store.insert(row("r2", state="verify"))                    # another robot: fine
    for final in nd.FINAL:                                           # any number of final rows
        await store.insert(row("r1", state=final, at=1))
    assert (await store.active_for("r1"))["id"] == first["id"]
    # the slot frees when the row ends, and not before
    assert await store.update(first["id"], expect=nd.ACTIVE, state=nd.DONE, finished_at=T0)
    await store.insert(row("r1", state="queued", at=5))
    # a violation of anything else is not reported as "another running"
    async with db.connection() as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            await conn.execute("INSERT INTO navstack_deployments (id, robot_name, kind, mode, state) "
                               "VALUES (%s, 'r9', 'deploy', 'image', 'bogus')", (str(uuid.uuid4()),))


@skip
async def test_racing_inserts_one_wins(db):
    from packages.api import navstack_deploy as nd
    store = nd.DeploymentStore(db)
    results = await asyncio.gather(*[store.insert(row("r1")) for _ in range(4)], return_exceptions=True)
    assert sum(r is None for r in results) == 1
    assert all(isinstance(r, nd.AnotherRunning) for r in results if r is not None)


@skip
async def test_store_roundtrip_cas_list_and_lock(db):
    from packages.api import navstack_deploy as nd
    store = nd.DeploymentStore(db)
    a, b = row("r1", state="done", at=0), row("r1", state="queued", at=10)
    await store.insert(a)
    await store.insert(b)
    got = await store.get(b["id"])
    assert got["steps"] == b["steps"] and got["requested_at"] == b["requested_at"] and got["state"] == "queued"
    assert [r["id"] for r in await store.list_for("r1", 10)] == [b["id"], a["id"]]
    assert [r["id"] for r in await store.list_for("r1", 1)] == [b["id"]]
    assert [r["id"] for r in await store.non_final()] == [b["id"]]
    # compare-and-set: only from an expected state
    steps = [{"name": "preflight", "state": "done", "started_at": "x", "finished_at": "y", "detail": "ok",
              "op_id": "op-1"}]
    assert await store.update(b["id"], expect="queued", state="preflight", steps=steps, started_at=T0)
    assert not await store.update(b["id"], expect=("queued", "pull"), state="cancelled")
    assert await store.update(b["id"], expect=nd.CANCELLABLE, state="pull")
    got = await store.get(b["id"])
    assert got["state"] == "pull" and got["steps"] == steps and got["started_at"] == T0
    assert not await store.update(str(uuid.uuid4()), state="done")
    with pytest.raises(ValueError):
        await store.update(b["id"], robot_name="other")
    # the public JSON
    shown = nd.public(got)
    assert shown["steps"] == [{"name": "preflight", "state": "done", "started_at": "x", "finished_at": "y",
                               "detail": "ok"}]
    assert shown["requested_at"].startswith("2026-10-27T12:10:00")
    # advisory lock: one runner per row
    release = await store.try_lock(b["id"])
    assert release is not None and await store.try_lock(b["id"]) is None
    await release()
    again = await store.try_lock(b["id"])
    assert again is not None
    await again()
    await store.delete(b["id"])
    assert await store.get(b["id"]) is None


@skip
async def test_robot_software_periods_view(db):
    from packages.api import navstack_deploy as nd
    store = nd.DeploymentStore(db)
    t = lambda m: T0 + datetime.timedelta(minutes=m)  # noqa: E731
    for r in (row("r1", "done", to_tag="humble-1-aaaaaaa", at=0, finished_at=t(1), to_git_sha="aaaaaaa"),
              row("r1", "done", "pull", to_tag="humble-2-bbbbbbb", at=2, finished_at=t(3)),
              row("r1", "failed", to_tag="humble-3-ccccccc", at=4, finished_at=t(5)),
              row("r1", "done", "rollback", to_tag="humble-1-aaaaaaa", at=6, finished_at=t(7)),
              row("r1", "failed_verify", to_tag="humble-4-ddddddd", at=8, finished_at=t(9)),
              row("r1", "done", to_tag=None, at=10, finished_at=t(11), mode="dev", to_git_sha=None),
              row("r2", "done", to_tag="humble-9-eeeeeee", at=0, finished_at=t(2))):
        await store.insert(r)
        await store.update(r["id"], finished_at=r["finished_at"])      # insert() writes a new row only
    async with db.connection() as conn:
        cur = await conn.execute("SELECT robot_name, mode, tag, valid_from, valid_to FROM robot_software_periods "
                                 "ORDER BY robot_name, valid_from")
        got = await cur.fetchall()
    assert got == [("r1", "image", "humble-1-aaaaaaa", t(1), t(7)),     # pull / failed rows are no period
                   ("r1", "image", "humble-1-aaaaaaa", t(7), t(11)),
                   ("r1", "dev", None, t(11), None),
                   ("r2", "image", "humble-9-eeeeeee", t(2), None)]


@skip
async def test_the_job_runs_against_the_real_store(db):
    """The deploy state machine with the real store, orchestrator and facts faked (tests/unit has the matrix)."""
    from packages.api import navstack_deploy as nd
    from tests.unit import test_navstack_deploy as fx
    env = fx.Env()
    env.store = nd.DeploymentStore(db)
    env.mgr = env.manager()
    dep_id = await env.deploy()
    dep = await env.run(dep_id)
    assert dep["state"] == "done", dep["error"]
    assert [s["state"] for s in dep["steps"]] == ["done", "done", "done", "done"]
    with_op = await env.store.get(dep_id)
    assert [s.get("op_id") for s in with_op["steps"]] == [None, "op-pull", "op-sw", None]   # kept for a resume
    assert "op_id" not in dep["steps"][1]
    # a second one while a pull is in flight is refused by the database too
    env.orch.pull_script = [{"id": "op-pull", "state": "running", "steps": []}]
    env.clock.gate = asyncio.Event()
    env.orch.status["running"] = {"mode": "image", "tag": fx.NEW}
    first = await env.deploy(tag=fx.OLD)
    await asyncio.sleep(0)
    with pytest.raises(Exception) as err:
        await env.deploy(tag=fx.PREV)
    assert getattr(err.value, "status_code", None) == 409
    env.clock.gate.set()
    await env.mgr.stop()
    # restart: a fresh manager resumes the row from the database
    env.clock.gate = None
    env.orch.op_scripts["op-pull"] = [{"id": "op-pull", "state": "done", "steps": []}]
    env.orch.target = ("image", fx.OLD)
    env.mgr = env.manager()
    await env.mgr.resume()
    assert (await env.run(first))["state"] == "done"


@skip
async def test_facts_and_fleet_software_sql(db):
    """PgFacts.robot_latest / active_run and fleet_software() read the real columns (minimal copies of the
    two Phase 0 tables)."""
    import psycopg
    from packages.api import navstack_deploy as nd
    from packages.api import navstack_images as ni
    async with await psycopg.AsyncConnection.connect(DSN, autocommit=True) as conn:
        await conn.execute("DROP TABLE IF EXISTS robot_latest, mission_runs;"
                           "CREATE TABLE robot_latest (robot_name text PRIMARY KEY, sw_version text, last_seen timestamptz);"
                           "CREATE TABLE mission_runs (run_id uuid PRIMARY KEY, mission_name text, robot_name text, state text)")
        await conn.execute("INSERT INTO robot_latest VALUES ('a', 'sati/navstack:humble-20261027-c879696', now()), "
                           "('b', 'sati/navstack:humble-20261026-9497ec0', now()), ('c', 'dev:abc1234', NULL)")
        await conn.execute("INSERT INTO mission_runs VALUES (gen_random_uuid(), 'patrol', 'a', 'RUNNING'), "
                           "(gen_random_uuid(), 'old', 'b', 'COMPLETED')")
    try:
        facts = nd.PgFacts(db)
        assert (await facts.robot_latest("a"))["sw_version"] == "sati/navstack:humble-20261027-c879696"
        assert await facts.robot_latest("zz") is None
        assert await facts.active_run("a") == "patrol" and await facts.active_run("b") is None

        class Reg:
            async def list_images(self, branch):
                return {"latest_tag": "humble-20261027-c879696", "images": [
                    {"tag": "humble-20261027-c879696", "aliases": ["humble-latest"]}]}
        out = await ni.fleet_software(db, Reg(), "humble")
        assert [(r["robot_name"], r["behind_latest"]) for r in out["robots"]] == [("a", False), ("b", True), ("c", False)]
        assert out["robots"][2]["last_seen"] is None and out["robots"][0]["last_seen"].endswith("+00:00")
    finally:
        async with await psycopg.AsyncConnection.connect(DSN, autocommit=True) as conn:
            await conn.execute("DROP TABLE robot_latest, mission_runs")
