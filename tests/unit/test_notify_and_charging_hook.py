"""Notify nodes and the charging hook run their HTTP calls off the event loop, with capped
timeouts and back-off (audit H2)."""
import asyncio
import threading
import time
from unittest.mock import MagicMock

import pytest
import requests

import cloud_common.objects.mission as mission_object
import packages.controllers.mission.server as server_module
from tests.unit.test_mission_lifecycle_fixes import _make_robot, _mission, _start, _tree

State = mission_object.MissionStateV1


def _notify_tree(timeout=30):
    return _tree({"name": "n", "parent": "root_sequence",
                  "notify": {"url": "http://hook.invalid/x", "json_data": {}, "timeout": timeout}})


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch):
    monkeypatch.setattr(server_module, "NOTIFY_RETRY_BACKOFF_S", (0.05, 0.05, 0.05), raising=False)


async def _run(r):
    m = await _start(r, _mission(tree=_notify_tree()))
    if r._notify_task is not None:
        await r._notify_task
    return m


def _resp(code):
    return MagicMock(status_code=code)


async def test_hanging_notify_does_not_block_the_loop(monkeypatch):
    release = threading.Event()

    def hang(**kwargs):
        release.wait(5)
        return _resp(200)
    monkeypatch.setattr(server_module.requests, "post", hang)
    r, _ = _make_robot()
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.01)
    t = asyncio.ensure_future(ticker())
    job = asyncio.ensure_future(_run(r))
    await asyncio.sleep(0.3)
    assert ticks > 10, "event loop was blocked by the notify request"
    release.set()
    await job
    t.cancel()


async def test_connection_error_fails_node_instead_of_raising(monkeypatch):
    calls = []

    def boom(**kwargs):
        calls.append(time.monotonic())
        raise requests.exceptions.ConnectionError("refused")
    monkeypatch.setattr(server_module.requests, "post", boom)
    r, _ = _make_robot()
    m = await _run(r)
    assert len(calls) == 4
    assert all(b - a >= 0.045 for a, b in zip(calls, calls[1:]))
    assert m.status.node_status["n"].state == State.FAILED
    assert "ConnectionError" in m.status.node_status["n"].error_msg


async def test_retryable_status_then_success(monkeypatch):
    codes = iter([503, 200])
    monkeypatch.setattr(server_module.requests, "post", lambda **kw: _resp(next(codes)))
    r, _ = _make_robot()
    m = await _run(r)
    assert m.status.node_status["n"].state == State.COMPLETED


async def test_timeout_is_capped(monkeypatch):
    seen = []
    monkeypatch.setattr(server_module.requests, "post",
                        lambda **kw: seen.append(kw["timeout"]) or _resp(200))
    r, _ = _make_robot()
    await _start(r, _mission(tree=_notify_tree(timeout=100000)))
    await r._notify_task
    assert seen == [server_module.NOTIFY_MAX_TIMEOUT_S]


async def test_cancel_during_notify_writes_nothing(monkeypatch):
    monkeypatch.setattr(server_module.requests, "post", lambda **kw: _resp(503))
    monkeypatch.setattr(server_module, "NOTIFY_RETRY_BACKOFF_S", (0.2, 0.2, 0.2), raising=False)
    r, _ = _make_robot()
    job = asyncio.ensure_future(_run(r))
    await asyncio.sleep(0.1)
    r._current_mission.needs_canceled = True
    m = r._current_mission
    await job
    assert m.status.node_status["n"].state == State.RUNNING


async def test_charging_hook_is_one_request_per_window(monkeypatch):
    posts = []
    monkeypatch.setattr(server_module.requests, "get", lambda *a, **k: _resp(200))
    monkeypatch.setattr(server_module.requests, "post",
                        lambda *a, **k: posts.append(k) or _resp(500))
    r, _ = _make_robot()
    r._robot_server.mission_ctrl_url = "http://mc.invalid"
    for _ in range(5):
        if not r._charging_hook_busy and time.monotonic() >= r._charging_hook_next_at:
            r._charging_hook_busy = True
            r._charging_hook_next_at = time.monotonic() + server_module.CHARGING_HOOK_RETRY_S
            asyncio.ensure_future(r._post_charging_mission())
        await asyncio.sleep(0.05)
    assert len(posts) == 1
    assert posts[0]["timeout"] == server_module.CHARGING_HOOK_TIMEOUT_S
    assert not r._charging_mission_received


async def test_send_order_returns_while_notify_hangs_and_end_cancels_it(monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(server_module.requests, "post",
                        lambda **kw: release.wait(5) or _resp(200))
    r, _ = _make_robot()
    m = await asyncio.wait_for(_start(r, _mission(tree=_notify_tree())), 1.0)
    task = r._notify_task
    assert task is not None and not task.done()
    await asyncio.sleep(0.05)
    r._cancel_wait()  # what every path that ends the mission calls
    release.set()
    await asyncio.sleep(0.1)
    assert task.cancelled()
    assert m.status.node_status["n"].state == State.RUNNING
