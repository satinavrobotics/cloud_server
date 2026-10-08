"""Unit tests for go-to missions, their dispatch-time replan, and the queue views.

- the planner: a go-to carries kind/goal and a unique name; the plan-only call creates no
  mission; a first path node behind the robot is dropped (H5)
- the dispatcher: a go-to queued behind another mission is replanned from the robot's pose
  when it starts; an unreachable or failing planner leaves the stored plan in use
- the robot's current_mission / queued_missions (robot_update, robot GET)
- POST /api/v1/navigate: state, queued_behind, Idempotency-Key
"""
import asyncio
import datetime
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import cloud_common.objects as api_objects
import cloud_common.objects.mission as mission_object
from cloud_common.objects import common
from packages.api.mission_index import RobotMissionIndex, mission_ahead
from packages.database.postgres import PostgresDatabase
from packages.services.mission_planner.server import MissionPlannerService
from packages.controllers.mission.server import Robot

State = mission_object.MissionStateV1
IDENTITY = {"map_name": "m", "aligned": True, "map_t_session": {"tx": 0.0, "ty": 0.0, "yaw": 0.0}}


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------
NODES = {"1": (0.0, 0.0), "2": (10.0, 0.0), "3": (20.0, 0.0)}


def _planner(robot_x=0.0, robot_y=0.0):
    with patch("packages.services.mission_planner.server.GraphDatabaseService") as graph, \
            patch("packages.services.mission_planner.server.PostgresDatabase") as db_cls:
        service = MissionPlannerService(default_map_id="m")
    robot = api_objects.RobotObjectV1(name="r1", status={})
    robot.status.pose.x, robot.status.pose.y = robot_x, robot_y
    db = AsyncMock(spec=PostgresDatabase)
    db.create_object = AsyncMock()
    service.database = db
    service.get_robot_status = AsyncMock(return_value=robot)
    service._open_session = AsyncMock(return_value=IDENTITY)
    g = MagicMock()
    g.k_nearest_neighbors.return_value = ([{"node_id": "1", "x": 0.0, "y": 0.0}], [1.0])
    g.nodes_in_range.return_value = ([{"node_id": "3", "x": 20.0, "y": 0.0}], [0.5])
    g.shortest_path.return_value = ["1", "2", "3"]
    g.get_node.side_effect = lambda map_id, node_id: {
        "node_id": node_id, "x": NODES[str(node_id)][0], "y": NODES[str(node_id)][1],
        "yaw": 0.0}
    service.graph_db = g
    return service, db


@pytest.mark.unit
async def test_a_go_to_mission_carries_kind_and_goal_and_a_unique_name():
    service, db = _planner()
    first = await service.plan_and_execute_mission("r1", target_x=20.0, target_y=0.0, map_id="m")
    second = await service.plan_and_execute_mission("r1", target_x=20.0, target_y=0.0, map_id="m")

    assert first["success"] and second["success"]
    assert first["mission_name"] != second["mission_name"]       # even within one second
    assert first["mission_name"].startswith("nav_r1_")
    mission = db.create_object.await_args_list[0].args[0]
    assert mission.kind == "goto"
    assert mission.goal == {"x": 20.0, "y": 0.0, "map_id": "m", "node_id": "3"}


@pytest.mark.unit
async def test_plan_route_creates_no_mission():
    service, db = _planner()
    plan = await service.plan_route("r1", target_x=20.0, target_y=0.0, map_id="m")

    assert plan["success"] is True
    assert [w.x for w in plan["waypoints"]] == [0.0, 10.0, 20.0]
    assert plan["path"] == ["1", "2", "3"]
    db.create_object.assert_not_awaited()


@pytest.mark.unit
async def test_plan_route_uses_the_pose_it_is_given_and_drops_a_first_node_behind_the_robot():
    # The stored robot row says x=0 (at node 1); the caller knows the robot is at x=4,
    # already on its way to node 2: node 1 is behind it.
    service, db = _planner(robot_x=0.0)
    plan = await service.plan_route("r1", target_x=20.0, target_y=0.0, map_id="m",
                                    robot_pose=(4.0, 0.0))

    assert [w.x for w in plan["waypoints"]] == [10.0, 20.0]
    assert plan["path"] == ["2", "3"]
    service.graph_db.k_nearest_neighbors.assert_called_once()
    assert service.graph_db.k_nearest_neighbors.call_args.kwargs["x"] == 4.0


