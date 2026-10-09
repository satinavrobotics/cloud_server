"""Unit tests for the order-churn fixes (the order/cancel loop of 2026-10-08, robot
formidable-peacock: 854 revisions of one order in 9 minutes).

- HEADER: headerId counts per topic (/order and /instantActions separately).
- IDENTITY: an orderId is only ever published with one content; a resend republishes it.
- BACKOFF: an order the robot has not adopted is resent with back-off, never while our
  cancelOrder is in flight.
- CHURN: more than ORDER_CHURN_MAX_REVISIONS new revisions within the window fail the
  mission and raise MISSION.ORDER_CHURN.
- RESUME: after a dispatcher restart the robot's first state decides: carry on, cancel
  first, or send a new revision without the waypoints already reached.
- PROGRESS: waypoint progress is counted on the current route only, and through the
  waypoint offset of a resumed order.
"""
import datetime
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import asyncio

import cloud_common.objects as api_objects
import cloud_common.objects.mission as mission_object
import packages.controllers.mission.server as server_module
import packages.controllers.mission.vda5050_types as types
from tests.unit.test_mission_lifecycle_fixes import (
    State, _cancel_done, _cancels, _make_robot, _mission, _order_id, _orders,
    _rerouted, _route, _start, _state, _tree, _xs)


@pytest.fixture(autouse=True)
def _no_cancel_dwell(monkeypatch):
    """These tests exercise the cancel/resend flow right after dispatch; the reroute-cancel
    dwell (a robot that has not yet reported the order just sent) is covered in
    test_offline_missions.py."""
    from packages.controllers.mission import order_policy
    monkeypatch.setattr(order_policy, "_current",
                        order_policy.OrderPolicy(cancel_min_dwell_s=0.0))


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(server_module, "time",
                        SimpleNamespace(monotonic=c.monotonic, time=time.time))
    return c


def _resumed(n=4, reached=None, route_rev=0):
    """A RUNNING mission as a restarted dispatcher reads it back."""
    m = _mission(tree=_tree(_route("a", n=n)))
    m.route_rev = route_rev
    m.status.state = State.RUNNING
    m.status.start_timestamp = datetime.datetime.now()
    m.status.run_id = "abcd1234"
    m.status.node_status["a"].state = State.RUNNING
    if reached is not None:
        m.status.task_status["a"] = reached
    return m


def _busy(order_id):
    """A state of a robot still executing `order_id`."""
    s = _state(order_id)
    s.nodeStates = [types.VDA5050NodeState(nodeId=f"{order_id}-s4", sequenceId=4)]
    return s


def _sent(order_id, route_node):
    """The record an earlier process stored before sending `order_id` for `route_node`."""
    route = mission_object.MissionRouteNodeV1(**route_node["route"])
    return mission_object.MissionSentOrderV1(order_id=order_id,
                                             route_digest=server_module._route_digest(route))


def _info(state, mission_status):
    state.information = [types.VDA5050Info(
        infoType="missionStatus", infoDescription=mission_status, infoLevel="INFO")]
    return state


def _cancel_ids(r):
    return {a["actionId"] for c in _cancels(r) for a in c["instantActions"]}


def _reach(r, waypoint):
    """The robot reports reaching waypoint `waypoint` of its current order."""
    seq = (waypoint + 1) * 2
    return _state(_order_id(r), last_node_id=f"{_order_id(r)}-s{seq}", last_seq=seq)


# ---------------------------------------------------------------------------
# HEADER
# ---------------------------------------------------------------------------
@pytest.mark.unit
async def test_header_ids_count_per_topic():
    r, _ = _make_robot()
    await _start(r, _mission())
    await r._on_mission_change(_rerouted())                  # a cancelOrder
    await r._on_client_message(_state(_order_id(r), actions=[_cancel_done(r)]))

    assert [o["headerId"] for o in _orders(r)] == [0, 1]
    assert [a["headerId"] for a in _cancels(r)] == [0]
    r._cancel_mission_timeout()


