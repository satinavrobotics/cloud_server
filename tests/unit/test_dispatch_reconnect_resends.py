"""Resends after a robot reconnect, instant-action back-off, the heartbeat watchdog.

The robots use a clean MQTT session: what we publish while one is offline is lost, so a
robot that comes back still reporting the old order is not ignoring us.

- RECONNECT: the order's resend budget and the mismatch counter start over on an
  offline -> online transition (and only then); a cancelOrder sent meanwhile is resent
  on the first state after it.
- BACK-OFF: an unacknowledged instant action is resent with back-off, not on every state
  message, and abandoned after MAX_INSTANT_ACTION_RESENDS.
- WATCHDOG: the heartbeat timer marks the robot offline and pauses the mission timeout.
- BLOCKED NODE: the exclusion write is written once and its task is referenced.
"""
import asyncio
import datetime
import time as _time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import packages.controllers.mission.server as server_module
import packages.controllers.mission.vda5050_types as types
from packages.utils import blocked_nodes
from tests.unit.test_mission_lifecycle_fixes import (
    _make_robot, _mission, _order_id, _orders, _published, _rerouted, _start, _state)
from tests.unit.test_offline_missions import _Conn, _edge_blocked, _start_on_map


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(server_module, "time",
                        SimpleNamespace(monotonic=c.monotonic, time=_time.time))
    return c


def _instant_actions(r):
    return _published(r, "/instantActions")


def _cancel_action(a_id="a1"):
    return types.VDA5050Action(
        actionType=types.VDA5050InstantActionType.CANCEL_ORDER, actionId=a_id)


# ---------------------------------------------------------------------------
# RECONNECT
# ---------------------------------------------------------------------------
@pytest.mark.unit
async def test_order_is_resent_on_the_first_state_after_a_reconnect(clock):
    r, _ = _make_robot()
    await _start(r, _mission())
    for _ in range(r.ORDER_MAX_RESENDS + 3):
        clock.t += r.ORDER_RESEND_MAX_S
        await r._on_client_message(_state("elsewhere-n0"))
    sent = len(_orders(r))
    assert sent == 1 + r.ORDER_MAX_RESENDS                # budget used up

    r._robot_object.status.online = False                 # went offline, came back
    await r._on_client_message(_state("elsewhere-n0"))    # same instant: no back-off wait
    assert len(_orders(r)) == sent + 1
    assert r._order_resends == 1
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_no_reset_without_an_offline_to_online_transition(clock):
    r, _ = _make_robot()
    await _start(r, _mission())
    for _ in range(r.ORDER_MAX_RESENDS + 3):
        clock.t += r.ORDER_RESEND_MAX_S
        await r._on_client_message(_state("elsewhere-n0"))
    sent = len(_orders(r))
    await r._on_client_message(_state("elsewhere-n0"))    # online all along
    assert len(_orders(r)) == sent and r._order_resends == r.ORDER_MAX_RESENDS
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_mismatch_counter_starts_over_on_a_reconnect(clock):
    r, _ = _make_robot()
    await _start(r, _mission())
    await r._on_client_message(_state("elsewhere-n0"))
    await r._on_client_message(_state("elsewhere-n0"))
    assert r._order_mismatch_count == 2
    r._order_mismatch_count = r.MAX_ORDER_MISMATCHES - 2
    r._robot_object.status.online = False
    await r._on_client_message(_state("elsewhere-n0"))
    assert r._order_mismatch_count == 1                   # reset, then this message counted
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_cancel_sent_while_offline_is_resent_on_the_first_state_after(clock):
    r, _ = _make_robot()
    await _start(r, _mission())
    await r._on_client_message(_state(_order_id(r)))      # adopted
    await r._on_mission_change(_rerouted())               # cancelOrder goes out
    assert len(_instant_actions(r)) == 1
    await r._on_client_message(_state(_order_id(r)))      # unacknowledged: resend 1
    assert len(_instant_actions(r)) == 2
    await r._on_client_message(_state(_order_id(r)))      # same instant: backed off
    assert len(_instant_actions(r)) == 2

    r._robot_object.status.online = False                 # the cancel was lost with the session
    await r._on_client_message(_state(_order_id(r)))
    assert len(_instant_actions(r)) == 3
    r._cancel_mission_timeout()