@pytest.mark.unit
@pytest.mark.parametrize("robot_x", [0.0, -3.0])
async def test_a_first_node_the_robot_has_still_to_pass_is_kept(robot_x):
    # At or before node 1 the robot is not closer to node 2 than node 1 is.
    service, _ = _planner(robot_x=robot_x)
    plan = await service.plan_route("r1", target_x=20.0, target_y=0.0, map_id="m")
    assert [w.x for w in plan["waypoints"]] == [0.0, 10.0, 20.0]


@pytest.mark.unit
async def test_plan_route_reports_a_planning_failure():
    service, _ = _planner()
    service.graph_db.shortest_path.return_value = None
    plan = await service.plan_route("r1", target_x=20.0, target_y=0.0, map_id="m")
    assert plan["success"] is False and plan["failed_at"] == "find_path"


@pytest.mark.unit
async def test_the_plan_endpoint_returns_waypoints_and_path():
    import packages.services.mission_planner.main as planner_main
    service, db = _planner()
    with patch.object(planner_main, "service", service):
        response = await planner_main.plan_only(planner_main.PlanRequest(
            robot_name="r1", target_x=20.0, target_y=0.0, map_id="m", robot_x=4.0, robot_y=0.0))

    assert response.success and response.planned_path == ["2", "3"]
    assert [w["x"] for w in response.waypoints] == [10.0, 20.0]
    assert response.goal["node_id"] == "3"
    db.create_object.assert_not_awaited()


# ---------------------------------------------------------------------------
# Dispatcher: replan at dispatch
# ---------------------------------------------------------------------------
def _goto(name="goto", x0=0.0):
    m = api_objects.MissionObjectV1(
        name=name, robot="r1", status={}, timeout=1000, kind="goto",
        goal={"x": 20.0, "y": 0.0, "map_id": "m", "node_id": "3"},
        mission_tree=[{"name": "navigate_to_target", "parent": "root", "route": {"waypoints": [
            {"x": x0 + i, "y": 0.0, "theta": 0.0, "allowedDeviationXY": 0.2}
            for i in range(3)]}}])
    return m


def _plain(name):
    return api_objects.MissionObjectV1(
        name=name, robot="r1", status={}, timeout=1000,
        mission_tree=[{"name": "a", "parent": "root", "route": {"waypoints": [
            {"x": 1.0, "y": 1.0, "theta": 0.0}]}}])


def _make_robot(planner=None):
    db = AsyncMock(spec=PostgresDatabase)
    db.update_status = AsyncMock()
    db.update_spec_fields = AsyncMock()
    client = MagicMock()
    server = MagicMock()
    server.push_telemetry = False
    server.mission_ctrl_url = None
    server.delete_pending_mission = AsyncMock(return_value=False)
    server.mission_planner = planner
    r = Robot("r1", db, client, "prefix", server)
    r._robot_object = api_objects.RobotObjectV1(name="r1", status={})
    r._robot_object.status.online = True
    r._set_robot_idle_after_mission = MagicMock()
    return r, db


def _orders(r):
    return [json.loads(c.args[1]) for c in r._mqtt_client.publish.call_args_list
            if c.args[0].endswith("/order")]


async def _run_busy_then_goto(r, move_to_x):
    busy = _plain("busy")
    r._missions["busy"] = busy
    await r._try_start_mission()
    goto = _goto()
    await r._on_mission_change(goto)            # queued behind "busy"
    assert r._current_mission is busy and r._current_behavior_tree is not None
    r._robot_object.status.pose.x = move_to_x    # the robot drives on while it waits
    r._set_mission_state(State.COMPLETED)
    await r.post_mission_completion()
    return goto


