"""Unit tests for the order-ready gate in Robot._send_order.

VDA5050 has a server wait for the previous order to finish (the robot's nodeStates and
edgeStates empty) before it sends a new orderId. A single-node mission completes on the
robot's missionStatus flag, which can come before the final lastNodeId, and the next
mission's order used to go out at once: the robot never reported the old order's final
node (2 of 4 orders in the 2026-10-09 runs).

Covers:
- a new order is held (owed via _pending_send) while the robot lists nodes of another
  dispatcher order, and goes out with the state that empties them;
- not gated: a resend of the same orderId, the robot's own offline order, an order
  released from the wait (a new revision the dispatcher chose, an unanswered cancel);
- a robot stuck on the old order, not driving, gets one CLEAR cancelOrder after the dwell;
  a driving one is left alone, and a cancel in flight is not doubled;
- a completion by flag before the final node is logged and recorded once.
"""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import cloud_common.objects as api_objects
import packages.controllers.mission.vda5050_types as types
from packages.controllers.mission import server
from packages.controllers.mission.server import CancelPurpose, Robot
from packages.database.postgres import PostgresDatabase

_OLD_ORDER = "old-r1aaaaaa-n0"


def _make_mission(name="m1"):
    return api_objects.MissionObjectV1(
        name=name, robot="r1",
        mission_tree=[{"name": "0", "route": {"waypoints": [
            {"x": 1.0, "y": 1.0, "theta": 0.0},
            {"x": 2.0, "y": 2.0, "theta": 0.0}]}, "parent": "root"}],
        status={}, timeout=1000)


def _make_robot():
    published = []
    db = AsyncMock(spec=PostgresDatabase)
    db.update_status = AsyncMock()
    client = MagicMock()

    def _publish(topic, payload, *args, **kwargs):
        published.append((topic.rsplit("/", 1)[-1], json.loads(payload)))
    client.publish = MagicMock(side_effect=_publish)
    srv = MagicMock()
    srv.push_telemetry = False
    srv.mission_ctrl_url = None
    r = Robot("r1", db, client, "prefix", srv)
    r._robot_object = api_objects.RobotObjectV1(name="r1", status={})
    r._robot_object.status.online = True
    return r, published


def _orders(published):
    return [p["orderId"] for topic, p in published if topic == "order"]


def _state(order_id, executing=False, last_node_id="", last_node_seq=0, driving=False):
    state = types.VDA5050State(
        headerId=0, timestamp="", orderId=order_id, nodeStates=[], edgeStates=[],
        actionStates=[], errors=[], batteryState=None, agvPosition=None, velocity=None,
        lastNodeId=last_node_id, lastNodeSequenceId=last_node_seq, driving=driving)
    if executing:
        state.nodeStates = [types.VDA5050NodeState(nodeId=f"{order_id}-s2", sequenceId=2)]
    return state


async def _start(r, mission, robot_order=_OLD_ORDER, driving=False):
    """Dispatch `mission` while the robot's last state shows it executing `robot_order`."""
    r._robot_order_id = robot_order
    r._robot_executing = True
    r._robot_driving = driving
    r._missions[mission.name] = mission
    await r._try_start_mission()


@pytest.mark.unit
async def test_new_order_waits_for_the_previous_one_and_goes_out_when_it_is_done():
    r, published = _make_robot()
    mission = _make_mission()
    await _start(r, mission)

    assert _orders(published) == []
    assert r._pending_send == server.SEND_NODE

    # Still executing: held again, no mismatch counted.
    await r._on_client_message(_state(_OLD_ORDER, executing=True))
    assert _orders(published) == []
    assert r._order_mismatch_count == 0

    # The robot's state shows the old order done.
    await r._on_client_message(_state(_OLD_ORDER, last_node_id=f"{_OLD_ORDER}-s2",
                                      last_node_seq=2))
    assert _orders(published) == [f"m1-r{mission.status.run_id}-n0"]
    assert r._pending_send is None


@pytest.mark.unit
async def test_idle_robot_gets_the_order_at_once():
    r, published = _make_robot()
    mission = _make_mission()
    r._missions[mission.name] = mission
    await r._try_start_mission()

    assert _orders(published) == [f"m1-r{mission.status.run_id}-n0"]


@pytest.mark.unit
async def test_resend_of_the_same_order_is_not_held():
    r, published = _make_robot()
    mission = _make_mission()
    r._missions[mission.name] = mission
    await r._try_start_mission()
    order = _orders(published)[0]

    r._robot_order_id, r._robot_executing = order, True
    await r._send_order()

    assert _orders(published) == [order, order]


@pytest.mark.unit
async def test_robots_own_offline_order_does_not_hold_a_mission():
    r, published = _make_robot()
    mission = _make_mission()
    await _start(r, mission, robot_order="offline-patrol-3")

    assert _orders(published) == [f"m1-r{mission.status.run_id}-n0"]


@pytest.mark.unit
async def test_new_revision_chosen_by_the_dispatcher_is_not_held():
    r, published = _make_robot()
    mission = _make_mission()
    r._missions[mission.name] = mission
    await r._try_start_mission()
    run_id = mission.status.run_id

    # The robot dropped the order ("canceled") but still lists its nodes.
    r._robot_order_id, r._robot_executing = f"m1-r{run_id}-n0", True
    r._pending_send = server.NEW_REVISION
    await r._flush_pending_send()

    assert _orders(published)[-1] == f"m1-r{run_id}v1-n0"