# ---------------------------------------------------------------------------
# BACK-OFF
# ---------------------------------------------------------------------------
@pytest.mark.unit
async def test_instant_action_resends_back_off(clock):
    r, _ = _make_robot()
    r._send_instant_action = AsyncMock()
    r._current_instant_actions["a1"] = _cancel_action()

    for _ in range(50):                                   # a fast stream, no time passing
        await r.handle_instant_action(_state("o"))
    assert r._send_instant_action.await_count == 1

    clock.t += r.INSTANT_ACTION_RESEND_BASE_S - 0.1
    await r.handle_instant_action(_state("o"))
    assert r._send_instant_action.await_count == 1
    clock.t += 0.1
    await r.handle_instant_action(_state("o"))
    assert r._send_instant_action.await_count == 2

    clock.t += r.INSTANT_ACTION_RESEND_BASE_S             # the interval doubled
    await r.handle_instant_action(_state("o"))
    assert r._send_instant_action.await_count == 2
    clock.t += r.INSTANT_ACTION_RESEND_BASE_S
    await r.handle_instant_action(_state("o"))
    assert r._send_instant_action.await_count == 3


@pytest.mark.unit
async def test_instant_action_is_abandoned_after_max_resends(clock):
    r, _ = _make_robot()
    r._send_instant_action = AsyncMock()
    r._cancel_resolved = MagicMock()
    action = _cancel_action()
    r._current_instant_actions["a1"] = action

    for _ in range(r.MAX_INSTANT_ACTION_RESENDS + 3):
        await r.handle_instant_action(_state("o"))
        clock.t += r.INSTANT_ACTION_RESEND_MAX_S

    assert r._send_instant_action.await_count == r.MAX_INSTANT_ACTION_RESENDS
    assert "a1" not in r._current_instant_actions
    assert "a1" not in r._instant_action_resends and "a1" not in r._instant_action_resent_at
    r._cancel_resolved.assert_called_once_with(action, abandoned=True)


# ---------------------------------------------------------------------------
# WATCHDOG
# ---------------------------------------------------------------------------
@pytest.mark.unit
async def test_heartbeat_timeout_marks_the_robot_offline_and_pauses_the_timeout():
    r, db = _make_robot()
    m = _mission()
    m.timeout = datetime.timedelta(seconds=100)
    await _start(r, m)
    r._robot_object.heartbeat_timeout = datetime.timedelta(seconds=0.05)

    await r._on_client_message(_state(_order_id(r)))
    first = r._robot_online_task
    await r._on_client_message(_state(_order_id(r)))      # re-armed, not stacked
    assert r._robot_online_task is not first and first.cancelled()
    assert r._robot_object.status.online and r._timeout_paused is None

    await asyncio.sleep(0.2)
    assert r._robot_object.status.online is False
    assert r._timeout_paused is not None
    r.shutdown()


@pytest.mark.unit
async def test_shutdown_cancels_the_heartbeat_timer():
    r, db = _make_robot()
    r._robot_object.heartbeat_timeout = datetime.timedelta(seconds=0.05)
    await r._on_client_message(_state(""))
    handle = r._robot_online_task
    r.shutdown()
    assert handle.cancelled() and r._robot_online_task is None
    await asyncio.sleep(0.15)
    assert r._robot_object.status.online is True          # never marked offline


# ---------------------------------------------------------------------------
# BLOCKED NODE
# ---------------------------------------------------------------------------
@pytest.mark.unit
async def test_blocked_node_write_is_referenced_and_written_once():
    r, db, m = await _start_on_map()
    writes = []
    db.connection = MagicMock(side_effect=lambda: _Conn(writes))
    order = _order_id(r)
    s = _state(order)
    s.errors = [_edge_blocked(f"{order}-s4")]
    await r._on_client_message(s)
    assert len(r._blocked_node_tasks) == 1                # held until it is done
    for _ in range(5):
        await r._on_client_message(s.copy(deep=True))     # the robot re-emits it
    for _ in range(3):
        await asyncio.sleep(0)

    assert len([p for sql, p in writes if sql == blocked_nodes.UPSERT_SQL]) == 1
    assert not r._blocked_node_tasks                      # discarded when done
    r._cancel_mission_timeout()
