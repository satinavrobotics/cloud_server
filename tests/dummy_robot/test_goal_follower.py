"""Unit tests for the dummy robot's goal-following mode (no MQTT broker).

Orders are built with the dispatcher's own VDA5050Order.from_route/from_move/
from_action, so the ids and sequence numbering are exactly what dispatch sends.
"""
import pytest

import cloud_common.objects as api_objects
from cloud_common.objects import mission as mission_object
from packages.controllers.mission.vda5050_types import vda5050_types as types
from tests.dummy_robot.goal_follower import GoalFollower

pytestmark = pytest.mark.unit

Status = types.VDA5050ActionStatus
PREFIX = "m1-rabcd1234"


def _robot(x=0.0, y=0.0):
    robot = api_objects.RobotObjectV1(name="r1", status={})
    robot.status.pose.x = x
    robot.status.pose.y = y
    return robot


def _route_order(waypoints, idx=0, prefix=PREFIX, x=0.0, y=0.0):
    route = mission_object.MissionRouteNodeV1(
        waypoints=[{"x": wx, "y": wy, "theta": 0.0} for wx, wy in waypoints])
    return types.VDA5050Order.from_route(route, _robot(x, y), prefix, idx)


def _action_order(action_type="scan_area", idx=0, prefix=PREFIX):
    action = mission_object.MissionActionNodeV1(action_type=action_type,
                                                action_parameters={})
    return types.VDA5050Order.from_action(action, _robot(), prefix, idx)


def _run(follower, ticks, dt=1.0):
    states = []
    for _ in range(ticks):
        follower.step(dt)
        states.append(follower.to_state(header_id=len(states)))
    return states


def _run_until_idle(follower, dt=1.0, limit=200):
    for _ in range(limit):
        follower.step(dt)
        if not follower.has_active_order:
            return follower.to_state(header_id=0)
    raise AssertionError("order never finished")


def test_initial_state_is_idle_without_order():
    state = GoalFollower().to_state(header_id=0)
    assert state.orderId == ""
    assert state.nodeStates == [] and state.edgeStates == []
    assert state.actionStates == []
    assert state.driving is False


def test_order_acceptance_reaches_start_node_and_reports_the_rest():
    follower = GoalFollower(speed=1.0)
    order = _route_order([(3.0, 0.0), (3.0, 4.0)])
    assert follower.handle_order(order) == "new"
    state = follower.to_state(header_id=0)
    assert state.orderId == f"{PREFIX}-n0"
    assert state.orderUpdateId == 0
    # The start node (the robot's own pose, seq 0) is reached on acceptance.
    assert state.lastNodeId == f"{PREFIX}-n0-s0"
    assert state.lastNodeSequenceId == 0
    assert [n.sequenceId for n in state.nodeStates] == [2, 4]
    assert [e.sequenceId for e in state.edgeStates] == [1, 3]


def test_sequential_progress_shrinks_node_and_edge_states():
    follower = GoalFollower(speed=1.0)
    follower.handle_order(_route_order([(3.0, 0.0), (3.0, 4.0)]))
    states = _run(follower, 3)
    # Driving toward (3, 0) at 1 m/s; reached on the third tick.
    assert states[0].driving is True
    assert states[0].agvPosition.x == pytest.approx(1.0)
    assert states[0].lastNodeSequenceId == 0
    reached = states[2]
    assert (reached.lastNodeId, reached.lastNodeSequenceId) == (f"{PREFIX}-n0-s2", 2)
    assert [n.sequenceId for n in reached.nodeStates] == [4]
    assert [e.sequenceId for e in reached.edgeStates] == [3]
    assert (reached.agvPosition.x, reached.agvPosition.y) == (3.0, 0.0)


def test_finished_state_after_last_node():
    follower = GoalFollower(speed=2.0)
    follower.handle_order(_route_order([(3.0, 0.0), (3.0, 4.0)]))
    state = _run_until_idle(follower)
    assert state.orderId == f"{PREFIX}-n0"
    # What dispatch's update_mission_node_state needs for a route of N waypoints:
    # lastNodeSequenceId == 2N on a node of this order.
    assert (state.lastNodeId, state.lastNodeSequenceId) == (f"{PREFIX}-n0-s4", 4)
    assert state.nodeStates == [] and state.edgeStates == []
    assert state.driving is False
    assert state.velocity.vx == 0.0 and state.velocity.vy == 0.0
    assert (state.agvPosition.x, state.agvPosition.y) == (3.0, 4.0)
    # Further ticks keep it idle and at the goal.
    assert _run(follower, 2)[-1] == follower.to_state(header_id=1)
    assert follower.to_state(header_id=0).driving is False


