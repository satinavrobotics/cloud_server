"""Unit tests for repeating, chaining and waiting missions, and for editing a PENDING one.

Covers:
- MissionSpecV1: `repeat` / `then_run`, and the validation of a "wait" action.
- Repeat: a completed mission with passes left runs again *in place* (new run id, no
  idle in between), `repeat=0` goes on until cancelled, and a cancel ends the loop.
- then_run: the finished mission's follower is created as a copy of the named mission,
  and a missing or foreign one never fails the finished mission.
- Multi-node missions: the robot's per-order missionStatus "completed" completes only
  its own node, so the mission goes on to the next one.
- "wait" action nodes: run by the dispatcher (no order to the robot), cancellable.
- PUT /missions/{name}: only a PENDING mission can be edited, the edit is validated
  and node statuses follow the new tree; the dispatcher applies it to a queued mission.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

import cloud_common.objects as api_objects
import cloud_common.objects.common as common
import cloud_common.objects.mission as mission_object
import packages.controllers.mission.vda5050_types as types
from packages.controllers.mission import order_ids
from packages.controllers.mission.server import Robot
from packages.database.postgres import PostgresDatabase

State = mission_object.MissionStateV1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _route(name, parent="root_sequence", n=2):
    return {"name": name, "parent": parent, "route": {"waypoints": [
        {"x": float(i), "y": 1.0, "theta": 0.0} for i in range(n)]}}


def _wait(name, seconds=0.01):
    return {"name": name, "parent": "root_sequence",
            "action": {"action_type": "wait", "action_parameters": {"seconds": seconds}}}


def _tree(*leaves):
    return [{"name": "root_sequence", "parent": "root", "sequence": {}}, *leaves]


def _mission(name="m1", robot="r1", tree=None, **spec):
    return api_objects.MissionObjectV1(
        name=name, robot=robot, mission_tree=tree or _tree(_route("a")),
        status={}, timeout=1000, **spec)


def _make_robot():
    db = AsyncMock(spec=PostgresDatabase)
    db.update_status = AsyncMock()
    db.create_object = AsyncMock()
    client = MagicMock()
    server = MagicMock()
    server.push_telemetry = False
    server.mission_ctrl_url = None
    server.delete_pending_mission = AsyncMock(return_value=False)
    r = Robot("r1", db, client, "prefix", server)
    r._robot_object = api_objects.RobotObjectV1(name="r1", status={})
    r._robot_object.status.online = True
    r._set_robot_idle_after_mission = MagicMock()
    return r, db


async def _start(r, mission):
    r._missions[mission.name] = mission
    await r._try_start_mission()
    assert r._current_mission is mission
    return mission


def _state(order_id, completed=False, last_node_id=""):
    info = [types.VDA5050Info(infoType="missionStatus", infoLevel="INFO", infoDescription="completed")] \
        if completed else []
    return types.VDA5050State(
        headerId=0, timestamp="", orderId=order_id, nodeStates=[], edgeStates=[],
        actionStates=[], errors=[], batteryState=None, agvPosition=None, velocity=None,
        lastNodeId=last_node_id, lastNodeSequenceId=0, informations=info)


def _published_order_ids(r):
    return [order_ids.order_prefix(__import__("json").loads(c.args[1])["orderId"])
            for c in r._mqtt_client.publish.call_args_list]


async def _finish(r):
    """Complete the current mission's pass the way robot feedback would."""
    r._set_mission_state(State.COMPLETED)
    await r.post_mission_completion()


# ---------------------------------------------------------------------------
# Spec validation
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_repeat_and_then_run_defaults_and_round_trip():
    m = _mission()
    assert m.repeat == 1 and m.then_run is None and m.status.passes_completed == 0
    m = _mission(repeat=0, then_run="dock")
    assert api_objects.MissionObjectV1(**{**m.dict()}).repeat == 0
    assert m.spec.then_run == "dock"


@pytest.mark.unit
def test_negative_repeat_is_rejected():
    with pytest.raises(common.ICSUsageError):
        _mission(repeat=-1)


@pytest.mark.unit
@pytest.mark.parametrize("params", [{}, {"seconds": 0}, {"seconds": -3}, {"seconds": "5"},
                                    {"seconds": True}, {"seconds": 3601}])
def test_bad_wait_duration_is_rejected(params):
    with pytest.raises(common.ICSUsageError):
        mission_object.MissionActionNodeV1(action_type="wait", action_parameters=params)