# ---------------------------------------------------------------------------
# IDENTITY + BACKOFF
# ---------------------------------------------------------------------------
@pytest.mark.unit
async def test_a_resend_is_the_same_order_with_back_off(clock):
    r, _ = _make_robot()
    await _start(r, _mission())
    first = _orders(r)[0]

    # The robot moved and still reports another order: not resent at once ...
    r._robot_object.status.pose.x = 42.0
    await r._on_client_message(_state("elsewhere-n0"))
    assert len(_orders(r)) == 1
    # ... but after the back-off, with exactly the content sent before.
    clock.t += r.ORDER_RESEND_BASE_S
    await r._on_client_message(_state("elsewhere-n0"))
    orders = _orders(r)
    assert len(orders) == 2
    assert orders[1]["nodes"] == first["nodes"] and orders[1]["edges"] == first["edges"]
    assert orders[1]["orderId"] == first["orderId"]
    assert orders[1]["headerId"] == first["headerId"] + 1

    # The interval doubles.
    clock.t += r.ORDER_RESEND_BASE_S
    await r._on_client_message(_state("elsewhere-n0"))
    assert len(_orders(r)) == 2
    clock.t += r.ORDER_RESEND_BASE_S
    await r._on_client_message(_state("elsewhere-n0"))
    assert len(_orders(r)) == 3
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_no_order_is_sent_while_our_cancel_is_in_flight(clock):
    r, _ = _make_robot()
    await _start(r, _mission())
    await r._on_mission_change(_rerouted())                  # cancelOrder outstanding
    clock.t += 100
    for _ in range(5):
        await r._on_client_message(_state("elsewhere-n0"))
    assert len(_orders(r)) == 1
    assert r._order_mismatch_count == 0

    # The cancel completes: then the new route goes out, once.
    await r._on_client_message(_state("elsewhere-n0", actions=[_cancel_done(r)]))
    orders = _orders(r)
    assert len(orders) == 2 and _xs(orders[1]) == [9.0, 10.0]
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_a_resend_that_cannot_be_revised_yet_is_retried_not_replaced_by_the_old_one(
        clock):
    """The revision write fails when the robot reports the cancel done: the cancelled
    order is not sent again; the next state message retries the revision."""
    r, db = _make_robot()
    await _start(r, _mission())
    cancelled_id = _orders(r)[0]["orderId"]
    await r._on_mission_change(_rerouted())
    robot_writes = db.update_status

    async def _mission_writes_fail(cls, *args):
        if cls is api_objects.MissionObjectV1:
            raise RuntimeError("db down")
        return await robot_writes(cls, *args)

    db.update_status = AsyncMock(side_effect=_mission_writes_fail)
    await r._on_client_message(_state("elsewhere-n0", actions=[_cancel_done(r)]))
    clock.t += 100
    await r._on_client_message(_state("elsewhere-n0"))
    assert [o["orderId"] for o in _orders(r)] == [cancelled_id]

    db.update_status = AsyncMock()
    await r._on_client_message(_state("elsewhere-n0"))
    orders = _orders(r)
    assert len(orders) == 2 and orders[1]["orderId"] != cancelled_id
    assert _xs(orders[1]) == [9.0, 10.0]
    r._cancel_mission_timeout()


# ---------------------------------------------------------------------------
# CHURN
# ---------------------------------------------------------------------------
async def _reroute_cycle(r, rev):
    await r._on_mission_change(_rerouted(x0=float(10 * rev), rev=rev))
    await r._on_client_message(_state(_order_id(r), actions=[_cancel_done(r)]))


@pytest.mark.unit
async def test_order_churn_fails_the_mission_and_raises_an_event(clock):
    r, _ = _make_robot()
    m = await _start(r, _mission())
    for rev in range(1, r.ORDER_CHURN_MAX_REVISIONS + 1):
        await _reroute_cycle(r, rev)
        clock.t += 1
    assert m.status.state == State.RUNNING
    assert len(_orders(r)) == 1 + r.ORDER_CHURN_MAX_REVISIONS

    await _reroute_cycle(r, r.ORDER_CHURN_MAX_REVISIONS + 1)

    assert m.status.state == State.FAILED
    assert "order churn" in m.status.failure_reason
    assert len(_orders(r)) == 1 + r.ORDER_CHURN_MAX_REVISIONS      # nothing more sent
    recorder = r._robot_server.fleet_recorder
    recorder.order_churn.assert_called_once()
    assert recorder.order_churn.call_args.args[3] == r.ORDER_CHURN_MAX_REVISIONS + 1
    await r._on_client_message(_state("elsewhere-n0"))
    assert r._current_mission is None                                # the queue moved on
    assert len(_orders(r)) == 1 + r.ORDER_CHURN_MAX_REVISIONS


@pytest.mark.unit
async def test_revisions_spread_over_time_are_not_churn(clock):
    r, _ = _make_robot()
    m = await _start(r, _mission())
    for rev in range(1, 3 * r.ORDER_CHURN_MAX_REVISIONS):
        await _reroute_cycle(r, rev)
        clock.t += r.ORDER_CHURN_WINDOW_S / r.ORDER_CHURN_MAX_REVISIONS + 1
    assert m.status.state == State.RUNNING
    r._robot_server.fleet_recorder.order_churn.assert_not_called()
    r._cancel_mission_timeout()