@pytest.mark.unit
async def test_a_queued_go_to_is_replanned_from_the_pose_at_dispatch():
    planner = SimpleNamespace(plan=AsyncMock(return_value={
        "success": True, "planned_path": ["7", "8"],
        "waypoints": [{"x": 51.0, "y": 0.0, "theta": 0.0, "allowedDeviationXY": 0.2},
                      {"x": 60.0, "y": 0.0, "theta": 0.0, "allowedDeviationXY": 0.2}]}))
    r, db = _make_robot(planner)

    goto = await _run_busy_then_goto(r, move_to_x=50.0)

    planner.plan.assert_awaited_once()
    kwargs = planner.plan.await_args.kwargs
    assert (kwargs["robot_x"], kwargs["target_x"], kwargs["map_id"]) == (50.0, 20.0, "m")
    assert r._current_mission is goto
    first_goto_order = [o for o in _orders(r) if o["orderId"].startswith("goto-")][0]
    assert [n["nodePosition"]["x"] for n in first_goto_order["nodes"][1:]] == [51.0, 60.0]
    assert goto.planned_path == ["7", "8"]
    # stored, and not mistaken for a reroute when the row comes back
    fields = db.update_spec_fields.await_args.args[2]
    assert fields["planned_path"] == ["7", "8"] and fields["route_rev"] == 1
    assert goto.route_rev == goto.status.applied_route_rev == 1
    await r._on_mission_change(_echo_of(goto, fields))
    assert not [c for c in r._mqtt_client.publish.call_args_list
                if c.args[0].endswith("/instantActions")]
    r._cancel_mission_timeout()


def _echo_of(goto, fields):
    echo = _goto()
    echo.mission_tree = [mission_object.MissionNodeV1(**n) for n in fields["mission_tree"]]
    echo.planned_path = fields["planned_path"]
    echo.route_rev = fields["route_rev"]
    return echo


@pytest.mark.unit
@pytest.mark.parametrize("planner", [
    SimpleNamespace(plan=AsyncMock(side_effect=ConnectionError("planner down"))),
    SimpleNamespace(plan=AsyncMock(return_value={"success": False, "error": "no path"})),
    SimpleNamespace(plan=AsyncMock(side_effect=asyncio.TimeoutError())),
    None,
])
async def test_an_unreachable_planner_leaves_the_stored_plan_in_use(planner):
    r, db = _make_robot(planner)

    goto = await _run_busy_then_goto(r, move_to_x=50.0)

    assert r._current_mission is goto and goto.status.state != State.FAILED
    first_goto_order = [o for o in _orders(r) if o["orderId"].startswith("goto-")][0]
    assert [n["nodePosition"]["x"] for n in first_goto_order["nodes"][1:]] == [0.0, 1.0, 2.0]
    assert goto.route_rev == 0
    db.update_spec_fields.assert_not_awaited()
    r._cancel_mission_timeout()


@pytest.mark.unit
async def test_only_a_go_to_is_replanned():
    planner = SimpleNamespace(plan=AsyncMock())
    r, _ = _make_robot(planner)
    r._missions["plain"] = _plain("plain")
    await r._try_start_mission()
    planner.plan.assert_not_awaited()
    r._cancel_mission_timeout()


# ---------------------------------------------------------------------------
# Robot: current_mission / queued_missions
# ---------------------------------------------------------------------------
def _row(name, robot="r1", state=State.PENDING, started=None, created=None,
         lifecycle=api_objects.object.ObjectLifecycleV1.ALIVE):
    m = _plain(name)
    m.robot = robot
    m.status.state = state
    m.status.start_timestamp = started
    m.created_at = created
    m.lifecycle = lifecycle
    return m


T0 = datetime.datetime(2026, 10, 8, 12, 0, 0)


def _at(seconds):
    return T0 + datetime.timedelta(seconds=seconds)


@pytest.mark.unit
def test_the_index_names_the_running_mission_and_the_queue_in_dispatch_order():
    index = RobotMissionIndex()
    for m in (_row("late", created=_at(30)), _row("early", created=_at(10)),
              _row("running", state=State.RUNNING, started=_at(5), created=_at(1)),
              _row("done", state=State.COMPLETED, created=_at(0)),
              _row("other", robot="r2", created=_at(2))):
        index.update(m)

    assert index.view("r1") == {"current_mission": "running",
                                "queued_missions": ["early", "late"]}
    assert index.view("r2") == {"current_mission": None, "queued_missions": ["other"]}
    assert index.view("r3") == {"current_mission": None, "queued_missions": []}