@pytest.mark.unit
@pytest.mark.parametrize("seconds", [0.5, 5, 3600])
def test_good_wait_duration_is_accepted(seconds):
    mission_object.MissionActionNodeV1(action_type="wait",
                                       action_parameters={"seconds": seconds})


@pytest.mark.unit
def test_other_actions_are_not_validated_as_waits():
    mission_object.MissionActionNodeV1(action_type="pick", action_parameters={})


# ---------------------------------------------------------------------------
# Repeat
# ---------------------------------------------------------------------------
@pytest.mark.unit
async def test_repeat_runs_the_same_mission_again_then_completes():
    r, db = _make_robot()
    m = await _start(r, _mission(repeat=3))
    run_ids = [m.status.run_id]

    for expected_passes in (1, 2):
        await _finish(r)
        assert r._current_mission is m
        assert m.status.passes_completed == expected_passes
        assert m.status.state == State.RUNNING
        assert m.status.end_timestamp is None
        assert all(ns.state != State.COMPLETED for ns in m.status.node_status.values())
        run_ids.append(m.status.run_id)

    # Every pass is a new run, so no two of them share an order id.
    assert len(set(run_ids)) == 3
    prefixes = _published_order_ids(r)
    assert len(prefixes) == 3 and len(set(prefixes)) == 3

    await _finish(r)
    assert r._current_mission is None
    assert m.status.state == State.COMPLETED
    assert m.status.passes_completed == 3
    # Between passes the robot never went idle; only the last completion idles it.
    r._set_robot_idle_after_mission.assert_called_once()
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_pass_is_persisted_before_its_first_order_is_sent():
    r, db = _make_robot()
    m = await _start(r, _mission(repeat=2))
    r._mqtt_client.publish.reset_mock()
    db.update_status.reset_mock()
    order = []
    db.update_status.side_effect = lambda *a, **k: order.append("persist")
    r._mqtt_client.publish.side_effect = lambda *a, **k: order.append("send")

    await _finish(r)

    assert "send" in order
    assert order.index("persist") < order.index("send")
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_repeat_zero_loops_until_cancelled():
    r, _ = _make_robot()
    m = await _start(r, _mission(repeat=0, then_run="dock"))

    for _ in range(4):
        await _finish(r)
        assert r._current_mission is m

    m.needs_canceled = True
    r._set_mission_state(State.CANCELED)
    await r.get_next_mission()

    assert r._current_mission is None
    assert m.status.state == State.CANCELED
    r._database.get_object.assert_not_called()      # a cancel also ends the chain
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_completion_after_a_cancel_request_does_not_run_another_pass():
    r, _ = _make_robot()
    m = await _start(r, _mission(repeat=5))
    run_id = m.status.run_id
    m.needs_canceled = True

    await _finish(r)

    assert r._current_mission is None
    assert m.status.run_id == run_id           # no new pass was started
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_failed_pass_does_not_repeat():
    r, _ = _make_robot()
    m = await _start(r, _mission(repeat=3))
    r._set_mission_state(State.FAILED)

    await r.get_next_mission()

    assert r._current_mission is None and m.status.passes_completed == 0
    r._cancel_mission_timeout()


# ---------------------------------------------------------------------------
# then_run
# ---------------------------------------------------------------------------
@pytest.mark.unit
async def test_then_run_creates_a_copy_of_the_named_mission():
    r, db = _make_robot()
    template = _mission(name="dock", tree=_tree(_route("home", n=3)), then_run="dock2",
                        repeat=2)
    template.status.state = State.COMPLETED           # a mission that already ran
    db.get_object = AsyncMock(return_value=template)
    await _start(r, _mission(then_run="dock"))

    await _finish(r)

    db.get_object.assert_awaited_once_with(api_objects.MissionObjectV1, "dock")
    created = db.create_object.await_args.args[0]
    assert created.name.startswith("dock-run-") and created.name != "dock"
    assert created.robot == "r1"
    assert created.status.state == State.PENDING
    assert created.status.run_id is None and created.status.order_rev == 0
    assert created.mission_tree == template.mission_tree
    assert (created.repeat, created.then_run) == (2, "dock2")
    assert created.lifecycle is api_objects.object.ObjectLifecycleV1.ALIVE
    assert r._current_mission is None