def test_move_order_finishes_at_sequence_two():
    follower = GoalFollower(speed=1.0)
    move = mission_object.MissionMoveNodeV1(distance=2.0)
    follower.handle_order(types.VDA5050Order.from_move(move, _robot(), PREFIX, 1))
    state = _run_until_idle(follower)
    assert (state.lastNodeId, state.lastNodeSequenceId) == (f"{PREFIX}-n1-s2", 2)
    assert state.agvPosition.x == pytest.approx(2.0)


def test_goal_tolerance_counts_a_nearby_node_as_reached():
    follower = GoalFollower(speed=1.0, goal_tolerance=0.5)
    follower.handle_order(_route_order([(0.4, 0.0)]))
    follower.step(0.01)
    assert follower.last_node_sequence_id == 2


def test_duplicate_order_is_ignored_and_does_not_reset_progress():
    follower = GoalFollower(speed=1.0)
    order = _route_order([(3.0, 0.0), (6.0, 0.0)])
    follower.handle_order(order)
    _run(follower, 3)
    assert follower.handle_order(order.copy(deep=True)) == "duplicate"
    assert follower.last_node_sequence_id == 2
    assert [n.sequenceId for n in follower.pending_nodes] == [4]


def test_order_update_extends_the_order_from_the_last_node():
    follower = GoalFollower(speed=1.0)
    order = _route_order([(3.0, 0.0)])
    follower.handle_order(order)
    _run_until_idle(follower)
    assert follower.last_node_sequence_id == 2

    update = _route_order([(3.0, 0.0), (3.0, 2.0)])  # same orderId, stitched at s2
    update.orderUpdateId = 1
    assert follower.handle_order(update) == "update"
    state = follower.to_state(header_id=0)
    assert state.orderUpdateId == 1
    assert state.lastNodeSequenceId == 2
    assert [n.sequenceId for n in state.nodeStates] == [4]
    state = _run_until_idle(follower)
    assert (state.lastNodeId, state.lastNodeSequenceId) == (f"{PREFIX}-n0-s4", 4)
    assert state.nodeStates == []


def test_stale_order_update_is_rejected():
    follower = GoalFollower()
    order = _route_order([(3.0, 0.0)])
    order.orderUpdateId = 2
    follower.handle_order(order)
    stale = _route_order([(9.0, 9.0)])
    stale.orderUpdateId = 1
    assert follower.handle_order(stale) == "rejected"
    assert follower.order_update_id == 2
    assert follower.pending_nodes[0].nodePosition.x == 3.0


def test_new_order_id_replaces_the_previous_order():
    follower = GoalFollower(speed=1.0)
    follower.handle_order(_route_order([(3.0, 0.0)], idx=0))
    _run_until_idle(follower)
    # Next mission node: new orderId, start node at the current pose.
    follower.handle_order(_route_order([(3.0, 2.0)], idx=1, x=3.0, y=0.0))
    state = follower.to_state(header_id=0)
    assert state.orderId == f"{PREFIX}-n1"
    assert (state.lastNodeId, state.lastNodeSequenceId) == (f"{PREFIX}-n1-s0", 0)
    assert [n.sequenceId for n in state.nodeStates] == [2]


def test_unreleased_horizon_is_not_driven():
    follower = GoalFollower(speed=10.0)
    order = _route_order([(1.0, 0.0), (2.0, 0.0)])
    order.nodes[-1].released = False
    order.edges[-1].released = False
    follower.handle_order(order)
    states = _run(follower, 5)
    assert states[-1].lastNodeSequenceId == 2
    assert [(n.sequenceId, n.released) for n in states[-1].nodeStates] == [(4, False)]
    assert states[-1].driving is False
    assert follower.has_active_order


