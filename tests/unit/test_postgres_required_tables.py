"""PostgresDatabase waits for tables created by the API's migrations (v2 WP1.7)."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


def _pool_reporting_missing(*rounds):
    """A mock pool whose to_regclass query reports `rounds[i]` as missing on call i."""
    cursors = []
    for missing in rounds:
        cur = AsyncMock()
        cur.fetchall = AsyncMock(return_value=[(t,) for t in missing])
        cursors.append(cur)
    conn = AsyncMock()
    conn.execute = AsyncMock(side_effect=cursors)
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.connection = MagicMock(return_value=conn)
    return pool, conn


def _db(required):
    from packages.database.postgres import PostgresDatabase
    return PostgresDatabase(dbname="d", user="u", password="p", host="h", port=5432,
                            required_tables=required)


async def test_no_required_tables_never_queries():
    pool, conn = _pool_reporting_missing()
    await _db(())._wait_for_required_tables(pool)
    conn.execute.assert_not_called()


async def test_waits_until_tables_exist():
    pool, conn = _pool_reporting_missing(["mission_runs", "fleet_events"], ["fleet_events"], [])
    with patch("packages.database.postgres.asyncio.sleep", new=AsyncMock()) as sleep:
        await _db(("mission_runs", "fleet_events"))._wait_for_required_tables(pool)
    assert conn.execute.await_count == 3
    assert sleep.await_count == 2
    assert conn.execute.await_args.args[1] == (["mission_runs", "fleet_events"],)


def test_dispatch_requires_phase0_tables():
    pytest.importorskip("py_trees")  # not in tests/Dockerfile's image (pre-existing gap)
    from packages.controllers.mission.server import DISPATCH_REQUIRED_TABLES
    assert set(DISPATCH_REQUIRED_TABLES) == {
        "mission_runs", "fleet_events", "robot_state_ts", "robot_latest"}
