"""H6: the cloud must not end a mission as done while the robot keeps driving its order.

- a: a started mission whose tree is not built (dispatcher restart, held robot) and that is
  cancelled gets a cancelOrder when the robot runs an order of its run; a never-dispatched
  one, or one the robot does not run, ends at once.
- b: a mission failed for an order mismatch or a FATAL error drops the robot's order of
  that run; a foreign (offline) order is left alone.
- c: a cancelOrder answered FAILED while the robot still lists the order's nodes is not a
  completed cancel.
"""
import datetime
import time
from types import SimpleNamespace

import pytest

import packages.controllers.mission.server as server_module
import packages.controllers.mission.vda5050_types as types
from tests.unit.test_mission_lifecycle_fixes import (
    State, _cancel_done, _make_robot, _mission, _order_id, _published, _start, _state)

CANCEL = types.VDA5050InstantActionType.CANCEL_ORDER


@pytest.fixture
def clock(monkeypatch):
    c = SimpleNamespace(t=1000.0)
    c.monotonic = lambda: c.t
    monkeypatch.setattr(server_module, "time", SimpleNamespace(
        monotonic=c.monotonic, time=time.time))
    return c


def _busy(order_id, errors=None):
    s = _state(order_id)
    s.nodeStates = [types.VDA5050NodeState(nodeId=f"{order_id}-s4", sequenceId=4)]
    s.errors = errors or []
    return s


def _started(name="m1"):
    """A mission as a restarted dispatcher reads it back: started, flagged for cancel."""
    m = _mission(name=name)
    m.status.state = State.RUNNING
    m.status.start_timestamp = datetime.datetime.now()
    m.status.run_id = "abcd1234"
    m.needs_canceled = True
    return m


def _our_order(name="m1"):
    return f"{name}-rabcd1234-n1"


def _cancels(r):
    """The distinct cancelOrder actions published (a resend repeats an action id)."""
    return sorted({a["actionId"] for m in _published(r, "/instantActions")
                   for a in m["instantActions"] if a["actionType"] == "cancelOrder"})


# --- a ----------------------------------------------------------------------------
@pytest.mark.unit
async def test_a_restart_resumed_cancel_sends_cancel_order_and_ends_on_confirmation():
    r, _ = _make_robot()
    m = _started()
    r._missions[m.name] = m
    await r._try_start_mission()
    # The robot has not reported yet: nothing is cancelled blind, nothing is ended.
    assert m.status.state == State.RUNNING and _cancels(r) == []

    await r._on_client_message(_busy(_our_order()))
    assert len(_cancels(r)) == 1
    assert m.status.state == State.RUNNING

    await r._on_client_message(_state(_our_order(), actions=[_cancel_done(r)]))
    assert m.status.state == State.CANCELED
    assert r._current_mission is None


@pytest.mark.unit
async def test_a_held_started_mission_cancel_waits_for_the_robot_to_return():
    r, _ = _make_robot(online=False)
    m = _started()
    m.needs_canceled = False
    r._missions[m.name] = m
    await r._try_start_mission()                     # held: offline
    assert r._current_behavior_tree is None
    r._robot_order_id, r._robot_executing = _our_order(), True   # last seen before it dropped

    flagged = _started()
    await r._on_mission_change(flagged)
    assert _cancels(r) == [] and m.status.state == State.RUNNING

    r._robot_object.status.online = True
    await r._on_client_message(_busy(_our_order()))
    assert len(_cancels(r)) == 1
    await r._on_client_message(_state(_our_order(), actions=[_cancel_done(r)]))
    assert m.status.state == State.CANCELED


@pytest.mark.unit
async def test_a_never_dispatched_held_mission_is_cancelled_at_once_without_cancel_order():
    r, _ = _make_robot(online=False)
    m = _mission()
    r._missions[m.name] = m
    await r._try_start_mission()
    assert r._current_behavior_tree is None and m.status.run_id is None

    flagged = _mission()
    flagged.needs_canceled = True
    await r._on_mission_change(flagged)

    assert m.status.state == State.CANCELED
    assert _cancels(r) == []


@pytest.mark.unit
async def test_a_started_mission_the_robot_does_not_run_is_cancelled_without_cancel_order():
    """The robot reports one of its own (offline) missions: not ours to cancel."""
    r, _ = _make_robot()
    m = _started()
    r._missions[m.name] = m
    await r._try_start_mission()
    await r._on_client_message(_busy("offline-mission-7"))
    assert m.status.state == State.CANCELED
    assert _cancels(r) == []


