"""Round-3 X2: status-write ordering (R2), poison writes, the offline marker, and the run
header / run continuity DB calls staying off the state loop (R8)."""
import asyncio
from unittest.mock import MagicMock

import psycopg
import pytest

import cloud_common.objects as api_objects
import packages.controllers.mission.server as sm
from tests.unit.test_mission_lifecycle_fixes import State, _make_robot, _mission

Mission = api_objects.MissionObjectV1


@pytest.mark.unit
async def test_stale_queued_write_does_not_overwrite_awaited_newer_one():
    r, db = _make_robot()
    m = _mission()
    committed = []

    async def update_status(cls, name, status, writer):
        snap = (status.state, status.passes_completed)
        await asyncio.sleep(0.01)
        committed.append(snap)
    db.update_status.side_effect = update_status
    m.status.state = State.COMPLETED
    r._queue_status_write(Mission, m.name, m.status, r._mission_writer_id())
    new = m.status.copy(deep=True)
    new.state = State.PENDING
    new.passes_completed = 1
    await r._write_status(Mission, m.name, new, r._mission_writer_id())
    m.status = new
    await asyncio.gather(*list(r._status_write_tasks))
    assert committed[-1] == (State.PENDING, 1)


@pytest.mark.unit
async def test_queued_write_requested_after_awaited_one_still_lands():
    r, db = _make_robot()
    m = _mission()
    committed = []

    async def update_status(cls, name, status, writer):
        await asyncio.sleep(0.01)
        committed.append(status.passes_completed)
    db.update_status.side_effect = update_status
    first = m.status.copy(deep=True)
    first.passes_completed = 1
    task = asyncio.ensure_future(r._write_status(Mission, m.name, first, r._mission_writer_id()))
    await asyncio.sleep(0)
    newer = m.status.copy(deep=True)
    newer.passes_completed = 2
    r._queue_status_write(Mission, m.name, newer, r._mission_writer_id())
    await task
    await asyncio.gather(*list(r._status_write_tasks))
    assert committed == [1, 2]


@pytest.mark.unit
async def test_poison_write_dropped_after_one_attempt_db_error_retried(monkeypatch):
    monkeypatch.setattr(sm, "STATUS_WRITE_RETRY_MIN_S", 0.001)
    monkeypatch.setattr(sm, "STATUS_WRITE_RETRY_MAX_S", 0.002)
    r, db = _make_robot()
    m = _mission()
    calls = {"poison": 0, "db": 0}

    async def poison(cls, name, status, writer):
        calls["poison"] += 1
        raise TypeError("not serializable")
    db.update_status.side_effect = poison
    r._queue_status_write(Mission, m.name, m.status, r._mission_writer_id())
    await asyncio.wait_for(asyncio.gather(*list(r._status_write_tasks)), 2)
    assert calls["poison"] == 1
    assert not r._status_rows

    async def flaky(cls, name, status, writer):
        calls["db"] += 1
        if calls["db"] < 3:
            raise psycopg.OperationalError("down")
    db.update_status.side_effect = flaky
    r._queue_status_write(Mission, m.name, m.status, r._mission_writer_id())
    await asyncio.wait_for(asyncio.gather(*list(r._status_write_tasks)), 2)
    assert calls["db"] == 3


@pytest.mark.unit
@pytest.mark.parametrize("was_online", [True, False])
async def test_offline_write_failure_is_not_an_unobserved_exception(monkeypatch, was_online):
    monkeypatch.setattr(sm, "STATUS_WRITE_RETRY_MIN_S", 0.001)
    r, db = _make_robot(online=was_online)
    db.update_status.side_effect = [TypeError("boom")]
    await r._check_robot_online()            # must not raise
    await asyncio.wait_for(asyncio.gather(*list(r._status_write_tasks)), 2)
    assert r._robot_object.status.online is False
    assert db.update_status.await_count == 1


class _HangingDb:
    def connection(self):
        return self

    async def __aenter__(self):
        await asyncio.sleep(3600)

    async def __aexit__(self, *a):
        return False


@pytest.mark.unit
async def test_hanging_run_header_persist_does_not_block_the_caller():
    r, _ = _make_robot()
    r._database = _HangingDb()
    r._run_epoch = __import__("uuid").uuid4()
    r._run_header_saved_at = None
    await asyncio.wait_for(r._persist_run_header(5), 1)
    task = r._run_header_task
    assert task is not None and not task.done()
    r._run_header_saved_at = None
    await asyncio.wait_for(r._persist_run_header(6), 1)
    assert r._run_header_task is task          # single-flight
    task.cancel()


@pytest.mark.unit
async def test_hanging_run_continuity_check_times_out_and_retries_later(monkeypatch):
    monkeypatch.setattr(sm, "RUN_CHECK_TIMEOUT_S", 0.05)
    r, _ = _make_robot()
    r._database = _HangingDb()
    await asyncio.wait_for(r._check_run_continuity(5), 1)
    assert r._run_checked is False
    assert r._run_check_after > 0