# ---------------------------------------------------------------------------
# RESUME
# ---------------------------------------------------------------------------
@pytest.mark.unit
async def test_resume_carries_on_when_the_robot_is_on_the_current_order():
    r, _ = _make_robot()
    m = _resumed()
    await r._on_mission_change(m)
    assert _orders(r) == []

    await r._on_client_message(_busy("m1-rabcd1234-n1"))
    await r._on_client_message(_state("m1-rabcd1234-n1", last_node_id="m1-rabcd1234-n1-s4",
                                      last_seq=4))
    assert _orders(r) == [] and _cancels(r) == []
    assert m.status.task_status == {"a": 1}
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_resume_cancels_an_order_the_robot_still_executes_before_sending():
    r, _ = _make_robot()
    m = _resumed()
    await r._on_mission_change(m)

    await r._on_client_message(_busy("someone-elses-n0"))
    assert _orders(r) == [] and len(_cancels(r)) == 1

    await r._on_client_message(_state("someone-elses-n0", actions=[_cancel_done(r)]))
    (order,) = _orders(r)
    assert order["orderId"] == "m1-rabcd1234v1-n1"
    assert m.status.order_rev == 1
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_resume_after_a_reroute_does_not_adopt_the_old_route():
    """Rerouted while the dispatcher was down; the robot still drives the old route
    under the current orderId: cancel it, then send the new route as a new revision."""
    r, _ = _make_robot()
    m = _rerouted(rev=1)
    m.status.state = State.RUNNING
    m.status.start_timestamp = datetime.datetime.now()
    m.status.run_id = "abcd1234"
    m.status.node_status["a"].state = State.RUNNING
    m.status.task_status = {"a": 0}                 # progress on the old route
    m.status.sent_order = _sent("m1-rabcd1234-n1", _route("a"))
    await r._on_mission_change(m)
    assert m.status.task_status == {}

    await r._on_client_message(_busy("m1-rabcd1234-n1"))
    # Progress the robot reports on the old route is not counted on the new one.
    s = _state("m1-rabcd1234-n1", last_node_id="m1-rabcd1234-n1-s4", last_seq=4)
    s.nodeStates = _busy("m1-rabcd1234-n1").nodeStates
    await r._on_client_message(s)
    assert m.status.task_status == {}
    assert len(_cancel_ids(r)) == 1 and _orders(r) == []

    await r._on_client_message(_state("m1-rabcd1234-n1", actions=[_cancel_done(r)]))
    (order,) = _orders(r)
    assert order["orderId"] == "m1-rabcd1234v1-n1" and _xs(order) == [9.0, 10.0]
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_resume_leaves_out_the_waypoints_already_reached():
    r, db = _make_robot()
    m = _resumed(n=4, reached=1)                    # waypoints 0 and 1 reached
    await r._on_mission_change(m)
    await r._on_client_message(_state("elsewhere-n0"))

    (order,) = _orders(r)
    assert order["orderId"] == "m1-rabcd1234v1-n1"
    assert _xs(order) == [2.0, 3.0]
    assert m.status.sent_order.order_id == "m1-rabcd1234v1-n1"
    assert m.status.sent_order.waypoint_offset == 2
    assert db.update_status.await_count            # stored before the order went out

    # Progress and completion are read through the offset.
    await r._on_client_message(_reach(r, 0))
    assert m.status.task_status == {"a": 2}
    await r._on_client_message(_reach(r, 1))
    assert m.status.task_status == {"a": 3}
    assert m.status.node_status["a"].state == State.COMPLETED


@pytest.mark.unit
async def test_resume_with_every_waypoint_reached_sends_only_the_last():
    r, _ = _make_robot()
    m = _resumed(n=3, reached=2)
    await r._on_mission_change(m)
    await r._on_client_message(_state("elsewhere-n0"))
    (order,) = _orders(r)
    assert _xs(order) == [2.0]
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_a_blocked_waypoint_index_is_read_through_the_offset():
    r, _ = _make_robot()
    m = _resumed(n=4, reached=1)
    await r._on_mission_change(m)
    await r._on_client_message(_state("elsewhere-n0"))
    s = _state(_order_id(r))
    s.errors = [types.VDA5050Error(
        errorType="edgeBlocked", errorDescription="Waypoint unreachable",
        errorReferences=[types.VDA5050ErrorReference(referenceKey="nodeId",
                                                     referenceValue=f"{_order_id(r)}-s4")])]
    await r._on_client_message(s)
    assert m.status.blocked and m.status.blocked_waypoint_index == 3
    r._cancel_mission_timeout()


