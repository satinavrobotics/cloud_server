"""H7: status writes of a Robot are serialized per row, retried and tracked.

A fire-and-forget write lost to a database hiccup left a finished mission RUNNING in the
database; two writes of one row ran concurrently and could commit out of order.
"""
import asyncio
import logging

import fastapi
import psycopg
import pytest

import packages.controllers.mission.server as server_module
from cloud_common import objects as api_objects
from cloud_common.objects import mission as mission_object
from tests.unit.test_mission_lifecycle_fixes import State, _make_robot, _mission

Mission = api_objects.MissionObjectV1


@pytest.fixture(autouse=True)
def fast_retry(monkeypatch):
    monkeypatch.setattr(server_module, "STATUS_WRITE_RETRY_MIN_S", 0.01)
    monkeypatch.setattr(server_module, "STATUS_WRITE_RETRY_MAX_S", 0.02)


async def _settle(r, timeout=2.0):
    await asyncio.wait_for(asyncio.gather(*list(r._status_write_tasks)), timeout)


@pytest.mark.unit
async def test_terminal_write_is_retried_until_it_lands():
    r, db = _make_robot()
    m = _mission()
    db_state = {}
    calls = {"n": 0}

    async def update_status(cls, name, status, writer):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise psycopg.OperationalError("pool timeout")
        db_state[name] = status.state

    db.update_status.side_effect = update_status
    m.status.state = State.COMPLETED
    r._queue_status_write(Mission, m.name, m.status, r._mission_writer_id())
    await _settle(r)
    assert db_state[m.name] == State.COMPLETED
    assert calls["n"] == 4
    assert not r._status_rows and not r._status_write_tasks


@pytest.mark.unit
async def test_rapid_writes_of_a_row_are_ordered_and_one_in_flight():
    r, db = _make_robot()
    m = _mission()
    in_flight = {"now": 0, "max": 0}
    committed = []
    first = {"slow": True}

    async def update_status(cls, name, status, writer):
        snapshot = status.state
        in_flight["now"] += 1
        in_flight["max"] = max(in_flight["max"], in_flight["now"])
        if first["slow"]:
            first["slow"] = False
            await asyncio.sleep(0.1)
        await asyncio.sleep(0)
        in_flight["now"] -= 1
        committed.append(snapshot)

    db.update_status.side_effect = update_status
    m.status.state = State.RUNNING
    r._queue_status_write(Mission, m.name, m.status, r._mission_writer_id())
    await asyncio.sleep(0.01)
    m.status.state = State.COMPLETED
    r._queue_status_write(Mission, m.name, m.status, r._mission_writer_id())
    r._queue_status_write(Mission, m.name, m.status, r._mission_writer_id())
    await _settle(r)
    assert in_flight["max"] == 1
    assert committed[-1] == State.COMPLETED
    assert committed == [State.RUNNING, State.COMPLETED]   # coalesced, in order


@pytest.mark.unit
async def test_awaited_write_does_not_overlap_a_queued_one():
    r, db = _make_robot()
    m = _mission()
    in_flight = {"now": 0, "max": 0}

    async def update_status(cls, name, status, writer):
        in_flight["now"] += 1
        in_flight["max"] = max(in_flight["max"], in_flight["now"])
        await asyncio.sleep(0.02)
        in_flight["now"] -= 1

    db.update_status.side_effect = update_status
    r._queue_status_write(Mission, m.name, m.status, r._mission_writer_id())
    await asyncio.sleep(0.005)
    await r._write_status(Mission, m.name, m.status, r._mission_writer_id())
    await _settle(r)
    assert in_flight["max"] == 1


@pytest.mark.unit
@pytest.mark.parametrize("code", [404, 400])
async def test_row_gone_stops_retrying(code):
    r, db = _make_robot()
    m = _mission()
    db.update_status.side_effect = fastapi.HTTPException(code, "gone")
    r._queue_status_write(Mission, m.name, m.status, r._mission_writer_id())
    await _settle(r)
    assert db.update_status.await_count == 1


@pytest.mark.unit
async def test_shutdown_stops_the_retries():
    r, db = _make_robot()
    m = _mission()
    db.update_status.side_effect = OSError("down")
    r._queue_status_write(Mission, m.name, m.status, r._mission_writer_id())
    await asyncio.sleep(0.05)
    assert db.update_status.await_count > 1
    r.shutdown()
    await asyncio.sleep(0.1)
    assert not r._status_write_tasks
    n = db.update_status.await_count
    await asyncio.sleep(0.05)
    assert db.update_status.await_count == n


@pytest.mark.unit
async def test_flush_waits_for_the_write_then_drops_the_rest():
    r, db = _make_robot()
    m = _mission()
    done = []

    async def update_status(cls, name, status, writer):
        await asyncio.sleep(0.05)
        done.append(name)

    db.update_status.side_effect = update_status
    r._queue_status_write(Mission, m.name, m.status, r._mission_writer_id())
    await r.flush_status_writes(1.0)
    assert done == [m.name]

    db.update_status.side_effect = RuntimeError("down")
    r._queue_status_write(Mission, m.name, m.status, r._mission_writer_id())
    await r.flush_status_writes(0.05)
    await asyncio.sleep(0.01)
    assert not r._status_write_tasks


@pytest.mark.unit
async def test_failures_are_logged_not_left_unretrieved(caplog):
    r, db = _make_robot()
    m = _mission()
    db.update_status.side_effect = [RuntimeError("boom"), None]
    with caplog.at_level(logging.WARNING):
        r._queue_status_write(Mission, m.name, m.status, r._mission_writer_id())
        await _settle(r)
    assert any(m.name in rec.getMessage() and "boom" in rec.getMessage()
               for rec in caplog.records)
    assert not r._status_write_tasks


@pytest.mark.unit
async def test_set_mission_state_terminal_write_goes_through_the_queue():
    r, db = _make_robot()
    m = _mission()
    r._current_mission = m
    r._queue_status_write = lambda *a, **k: queued.append(a)
    queued = []
    r._set_mission_state(mission_object.MissionStateV1.COMPLETED)
    assert queued and queued[-1][1] == m.name
    assert queued[-1][2].state == State.COMPLETED


@pytest.mark.unit
async def test_write_queued_right_before_shutdown_still_lands():
    r, db = _make_robot()
    m = _mission()
    done = []

    async def update_status(cls, name, status, writer):
        await asyncio.sleep(0.03)
        done.append(status.state)

    db.update_status.side_effect = update_status
    m.status.state = State.FAILED
    r._queue_status_write(Mission, m.name, m.status, r._mission_writer_id())
    r.shutdown()
    await asyncio.gather(*list(r._status_flush_tasks))
    assert done == [State.FAILED]


@pytest.mark.unit
async def test_robot_delete_path_lands_the_terminal_failed_write():
    from packages.controllers.mission.server import RobotServer
    r, db = _make_robot()
    m = _mission()
    m.status.state = State.RUNNING
    r._current_mission = m
    r._missions[m.name] = m
    r._current_behavior_tree = None
    landed = []

    async def update_status(cls, name, status, writer):
        await asyncio.sleep(0.02)
        landed.append((name, status.state))

    db.update_status.side_effect = update_status
    robots = {"r1": r}

    async def delete_robot(name):
        robots.pop(name, None)
        r.shutdown()

    r._robot_server.delete_robot = delete_robot
    await r._delete_robot_object()
    await asyncio.gather(*list(r._status_flush_tasks))
    assert landed and landed[-1] == (m.name, State.FAILED)