@pytest.mark.unit
def test_the_index_forgets_finished_deleted_and_moved_missions():
    index = RobotMissionIndex()
    index.update(_row("a", created=_at(1)))
    index.update(_row("b", created=_at(2)))
    index.update(_row("a", state=State.COMPLETED, created=_at(1)))
    index.update(_row("b", robot="r2", created=_at(2)))
    assert index.view("r1")["queued_missions"] == []
    index.update(_row("b", robot="r2", created=_at(2),
                      lifecycle=api_objects.object.ObjectLifecycleV1.DELETED))
    assert index.view("r2")["queued_missions"] == []


@pytest.mark.unit
async def test_robot_update_carries_current_and_queued_missions():
    from packages.api.server import ApiDelegationService
    svc = object.__new__(ApiDelegationService)
    svc.mission_index = RobotMissionIndex()
    svc.mission_index.update(_row("now", state=State.RUNNING, started=_at(1)))
    svc.mission_index.update(_row("next", created=_at(2)))
    svc._running = True
    svc._robot_changes = asyncio.Queue()
    svc.telemetry = None
    svc.logger = MagicMock()
    svc._robot_session = AsyncMock(return_value=None)
    svc.ws_manager = MagicMock()

    async def stop(*a, **k):
        svc._running = False
    svc.ws_manager.broadcast = AsyncMock(side_effect=stop)
    await svc._robot_changes.put(api_objects.RobotObjectV1(name="r1", status={}))
    await asyncio.wait_for(svc._handle_robot_updates(), timeout=2)

    (_, _, message), _ = svc.ws_manager.broadcast.await_args
    assert message["current_mission"] == "now"
    assert message["queued_missions"] == ["next"]


@pytest.mark.unit
async def test_robot_get_carries_current_and_queued_missions():
    import packages.api.main as api_main
    index = RobotMissionIndex()
    index.update(_row("now", state=State.RUNNING, started=_at(1)))
    index.update(_row("next", created=_at(2)))
    fake = SimpleNamespace(
        mission_index=index,
        mapping_switch=SimpleNamespace(snapshots=AsyncMock(return_value={})),
        database=SimpleNamespace(
            get_object=AsyncMock(return_value=api_objects.RobotObjectV1(name="r1", status={}))))
    with patch.object(api_main, "service", fake), \
            patch.object(api_main.maps, "robot_sessions", AsyncMock(return_value={})):
        robot = await api_main.get_robot("r1")
        idle = (await api_main._robot_views(
            [api_objects.RobotObjectV1(name="r9", status={})], {}))[0]

    assert robot["current_mission"] == "now" and robot["queued_missions"] == ["next"]
    assert idle["current_mission"] is None and idle["queued_missions"] == []


# ---------------------------------------------------------------------------
# POST /api/v1/navigate
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_mission_ahead_is_the_one_directly_in_front():
    rows = [_row("running", state=State.RUNNING, started=_at(1)),
            _row("queued", created=_at(5)), _row("mine", created=_at(9)),
            _row("theirs", robot="r2", created=_at(1))]
    assert mission_ahead(rows, "mine") == "queued"
    assert mission_ahead(rows, "queued") == "running"
    assert mission_ahead(rows, "running") is None
    assert mission_ahead(rows, "unknown") is None


@pytest.mark.unit
async def test_navigate_returns_state_and_the_mission_ahead():
    import packages.api.main as api_main
    rows = [_row("running", state=State.RUNNING, started=_at(1)),
            _row("nav_r1_x_ab12", created=_at(9))]
    fake = SimpleNamespace(
        navigate=AsyncMock(return_value={"success": True, "mission_name": "nav_r1_x_ab12"}),
        database=SimpleNamespace(list_objects=AsyncMock(return_value=rows)))
    with patch.object(api_main, "service", fake):
        response = await api_main.navigate(api_main.NavigationRequest(
            robot_name="r1", target_x=1.0, target_y=2.0))

    assert response.success and response.state == "PENDING"
    assert response.queued_behind == "running"