# ---------------------------------------------------------------------------
# PROGRESS
# ---------------------------------------------------------------------------
@pytest.mark.unit
async def test_a_reroute_resets_progress_and_ignores_the_old_routes():
    r, _ = _make_robot()
    m = await _start(r, _mission(tree=_tree(_route("a", n=4))))
    await r._on_client_message(_reach(r, 1))
    assert m.status.task_status == {"a": 1}

    await r._on_mission_change(_rerouted(n=4))
    assert m.status.task_status == {}
    await r._on_client_message(_reach(r, 2))        # still driving the old route
    assert m.status.task_status == {}

    await r._on_client_message(_state(_order_id(r), actions=[_cancel_done(r)]))
    await r._on_client_message(_reach(r, 0))
    assert m.status.task_status == {"a": 0}
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_the_incident_echo_storm_issues_one_cancel_and_one_resend():
    """2026-10-08: every status write echoed back and re-applied the reroute, so each
    order was cancelled ~0.2 s after it was sent, and each reached waypoint cancelled the
    reroute. Replay that shape: the row (with the reroute) delivered after every send and
    every waypoint reached."""
    r, _ = _make_robot()
    await _start(r, _mission(tree=_tree(_route("a", n=3))))
    await r._on_mission_change(_rerouted(n=3))
    for _ in range(3):
        await r._on_mission_change(_rerouted(n=3))
    await r._on_client_message(_state(_order_id(r), actions=[_cancel_done(r)]))
    for waypoint in range(2):
        await r._on_mission_change(_rerouted(n=3))
        await r._on_client_message(_reach(r, waypoint))
        await r._on_mission_change(_rerouted(n=3))
    await r._on_client_message(_reach(r, 2))

    assert len(_cancel_ids(r)) == 1 and len(_orders(r)) == 2
    assert r._current_mission is None
    assert len({o["orderId"] for o in _orders(r)}) == 2


@pytest.mark.unit
async def test_a_reroute_of_a_later_node_leaves_the_running_one_alone():
    """Only the rerouted node's progress is reset; the node the robot is driving keeps
    counting and completes (a mission-wide check would have stalled it)."""
    r, _ = _make_robot()
    m = await _start(r, _mission(tree=_tree(_route("a", n=2), _route("b", n=2, x0=5.0))))
    rerouted = _mission(tree=_tree(_route("a", n=2), _route("b", n=2, x0=50.0)))
    rerouted.route_rev = 1
    await r._on_mission_change(rerouted)
    assert _cancels(r) == []

    await r._on_client_message(_reach(r, 0))
    assert m.status.task_status == {"a": 0}
    await r._on_client_message(_reach(r, 1))
    assert m.status.node_status["a"].state == State.COMPLETED
    orders = _orders(r)
    assert len(orders) == 2 and _xs(orders[1]) == [50.0, 51.0]
    r._cancel_mission_timeout()


# ---------------------------------------------------------------------------
# Review repros (one per finding of the adversarial review of this change)
# ---------------------------------------------------------------------------
# R1: resume adopts an idle robot whose (current) order was cancelled -> stall.
@pytest.mark.unit
async def test_R1_resume_does_not_adopt_a_cancelled_idle_order(clock):
    r, _ = _make_robot()
    m = _resumed(n=4, reached=0)
    await r._on_mission_change(m)
    # The robot holds our current orderId but is idle mid-route (its order was cancelled
    # -- e.g. the earlier process's reroute cancel finished just before it died).
    idle = _state("m1-rabcd1234-n1", last_node_id="m1-rabcd1234-n1-s2", last_seq=2)
    for _ in range(30):
        await r._on_client_message(idle)
        clock.t += 1
    assert _orders(r), "nothing is ever sent: mission RUNNING forever"
    r._cancel_mission_timeout()


# R2: reroute applied + persisted by the earlier process, crash before the resend.
# The robot still drives the old route under the current orderId: it is adopted and the
# old route completes the node.
@pytest.mark.unit
async def test_R2_resume_after_crash_mid_reroute_completes_on_old_route():
    r, _ = _make_robot()
    m = _rerouted(rev=1)                    # stored tree has the NEW route (x 9, 10)
    m.status.state = State.RUNNING
    m.status.start_timestamp = datetime.datetime.now()
    m.status.run_id = "abcd1234"
    m.status.applied_route_rev = 1          # the earlier process applied it
    m.status.sent_order = _sent("m1-rabcd1234-n1", _route("a"))   # ... after sending it
    m.status.node_status["a"].state = State.RUNNING
    await r._on_mission_change(m)
    await r._on_client_message(_busy("m1-rabcd1234-n1"))     # old route still running
    # the robot reaches the end of its (old, 2-waypoint) route
    await r._on_client_message(_state("m1-rabcd1234-n1", last_node_id="m1-rabcd1234-n1-s4",
                                      last_seq=4))
    assert m.status.node_status["a"].state != State.COMPLETED, \
        "node completed on the old route; the new route was never sent"
    r._cancel_mission_timeout()


# R3: a cancel requested between resume and the robot's first state: the resume sends a
# new order for a mission being cancelled, and the cancel completion is lost.
@pytest.mark.unit
async def test_R3_cancel_before_first_state_after_resume():
    r, _ = _make_robot()
    m = _resumed()
    await r._on_mission_change(m)
    c = m.copy(deep=True)
    c.needs_canceled = True
    await r._on_mission_change(c)
    assert len(_cancels(r)) == 1
    done = _cancel_done(r)
    await r._on_client_message(_state("elsewhere-n0", actions=[done]))
    assert _orders(r) == [], "an order was sent for a mission being cancelled"
    assert m.status.state == State.CANCELED