@pytest.mark.unit
async def test_then_run_waits_for_the_last_pass():
    r, db = _make_robot()
    db.get_object = AsyncMock(return_value=_mission(name="dock"))
    await _start(r, _mission(repeat=2, then_run="dock"))

    await _finish(r)
    db.create_object.assert_not_awaited()
    await _finish(r)
    db.create_object.assert_awaited_once()
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_then_run_skips_a_missing_mission_without_failing():
    r, db = _make_robot()
    db.get_object = AsyncMock(side_effect=HTTPException(404, "not found"))
    m = await _start(r, _mission(then_run="ghost"))

    await _finish(r)

    db.create_object.assert_not_awaited()
    assert m.status.state == State.COMPLETED and r._current_mission is None


@pytest.mark.unit
async def test_then_run_skips_a_mission_of_another_robot():
    r, db = _make_robot()
    db.get_object = AsyncMock(return_value=_mission(name="dock", robot="other"))
    await _start(r, _mission(then_run="dock"))

    await _finish(r)

    db.create_object.assert_not_awaited()


@pytest.mark.unit
async def test_the_same_completion_chains_only_once():
    r, db = _make_robot()
    db.get_object = AsyncMock(return_value=_mission(name="dock"))
    m = _mission(then_run="dock")

    await r._chain_then_run(m)
    await r._chain_then_run(m)

    db.create_object.assert_awaited_once()


# ---------------------------------------------------------------------------
# Multi-node missions
# ---------------------------------------------------------------------------
@pytest.mark.unit
async def test_completed_status_advances_to_the_next_node_instead_of_completing():
    r, _ = _make_robot()
    m = await _start(r, _mission(tree=_tree(_route("a"), _route("b"))))
    prefix = order_ids.run_prefix("m1", m.status.run_id, m.status.order_rev)
    r._mqtt_client.publish.reset_mock()

    await r._on_client_message(
        _state(f"{prefix}-n1", completed=True, last_node_id=f"{prefix}-n1-s4"))

    assert m.status.node_status["a"].state == State.COMPLETED
    assert m.status.state == State.RUNNING
    assert r._current_behavior_tree.current_node.name == "b"
    # ... and the next node's order went out.
    order = __import__("json").loads(r._mqtt_client.publish.call_args.args[1])
    assert order["orderId"] == f"{prefix}-n2"
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_completed_status_of_the_last_node_completes_the_mission():
    r, _ = _make_robot()
    m = await _start(r, _mission(tree=_tree(_route("a"), _route("b"))))
    prefix = order_ids.run_prefix("m1", m.status.run_id, m.status.order_rev)
    await r._on_client_message(
        _state(f"{prefix}-n1", completed=True, last_node_id=f"{prefix}-n1-s4"))

    await r._on_client_message(
        _state(f"{prefix}-n2", completed=True, last_node_id=f"{prefix}-n2-s4"))

    assert m.status.state == State.COMPLETED and r._current_mission is None


@pytest.mark.unit
async def test_single_node_mission_still_completes_on_completed_status():
    r, _ = _make_robot()
    m = await _start(r, _mission())
    prefix = order_ids.run_prefix("m1", m.status.run_id, m.status.order_rev)

    await r._on_client_message(_state(f"{prefix}-n1", completed=True))

    assert m.status.state == State.COMPLETED and r._current_mission is None


@pytest.mark.unit
async def test_stale_completed_status_does_not_complete_a_fresh_route_of_a_multi_node_mission():
    """The previous order's "completed" is still on the robot's state right after it
    accepts the next order; the lastNodeId of the previous order gives it away."""
    r, _ = _make_robot()
    m = await _start(r, _mission(tree=_tree(_route("a"), _route("b"))))
    prefix = order_ids.run_prefix("m1", m.status.run_id, m.status.order_rev)
    await r._on_client_message(
        _state(f"{prefix}-n1", completed=True, last_node_id=f"{prefix}-n1-s4"))

    await r._on_client_message(
        _state(f"{prefix}-n2", completed=True, last_node_id="elsewhere-n0-s2"))

    assert m.status.node_status["b"].state != State.COMPLETED
    assert m.status.state == State.RUNNING
    r._cancel_mission_timeout()


