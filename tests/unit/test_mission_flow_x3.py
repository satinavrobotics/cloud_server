"""Round-3 audit X3: notify completion (R4), a timeout of a deleted mission (R5), the
robot's reported order read before it is acted on (R7), and the one cancel helper."""
import asyncio
import datetime
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest

import cloud_common.objects as api_objects
import cloud_common.objects.mission as mission_object
import packages.controllers.mission.server as server_module
import packages.controllers.mission.vda5050_types as types
from packages.controllers.mission.server import CancelPurpose, NotifyDone
from tests.unit.test_mission_lifecycle_fixes import (
    _cancels, _make_robot, _mission, _orders, _route, _start, _state, _tree)

State = mission_object.MissionStateV1


def _notify_leaf(name="n"):
    return {"name": name, "parent": "root_sequence",
            "notify": {"url": "http://hook.invalid/x", "json_data": {}, "timeout": 30}}


@pytest.fixture(autouse=True)
def _ok_notify(monkeypatch):
    monkeypatch.setattr(server_module.requests, "post",
                        lambda **kw: MagicMock(status_code=200))


def _executing(order_id):
    node = types.VDA5050NodeState(nodeId=f"{order_id}-s2", sequenceId=2)
    s = _state(order_id)
    s.nodeStates = [node]
    return s


async def _until(cond, timeout=2.0):
    end = asyncio.get_event_loop().time() + timeout
    while not cond() and asyncio.get_event_loop().time() < end:
        await asyncio.sleep(0.01)


# R4 ------------------------------------------------------------------------------------
@pytest.mark.unit
async def test_R4_next_order_goes_out_right_after_the_notify():
    r, _ = _make_robot()
    await _start(r, _mission(tree=_tree(_notify_leaf(), _route("a"))))
    assert not _orders(r)
    await _until(lambda: _orders(r))     # no state message needed
    assert len(_orders(r)) == 1


@pytest.mark.unit
async def test_R4_a_mission_ending_on_a_notify_completes_at_once():
    r, _ = _make_robot()
    r.post_mission_completion = AsyncMock()
    m = await _start(r, _mission(tree=_tree(_notify_leaf())))
    await _until(lambda: r.post_mission_completion.await_count)
    assert m.status.state == State.COMPLETED
    r.post_mission_completion.assert_awaited_once()


@pytest.mark.unit
async def test_R4_a_stale_notify_done_is_dropped():
    r, _ = _make_robot()
    await _start(r, _mission(tree=_tree(_notify_leaf(), _route("a"))))
    await _until(lambda: _orders(r))
    n = len(_orders(r))
    await r._on_notify_done(NotifyDone(key=("m1", "n", "gone", 0, 0)))
    assert len(_orders(r)) == n


