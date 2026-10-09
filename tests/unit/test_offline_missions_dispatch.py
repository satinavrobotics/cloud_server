"""Regression tests for the dispatcher review of the offline-missions change (2026-10-08).

- BLOCK: a "canceled" without the edgeBlocked error keeps the block (no resend).
- TIMEOUT: a paused timeout resumes on any state; an operator cancel keeps its backstop.
- REROUTE: node reports on a replaced route are not taken, and those taken are dropped.
- LEFTOVER: a "canceled" with nodeStates is a real drop once the grace has passed.
- DWELL: the reroute cancel dwell counts from the first send; the held flag is reset.
- REPORTS: skipped nodes are capped, repeats cost nothing, offsets must be finite.
- POLICY: the legacy 0.1 tolerance reads as unset; an unknown nodePolicy mode warns.
"""
import asyncio
import datetime
import logging
import time as _time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import packages.controllers.mission.server as server_module
import packages.controllers.mission.vda5050_types as types
from packages.controllers.mission import order_policy
from packages.utils import blocked_nodes
from tests.unit.test_mission_lifecycle_fixes import (
    State, _cancel_done, _make_robot, _mission, _order_id, _orders, _rerouted, _route,
    _start, _state, _tree)
from tests.unit.test_offline_missions import (
    _Conn, _edge_blocked, _info, _map_route, _note, _robot_obj, _route_of, _skipped,
    _start_on_map)


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


def _cancel_actions(r):
    return [a for a in r._current_instant_actions.values()
            if a.actionType == types.VDA5050InstantActionType.CANCEL_ORDER]


async def _block(r, order):
    s = _state(order)
    s.errors = [_edge_blocked(f"{order}-s4")]
    await r._on_client_message(s)


# ---------------------------------------------------------------------------
# BLOCK
# ---------------------------------------------------------------------------
@pytest.mark.unit
async def test_canceled_messages_without_the_error_keep_the_block_and_send_nothing():
    r, _ = _make_robot()
    m = await _start(r, _mission(tree=_tree(_route("a", n=3))))
    order = _order_id(r)
    await _block(r, order)
    for _ in range(3):
        await r._on_client_message(_info(_state(order), "canceled"))

    assert m.status.blocked and r._mission_timeout_task is None
    assert len(_orders(r)) == 1 and m.status.order_rev == 0 and r._pending_send is None
    assert r._robot_server.fleet_recorder.rerouted.call_count == 0


@pytest.mark.unit
async def test_after_a_restart_a_canceled_while_blocked_keeps_the_block():
    r, _ = _make_robot()
    m = await _start(r, _mission(tree=_tree(_route("a", n=3))))
    order = _order_id(r)
    await _block(r, order)
    r._blocked_order_id = None          # not known to a restarted dispatcher
    r._sent_order = None
    await r._on_client_message(_info(_state(order), "canceled"))
    await r._on_client_message(_info(_state(order), "canceled"))
    assert m.status.blocked and len(_orders(r)) == 1


async def _reroute_while_blocked(r):
    order = _order_id(r)
    await _block(r, order)
    await r._on_mission_change(_rerouted(n=3))
    await r._on_client_message(_state(order, actions=[_cancel_done(r)]))
    assert len(_orders(r)) == 2
    return _orders(r)[1]["orderId"]


@pytest.mark.unit
async def test_a_reroutes_order_blocked_on_the_same_node_keeps_the_block():
    r, _ = _make_robot()
    m = await _start(r, _mission(tree=_tree(_route("a", n=3))))
    new_order = await _reroute_while_blocked(r)
    await _block(r, new_order)
    for _ in range(2):
        await r._on_client_message(_info(_state(new_order), "canceled"))
    assert m.status.blocked and len(_orders(r)) == 2
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_the_reroutes_own_order_canceled_without_the_error_ends_the_block():
    r, _ = _make_robot()
    m = await _start(r, _mission(tree=_tree(_route("a", n=3))))
    new_order = await _reroute_while_blocked(r)
    assert m.status.blocked             # only the robot's report on the new order clears it
    await r._on_client_message(_info(_state(new_order), "canceled"))
    assert not m.status.blocked

    r._cancel_mission_timeout()


# ---------------------------------------------------------------------------
# TIMEOUT
# ---------------------------------------------------------------------------
@pytest.mark.unit
async def test_a_paused_timeout_resumes_even_if_the_robot_still_reads_online():
    r, _ = _make_robot()
    m = _mission()
    m.timeout = datetime.timedelta(seconds=100)
    await _start(r, m)
    r._pause_mission_timeout()
    assert r._timeout_paused is not None
    r._robot_object.status.online = True     # a stale watcher row set it again
    await r._on_client_message(_state(_order_id(r)))
    assert r._timeout_paused is None and r._mission_timeout_task is not None
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_an_operator_cancel_while_the_robot_is_offline_still_ends_the_mission():
    r, _ = _make_robot()
    m = _mission()
    m.timeout = datetime.timedelta(seconds=0.15)
    await _start(r, m)
    r._pause_mission_timeout()
    r._robot_object.status.online = False
    cancelled = _mission()
    cancelled.needs_canceled = True
    await r._on_mission_change(cancelled)
    assert r._mission_timeout_task is not None and r._timeout_paused is None
    await asyncio.sleep(0.3)
    assert m.status.state == State.CANCELED