# R4: adopted order (sent_route None), then a reroute of the running node: the robot's
# progress on the OLD route is counted (and would complete the node).
@pytest.mark.unit
async def test_R4_after_adopt_a_reroute_ignores_old_route_progress():
    r, _ = _make_robot()
    m = _resumed(n=2)
    await r._on_mission_change(m)
    await r._on_client_message(_busy("m1-rabcd1234-n1"))     # adopt
    await r._on_mission_change(_rerouted(rev=1))              # cancel goes out
    assert len(_cancels(r)) == 1
    await r._on_client_message(_state("m1-rabcd1234-n1", last_node_id="m1-rabcd1234-n1-s4",
                                      last_seq=4))           # finishes the OLD route
    assert m.status.node_status["a"].state != State.COMPLETED
    assert m.status.task_status == {}
    r._cancel_mission_timeout()


# R5: missionStatus "canceled" reported one message before the cancel action FINISHED:
# two new revisions for one reroute, the second sent while the robot runs the first.
@pytest.mark.unit
async def test_R5_split_cancel_signals_make_one_revision():
    r, _ = _make_robot()
    m = await _start(r, _mission())
    await r._on_mission_change(_rerouted())
    cancel_id = next(iter(r._current_instant_actions))
    running = types.VDA5050ActionState(actionId=cancel_id, actionType=types.VDA5050InstantActionType.CANCEL_ORDER,
                                       actionStatus=types.VDA5050ActionStatus.RUNNING)
    old = _order_id(r)
    await r._on_client_message(_info(_state(old, actions=[running]), "canceled"))
    await r._on_client_message(_state(old, actions=[_cancel_done(r)]))
    assert m.status.order_rev == 1, f"order_rev={m.status.order_rev}"
    assert len(_orders(r)) == 2, [o["orderId"] for o in _orders(r)]
    r._cancel_mission_timeout()


# R6: mission A times out (timeout cancel sent), B is dispatched at once, then A's cancel
# completes and B -- already sent -- is re-issued as a new revision.
@pytest.mark.unit
async def test_R6_timeout_cancel_of_previous_mission_reissues_next_one():
    r, _ = _make_robot()
    a = _mission(name="A")
    b = _mission(name="B")
    r._missions["A"] = a
    r._missions["B"] = b
    await r._try_start_mission()
    assert r._current_mission is a
    a.timeout = datetime.timedelta(seconds=0)
    r._arm_mission_timeout()
    await asyncio.sleep(0.01)
    assert r._current_mission is b
    sent_b = [o["orderId"] for o in _orders(r) if o["orderId"].startswith("B-")]
    done = _cancel_done(r)
    await r._on_client_message(_state(f"A-r{a.status.run_id}-n1", actions=[done]))
    sent_b2 = [o["orderId"] for o in _orders(r) if o["orderId"].startswith("B-")]
    assert sent_b == [] , f"B sent while A's cancel was in flight: {sent_b}"
    assert len(sent_b2) == 1 and b.status.order_rev == 0, sent_b2
    r._cancel_mission_timeout()


# R7: _updating_mission_from_api left set (revision write failed) leaks into the next
# mission and makes its first order be superseded at once.
@pytest.mark.unit
async def test_R7_resend_flag_does_not_leak_into_next_mission():
    r, db = _make_robot()
    a = await _start(r, _mission(name="A"))
    r._pending_send = server_module.NEW_REVISION   # what a failed revision write leaves
    a.status.failure_reason = "x"
    r._set_mission_state(State.FAILED)
    b = _mission(name="B")
    r._missions["B"] = b
    await r.get_next_mission()
    assert r._current_mission is b
    await r._on_client_message(_state(f"B-r{b.status.run_id}-n1"))   # robot adopts B v0
    assert b.status.order_rev == 0, [o["orderId"] for o in _orders(r)]
    r._cancel_mission_timeout()


# R8: a reroute cancel the robot never acknowledges is abandoned; the reroute is then
# never sent and progress is never counted -> stuck until mission timeout.
@pytest.mark.unit
async def test_R8_an_abandoned_reroute_cancel_fails_the_mission(clock):
    r, _ = _make_robot()
    m = await _start(r, _mission())
    await r._on_mission_change(_rerouted())
    order_id = _order_id(r)
    for _ in range(r.MAX_INSTANT_ACTION_RESENDS + 5):
        await r._on_client_message(_busy(order_id))
        clock.t += r.INSTANT_ACTION_RESEND_MAX_S  # resends are backed off
    assert not r._has_outstanding_cancel()
    # Not stuck: failed, saying why, and nothing more sent.
    assert m.status.state == State.FAILED
    assert "never confirmed the cancelOrder" in m.status.failure_reason
    assert len(_orders(r)) == 1


# R9: legacy mission (no run_id) resumed: same orderId republished with different
# (trimmed) content than the earlier process sent.
@pytest.mark.unit
async def test_R9_legacy_resume_reuses_order_id_with_new_content():
    r, _ = _make_robot()
    m = _resumed(n=4, reached=1)
    m.status.run_id = None
    await r._on_mission_change(m)
    await r._on_client_message(_state("elsewhere-n0"))
    (order,) = _orders(r)
    assert not (order["orderId"] == "m1-n1" and _xs(order) != [0.0, 1.0, 2.0, 3.0]), \
        f"{order['orderId']} re-sent with different content {_xs(order)}"


