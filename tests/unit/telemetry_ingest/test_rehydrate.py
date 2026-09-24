"""Unit tests for packages/telemetry_ingest/rehydrate.py."""

import uuid

import pytest

from packages.events.detectors import Hysteresis, StateDiff
from packages.telemetry_ingest import tables
from packages.telemetry_ingest.rehydrate import SELECT_SQL, LatestRow, load_latest
from tests.unit.telemetry_ingest.conftest import ConnectionLost
from tests.unit.telemetry_ingest.helpers import T0

pytestmark = pytest.mark.unit

RUN = uuid.UUID("11111111-2222-3333-4444-555555555555")


def record(robot, **values):
    row = {c: None for c in tables.LATEST_COLUMNS}
    row.update(robot_name=robot, updated_at=T0, **values)
    return tuple(row[c] for c in tables.LATEST_COLUMNS)


async def test_loads_rows_keyed_by_robot(db, pool):
    db.query_results = {r"FROM robot_latest": [
        record("r1", state_msg={"operatingMode": "AUTOMATIC", "batteryState": {"batteryCharge": 18}},
               active_run_id=RUN, sw_version="v1", last_seen=T0),
        record("r2", diagnostics='{"gnss": {"fix": "RTK_FIXED"}}', active_run_id=str(RUN)),
    ]}
    rows = await load_latest(pool)
    assert set(rows) == {"r1", "r2"}
    assert rows["r1"] == LatestRow(
        robot_name="r1", active_run_id=RUN, sw_version="v1", last_seen=T0, updated_at=T0,
        state_msg={"operatingMode": "AUTOMATIC", "batteryState": {"batteryCharge": 18}})
    assert rows["r2"].diagnostics == {"gnss": {"fix": "RTK_FIXED"}}  # text jsonb decoded
    assert rows["r2"].active_run_id == RUN
    assert db.statements == [SELECT_SQL]


async def test_rows_seed_detectors_without_spurious_events(db, pool):
    """Seeded detectors report a change that happened while down, and stay quiet otherwise."""
    db.query_results = {r"FROM robot_latest": [
        record("r1", state_msg={"state": "DRIVING", "battery": 18.0})]}
    latest = (await load_latest(pool))["r1"]
    state = StateDiff(latest.state_msg["state"])
    battery = Hysteresis(20, 25, "below", active=latest.state_msg["battery"] <= 20)
    assert state.update("DRIVING") is None
    assert battery.update(19.0) is None                   # still low: no second BATTERY.LOW
    assert state.update("IDLE") is not None


async def test_filter_by_robots(db, pool):
    seen = {}

    def rows(sql, params):
        seen["sql"], seen["params"] = sql, params
        return [record("r1")]

    db.query_results = {r"FROM robot_latest": rows}
    assert set(await load_latest(pool, ["r1", "r9"])) == {"r1"}
    assert seen["sql"].endswith("WHERE robot_name = ANY(%s)")
    assert seen["params"] == (["r1", "r9"],)


async def test_accepts_a_connection(db, pool):
    db.query_results = {r"FROM robot_latest": [record("r1")]}
    async with pool.connection() as conn:
        assert set(await load_latest(conn)) == {"r1"}


async def test_failure_returns_empty_or_raises(db, pool):
    db.fail = lambda sql, params: ConnectionLost("down")
    assert await load_latest(pool) == {}
    with pytest.raises(ConnectionLost):
        await load_latest(pool, raise_errors=True)


async def test_unreadable_row_is_skipped(db, pool):
    db.query_results = {r"FROM robot_latest": [record("bad", state_msg="{not json"), record("ok")]}
    assert set(await load_latest(pool)) == {"ok"}