@pytest.mark.unit
async def test_a_mission_being_cancelled_does_not_pause_its_timeout():
    r, _ = _make_robot()
    m = await _start(r, _mission())
    m.needs_canceled = True
    task = r._mission_timeout_task
    r._pause_mission_timeout()
    assert r._mission_timeout_task is task and r._timeout_paused is None
    r._cancel_mission_timeout()


# ---------------------------------------------------------------------------
# REROUTE: reports on a replaced route
# ---------------------------------------------------------------------------
def _map_rerouted(points=((20.0, 1.0), (20.0, 2.0), (20.0, 3.0))):
    m = _mission(tree=_tree(_map_route(points=points)))
    m.route_rev = 1
    return m


@pytest.mark.unit
async def test_reports_on_a_route_a_reroute_replaced_are_not_taken():
    r, db, m = await _start_on_map()
    writes = []
    db.connection = MagicMock(side_effect=lambda: _Conn(writes))
    order = _order_id(r)
    await r._on_client_message(_state(order))           # adopted: the cancel goes now
    await r._on_mission_change(_map_rerouted())         # edit applied, order not yet new
    s = _state(order)
    s.errors = [_skipped(f"{order}-s4"), _edge_blocked(f"{order}-s4")]
    s.information = [_note(f"{order}-s2", offsetX=0.1, offsetY=0.0)]
    await r._on_client_message(s)
    await asyncio.sleep(0)

    assert m.status.skipped_nodes == [] and m.status.node_notes == []
    assert not [p for sql, p in writes if sql == blocked_nodes.UPSERT_SQL]
    assert m.status.blocked_waypoint_index is None
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_a_reroute_drops_the_reports_on_the_nodes_old_route():
    r, _, m = await _start_on_map()
    order = _order_id(r)
    s = _state(order)
    s.errors = [_skipped(f"{order}-s4")]
    s.information = [_note(f"{order}-s2", offsetX=0.1, offsetY=0.0)]
    await r._on_client_message(s)
    assert len(m.status.skipped_nodes) == 1 and len(m.status.node_notes) == 1
    assert m.status.offset_summary is not None

    await r._on_mission_change(_map_rerouted())
    assert m.status.skipped_nodes == [] and m.status.node_notes == []
    assert m.status.offset_summary is None
    r._cancel_mission_timeout()


# ---------------------------------------------------------------------------
# LEFTOVER "canceled"
# ---------------------------------------------------------------------------
def _canceled_while_listing_nodes(order, driving=False):
    s = _info(_state(order), "canceled")
    s.nodeStates = [types.VDA5050NodeState(nodeId=f"{order}-s4", sequenceId=4)]
    s.driving = driving
    return s


@pytest.mark.unit
async def test_a_canceled_with_nodes_listed_long_after_the_send_is_a_drop(clock):
    r, _ = _make_robot()
    m = await _start(r, _mission())
    order = _order_id(r)
    clock.t += r.CANCELED_LEFTOVER_GRACE_S
    await r._on_client_message(_canceled_while_listing_nodes(order, driving=True))
    assert len(_orders(r)) == 1                         # driving: still executing
    await r._on_client_message(_canceled_while_listing_nodes(order))
    assert len(_orders(r)) == 2 and m.status.order_rev == 1
    r._cancel_mission_timeout()


# ---------------------------------------------------------------------------
# DWELL
# ---------------------------------------------------------------------------
@pytest.mark.unit
async def test_the_cancel_dwell_counts_from_the_first_send_not_the_resends(clock):
    r, _ = _make_robot()
    await _start(r, _mission())
    await r._on_mission_change(_rerouted())
    assert r._deferred_replace_cancel and not _cancel_actions(r)
    clock.t += 1.5
    await r._on_client_message(_state("elsewhere-n0"))   # resent (back-off 1 s)
    assert len(_orders(r)) == 2 and not _cancel_actions(r)
    clock.t += 0.6                                       # 2.1 s after the first send
    await r._on_client_message(_state("elsewhere-n0"))
    assert len(_cancel_actions(r)) == 1
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_a_held_reroute_cancel_does_not_outlive_its_mission_or_pass():
    r, _ = _make_robot()
    m = await _start(r, _mission())
    r._deferred_replace_cancel = True
    r._set_mission_state(State.COMPLETED)
    assert not r._deferred_replace_cancel

    r, _ = _make_robot()
    m = await _start(r, _mission())
    m.repeat = 2
    r._deferred_replace_cancel = True
    assert await r._start_next_pass()
    assert not r._deferred_replace_cancel
    r._cancel_mission_timeout()