@pytest.mark.unit
async def test_navigate_to_an_idle_robot_is_not_queued_behind_anything():
    import packages.api.main as api_main
    rows = [_row("nav_r1_x_ab12", created=_at(9))]
    fake = SimpleNamespace(
        navigate=AsyncMock(return_value={"success": True, "mission_name": "nav_r1_x_ab12"}),
        database=SimpleNamespace(list_objects=AsyncMock(return_value=rows)))
    with patch.object(api_main, "service", fake):
        response = await api_main.navigate(api_main.NavigationRequest(
            robot_name="r1", target_x=1.0, target_y=2.0))
    assert response.queued_behind is None and response.state == "PENDING"


@pytest.mark.unit
async def test_navigate_survives_an_unreadable_queue_and_a_failure_has_no_position():
    import packages.api.main as api_main
    fake = SimpleNamespace(
        navigate=AsyncMock(return_value={"success": True, "mission_name": "n"}),
        database=SimpleNamespace(list_objects=AsyncMock(side_effect=RuntimeError("db"))))
    with patch.object(api_main, "service", fake):
        response = await api_main.navigate(api_main.NavigationRequest(
            robot_name="r1", target_x=1.0, target_y=2.0))
    assert response.success and response.state is None and response.queued_behind is None

    failed = SimpleNamespace(
        navigate=AsyncMock(return_value={"success": False, "error": "no path"}),
        database=SimpleNamespace(list_objects=AsyncMock()))
    with patch.object(api_main, "service", failed):
        response = await api_main.navigate(api_main.NavigationRequest(
            robot_name="r1", target_x=1.0, target_y=2.0))
    assert not response.success and response.state is None
    failed.database.list_objects.assert_not_awaited()


class _Store:
    """The idempotency store's contract, in memory (see tests/unit/test_idempotency.py)."""
    rows = {}

    def __init__(self, *args, **kwargs):
        pass

    async def claim(self, key, route, digest):
        from packages.api.idempotency import Claim, NEW, REPLAY
        row = self.rows.get((key, route))
        if row is None:
            self.rows[(key, route)] = {"status": None, "body": None}
            return Claim(NEW)
        return Claim(REPLAY, row["status"], row["body"])

    async def complete(self, key, route, digest, status, body):
        self.rows[(key, route)] = {"status": status, "body": json.loads(json.dumps(body))}

    async def release(self, key, route, digest):
        self.rows.pop((key, route), None)

    async def purge(self):
        return 0


@pytest.mark.unit
async def test_a_double_submit_with_an_idempotency_key_makes_one_mission():
    import packages.api.main as api_main
    _Store.rows = {}
    rows = [_row("nav_r1_x_ab12", created=_at(9))]
    fake = SimpleNamespace(
        navigate=AsyncMock(return_value={"success": True, "mission_name": "nav_r1_x_ab12"}),
        database=SimpleNamespace(list_objects=AsyncMock(return_value=rows),
                                 is_running=lambda: True, connection=MagicMock()))
    body = {"robot_name": "r1", "target_x": 1.0, "target_y": 2.0}
    transport = httpx.ASGITransport(app=api_main.app, raise_app_exceptions=False)
    with patch.object(api_main, "service", fake), patch.object(api_main, "IdempotencyStore", _Store):
        async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
            first = await client.post("/api/v1/navigate", json=body,
                                      headers={"Idempotency-Key": "k1"})
            again = await client.post("/api/v1/navigate", json=body,
                                      headers={"Idempotency-Key": "k1"})
            # no key: never refused, every request is its own
            plain = await client.post("/api/v1/navigate", json=body)
            plain2 = await client.post("/api/v1/navigate", json=body)

    assert first.status_code == again.status_code == plain.status_code == plain2.status_code == 200
    assert again.json() == first.json()
    assert again.headers["idempotent-replayed"] == "true"
    assert first.json()["state"] == "PENDING"
    assert fake.navigate.await_count == 3                # 1 keyed + 2 unkeyed