# R10: force-cancel (operator escape hatch) during a tracked mission: the dispatcher
# immediately re-issues the order the operator wanted gone.
@pytest.mark.unit
async def test_R10_force_cancel_is_not_followed_by_a_reissue():
    r, db = _make_robot()
    m = await _start(r, _mission())
    robot = r._robot_object.copy(deep=True)
    robot.needs_order_cancel = True
    await r._handle_force_cancel(robot)
    order_id = _order_id(r)
    await r._on_client_message(_state(order_id, actions=[_cancel_done(r)]))
    assert len(_orders(r)) == 1, [o["orderId"] for o in _orders(r)]
    # The operator's cancel ends the mission rather than leaving it RUNNING orderless.
    assert m.status.state == State.CANCELED
    assert "force cancel" in m.status.failure_reason


@pytest.mark.unit
async def test_R6b_previous_missions_cancel_completion_reissues_next_one():
    r, _ = _make_robot()
    a = _mission(name="A")
    b = _mission(name="B")
    r._missions["A"] = a
    r._missions["B"] = b
    await r._try_start_mission()
    a.timeout = datetime.timedelta(seconds=0)
    r._arm_mission_timeout()
    await asyncio.sleep(0.01)
    assert r._current_mission is b
    done = _cancel_done(r)
    # robot already adopted B v0, and reports A's timeout cancel finished
    await r._on_client_message(_busy(f"B-r{b.status.run_id}-n1").copy(update={"actionStates": [done]}))
    sent_b = [o["orderId"] for o in _orders(r) if o["orderId"].startswith("B-")]
    assert len(sent_b) == 1 and b.status.order_rev == 0, sent_b
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_R3b_cancelled_mission_after_resume_runs_to_completion():
    r, _ = _make_robot()
    m = _resumed(n=2)
    await r._on_mission_change(m)
    c = m.copy(deep=True)
    c.needs_canceled = True
    await r._on_mission_change(c)
    done = _cancel_done(r)
    await r._on_client_message(_state("elsewhere-n0", actions=[done]))
    assert m.status.state == State.CANCELED
    assert _orders(r) == []


@pytest.mark.unit
async def test_R11_legacy_reroute_resends_the_new_route():
    r, _ = _make_robot()
    m = _resumed(n=2)
    m.status.run_id = None
    await r._on_mission_change(m)
    await r._on_client_message(_state("elsewhere-n0"))       # resume sends m1-n1
    assert _xs(_orders(r)[0]) == [0.0, 1.0]
    await r._on_mission_change(_rerouted(rev=1))
    await r._on_client_message(_state("m1-n1", actions=[_cancel_done(r)]))
    assert _xs(_orders(r)[-1]) == [9.0, 10.0], _xs(_orders(r)[-1])
    r._cancel_mission_timeout()


# ---------------------------------------------------------------------------
# Second review pass repros
# ---------------------------------------------------------------------------
async def _timeout_a_then_b(r):
    a = _mission(name="A")
    b = _mission(name="B")
    r._missions["A"] = a
    r._missions["B"] = b
    await r._try_start_mission()
    a.timeout = datetime.timedelta(seconds=0)
    r._arm_mission_timeout()
    await asyncio.sleep(0.01)
    assert r._current_mission is b
    return a, b


def _b_orders(r):
    return [o["orderId"] for o in _orders(r) if o["orderId"].startswith("B-")]


# S1: the operator cancels B while A's (timeout) cancel is still in flight: B sends its
# own cancel (another run's does not cancel it), and is never dispatched.
@pytest.mark.unit
async def test_S1_a_mission_cancelled_behind_the_previous_ones_cancel_is_never_sent():
    r, _ = _make_robot()
    a, b = await _timeout_a_then_b(r)
    assert _b_orders(r) == []                               # deferred behind A's cancel
    c = b.copy(deep=True)
    c.needs_canceled = True
    await r._on_mission_change(c)
    assert len(_cancel_ids(r)) == 2                         # B's own, besides A's
    done = [types.VDA5050ActionState(actionId=i, actionType=types.VDA5050InstantActionType.CANCEL_ORDER,
                                     actionStatus=types.VDA5050ActionStatus.FINISHED)
            for i in list(r._current_instant_actions)]
    await r._on_client_message(_state(f"A-r{a.status.run_id}-n1", actions=done))
    assert _b_orders(r) == []
    assert b.status.state == State.CANCELED