# ---------------------------------------------------------------------------
# Wait action
# ---------------------------------------------------------------------------
@pytest.mark.unit
async def test_wait_node_sends_no_order_then_moves_on_to_the_next_node():
    r, _ = _make_robot()
    m = await _start(r, _mission(tree=_tree(_wait("pause", 0.02), _route("b"))))

    # The mission started on the wait: nothing went to the robot.
    r._mqtt_client.publish.assert_not_called()
    assert m.status.node_status["pause"].state == State.RUNNING

    await asyncio.sleep(0.15)                  # the robot's own loop handles the timer

    assert m.status.node_status["pause"].state == State.COMPLETED
    assert r._current_behavior_tree.current_node.name == "b"
    r._mqtt_client.publish.assert_called_once()   # only the route's order
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_wait_as_last_node_completes_the_mission():
    r, _ = _make_robot()
    m = await _start(r, _mission(tree=_tree(_route("a"), _wait("pause", 0.02))))
    prefix = order_ids.run_prefix("m1", m.status.run_id, m.status.order_rev)

    await r._on_client_message(
        _state(f"{prefix}-n1", completed=True, last_node_id=f"{prefix}-n1-s4"))
    assert m.status.node_status["pause"].state == State.RUNNING
    assert m.status.state == State.RUNNING

    await asyncio.sleep(0.15)

    assert m.status.state == State.COMPLETED and r._current_mission is None


@pytest.mark.unit
async def test_robot_state_during_a_wait_does_not_restart_or_count_as_a_mismatch():
    r, _ = _make_robot()
    await _start(r, _mission(tree=_tree(_wait("pause", 5), _route("b"))))
    key, task = r._wait_key, r._wait_task

    for _ in range(Robot.MAX_ORDER_MISMATCHES + 5):
        await r._on_client_message(_state("previous-n1"))

    assert r._order_mismatch_count == 0
    assert r._wait_key == key and r._wait_task is task
    r._mqtt_client.publish.assert_not_called()
    r._cancel_wait()
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_cancel_during_a_wait_cancels_the_mission_without_a_cancel_order():
    r, _ = _make_robot()
    m = await _start(r, _mission(tree=_tree(_wait("pause", 5), _route("b"))))
    r._send_cancel_order = AsyncMock()
    task = r._wait_task

    cancel_request = m.copy(deep=True)
    cancel_request.needs_canceled = True
    await r._on_mission_change(cancel_request)
    await asyncio.sleep(0)

    assert m.status.state == State.CANCELED
    assert r._current_mission is None and r._wait_task is None
    assert task.cancelled()
    r._send_cancel_order.assert_not_awaited()
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_leaving_the_mission_cancels_the_wait_timer():
    r, _ = _make_robot()
    m = await _start(r, _mission(tree=_tree(_wait("pause", 5))))
    task = r._wait_task
    r._set_mission_state(State.FAILED)

    await r.get_next_mission()
    await asyncio.sleep(0)

    assert task.cancelled() and r._wait_task is None
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_a_stale_wait_timer_is_ignored():
    r, _ = _make_robot()
    m = await _start(r, _mission(tree=_tree(_wait("pause", 5), _route("b"))))

    await r._on_wait_elapsed(
        __import__("packages.controllers.mission.server", fromlist=["WaitElapsed"]).WaitElapsed(
            key=("m1", "pause", "old-run", 0, 0)))

    assert m.status.node_status["pause"].state == State.RUNNING
    r._cancel_wait()
    r._cancel_mission_timeout()


# ---------------------------------------------------------------------------
# Editing a PENDING mission: API
# ---------------------------------------------------------------------------
def _api():
    with patch("packages.api.main.service", None):
        pass
    import packages.api.main as api_main
    return api_main


def _db_for(existing):
    return SimpleNamespace(get_object=AsyncMock(return_value=existing),
                           update_spec=AsyncMock(), update_status=AsyncMock())


@pytest.mark.unit
async def test_api_edits_a_pending_mission_and_syncs_node_status():
    api_main = _api()
    existing = _mission(tree=_tree(_route("a")))
    db = _db_for(existing)
    new_tree = _tree(_route("a"), _wait("pause", 5), _route("b"))

    with patch.object(api_main, "service", SimpleNamespace(database=db)):
        await api_main.update_mission("m1", {"mission_tree": new_tree, "repeat": 3,
                                             "then_run": "dock", "timeout": 500})

    spec = db.update_spec.await_args.args[2]
    assert spec.repeat == 3 and spec.then_run == "dock"
    assert [n.name for n in spec.mission_tree] == ["root_sequence", "a", "pause", "b"]
    status = db.update_status.await_args.args[2]
    assert set(status.node_status) == {"root", "root_sequence", "a", "pause", "b"}


