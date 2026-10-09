"""Unit tests for the mission-lifecycle fixes (branch fix/mission-lifecycle).

- C1: a reroute is folded into mission_tree and announced by route_rev; the dispatcher acts
  on it once, however often the row is delivered, and a resumed mission is sent from the
  stored (rerouted) tree.
- C2: a node cancelled for a reroute is never persisted CANCELED; a resumed mission whose
  tree is already finished sends nothing.
- C3: a resumed (already started) mission is dispatched before a PENDING one.
- M7: cancelling a mission that was never dispatched ends it without a cancelOrder.
- S1: a finished cancelOrder is not lost to an order-id mismatch.
- PROGRESS: task_status advances for waypoints with a non-zero allowed deviation.
"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import cloud_common.objects as api_objects
import cloud_common.objects.mission as mission_object
import packages.controllers.mission.vda5050_types as types
from packages.controllers.mission import order_ids
from packages.controllers.mission.server import Robot
from packages.database.postgres import PostgresDatabase

State = mission_object.MissionStateV1
CANCEL = types.VDA5050InstantActionType.CANCEL_ORDER


@pytest.fixture(autouse=True)
def _no_cancel_dwell(monkeypatch):
    """These tests exercise the cancel/resend flow right after dispatch; the reroute-cancel
    dwell (a robot that has not yet reported the order just sent) is covered in
    test_offline_missions.py."""
    from packages.controllers.mission import order_policy
    monkeypatch.setattr(order_policy, "_current",
                        order_policy.OrderPolicy(cancel_min_dwell_s=0.0))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _route(name, n=2, x0=0.0, deviation=0.0, parent="root_sequence"):
    return {"name": name, "parent": parent, "route": {"waypoints": [
        {"x": x0 + i, "y": 1.0, "theta": 0.0, "allowedDeviationXY": deviation}
        for i in range(n)]}}


def _tree(*leaves):
    return [{"name": "root_sequence", "parent": "root", "sequence": {}}, *leaves]


def _mission(name="m1", robot="r1", tree=None, **spec):
    return api_objects.MissionObjectV1(
        name=name, robot=robot, mission_tree=tree or _tree(_route("a")),
        status={}, timeout=1000, **spec)


def _make_robot(online=True):
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
    r._robot_object.status.online = online
    r._set_robot_idle_after_mission = MagicMock()
    return r, db


async def _start(r, mission):
    r._missions[mission.name] = mission
    await r._try_start_mission()
    assert r._current_mission is mission
    return mission


def _published(r, suffix):
    return [json.loads(c.args[1]) for c in r._mqtt_client.publish.call_args_list
            if c.args[0].endswith(suffix)]


def _orders(r):
    return _published(r, "/order")


def _cancels(r):
    return _published(r, "/instantActions")


def _state(order_id="", actions=None, last_node_id="", last_seq=0):
    return types.VDA5050State(
        headerId=0, timestamp="", orderId=order_id, nodeStates=[], edgeStates=[],
        actionStates=actions or [], errors=[], batteryState=None, agvPosition=None,
        velocity=None, lastNodeId=last_node_id, lastNodeSequenceId=last_seq)


def _cancel_done(r):
    (action,) = [a for a in r._current_instant_actions.values() if a.actionType == CANCEL]
    return types.VDA5050ActionState(
        actionId=action.actionId, actionType=CANCEL,
        actionStatus=types.VDA5050ActionStatus.FINISHED)


def _order_id(r, idx=1):
    return f"{r._order_prefix()}-n{idx}"


def _rerouted(x0=9.0, n=2, rev=1, name="m1"):
    """The row as the API stores it after a reroute of node "a"."""
    m = _mission(name=name, tree=_tree(_route("a", n=n, x0=x0)))
    m.route_rev = rev
    return m


def _xs(order):
    return [n["nodePosition"]["x"] for n in order["nodes"][1:]]


# ---------------------------------------------------------------------------
# C1: reroute
# ---------------------------------------------------------------------------
@pytest.mark.unit
async def test_a_reroute_delivered_three_times_cancels_and_resends_once():
    r, _ = _make_robot()
    m = await _start(r, _mission())
    assert len(_orders(r)) == 1

    for _ in range(3):
        await r._on_mission_change(_rerouted())
    assert len(_cancels(r)) == 1                      # exactly one cancelOrder
    assert m.status.applied_route_rev == 1

    # The robot answers the cancel: the node is resent once, with the new route.
    await r._on_client_message(_state(_order_id(r), actions=[_cancel_done(r)]))
    orders = _orders(r)
    assert len(orders) == 2
    assert _xs(orders[1]) == [9.0, 10.0]
    assert orders[1]["orderId"] != orders[0]["orderId"]

    # Every later delivery of the same row (resync, echo) is a no-op.
    for _ in range(3):
        await r._on_mission_change(_rerouted())
    await r._on_client_message(_state(_order_id(r)))
    assert len(_cancels(r)) == 1 and len(_orders(r)) == 2
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_a_second_reroute_is_a_new_revision_and_acts_again():
    r, _ = _make_robot()
    await _start(r, _mission())
    await r._on_mission_change(_rerouted(rev=1))
    await r._on_client_message(_state(_order_id(r), actions=[_cancel_done(r)]))
    await r._on_mission_change(_rerouted(x0=20.0, rev=2))
    await r._on_client_message(_state(_order_id(r), actions=[_cancel_done(r)]))

    orders = _orders(r)
    assert len(orders) == 3 and _xs(orders[2]) == [20.0, 21.0]
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_dispatcher_mission_writes_use_the_servers_writer_id():
    """One stable publisher id, so the mission watcher can skip the dispatcher's own
    writes (a fresh id per write made every one of them echo back)."""
    r, db = _make_robot()
    r._robot_server.mission_writer_id = __import__("uuid").uuid4()
    await _start(r, _mission())
    r._set_mission_state(State.COMPLETED)
    import asyncio
    await asyncio.sleep(0)

    ids = {c.args[3] for c in db.update_status.call_args_list
           if c.args[0] is api_objects.MissionObjectV1}
    assert ids == {r._robot_server.mission_writer_id}
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_resume_after_restart_sends_the_rerouted_waypoints():
    """The dispatcher restarted between the reroute being stored and being applied: the
    resumed mission is sent from the stored tree, as a new order revision."""
    r, db = _make_robot()
    stored = _rerouted(rev=1)
    stored.status.state = State.RUNNING
    stored.status.start_timestamp = __import__("datetime").datetime.now()
    stored.status.run_id = "abcd1234"
    stored.status.node_status["a"].state = State.RUNNING
    assert stored.status.applied_route_rev == 0

    await r._on_mission_change(stored)
    assert _orders(r) == []                           # the robot's state decides
    await r._on_client_message(_state("m1-rabcd1234-n1"))   # idle on the old route

    (order,) = _orders(r)
    assert _xs(order) == [9.0, 10.0]
    assert order["orderId"].startswith("m1-rabcd1234v1-")
    assert stored.status.applied_route_rev == 1
    assert len(_cancels(r)) == 0
    # ... and the row delivered again does not reroute it a second time.
    await r._on_mission_change(_rerouted(rev=1))
    assert len(_cancels(r)) == 0 and len(_orders(r)) == 1
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_a_reroute_before_dispatch_is_just_the_tree_to_send():
    r, _ = _make_robot(online=False)                  # held: queued, not dispatched
    await _start(r, _mission())
    await r._on_mission_change(_rerouted(rev=1))
    r._robot_object.status.online = True
    await r._try_start_mission()

    (order,) = _orders(r)
    assert _xs(order) == [9.0, 10.0] and len(_cancels(r)) == 0
    assert r._current_mission.status.applied_route_rev == 1
    r._cancel_mission_timeout()


# ---------------------------------------------------------------------------
# C1: API
# ---------------------------------------------------------------------------
class _FakeMissionDb:
    """Keeps one mission row the way the database does: spec and status separately."""

    def __init__(self, mission):
        self.row = mission

    async def get_object(self, cls, name):
        return cls(name=name, lifecycle=self.row.lifecycle, status=self.row.status.dict(),
                   **json.loads(self.row.spec.json()))

    async def update_spec(self, cls, name, spec, publisher_id):
        self.row = cls(name=name, lifecycle=self.row.lifecycle, status=self.row.status.dict(),
                       **json.loads(spec.json()))

    async def update_spec_fields(self, cls, name, fields, publisher_id):
        self.row = cls(name=name, lifecycle=self.row.lifecycle, status=self.row.status.dict(),
                       **{**json.loads(self.row.spec.json()), **fields})

    async def update_status(self, cls, name, status, publisher_id):
        self.row.status = status


@pytest.mark.unit
async def test_api_reroute_folds_into_the_tree_and_bumps_route_rev():
    import packages.api.main as api_main
    existing = _mission(tree=_tree(_route("a", n=3)))
    existing.planned_path = ["n1", "n2", "n3"]
    existing.status.state = State.RUNNING
    db = _FakeMissionDb(existing)
    body = {"update_nodes": {"a": {"waypoints": [{"x": 5.0, "y": 6.0, "theta": 0.0}]}}}

    with patch.object(api_main, "service", SimpleNamespace(database=db)):
        response = await api_main.update_mission("m1", body)
        again = await api_main.update_mission("m1", body)
        got = await db.get_object(api_main.MissionObjectV1, "m1")

    assert response["route_rev"] == 1 and again["route_rev"] == 2
    assert [(w["x"], w["y"]) for w in response["mission_tree"][1]["route"]["waypoints"]] \
        == [(5.0, 6.0)]
    assert response["planned_path"] is None
    assert response["update_nodes"] is None
    assert got.route_rev == 2
    assert [(w.x, w.y) for w in got.mission_tree[1].route.waypoints] == [(5.0, 6.0)]


@pytest.mark.unit
async def test_api_reroute_is_validated():
    import httpx
    import packages.api.main as api_main
    existing = _mission()
    existing.status.state = State.RUNNING
    db = _FakeMissionDb(existing)
    with patch.object(api_main, "service", SimpleNamespace(database=db)):
        for bad in ({"nope": {"waypoints": [{"x": 1.0, "y": 1.0, "theta": 0.0}]}},
                    {"root_sequence": {"waypoints": [{"x": 1.0, "y": 1.0, "theta": 0.0}]}},
                    {"a": {"waypoints": []}}):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_main.app),
                                         base_url="http://t") as client:
                resp = await client.put("/api/v1/missions/m1", json={"update_nodes": bad})
            assert resp.status_code == 400  # ICSUsageError -> 400 (central handler)
    assert db.row.route_rev == 0


# ---------------------------------------------------------------------------
# C2
# ---------------------------------------------------------------------------
@pytest.mark.unit
async def test_a_node_cancelled_for_a_reroute_is_not_recorded_canceled():
    r, _ = _make_robot()
    m = await _start(r, _mission())
    await r._on_mission_change(_rerouted())
    await r._on_client_message(_state(_order_id(r), actions=[_cancel_done(r)]))

    assert m.status.node_status["a"].state == State.RUNNING     # not recorded CANCELED ...
    assert len(_orders(r)) == 2                                  # ... but resent
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_a_node_cancelled_with_the_mission_is_canceled():
    r, _ = _make_robot()
    m = await _start(r, _mission())
    m.needs_canceled = True
    await r._send_cancel_order("c1")
    await r._on_client_message(_state(_order_id(r), actions=[_cancel_done(r)]))

    assert m.status.state == State.CANCELED
    assert m.status.node_status["a"].state == State.CANCELED


@pytest.mark.unit
async def test_resume_with_a_node_left_canceled_by_a_reroute_is_not_failed():
    r, _ = _make_robot()
    stored = _mission()
    stored.status.state = State.RUNNING
    stored.status.start_timestamp = __import__("datetime").datetime.now()
    stored.status.run_id = "abcd1234"
    stored.status.node_status["a"].state = State.CANCELED     # what older versions persisted

    await r._on_mission_change(stored)
    await r._on_client_message(_state("elsewhere-n0"))

    assert stored.status.state == State.RUNNING
    assert stored.status.failure_reason is None
    assert len(_orders(r)) == 1
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_resume_of_a_finished_tree_sends_no_order():
    r, _ = _make_robot()
    stored = _mission()
    stored.status.state = State.RUNNING
    stored.status.start_timestamp = __import__("datetime").datetime.now()
    stored.status.run_id = "abcd1234"
    stored.status.node_status["a"].state = State.COMPLETED

    await r._on_mission_change(stored)

    assert _orders(r) == []
    assert stored.status.state == State.COMPLETED
    assert r._current_mission is None and "m1" not in r._missions
    r._robot_server.delete_pending_mission.assert_awaited()


# ---------------------------------------------------------------------------
# C3
# ---------------------------------------------------------------------------
@pytest.mark.unit
async def test_a_resumed_mission_is_dispatched_before_a_pending_one():
    r, _ = _make_robot()
    robot_object, r._robot_object = r._robot_object, None     # the robot row comes later
    pending = _mission(name="pending")
    running = _mission(name="running")
    running.status.state = State.RUNNING
    running.status.start_timestamp = __import__("datetime").datetime.now()
    running.status.run_id = "abcd1234"
    running.status.node_status["a"].state = State.RUNNING

    await r._on_mission_change(pending)
    await r._on_mission_change(running)
    assert list(r._missions) == ["running", "pending"]
    r._robot_object = robot_object
    await r._try_start_mission()

    assert r._current_mission.name == "running"
    await r._on_client_message(_state("elsewhere-n0"))
    assert _orders(r)[0]["orderId"].startswith("running-")
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_the_first_mission_to_arrive_does_not_take_the_slot_before_the_robot_is_known():
    r, _ = _make_robot()
    robot_object, r._robot_object = r._robot_object, None
    await r._on_mission_change(_mission(name="pending"))
    assert r._current_mission is None
    r._robot_object = robot_object


@pytest.mark.unit
def test_the_watcher_resync_orders_missions_by_start_then_creation():
    from packages.database.postgres import PostgresWatcher
    sql = PostgresWatcher.resync_query(api_objects.MissionObjectV1)
    assert "ORDER BY" in sql
    assert sql.index("start_timestamp") < sql.index("created_at")
    assert "NULLS LAST" in sql
    assert PostgresWatcher.resync_query(api_objects.RobotObjectV1) \
        == "SELECT * FROM robotobjectv1;"


@pytest.mark.unit
async def test_create_object_stamps_created_at_on_a_mission():
    db = PostgresDatabase.__new__(PostgresDatabase)
    db._logger = MagicMock()
    cursor = MagicMock()
    cursor.execute = AsyncMock()
    conn = MagicMock()
    conn.cursor.return_value.__aenter__ = AsyncMock(return_value=cursor)
    conn.cursor.return_value.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.connection.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.connection.return_value.__aexit__ = AsyncMock(return_value=False)
    db._pool = pool
    db._notify = AsyncMock()
    mission = _mission()
    assert mission.created_at is None

    await db.create_object(mission, __import__("uuid").uuid4())

    assert mission.created_at is not None
    inserted_spec = json.loads(cursor.execute.await_args.args[1][2])
    assert inserted_spec["created_at"] is not None


# ---------------------------------------------------------------------------
# M7
# ---------------------------------------------------------------------------
@pytest.mark.unit
async def test_cancelling_a_never_dispatched_mission_ends_it_without_a_cancel_order():
    r, db = _make_robot(online=False)                 # offline: held, no order sent
    held = await _start(r, _mission(name="held"))
    nxt = _mission(name="next")
    r._missions["next"] = nxt
    assert r._current_behavior_tree is None and held.status.held

    cancel = _mission(name="held")
    cancel.needs_canceled = True
    await r._on_mission_change(cancel)

    assert held.status.state == State.CANCELED
    assert _cancels(r) == [] and _orders(r) == []
    assert "held" not in r._missions
    assert r._current_mission is nxt                  # the queue moved on at once


# ---------------------------------------------------------------------------
# S1
# ---------------------------------------------------------------------------
@pytest.mark.unit
async def test_a_finished_cancel_on_a_foreign_order_id_still_cancels_the_mission():
    r, _ = _make_robot()
    m = await _start(r, _mission())
    cancel = _mission()
    cancel.needs_canceled = True
    await r._on_mission_change(cancel)
    assert len(_cancels(r)) == 1
    orders_before = len(_orders(r))

    await r._on_client_message(_state("other-mission-n0", actions=[_cancel_done(r)]))

    assert m.status.state == State.CANCELED
    assert len(_orders(r)) == orders_before           # our cancelled order is not resent
    assert r._current_mission is None
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_a_finished_cancel_on_a_foreign_order_id_lets_a_reroute_go_out():
    r, _ = _make_robot()
    m = await _start(r, _mission())
    await r._on_mission_change(_rerouted())

    await r._on_client_message(_state("other-mission-n0", actions=[_cancel_done(r)]))

    orders = _orders(r)
    assert len(orders) == 2 and _xs(orders[1]) == [9.0, 10.0]
    assert m.status.state == State.RUNNING
    r._cancel_mission_timeout()


# ---------------------------------------------------------------------------
# PROGRESS
# ---------------------------------------------------------------------------
@pytest.mark.unit
async def test_task_status_advances_for_waypoints_with_a_deviation():
    """A planner go-to's waypoints have allowedDeviationXY 0.2, not 0."""
    r, _ = _make_robot()
    m = await _start(r, _mission(tree=_tree(_route("a", n=3, deviation=0.2))))
    prefix = r._order_prefix()
    seen = []
    for seq in (2, 4, 6):
        await r._on_client_message(_state(_order_id(r), last_node_id=f"{prefix}-n1-s{seq}",
                                          last_seq=seq))
        seen.append(m.status.task_status.get("a"))
        if m.status.state.done:
            break

    assert seen[:2] == [0, 1]
    r._cancel_mission_timeout()
