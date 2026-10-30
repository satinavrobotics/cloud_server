"""update_spec_fields keeps route_rev monotonic in SQL (GREATEST), parameterised."""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from packages.database.postgres import PostgresDatabase
from cloud_common.objects.mission import MissionObjectV1

pytestmark = pytest.mark.unit
build = PostgresDatabase._spec_fields_query


def test_plain_patch_unchanged():
    q, p = build("missions", "m1", {"name_x": 1})
    assert "GREATEST" not in q and q.count("%s") == 2
    assert p == [json.dumps({"name_x": 1}), "m1"]


def test_route_rev_uses_greatest_and_is_parameterised():
    q, p = build("missions", "m1", {"route_rev": 7, "routes": [1]})
    assert "GREATEST(coalesce((spec->>'route_rev')::int, 0), %s::int)" in q
    assert q.count("%s") == 3 and "7" not in q
    assert p == [json.dumps({"routes": [1]}), 7, "m1"]


def test_route_rev_only_patch():
    q, p = build("missions", "m1", {"route_rev": 3})
    assert p == ["{}", 3, "m1"]


@pytest.mark.parametrize("bad", ["1; DROP TABLE x", 1.5, True, None])
def test_route_rev_must_be_int(bad):
    with pytest.raises(ValueError):
        build("missions", "m1", {"route_rev": bad})


@pytest.mark.asyncio
async def test_update_spec_fields_executes_built_query_and_hook_sees_max():
    cur = AsyncMock()
    cur.fetchone = AsyncMock(return_value=({"route_rev": 9, "x": 1},))
    cur.__aenter__ = AsyncMock(return_value=cur)
    cur.__aexit__ = AsyncMock(return_value=False)
    conn = AsyncMock()
    conn.cursor = MagicMock(return_value=cur)
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)
    db = PostgresDatabase(dbname="d", user="u", password="p", host="h", port=5432)
    db._pool = MagicMock()
    db._pool.connection = MagicMock(return_value=conn)
    db._commit_update = AsyncMock(return_value=("m1", "ACTIVE", {"route_rev": 9, "x": 1}, {}))
    seen = {}

    async def hook(_c, old, new):
        seen["new"] = new

    await db.update_spec_fields(MissionObjectV1, "m1", {"route_rev": 4},
                                "pub", before_commit=hook)
    sql, params = cur.execute.await_args_list[-1].args
    assert "GREATEST" in sql and params[1] == 4
    assert seen["new"]["route_rev"] == 9


# --- bump_route_rev: stored + 1 in SQL, returned, and seen by the hook ---------------------------
def test_bump_is_a_relative_sql_increment_and_parameterised():
    q, p = build("missions", "m1", {"routes": [1]}, bump_route_rev=True)
    assert "coalesce((spec->>'route_rev')::int, 0) + 1" in q and "GREATEST" not in q
    assert q.count("%s") == 2 and p == [json.dumps({"routes": [1]}), "m1"]
    q, p = build("missions", "m1", {}, bump_route_rev=True)
    assert p == ["{}", "m1"]


class _FakeStore:
    """A mission row with the SQL's semantics: GREATEST for an absolute route_rev, +1 for a bump."""
    def __init__(self, rev):
        self.spec = {"route_rev": rev}
        self.last = None

    def make_db(self):
        store = self
        cur = AsyncMock()

        async def execute(sql, params=None):
            store.last = sql
            if sql.startswith("UPDATE"):
                patch = json.loads(params[0])
                rev = store.spec["route_rev"]
                if "+ 1" in sql:
                    store.spec = {**store.spec, **patch, "route_rev": rev + 1}
                elif "GREATEST" in sql:
                    store.spec = {**store.spec, **patch, "route_rev": max(rev, params[1])}
                else:
                    store.spec = {**store.spec, **patch}

        async def fetchone():
            if store.last.startswith("SELECT"):
                return (dict(store.spec),)
            return ("m1", "ACTIVE", dict(store.spec), {})

        cur.execute = AsyncMock(side_effect=execute)
        cur.fetchone = AsyncMock(side_effect=fetchone)
        cur.__aenter__ = AsyncMock(return_value=cur)
        cur.__aexit__ = AsyncMock(return_value=False)
        conn = AsyncMock()
        conn.cursor = MagicMock(return_value=cur)
        conn.__aenter__ = AsyncMock(return_value=conn)
        conn.__aexit__ = AsyncMock(return_value=False)
        db = PostgresDatabase(dbname="d", user="u", password="p", host="h", port=5432)
        db._pool = MagicMock()
        db._pool.connection = MagicMock(return_value=conn)
        db._notify = AsyncMock()
        return db


@pytest.mark.asyncio
async def test_two_sequential_bumps_both_strictly_increase():
    store = _FakeStore(0)
    db = store.make_db()
    first = await db.update_spec_fields(MissionObjectV1, "m1", {}, "pub", bump_route_rev=True)
    second = await db.update_spec_fields(MissionObjectV1, "m1", {"planned_path": None}, "pub",
                                         bump_route_rev=True)
    assert (first, second) == (1, 2) and store.spec["route_rev"] == 2


@pytest.mark.asyncio
async def test_a_bump_after_a_writer_the_caller_never_saw_does_not_tie():
    # Both read revision 3; the dispatcher stored 4 meanwhile; the operator's bump lands on 5.
    store = _FakeStore(3)
    db = store.make_db()
    dispatcher = await db.update_spec_fields(MissionObjectV1, "m1", {}, "pub", bump_route_rev=True)
    operator = await db.update_spec_fields(MissionObjectV1, "m1", {}, "pub", bump_route_rev=True)
    assert dispatcher == 4 and operator == 5
    # the old absolute write (rev read + 1 = 4) is still clamped, never lowered
    assert await db.update_spec_fields(MissionObjectV1, "m1", {"route_rev": 4}, "pub") == 5


@pytest.mark.asyncio
async def test_the_hook_sees_the_stored_plus_one_value():
    store = _FakeStore(6)
    db = store.make_db()
    seen = {}

    async def hook(_c, old, new):
        seen["old"], seen["new"] = old, new

    stored = await db.update_spec_fields(MissionObjectV1, "m1", {"planned_path": None}, "pub",
                                         before_commit=hook, bump_route_rev=True)
    assert stored == 7 and seen["old"]["route_rev"] == 6 and seen["new"]["route_rev"] == 7


@pytest.mark.asyncio
async def test_bump_and_an_explicit_route_rev_are_exclusive():
    db = _FakeStore(0).make_db()
    with pytest.raises(ValueError):
        await db.update_spec_fields(MissionObjectV1, "m1", {"route_rev": 2}, "pub",
                                    bump_route_rev=True)
