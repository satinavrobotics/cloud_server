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
    db._commit_update = AsyncMock()
    seen = {}

    async def hook(_c, old, new):
        seen["new"] = new

    await db.update_spec_fields(MissionObjectV1, "m1", {"route_rev": 4},
                                "pub", before_commit=hook)
    sql, params = cur.execute.await_args_list[-1].args
    assert "GREATEST" in sql and params[1] == 4
    assert seen["new"]["route_rev"] == 9