@pytest.mark.unit
@pytest.mark.parametrize("state", [State.RUNNING, State.COMPLETED, State.FAILED, State.CANCELED])
async def test_api_refuses_to_edit_a_mission_that_has_started(state):
    api_main = _api()
    existing = _mission()
    existing.status.state = state
    db = _db_for(existing)

    with patch.object(api_main, "service", SimpleNamespace(database=db)):
        with pytest.raises(HTTPException) as err:
            await api_main.update_mission("m1", {"repeat": 2})

    assert err.value.status_code == 409
    db.update_spec.assert_not_awaited()


@pytest.mark.unit
async def test_api_rejects_an_invalid_edit_with_400():
    api_main = _api()
    db = _db_for(_mission())

    with patch.object(api_main, "service", SimpleNamespace(database=db)):
        for bad in ({"repeat": -2},
                    {"mission_tree": _tree({"name": "w", "parent": "root_sequence",
                                            "action": {"action_type": "wait",
                                                       "action_parameters": {"seconds": 0}}})}):
            with pytest.raises(HTTPException) as err:
                await api_main.update_mission("m1", bad)
            assert err.value.status_code == 400
    db.update_spec.assert_not_awaited()


@pytest.mark.unit
async def test_api_reroute_of_a_running_mission_is_not_treated_as_an_edit():
    api_main = _api()
    existing = _mission()
    existing.status.state = State.RUNNING
    db = _db_for(existing)

    with patch.object(api_main, "service", SimpleNamespace(database=db)):
        await api_main.update_mission("m1", {"update_nodes": {"a": {"waypoints": [
            {"x": 1.0, "y": 1.0, "theta": 0.0}]}}})

    db.update_spec.assert_awaited_once()


# ---------------------------------------------------------------------------
# Editing a PENDING mission: dispatcher
# ---------------------------------------------------------------------------
@pytest.mark.unit
async def test_dispatcher_applies_a_spec_edit_to_a_queued_mission():
    r, _ = _make_robot()
    current = _mission(name="busy")
    r._current_mission = current
    queued = _mission(name="m1", tree=_tree(_route("a")))
    r._missions["busy"], r._missions["m1"] = current, queued
    edit = _mission(name="m1", tree=_tree(_route("a"), _wait("pause", 5), _route("b")),
                    repeat=4, then_run="dock")

    await r._on_mission_change(edit)

    assert queued.repeat == 4 and queued.then_run == "dock"
    assert [n.name for n in queued.mission_tree] == ["root_sequence", "a", "pause", "b"]
    assert set(queued.status.node_status) == {"root", "root_sequence", "a", "pause", "b"}


@pytest.mark.unit
async def test_dispatcher_applies_an_edit_to_a_held_undispatched_mission():
    r, _ = _make_robot()
    held = _mission()
    r._missions["m1"] = held
    r._current_mission = held           # picked, but never dispatched: no behavior tree

    await r._on_mission_change(_mission(repeat=2))

    assert held.repeat == 2


@pytest.mark.unit
async def test_dispatcher_ignores_an_edit_to_a_dispatched_mission():
    r, _ = _make_robot()
    m = await _start(r, _mission())

    await r._on_mission_change(_mission(repeat=9))

    assert m.repeat == 1
    assert "m1" in r._ignored_spec_edits
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_dispatcher_lets_go_of_a_queued_mission_moved_to_another_robot():
    r, _ = _make_robot()
    queued = _mission(name="m1")
    r._current_mission = _mission(name="busy")
    r._missions["busy"], r._missions["m1"] = r._current_mission, queued

    await r._on_mission_change(_mission(name="m1", robot="r2"))

    assert "m1" not in r._missions and "busy" in r._missions


@pytest.mark.unit
async def test_a_reroute_of_a_dispatched_mission_is_not_reported_as_an_ignored_edit():
    r, _ = _make_robot()
    m = await _start(r, _mission())
    echo = _mission()
    echo.update_nodes = {"a": mission_object.MissionRouteNodeV1(waypoints=[
        {"x": 9.0, "y": 9.0, "theta": 0.0}])}
    m.mission_tree[1].route = echo.update_nodes["a"]      # what the dispatcher did

    r._apply_spec_edit(m, echo, dispatched=True)

    assert "m1" not in r._ignored_spec_edits
    r._cancel_mission_timeout()