# S2: operator force-cancels a zombie order (the documented use of the hatch) while
# mission B waits for the robot to adopt its order: B is cancelled instead of being sent.
@pytest.mark.unit
async def test_S2_force_cancel_of_a_zombie_does_not_cancel_the_waiting_mission(clock):
    r, _ = _make_robot()
    b = await _start(r, _mission(name="B"))
    await r._on_client_message(_busy("zombie-n0"))         # robot runs something else
    robot = r._robot_object.copy(deep=True)
    robot.needs_order_cancel = True
    await r._handle_force_cancel(robot)
    await r._on_client_message(_state("zombie-n0", actions=[_cancel_done(r)]))
    assert b.status.state == State.RUNNING, (b.status.state, b.status.failure_reason)


# S3: a robot that never reports instant-action states (cancel works, just no ack):
# every reroute now FAILS the mission, though the robot plainly dropped the order.
@pytest.mark.unit
async def test_S3_unacked_reroute_cancel_on_a_robot_that_dropped_the_order(clock):
    r, _ = _make_robot()
    m = await _start(r, _mission())
    await r._on_mission_change(_rerouted())
    for _ in range(r.MAX_INSTANT_ACTION_RESENDS + 2):
        await r._on_client_message(_state(_order_id(r)))   # idle: nodeStates empty
        clock.t += r.INSTANT_ACTION_RESEND_MAX_S  # resends are backed off
    assert m.status.state == State.RUNNING and len(_orders(r)) == 2, \
        (m.status.state, m.status.failure_reason)
    r._cancel_mission_timeout()


# S4: how long does an unacknowledged STOP of the previous mission hold the next one?
@pytest.mark.unit
async def test_S4_unacked_previous_stop_delays_next_mission(clock):
    r, _ = _make_robot()
    a, b = await _timeout_a_then_b(r)
    n = 0
    while not _b_orders(r) and n < 100:
        await r._on_client_message(_busy(f"A-r{a.status.run_id}-n1"))
        n += 1
        clock.t += r.INSTANT_ACTION_RESEND_MAX_S  # resends are backed off
    print("state messages before B went out:", n)
    assert _b_orders(r) and n <= r.MAX_INSTANT_ACTION_RESENDS + 1
    r._cancel_mission_timeout()


# S5: persist of the sent-order record fails at first dispatch: order owed; next state
# message must send it (and only once).
@pytest.mark.unit
async def test_S5_record_write_failure_is_retried():
    r, db = _make_robot()
    real = db.update_status
    fails = {"n": 1}

    async def flaky(cls, name, status, *a):
        if cls is api_objects.MissionObjectV1 and status.sent_order is not None and fails["n"]:
            fails["n"] -= 1
            raise RuntimeError("db down")
        return await real(cls, name, status, *a)

    db.update_status = AsyncMock(side_effect=flaky)
    m = _mission()
    r._missions[m.name] = m
    await r._try_start_mission()
    assert _orders(r) == []
    await r._on_client_message(_state("elsewhere-n0"))
    assert len(_orders(r)) == 1
    await r._on_client_message(_state("elsewhere-n0"))
    assert len(_orders(r)) == 1
    r._cancel_mission_timeout()


# S6: node advance on a two-node mission while a REPLACE of ... (reroute of node b
# while a is running -> no cancel). Then reroute b again after a completes but before
# robot adopts b: b's order is RUNNING; REPLACE cancel; robot has no order -> FAILED ->
# NEW_REVISION: b sent once with newest route, unique ids.
@pytest.mark.unit
async def test_S6_reroute_before_adopt_of_next_node(clock):
    r, _ = _make_robot()
    await _start(r, _mission(tree=_tree(_route("a", n=2), _route("b", n=2, x0=5.0))))
    await r._on_client_message(_reach(r, 0))
    await r._on_client_message(_reach(r, 1))               # a done -> b sent
    assert len(_orders(r)) == 2
    rr = _mission(tree=_tree(_route("a", n=2), _route("b", n=2, x0=50.0)))
    rr.route_rev = 1
    await r._on_mission_change(rr)
    assert len(_cancels(r)) == 1
    (aid,) = list(r._current_instant_actions)
    failed = types.VDA5050ActionState(actionId=aid, actionType=types.VDA5050InstantActionType.CANCEL_ORDER,
                                      actionStatus=types.VDA5050ActionStatus.FAILED)
    await r._on_client_message(_state(_order_id(r, 1), actions=[failed]))
    ids = [o["orderId"] for o in _orders(r)]
    assert len(ids) == 3 and len(set(ids)) == 3 and _xs(_orders(r)[-1]) == [50.0, 51.0], ids
    r._cancel_mission_timeout()