# --- b ----------------------------------------------------------------------------
async def _mismatch_until_failed(r, m, order_id):
    # The give-up also needs ORDER_GIVE_UP_MIN_S since the last (re)send: each state
    # arrives as if that long after it (the resends then run out first, as in production).
    for _ in range(r.MAX_ORDER_MISMATCHES + r.ORDER_MAX_RESENDS + 1):
        if m.status.state.done:
            break
        r._order_sent_at = time.monotonic() - r.ORDER_GIVE_UP_MIN_S - 1
        await r._on_client_message(_busy(order_id))


@pytest.mark.unit
async def test_b_mismatch_failure_while_robot_runs_our_stale_order_sends_cancel():
    r, _ = _make_robot()
    m = await _start(r, _mission())
    stale = _order_id(r)
    m.status.order_rev += 1                          # the robot holds an older revision
    await _mismatch_until_failed(r, m, stale)
    assert m.status.state == State.FAILED
    assert len(_cancels(r)) == 1
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_b_mismatch_failure_on_a_foreign_order_sends_no_cancel():
    r, _ = _make_robot()
    m = await _start(r, _mission())
    await _mismatch_until_failed(r, m, "offline-mission-7")
    assert m.status.state == State.FAILED
    assert _cancels(r) == []
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_b_fatal_error_failure_sends_cancel_while_robot_runs_our_order():
    r, _ = _make_robot()
    m = await _start(r, _mission())
    order = _order_id(r)
    fatal = types.VDA5050Error(errorType="motorFault", errorDescription="boom",
                               errorLevel=types.VDA5050ErrorLevel.FATAL)
    await r._on_client_message(_busy(order, errors=[fatal]))
    assert m.status.state == State.FAILED
    assert len(_cancels(r)) == 1
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_b_no_second_cancel_when_one_is_outstanding():
    r, _ = _make_robot()
    m = await _start(r, _mission())
    await r._send_cancel_order("earlier-cancel")
    order = _order_id(r)
    fatal = types.VDA5050Error(errorType="motorFault", errorDescription="boom",
                               errorLevel=types.VDA5050ErrorLevel.FATAL)
    await r._on_client_message(_busy(order, errors=[fatal]))
    assert len(_cancels(r)) == 1
    r._cancel_mission_timeout()


# --- c ----------------------------------------------------------------------------
def _failed(r):
    (action,) = [a for a in r._current_instant_actions.values() if a.actionType == CANCEL]
    return types.VDA5050ActionState(
        actionId=action.actionId, actionType=CANCEL,
        actionStatus=types.VDA5050ActionStatus.FAILED)


async def _cancelling(r):
    m = await _start(r, _mission())
    m.needs_canceled = True
    await r._on_mission_change(m.copy(deep=True))
    assert len(_cancels(r)) == 1
    return m


@pytest.mark.unit
async def test_c_failed_cancel_while_robot_still_reports_our_nodes_is_not_cancelled(clock):
    r, _ = _make_robot()
    m = await _cancelling(r)
    order = _order_id(r)
    await r._on_client_message(_busy(order))
    s = _busy(order)
    s.actionStates = [_failed(r)]
    await r._on_client_message(s)
    assert m.status.state == State.RUNNING
    assert r._has_outstanding_cancel()
    before = len(_published(r, "/instantActions"))
    clock.t += r.INSTANT_ACTION_RESEND_MAX_S
    s = _busy(order)
    s.actionStates = [_failed(r)]
    await r._on_client_message(s)
    assert len(_published(r, "/instantActions")) > before   # resent within the budget
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_c_failed_cancel_with_no_nodes_still_counts_as_cancelled():
    r, _ = _make_robot()
    m = await _cancelling(r)
    await r._on_client_message(_state(_order_id(r), actions=[_failed(r)]))
    assert m.status.state == State.CANCELED


@pytest.mark.unit
async def test_c_failed_cancel_saying_no_order_to_cancel_counts_as_cancelled():
    r, _ = _make_robot()
    m = await _cancelling(r)
    failed = _failed(r)
    failed.resultDescription = "noOrderToCancel"
    s = _busy(_order_id(r))
    s.actionStates = [failed]
    await r._on_client_message(s)
    assert m.status.state == State.CANCELED


@pytest.mark.unit
async def test_c_budget_exhausted_reports_why_the_mission_was_cancelled(clock):
    r, _ = _make_robot()
    m = await _cancelling(r)
    order = _order_id(r)
    for _ in range(r.MAX_INSTANT_ACTION_RESENDS + 5):
        s = _busy(order)
        if r._current_instant_actions:
            s.actionStates = [_failed(r)]
        await r._on_client_message(s)
        clock.t += r.INSTANT_ACTION_RESEND_MAX_S
        if m.status.state.done:
            break
    assert m.status.state == State.CANCELED
    assert "never confirmed the cancelOrder" in m.status.failure_reason
    assert order in m.status.failure_reason
