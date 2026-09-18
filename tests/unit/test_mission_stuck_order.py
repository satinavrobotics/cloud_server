"""Unit tests for the guards against a mission that can never make progress.

These cover two related field incidents in which a mission could never make progress.

The first: a robot dropped a dispatched order (its VDA5050 client threw while
cancelling the previous Nav2 goal), so it kept publishing state with the *previous*
order's orderId. The dispatcher resent forever, the mission stayed RUNNING, and the
operator saw "ON_TASK" against a robot sitting still.

The second: two missions queued back to back. The first completed, but the database
watcher echoed a snapshot of it written before that terminal status landed, so it was
re-queued as new, re-dispatched on top of the second, and failed with "Robot did not
accept the dispatched order" — while the second was marked COMPLETED 55ms after
dispatch, off the previous route's lastNodeSequenceId.

Covers:
- handle_instant_action(): an instant action the robot never reports FINISHED is
  abandoned after MAX_INSTANT_ACTION_RESENDS instead of being resent indefinitely.
- _on_client_message(): a robot that never adopts our order fails the mission after
  MAX_ORDER_MISMATCHES state messages rather than spinning silently, and the counter
  resets once the robot does adopt it.
- _on_mission_change(): a mission already in a terminal state is not re-queued when
  its own status write echoes back from the database watcher, nor when that echo
  predates the terminal write and still reads RUNNING.
- update_mission_node_state(): a lastNodeId belonging to a previous mission is not
  read as progress through the current route.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

import cloud_common.objects as api_objects
import cloud_common.objects.mission as mission_object
import packages.controllers.mission.vda5050_types as types
from packages.controllers.mission.server import Robot
from packages.database.postgres import PostgresDatabase


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_mission(name="m1", robot="r1", state=None):
    mission = api_objects.MissionObjectV1(
        name=name, robot=robot,
        mission_tree=[{"name": "0", "route": {"waypoints": [
            {"x": 1.0, "y": 1.0, "theta": 0.0},
            {"x": 2.0, "y": 2.0, "theta": 0.0}]}, "parent": "root"}],
        status={}, timeout=1000)
    if state is not None:
        mission.status.state = state
    return mission


def _make_robot():
    db = AsyncMock(spec=PostgresDatabase)
    db.update_status = AsyncMock()
    client = MagicMock()
    server = MagicMock()
    server.push_telemetry = False
    server.mission_ctrl_url = None
    r = Robot("r1", db, client, "prefix", server)
    r._robot_object = api_objects.RobotObjectV1(name="r1", status={})
    r._robot_object.status.online = True
    return r, db


def _build_state(order_id="", action_states=None, last_node_id="", last_node_seq=0):
    return types.VDA5050State(
        headerId=0, timestamp="", orderId=order_id, nodeStates=[], edgeStates=[],
        actionStates=action_states or [], errors=[], batteryState=None,
        agvPosition=None, velocity=None,
        lastNodeId=last_node_id, lastNodeSequenceId=last_node_seq)


# ---------------------------------------------------------------------------
# handle_instant_action: bounded resends
# ---------------------------------------------------------------------------
@pytest.mark.unit
async def test_unacknowledged_instant_action_is_abandoned_after_max_resends():
    """The robot never echoes the action, so it must not be resent forever."""
    r, _ = _make_robot()
    r._send_instant_action = AsyncMock()
    action = types.VDA5050Action(
        actionType=types.VDA5050InstantActionType.CANCEL_ORDER, actionId="a1")
    r._current_instant_actions["a1"] = action

    # Every state message omits the action from actionStates, as a robot that
    # rejected it outright would.
    for _ in range(Robot.MAX_INSTANT_ACTION_RESENDS + 5):
        await r.handle_instant_action(_build_state())

    assert r._send_instant_action.await_count == Robot.MAX_INSTANT_ACTION_RESENDS
    assert "a1" not in r._current_instant_actions
    assert "a1" not in r._instant_action_resends


@pytest.mark.unit
async def test_acknowledged_instant_action_clears_resend_counter():
    r, _ = _make_robot()
    r._send_instant_action = AsyncMock()
    action = types.VDA5050Action(
        actionType=types.VDA5050InstantActionType.CANCEL_ORDER, actionId="a1")
    r._current_instant_actions["a1"] = action

    await r.handle_instant_action(_build_state())
    assert r._instant_action_resends["a1"] == 1

    finished = types.VDA5050ActionState(
        actionId="a1", actionType=types.VDA5050InstantActionType.CANCEL_ORDER,
        actionStatus=types.VDA5050ActionStatus.FINISHED)
    result = await r.handle_instant_action(_build_state(action_states=[finished]))

    assert len(result) == 1
    assert "a1" not in r._current_instant_actions
    assert "a1" not in r._instant_action_resends


@pytest.mark.unit
async def test_failed_cancel_order_is_terminal_and_counts_as_cancelled():
    """The robot answers cancelOrder with FAILED when it has no order running
    (noOrderToCancel). Nothing of the mission is left on the robot, which is what
    a cancel wanted -- and a FAILED action never changes state again, so keeping
    it outstanding only blocked every later cancel via _has_outstanding_cancel()."""
    r, _ = _make_robot()
    r._send_instant_action = AsyncMock()
    action = types.VDA5050Action(
        actionType=types.VDA5050InstantActionType.CANCEL_ORDER, actionId="a1")
    r._current_instant_actions["a1"] = action
    assert r._has_outstanding_cancel()

    failed = types.VDA5050ActionState(
        actionId="a1", actionType=types.VDA5050InstantActionType.CANCEL_ORDER,
        actionStatus=types.VDA5050ActionStatus.FAILED)
    result = await r.handle_instant_action(_build_state(action_states=[failed]))

    assert [a.actionId for a in result] == ["a1"]
    assert not r._has_outstanding_cancel()
    r._send_instant_action.assert_not_awaited()


@pytest.mark.unit
async def test_failed_non_cancel_instant_action_is_dropped_but_not_reported_finished():
    r, _ = _make_robot()
    r._send_instant_action = AsyncMock()
    action = types.VDA5050Action(
        actionType=types.VDA5050InstantActionType.FACTSHEET_REQUEST, actionId="f1")
    r._current_instant_actions["f1"] = action

    failed = types.VDA5050ActionState(
        actionId="f1", actionType=types.VDA5050InstantActionType.FACTSHEET_REQUEST,
        actionStatus=types.VDA5050ActionStatus.FAILED)
    result = await r.handle_instant_action(_build_state(action_states=[failed]))

    assert result == []
    assert "f1" not in r._current_instant_actions


# ---------------------------------------------------------------------------
# _on_mission_change: one cancelOrder at a time
# ---------------------------------------------------------------------------
@pytest.mark.unit
async def test_running_mission_cancel_is_not_reissued_on_every_change_event():
    """Every status write we make echoes back as a change event for the running
    mission; with needs_canceled set, each echo used to mint a brand-new cancelOrder
    (23k+ distinct cancel actions observed for one mission)."""
    r, _ = _make_robot()
    r._send_instant_action = AsyncMock()
    mission = _make_mission(name="m1")
    mission.needs_canceled = True
    r._missions["m1"] = mission
    r._current_mission = mission

    def echo():
        # The watcher echo of a mission the operator cancelled via the API.
        m = _make_mission(name="m1")
        m.needs_canceled = True
        return m

    for _ in range(5):
        await r._on_mission_change(echo())

    r._send_instant_action.assert_awaited_once()
    cancels = [a for a in r._current_instant_actions.values()
               if a.actionType == types.VDA5050InstantActionType.CANCEL_ORDER]
    assert len(cancels) == 1

    # Once the robot has answered that cancel, a later cancel request is sent again.
    done = types.VDA5050ActionState(
        actionId=cancels[0].actionId, actionType=types.VDA5050InstantActionType.CANCEL_ORDER,
        actionStatus=types.VDA5050ActionStatus.FINISHED)
    await r.handle_instant_action(_build_state(action_states=[done]))
    await r._on_mission_change(echo())
    assert r._send_instant_action.await_count == 2


# ---------------------------------------------------------------------------
# _on_mission_change: terminal missions are not re-queued
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.parametrize("state", [
    mission_object.MissionStateV1.COMPLETED,
    mission_object.MissionStateV1.FAILED,
    mission_object.MissionStateV1.CANCELED,
])
async def test_terminal_mission_is_not_requeued(state):
    """Our own terminal-status write echoes back through the watcher; ignore it."""
    r, _ = _make_robot()
    await r._on_mission_change(_make_mission(name="done", state=state))

    assert "done" not in r._missions
    assert r._current_mission is None


@pytest.mark.unit
async def test_pending_mission_is_still_queued():
    """The terminal-state guard must not block genuinely new work."""
    r, _ = _make_robot()
    r._try_start_mission = AsyncMock()
    await r._on_mission_change(_make_mission(name="fresh"))

    assert "fresh" in r._missions
    r._try_start_mission.assert_awaited_once()


@pytest.mark.unit
async def test_finished_mission_is_not_requeued_by_a_stale_echo():
    """The echo can predate our terminal write, so its own state is not evidence.

    The field incident: Testfirst COMPLETED, then the watcher delivered a snapshot
    of it still marked RUNNING. state.done was False, so it was re-queued as "a new
    mission", re-dispatched on top of the running Testsecond, and failed with
    "Robot did not accept the dispatched order".
    """
    r, _ = _make_robot()
    r._try_start_mission = AsyncMock()
    mission = _make_mission(name="done")
    r._missions["done"] = mission
    r._current_mission = mission

    await r.get_next_mission()
    assert "done" in r._finished_missions

    # The stale echo: still RUNNING, because it was written before we finished.
    await r._on_mission_change(
        _make_mission(name="done", state=mission_object.MissionStateV1.RUNNING))

    assert "done" not in r._missions


@pytest.mark.unit
async def test_freshly_created_mission_is_queued_even_before_the_delete_echo_arrives():
    """A name reused via delete-then-recreate must not depend on watcher ordering.

    The 2026-09-15 field incident: an operator deleted a completed mission named
    "Test" and immediately re-created a new one under the same name. The delete's
    own lifecycle-change event (which clears _finished_missions -- see the
    recycled-name test below) and the re-create's PENDING echo are two
    independent writes with no ordering guarantee between them; the re-create's
    echo arrived first here, got silently swallowed by _finished_missions, and
    the mission sat PENDING forever with no error surfaced anywhere. Deleting it
    again was the only fix, and only by luck of timing.

    A message this fresh -- PENDING, never dispatched (no start_timestamp) -- is
    unambiguously a new mission, not the stale echo _finished_missions exists to
    catch (see test_finished_mission_is_not_requeued_by_a_stale_echo above: that
    echo always carries a start_timestamp, since the mission had to reach
    RUNNING to ever become "finished"), so it must be queued immediately
    regardless of whether the delete's own event has arrived yet.
    """
    r, _ = _make_robot()
    r._try_start_mission = AsyncMock()
    r._remember_finished("Test")

    # The re-create's echo, arriving BEFORE any delete/lifecycle event for the
    # old "Test" -- deliberately no PENDING_DELETE step in this test.
    recreated = _make_mission(name="Test")
    await r._on_mission_change(recreated)

    assert "Test" in r._missions
    assert "Test" not in r._finished_missions
    r._try_start_mission.assert_awaited_once()


@pytest.mark.unit
async def test_deleted_mission_is_forgotten_so_its_name_can_be_reused():
    """Otherwise a mission later created under a reused name looks like an echo."""
    r, _ = _make_robot()
    r._try_start_mission = AsyncMock()
    r._remember_finished("recycled")

    deleted = _make_mission(name="recycled")
    deleted.lifecycle = api_objects.object.ObjectLifecycleV1.PENDING_DELETE
    await r._on_mission_change(deleted)

    assert "recycled" not in r._finished_missions
    # The delete itself is not work: it must not land in the queue on its way past.
    assert "recycled" not in r._missions

    # With the name forgotten, a genuinely new mission under it is queued rather
    # than suppressed as a re-queue of the one we already ran.
    await r._on_mission_change(_make_mission(name="recycled"))
    assert "recycled" in r._missions


@pytest.mark.unit
async def test_finished_mission_tracking_is_bounded():
    r, _ = _make_robot()
    for i in range(Robot.MAX_FINISHED_MISSIONS_TRACKED + 10):
        r._remember_finished(f"m{i}")

    assert len(r._finished_missions) == Robot.MAX_FINISHED_MISSIONS_TRACKED
    # Oldest evicted first, newest retained.
    assert "m0" not in r._finished_missions
    assert f"m{Robot.MAX_FINISHED_MISSIONS_TRACKED + 9}" in r._finished_missions


# ---------------------------------------------------------------------------
# _on_client_message: bounded order mismatch
# ---------------------------------------------------------------------------
def _arm_running_mission(r):
    """Put the robot in the state the mismatch guard runs in."""
    mission = _make_mission(name="m1")
    mission.status.state = mission_object.MissionStateV1.RUNNING
    r._missions["m1"] = mission
    r._current_mission = mission
    r._current_behavior_tree = MagicMock()
    r._send_order = AsyncMock()
    return mission


@pytest.mark.unit
async def test_order_mismatch_fails_mission_after_max_attempts():
    r, _ = _make_robot()
    mission = _arm_running_mission(r)

    # The robot keeps reporting the *previous* mission's order, never ours.
    for _ in range(Robot.MAX_ORDER_MISMATCHES):
        await r._on_client_message(_build_state(order_id="previous-n1"))

    assert mission.status.state == mission_object.MissionStateV1.FAILED
    assert "did not accept" in (mission.status.failure_reason or "")
    # It stopped resending rather than continuing once it gave up.
    assert r._send_order.await_count == Robot.MAX_ORDER_MISMATCHES - 1


@pytest.mark.unit
async def test_order_mismatch_below_threshold_keeps_resending():
    r, _ = _make_robot()
    mission = _arm_running_mission(r)

    for _ in range(3):
        await r._on_client_message(_build_state(order_id="previous-n1"))

    assert mission.status.state == mission_object.MissionStateV1.RUNNING
    assert r._send_order.await_count == 3
    assert r._order_mismatch_count == 3


@pytest.mark.unit
async def test_matching_order_resets_mismatch_count():
    """A transient mismatch (the normal case) must not accumulate toward the cap."""
    r, _ = _make_robot()
    _arm_running_mission(r)
    r.update_mission_state = MagicMock()

    await r._on_client_message(_build_state(order_id="previous-n1"))
    assert r._order_mismatch_count == 1

    await r._on_client_message(_build_state(order_id="m1-n0"))
    assert r._order_mismatch_count == 0


# ---------------------------------------------------------------------------
# update_mission_node_state: a previous mission's lastNodeId is not progress
# ---------------------------------------------------------------------------
# _make_mission's route has 2 waypoints, so the route-complete test is
# current_order_node_id == route.size * 2 + 2 == 6, i.e. lastNodeSequenceId 4.
_TERMINAL_SEQ_ID = 4


@pytest.mark.unit
async def test_previous_missions_last_node_does_not_complete_a_fresh_mission():
    """The robot echoes our new orderId before it has moved off the old route.

    Its lastNodeId/lastNodeSequenceId still describe the *previous* mission's final
    node; read as progress through this route they satisfy the completion test and
    the new mission completes on its first state message without the robot moving.
    """
    r, _ = _make_robot()
    mission = _arm_running_mission(r)

    node_state = r.update_mission_node_state(
        _build_state(order_id="m1-n0",
                     last_node_id="previous-n0-s4", last_node_seq=_TERMINAL_SEQ_ID),
        [])

    assert node_state != mission_object.MissionStateV1.COMPLETED
    assert mission.status.state == mission_object.MissionStateV1.RUNNING


@pytest.mark.unit
async def test_own_last_node_still_completes_the_mission():
    """The guard must not stop a genuine completion of the current mission."""
    r, _ = _make_robot()
    _arm_running_mission(r)

    node_state = r.update_mission_node_state(
        _build_state(order_id="m1-n0",
                     last_node_id="m1-n0-s4", last_node_seq=_TERMINAL_SEQ_ID),
        [])

    assert node_state == mission_object.MissionStateV1.COMPLETED


@pytest.mark.unit
async def test_stale_sequence_id_under_own_node_id_does_not_complete_mission():
    """The robot reset lastNodeId to the new order's node 0 but left
    lastNodeSequenceId at the previous route's terminal value (2026-09-14:
    lastNodeId=Test2-n1-s0, lastNodeSequenceId=6). The prefix guard passes, so
    the id's own "-s0" suffix must override the stale sequence id.
    """
    r, _ = _make_robot()
    mission = _arm_running_mission(r)

    node_state = r.update_mission_node_state(
        _build_state(order_id="m1-n0",
                     last_node_id="m1-n0-s0", last_node_seq=_TERMINAL_SEQ_ID),
        [])

    assert node_state != mission_object.MissionStateV1.COMPLETED
    assert mission.status.state == mission_object.MissionStateV1.RUNNING
    assert r.last_node_seq_id == 0


@pytest.mark.unit
def test_sequence_id_from_node_id():
    assert Robot._sequence_id_from_node_id("m1-n0-s4") == 4
    assert Robot._sequence_id_from_node_id("Test2-n1-s0") == 0
    assert Robot._sequence_id_from_node_id("") is None
    assert Robot._sequence_id_from_node_id("free-form") is None


@pytest.mark.unit
async def test_stale_last_node_does_not_advance_the_waypoint_counter():
    r, _ = _make_robot()
    mission = _arm_running_mission(r)

    r.update_mission_node_state(
        _build_state(order_id="m1-n0",
                     last_node_id="previous-n0-s2", last_node_seq=2),
        [])

    assert mission.status.task_status == {}
    # The saved sequence id tracks this mission, so the next genuine reading of
    # sequence 2 still registers as forward progress rather than being swallowed.
    assert r.last_node_seq_id == 0


@pytest.mark.unit
def test_task_status_reflects_the_reached_index_even_on_a_non_zero_first_reach():
    """Regression test: task_status is assigned directly from the computed `idx`
    rather than a separate 0-then-increment counter. The old counter form was wrong
    whenever the very *first* update_mission_node_state call for a mission reported
    reaching a waypoint other than index 0 (e.g. a resumed/rerouted order) -- it
    always initialized to 0 regardless of the real idx. sati-client's
    utils/missionRouteProgress.ts now reads this value as the authoritative
    "which waypoint" signal, so it must be correct on the very first reach, not
    just in steady-state increments."""
    r, _ = _make_robot()
    mission = _arm_running_mission(r)  # mission_tree[0], node name "0", 2 waypoints
    # task_status only updates for "user-defined" waypoints (allowedDeviationXY == 0,
    # per update_mission_node_state's own comment) -- _make_mission's default (0.1)
    # would skip that branch entirely, so set it explicitly for this test.
    for wp in mission.mission_tree[0].route.waypoints:
        wp.allowedDeviationXY = 0.0

    # last_node_seq_id=4 -> idx = 4 // 2 - 1 = 1, the *second* waypoint, reported as
    # the very first progress this mission has ever registered.
    r.update_mission_node_state(
        _build_state(order_id="m1-n0", last_node_id="m1-n0-s4", last_node_seq=4), [])

    assert mission.status.task_status == {"0": 1}