# ---------------------------------------------------------------------------
# REPORTS
# ---------------------------------------------------------------------------
async def _writes_of(r, db, state, times=3):
    """Mission status writes `times` copies of `state` make (a plain state makes some too).
    The robot row is left out: it is written when its discrete fields (errors...) change."""
    def mission_writes():
        return sum(1 for c in db.update_status.call_args_list
                   if c.args[0] is not server_module.api_objects.RobotObjectV1)
    before = mission_writes()
    for _ in range(times):
        await r._on_client_message(state.copy(deep=True))
    await asyncio.sleep(0)
    return mission_writes() - before


@pytest.mark.unit
async def test_skipped_nodes_are_capped_and_repeats_write_nothing(monkeypatch):

    monkeypatch.setattr(server_module, "MISSION_SKIPPED_NODES_MAX", 2)
    points = tuple((10.0, float(i)) for i in range(4))
    r, db, m = await _start_on_map(tree=_tree(_map_route(points=points)))
    order = _order_id(r)
    s = _state(order)
    s.errors = [_skipped(f"{order}-s{seq}") for seq in (2, 4, 6)]
    await r._on_client_message(s)
    await asyncio.sleep(0)
    assert [n.node_id for n in m.status.skipped_nodes] == [f"{order}-s4", f"{order}-s6"]

    resolves = []
    original = r._resolve_node_ref
    r._resolve_node_ref = lambda ref: resolves.append(ref) or original(ref)
    assert await _writes_of(r, db, s) == await _writes_of(r, db, _state(order))
    assert len(m.status.skipped_nodes) == 2 and resolves == []
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_a_note_the_cap_dropped_is_not_taken_again(monkeypatch):
    monkeypatch.setattr(server_module, "MISSION_NODE_NOTES_MAX", 1)
    r, db, m = await _start_on_map()
    order = _order_id(r)
    s = _state(order)
    s.information = [_note(f"{order}-s2", info_type="a"), _note(f"{order}-s4", info_type="b")]
    await r._on_client_message(s)
    await asyncio.sleep(0)
    recorder = r._robot_server.fleet_recorder
    assert [n.info_type for n in m.status.node_notes] == ["b"]
    notes = recorder.node_note.call_count
    assert await _writes_of(r, db, s) == await _writes_of(r, db, _state(order))
    assert recorder.node_note.call_count == notes
    assert m.status.node_notes[0].last_seen >= m.status.node_notes[0].first_seen
    r._cancel_mission_timeout()


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["nan", "inf", "-inf"])
def test_an_offset_that_is_not_a_finite_number_is_not_taken(bad):
    robot = server_module.Robot
    assert robot._note_offset({"offsetX": bad, "offsetY": "0"}) is None
    assert robot._note_offset({"offsetX": "0.1", "offsetY": "0", "offsetTheta": bad}) is None
    assert robot._note_offset({"offsetX": "0.1", "offsetY": "0"}) == \
        {"dx": 0.1, "dy": 0.0, "dtheta": 0.0}


# ---------------------------------------------------------------------------
# POLICY
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_the_old_default_tolerance_of_stored_routes_reads_as_unset():
    route = _route_of({"x": 1, "allowedDeviationXY": 0.1}, {"x": 2, "allowedDeviationXY": 0.1},
                      {"x": 3, "allowedDeviationXY": 0.1})
    policy = order_policy.OrderPolicy()
    order = types.VDA5050Order.from_route(route, _robot_obj(), "m1", 1, policy=policy)
    assert [n.nodePosition.allowedDeviationXY for n in order.nodes[1:]] == [0.35, 0.35, 0.1]
    exact = order_policy.OrderPolicy(deviation_xy_legacy_default_m=None)
    order = types.VDA5050Order.from_route(route, _robot_obj(), "m1", 1, policy=exact)
    assert [n.nodePosition.allowedDeviationXY for n in order.nodes[1:]] == [0.1, 0.1, 0.1]


@pytest.mark.unit
def test_the_legacy_default_can_be_turned_off_from_the_environment(monkeypatch):
    monkeypatch.setenv("ROUTE_DEVIATION_XY_LEGACY_DEFAULT_M", "none")
    assert order_policy.from_env().deviation_xy_legacy_default_m is None
    monkeypatch.setenv("ROUTE_DEVIATION_XY_LEGACY_DEFAULT_M", "0.2")
    assert order_policy.from_env().deviation_xy_legacy_default_m == 0.2


@pytest.mark.unit
def test_an_unknown_node_policy_mode_warns_and_falls_back(monkeypatch, caplog):
    monkeypatch.setenv("VDA5050_NODE_POLICY_MODE", " ON ")
    assert order_policy.from_env().node_policy_mode == order_policy.NodePolicyMode.ON
    monkeypatch.setenv("VDA5050_NODE_POLICY_MODE", "always")
    with caplog.at_level(logging.WARNING):
        policy = order_policy.from_env()
    assert policy.node_policy_mode == order_policy.NodePolicyMode.FACTSHEET
    assert "always" in caplog.text