def test_cancel_order_stops_and_clears_the_order():
    follower = GoalFollower(speed=1.0)
    follower.handle_order(_route_order([(10.0, 0.0)]))
    _run(follower, 2)
    cancel = types.VDA5050Action(actionType="cancelOrder", actionId="c1")
    follower.handle_instant_actions([cancel])
    state = follower.to_state(header_id=0)
    assert state.orderId == f"{PREFIX}-n0"  # the cancelled order's id is kept
    assert state.nodeStates == [] and state.edgeStates == []
    assert state.driving is False
    assert [(s.actionId, s.actionType, s.actionStatus) for s in state.actionStates] == \
        [("c1", "cancelOrder", Status.FINISHED)]
    x = state.agvPosition.x
    assert _run(follower, 3)[-1].agvPosition.x == x  # it stays put

    # A resend of the same cancel is not executed again nor reported twice.
    follower.handle_instant_actions([cancel])
    assert len(follower.to_state(header_id=0).actionStates) == 1


def test_cancel_without_active_order_reports_failed():
    follower = GoalFollower(speed=5.0)
    follower.handle_order(_route_order([(1.0, 0.0)]))
    _run_until_idle(follower)
    follower.handle_instant_actions(
        [types.VDA5050Action(actionType="cancelOrder", actionId="c2")])
    [state] = follower.to_state(header_id=0).actionStates
    assert state.actionStatus == Status.FAILED
    assert state.resultDescription == "noOrderToCancel"


def test_instant_action_states_are_cleared_by_a_new_order():
    follower = GoalFollower()
    follower.handle_instant_actions(
        [types.VDA5050Action(actionType="cancelOrder", actionId="c3")])
    follower.handle_order(_route_order([(1.0, 0.0)]))
    assert follower.to_state(header_id=0).actionStates == []


def test_factsheet_request_is_acknowledged_and_flagged():
    follower = GoalFollower()
    follower.handle_instant_actions(
        [types.VDA5050Action(actionType="factsheetRequest", actionId="f1")])
    assert follower.factsheet_requested
    [state] = follower.to_state(header_id=0).actionStates
    assert state.actionStatus == Status.FINISHED


def test_unknown_instant_actions_are_not_reported():
    follower = GoalFollower()
    follower.handle_instant_actions(
        [types.VDA5050Action(actionType="selfDestruct", actionId="x1")])
    assert follower.to_state(header_id=0).actionStates == []


def test_action_order_goes_waiting_running_finished():
    follower = GoalFollower(action_duration=2.0)
    follower.handle_order(_action_order())
    statuses = [follower.to_state(header_id=0).actionStates[0].actionStatus]
    for state in _run(follower, 4):
        statuses.append(state.actionStates[0].actionStatus)
        assert state.driving is False
    assert statuses == [Status.WAITING, Status.RUNNING, Status.RUNNING,
                        Status.FINISHED, Status.FINISHED]
    state = follower.to_state(header_id=0)
    assert state.actionStates[0].actionId == f"{PREFIX}-n0-s0-n0"
    assert state.actionStates[0].actionType == "scan_area"
    assert state.nodeStates == [] and not follower.has_active_order


def test_order_actions_stay_ahead_of_instant_action_states():
    """Dispatch reads actionStates[0] for an action node and scans instant actions
    from the end, so order actions must come first."""
    follower = GoalFollower(action_duration=5.0)
    follower.handle_order(_action_order())
    follower.step(1.0)
    follower.handle_instant_actions(
        [types.VDA5050Action(actionType="cancelOrder", actionId="c4")])
    states = follower.to_state(header_id=0).actionStates
    assert [s.actionType for s in states] == ["scan_area", "cancelOrder"]
    assert states[0].actionStatus == Status.FAILED  # cancelled while RUNNING
    assert states[1].actionStatus == Status.FINISHED


def test_node_actions_block_driving_until_finished():
    follower = GoalFollower(speed=1.0, action_duration=1.0)
    order = _route_order([(1.0, 0.0), (2.0, 0.0)])
    order.nodes[1].actions = [types.VDA5050Action(actionType="pick_object", actionId="a1")]
    follower.handle_order(order)
    assert follower.to_state(header_id=0).actionStates[0].actionStatus == Status.WAITING
    states = _run(follower, 5)
    # tick 1: reach s2; 2: RUNNING; 3: FINISHED; 4: drive to s4 (reached)
    assert states[0].lastNodeSequenceId == 2
    assert states[0].actionStates[0].actionStatus == Status.WAITING
    assert states[1].actionStates[0].actionStatus == Status.RUNNING
    assert states[1].driving is False and states[1].agvPosition.x == 1.0
    assert states[2].actionStates[0].actionStatus == Status.FINISHED
    assert states[3].lastNodeSequenceId == 4
    assert states[4].nodeStates == [] and not follower.has_active_order