# S7: repeat pass: a reroute REPLACE cancel outstanding when the pass ends? The pass
# can't end on the old route; but a timeout of pass N with REPLACE outstanding means no
# STOP is sent (has_outstanding_cancel), and if that REPLACE is abandoned the robot keeps
# running the timed-out order while the next mission is dispatched.
@pytest.mark.unit
async def test_S7_timeout_during_replace_cancel_next_mission_waits(clock):
    r, _ = _make_robot()
    a = _mission(name="A")
    b = _mission(name="B")
    r._missions["A"] = a
    r._missions["B"] = b
    await r._try_start_mission()
    rr = _rerouted(name="A")
    await r._on_mission_change(rr)                          # REPLACE outstanding
    a.timeout = datetime.timedelta(seconds=0)
    r._arm_mission_timeout()
    await asyncio.sleep(0.01)
    assert r._current_mission is b
    assert len(_cancels(r)) == 1                             # no STOP sent
    assert _b_orders(r) == []
    r._cancel_mission_timeout()


# S8: missionStatus "canceled" level-triggered on an adopted order after resume, robot
# idle on it: one revision only.
@pytest.mark.unit
async def test_S8_robot_side_cancel_one_revision(clock):
    r, _ = _make_robot()
    m = await _start(r, _mission())
    s = _state(_order_id(r))
    s.information = [types.VDA5050Info(infoType="missionStatus", infoDescription="canceled",
                                       infoLevel="INFO")]
    old = _order_id(r)
    for _ in range(5):
        s2 = s.copy(deep=True)
        s2.orderId = old
        await r._on_client_message(s2)
        clock.t += 1
    assert m.status.order_rev == 1, m.status.order_rev
    r._cancel_mission_timeout()




# ---------------------------------------------------------------------------
# OPERATOR TAKEOVER (robot-side rescue: an operator goal ends the running order)
# ---------------------------------------------------------------------------
def _takeover(state, order_id):
    state.errors = [types.VDA5050Error(
        errorType="operatorTakeover", errorDescription="Operator goal took over",
        errorReferences=[types.VDA5050ErrorReference(referenceKey="orderId",
                                                     referenceValue=order_id),
                         types.VDA5050ErrorReference(referenceKey="orderUpdateId",
                                                     referenceValue="0")])]
    return state


@pytest.mark.unit
async def test_an_operator_takeover_cancels_the_mission_and_holds_the_queue(clock):
    r, _ = _make_robot()
    m = await _start(r, _mission())
    taken = _order_id(r)
    queued = _mission(name="next")
    await r._on_mission_change(queued)

    s = _info(_takeover(_state(taken), taken), "canceled")
    r._robot_object.status.errors = server_module.vda5050_errors_to_status_dict(s.errors)
    await r._on_client_message(s)

    assert m.status.state == State.CANCELED
    assert "operatorTakeover" in m.status.failure_reason
    assert len(_orders(r)) == 1                      # nothing re-sent ...
    assert queued.status.held                        # ... nor the next mission dispatched
    for _ in range(5):
        clock.t += 10
        await r._on_client_message(_takeover(_state(taken), taken))
    assert len(_orders(r)) == 1

    # The operator hands the robot back: the queue goes on.
    await r._on_client_message(_state(taken))
    assert len(_orders(r)) == 2 and _orders(r)[1]["orderId"].startswith("next-")
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_an_operator_takeover_of_an_earlier_revision_of_the_run_counts():
    r, _ = _make_robot()
    m = await _start(r, _mission())
    first = _order_id(r)
    await r._on_mission_change(_rerouted())
    await r._on_client_message(_state(first, actions=[_cancel_done(r)]))   # now v1
    await r._on_client_message(_takeover(_state(first), first))
    assert m.status.state == State.CANCELED


@pytest.mark.unit
async def test_a_takeover_of_another_order_does_not_end_the_mission():
    r, _ = _make_robot()
    m = await _start(r, _mission())
    await r._on_client_message(_takeover(_state("someone-elses-n0"), "someone-elses-n0"))
    assert m.status.state == State.RUNNING
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_a_takeover_seen_on_resume_cancels_without_sending():
    r, _ = _make_robot()
    m = _resumed()
    await r._on_mission_change(m)
    await r._on_client_message(_takeover(_state("m1-rabcd1234-n1"), "m1-rabcd1234-n1"))
    assert m.status.state == State.CANCELED
    assert _orders(r) == [] and _cancels(r) == []


@pytest.mark.unit
async def test_a_mission_held_after_another_is_not_treated_as_dispatched():
    """Pre-existing: the previous mission's behavior tree was kept when the next one was
    picked and held, so the held mission's state handling sent orders for it."""
    r, _ = _make_robot()
    await _start(r, _mission())
    queued = _mission(name="next")
    await r._on_mission_change(queued)
    not_ready = [types.VDA5050Error(errorType="navigationNotReadyError",
                                    errorDescription="nav down")]
    r._robot_object.status.errors = server_module.vda5050_errors_to_status_dict(not_ready)
    r._set_mission_state(State.COMPLETED)
    await r.get_next_mission()
    assert r._current_mission is queued and queued.status.held
    assert r._current_behavior_tree is None

    for _ in range(3):
        s = _state("m1-elsewhere-n1")
        s.errors = not_ready
        await r._on_client_message(s)
    assert len(_orders(r)) == 1 and queued.status.held       # nothing sent while held
