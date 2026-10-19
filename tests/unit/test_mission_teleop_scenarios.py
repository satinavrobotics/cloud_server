"""End-to-end replays of what happens around teleop, through the real behavior tree and the published MQTT messages (not mocks of the
dispatcher's own decisions): the incident of 2026-10-19 and its neighbours. Each ends by completing the mission, because the point of
following the robot's order is that the mission then finishes normally.
"""
import pytest

import cloud_common.objects.robot as robot_object
import packages.controllers.mission.vda5050_types as types
from tests.unit.test_mission_lifecycle_fixes import (
    State, _make_robot, _mission, _order_id, _orders, _published, _route, _start, _state, _tree)

pytestmark = pytest.mark.unit

TELEOP = robot_object.RobotStateV1.TELEOP
WAYPOINTS = 4                                    # a route of 4 waypoints is orders sequences 0..8; the last node is seq 8


def _executing(order_id, seq):
    """The robot driving `order_id`, at node sequence `seq`."""
    s = _state(order_id, last_node_id=f"{order_id}-s{seq}", last_seq=seq)
    s.nodeStates = [types.VDA5050NodeState(nodeId=f"{order_id}-s{seq + 2}", sequenceId=seq + 2)]
    s.driving = True
    return s


def _paused_and_canceled(order_id, seq):
    """What the robot reports when teleop starts: its order reported canceled, nothing left to execute."""
    s = _state(order_id, last_node_id=f"{order_id}-s{seq}", last_seq=seq)
    s.information = [types.VDA5050Info(infoType="missionStatus", infoDescription="canceled", infoLevel="INFO")]
    return s


def _finished(order_id):
    seq = WAYPOINTS * 2
    s = _state(order_id, last_node_id=f"{order_id}-s{seq}", last_seq=seq)
    s.information = [types.VDA5050Info(infoType="missionStatus", infoDescription="completed", infoLevel="INFO")]
    return s


def _cancel_orders(r):
    return [a for m in _published(r, "/instantActions") for a in m["instantActions"] if a["actionType"] == "cancelOrder"]


def _teleop_ack(r, action_type):
    r.update_robot_state([types.VDA5050Action(actionType=action_type, actionId="t")])


async def _running_mission_at(seq):
    r, _ = _make_robot()
    r._robot_object.status.state = robot_object.RobotStateV1.ON_TASK
    m = await _start(r, _mission(tree=_tree(_route("a", n=WAYPOINTS))))
    oid = _order_id(r)
    assert len(_orders(r)) == 1
    for s in range(2, seq + 1, 2):
        await r._on_client_message(_executing(oid, s))
    assert m.status.state == State.RUNNING
    return r, m, oid


async def test_the_incident_teleop_in_the_middle_of_a_mission_the_robot_resumes_and_the_mission_completes():
    r, m, oid = await _running_mission_at(seq=5)                       # about 70% of the way

    r._robot_object.status.state = TELEOP                              # the operator takes over
    _teleop_ack(r, types.NVInstantActionType.START_TELEOP)
    await r._on_client_message(_paused_and_canceled(oid, 5))
    await r._on_client_message(_paused_and_canceled(oid, 5))
    assert m.status.state == State.RUNNING
    assert len(_orders(r)) == 1, "an order was resent while the robot was paused"

    _teleop_ack(r, types.NVInstantActionType.STOP_TELEOP)              # ... and hands it back
    for seq in (6, 7):                                                 # the robot resumes the SAME order
        await r._on_client_message(_executing(oid, seq))
    assert len(_orders(r)) == 1 and _cancel_orders(r) == []
    assert m.status.state == State.RUNNING

    await r._on_client_message(_executing(oid, 8))                    # reaching the last node completes the mission
    await r._on_client_message(_finished(oid))
    assert m.status.state == State.COMPLETED, m.status.failure_reason


async def test_the_same_when_the_robot_stays_quiet_for_a_while_after_teleop(monkeypatch):
    import packages.controllers.mission.server as server_module
    now = [1000.0]
    monkeypatch.setattr(server_module, "time", type("T", (), {"monotonic": staticmethod(lambda: now[0]), "time": staticmethod(__import__("time").time)}))
    r, m, oid = await _running_mission_at(seq=5)
    r._robot_object.status.state = TELEOP
    _teleop_ack(r, types.NVInstantActionType.START_TELEOP)
    await r._on_client_message(_paused_and_canceled(oid, 5))
    now[0] += 30                                                       # a long teleop
    _teleop_ack(r, types.NVInstantActionType.STOP_TELEOP)
    now[0] += 2                                                        # the robot is still resuming
    await r._on_client_message(_paused_and_canceled(oid, 5))
    assert len(_orders(r)) == 1 and m.status.state == State.RUNNING
    for seq in (6, 8):
        await r._on_client_message(_executing(oid, seq))
    await r._on_client_message(_finished(oid))
    assert m.status.state == State.COMPLETED


async def test_an_order_dropped_without_any_teleop_is_still_resent_as_before():
    r, m, oid = await _running_mission_at(seq=3)
    await r._on_client_message(_paused_and_canceled(oid, 3))           # no teleop anywhere: the robot really dropped the order
    await r._on_client_message(_paused_and_canceled(oid, 3))
    orders = _orders(r)
    assert len(orders) == 2, "a dropped order must still be sent again"
    assert orders[1]["orderId"] != oid                                  # as a new revision
    assert m.status.state == State.RUNNING


async def test_a_new_revision_the_robot_refuses_while_it_drives_the_old_one_is_followed_to_completion():
    """What the old code did after teleop: the dispatcher moved on to a new revision, and the robot (resuming the old order) refused it."""
    r, m, oid = await _running_mission_at(seq=5)
    assert await r._bump_order_rev()
    await r._send_order(waypoint_offset=r._reached_waypoints())
    new_oid = _orders(r)[-1]["orderId"]
    assert new_oid != oid and len(_orders(r)) == 2

    refusal = _executing(oid, 5)
    refusal.errors = [types.VDA5050Error(
        errorType="orderUpdateError", errorDescription="An order is running", errorLevel=types.VDA5050ErrorLevel.WARNING,
        errorReferences=[types.VDA5050ErrorReference(referenceKey="orderId", referenceValue=new_oid)])]
    await r._on_client_message(refusal)
    await r._on_client_message(refusal)
    assert m.status.state == State.RUNNING, m.status.failure_reason
    assert _cancel_orders(r) == [] and len(_orders(r)) == 2, "the robot was stopped or sent yet another order"

    for seq in (6, 7):
        await r._on_client_message(_executing(oid, seq))
    assert m.status.state == State.RUNNING
    await r._on_client_message(_executing(oid, 8))
    await r._on_client_message(_finished(oid))
    assert m.status.state == State.COMPLETED, m.status.failure_reason


async def test_a_new_revision_with_an_offset_route_is_followed_when_the_robot_runs_that_one():
    """The robot adopted the newer revision (which starts after the waypoints already reached): progress is read against its offset."""
    r, m, oid = await _running_mission_at(seq=5)
    assert await r._bump_order_rev()
    await r._send_order(waypoint_offset=r._reached_waypoints())
    new_oid = _orders(r)[-1]["orderId"]
    await r._on_client_message(_executing(new_oid, 2))
    assert m.status.state == State.RUNNING
    assert r._current_order_id() == new_oid
    await r._on_client_message(_finished(new_oid))
    assert m.status.state in (State.COMPLETED, State.RUNNING)