@pytest.mark.unit
async def test_R4_no_mismatch_is_counted_during_a_long_notify(monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(server_module.requests, "post",
                        lambda **kw: release.wait(5) or MagicMock(status_code=200))
    r, _ = _make_robot()
    m = await _start(r, _mission(tree=_tree(_notify_leaf(), _route("a"))))
    r._order_sent_at = -1e9     # the give-up grace has long passed
    for _ in range(r.MAX_ORDER_MISMATCHES + 3):
        await r._on_client_message(_state("previous-mission-n0"))
    assert r._order_mismatch_count == 0
    assert m.status.state != State.FAILED
    release.set()
    await r._notify_task


# R5 ------------------------------------------------------------------------------------
@pytest.mark.unit
async def test_R5_timeout_of_a_deleted_mission_moves_the_queue_on():
    r, _ = _make_robot()
    a = await _start(r, _mission("a"))
    b = _mission("b")
    r._missions["b"] = b
    a.status.state = State.RUNNING
    a.lifecycle = api_objects.object.ObjectLifecycleV1.PENDING_DELETE
    r._robot_server.delete_pending_mission = AsyncMock(
        side_effect=lambda m: m.lifecycle == api_objects.object.ObjectLifecycleV1.PENDING_DELETE)
    r._robot_order_id = f"{r._order_prefix()}-n1"
    r._robot_executing = True
    r._send_instant_action = AsyncMock()

    await r._fail_mission_on_timeout("a")

    assert a.status.state == State.CANCELED
    r._send_instant_action.assert_awaited_once()          # the robot drops the order
    assert r._current_mission is b                         # the queue moved on
    r._set_robot_idle_after_mission.assert_called()


# R7 ------------------------------------------------------------------------------------
@pytest.mark.unit
async def test_R7_a_resumed_cancelled_mission_is_cancelled_on_the_robot_this_state_reports():
    r, _ = _make_robot()
    m = _mission()
    m.status.run_id = "abcd1234"
    m.status.start_timestamp = datetime.datetime.now()
    m.status.state = State.RUNNING
    m.needs_canceled = True
    r._missions["m1"] = m
    r._current_mission = m
    r._robot_order_id = ""          # the previous state: idle
    r._robot_executing = False
    r._start_retry_at = 0.0
    r._send_instant_action = AsyncMock()

    await r._on_client_message(_executing(f"{r._order_prefix()}-n1"))

    assert m.status.state != State.CANCELED     # not ended while its order runs
    assert r._send_instant_action.await_count >= 1
    assert r._send_instant_action.await_args_list[0].args[0].actionType == \
        types.VDA5050InstantActionType.CANCEL_ORDER


# Cancel helper -------------------------------------------------------------------------
@pytest.mark.unit
async def test_cancel_helper_never_cancels_a_foreign_order():
    r, _ = _make_robot()
    await _start(r, _mission())
    r._robot_order_id = "robots-own-offline-order"
    r._robot_executing = True
    for ours in (server_module.OURS_RUN, server_module.OURS_SENT,
                 server_module.OURS_DISPATCHER):
        assert not await r._cancel_order(CancelPurpose.STOP, "t", "note", ours=ours)
    assert not _cancels(r)
    assert await r._cancel_order(CancelPurpose.CLEAR, "t", "note", ours=server_module.OURS_ANY)


@pytest.mark.unit
async def test_cancel_helper_one_cancel_at_a_time_and_one_id_format():
    r, _ = _make_robot()
    await _start(r, _mission())
    r._robot_order_id = f"{r._order_prefix()}-n1"
    r._robot_executing = True
    assert await r._cancel_order(CancelPurpose.STOP, "timeout", "note")
    assert not await r._cancel_order(CancelPurpose.STOP, "failed", "note")
    (action_id,) = list(r._current_instant_actions)
    assert action_id.startswith(f"{r._order_prefix()}-timeout-cancel-")


@pytest.mark.unit
async def test_resume_does_not_cancel_the_robots_own_order():
    r, _ = _make_robot()
    m = await _start(r, _mission())
    r._resume_pending = True
    r._unknown_content_order_id = None
    r._robot_order_id = "robots-own-offline-order"
    r._robot_executing = True
    s = _executing("robots-own-offline-order")
    assert await r._resume_from_state(s)
    assert not _cancels(r)
    assert r._pending_send is None or len(_orders(r)) >= 1


@pytest.mark.unit
async def test_stop_cancel_is_abandoned_sooner_than_a_mission_cancel():
    r, _ = _make_robot()
    await _start(r, _mission())
    r._robot_order_id = f"{r._order_prefix()}-n1"
    r._robot_executing = True
    await r._cancel_order(CancelPurpose.STOP, "failed", "note")
    (stop_id,) = list(r._current_instant_actions)
    r._instant_action_resends[stop_id] = r.STOP_CANCEL_MAX_RESENDS
    await r.handle_instant_action(_state(r._robot_order_id))
    assert stop_id not in r._current_instant_actions       # abandoned

    await r._cancel_order(CancelPurpose.MISSION, "mission", "note")
    (mission_id,) = list(r._current_instant_actions)
    r._instant_action_resends[mission_id] = r.STOP_CANCEL_MAX_RESENDS
    await r.handle_instant_action(_state(r._robot_order_id))
    assert mission_id in r._current_instant_actions        # still resent