@pytest.mark.unit
async def test_robot_stuck_on_the_old_order_is_cleared_once_after_the_dwell():
    r, published = _make_robot()
    mission = _make_mission()
    await _start(r, mission)
    assert r._order_gate_since is not None

    await r._send_order()                       # within the dwell: still just held
    assert not r._has_outstanding_cancel()

    r._order_gate_since -= Robot.ORDER_CLEAR_DWELL_S + 1
    await r._send_order()

    assert r._has_outstanding_cancel()
    assert [p for p in r._cancel_purposes.values() if p[0] is CancelPurpose.CLEAR]
    assert _orders(published) == []
    cancels = len(r._cancel_purposes)

    r._order_gate_since -= Robot.ORDER_CLEAR_DWELL_S + 1
    await r._send_order()                       # one cancel at a time
    assert len(r._cancel_purposes) == cancels
    assert _orders(published) == []
    assert r._pending_send == server.SEND_NODE  # owed until the cancel resolves


@pytest.mark.unit
async def test_robot_that_is_driving_is_not_cleared():
    r, published = _make_robot()
    mission = _make_mission()
    await _start(r, mission, driving=True)

    r._order_gate_since -= Robot.ORDER_CLEAR_DWELL_S + 1
    await r._send_order()

    assert not r._has_outstanding_cancel()
    assert _orders(published) == []
    # The dwell restarted: it needs a stretch of not driving.
    r._robot_driving = False
    await r._send_order()
    assert not r._has_outstanding_cancel()


@pytest.mark.unit
async def test_completion_by_flag_before_the_final_node_is_recorded_once():
    r, _ = _make_robot()
    mission = _make_mission()
    r._missions[mission.name] = mission
    await r._try_start_mission()
    r._record = MagicMock()
    order = r._sent_order.orderId
    node = mission.mission_tree[0]

    behind = _state(order, last_node_id=f"{order}-s0", last_node_seq=0)
    r._note_completion_without_final_node(behind, node)
    r._note_completion_without_final_node(behind, node)

    assert r._record.call_count == 1
    assert r._record.call_args[0][0] == "completed_without_final_node"


@pytest.mark.unit
async def test_completion_at_the_final_node_is_not_recorded():
    r, _ = _make_robot()
    mission = _make_mission()
    r._missions[mission.name] = mission
    await r._try_start_mission()
    r._record = MagicMock()
    order = r._sent_order.orderId
    final = max(n.sequenceId for n in r._sent_order.nodes)

    r._note_completion_without_final_node(
        _state(order, last_node_id=f"{order}-s{final}", last_node_seq=final),
        mission.mission_tree[0])

    r._record.assert_not_called()


@pytest.mark.unit
async def test_order_the_robot_kept_after_an_unanswered_cancel_does_not_hold_the_next():
    r, published = _make_robot()
    mission = _make_mission()
    await _start(r, mission)
    assert _orders(published) == []

    action = types.VDA5050Action(
        actionType=types.VDA5050InstantActionType.CANCEL_ORDER, actionId="c1")
    r._cancel_resolved(action, abandoned=True)      # the robot never answered it
    await r._send_order()

    assert _orders(published) == [f"m1-r{mission.status.run_id}-n0"]


@pytest.mark.unit
async def test_order_the_robot_reported_failed_or_cancelled_does_not_hold_the_next():
    r, published = _make_robot()
    mission = _make_mission()
    await _start(r, mission)
    assert _orders(published) == []

    r._dead_order_ids.add(_OLD_ORDER)               # "canceled", but the nodes are listed
    await r._send_order()

    assert _orders(published) == [f"m1-r{mission.status.run_id}-n0"]


@pytest.mark.unit
async def test_released_orders_are_forgotten_once_the_robot_lists_no_nodes():
    r, _ = _make_robot()
    mission = _make_mission()
    await _start(r, mission)
    r._released_order_ids.add(_OLD_ORDER)

    await r._on_client_message(_state(_OLD_ORDER, executing=True))
    assert r._released_order_ids == {_OLD_ORDER}
    await r._on_client_message(_state(_OLD_ORDER, last_node_id=f"{_OLD_ORDER}-s2",
                                      last_node_seq=2))
    assert not r._released_order_ids


@pytest.mark.unit
async def test_completion_with_the_previous_orders_last_node_is_recorded():
    """lastNodeId lags orderId: the previous order's node at the same sequence number is
    not the new order's final node."""
    r, _ = _make_robot()
    mission = _make_mission()
    r._missions[mission.name] = mission
    await r._try_start_mission()
    r._record = MagicMock()
    order = r._sent_order.orderId
    final = max(n.sequenceId for n in r._sent_order.nodes)

    r._note_completion_without_final_node(
        _state(order, last_node_id=f"m1-r{mission.status.run_id}-n9-s{final}",
               last_node_seq=final), mission.mission_tree[0])

    assert r._record.call_count == 1
