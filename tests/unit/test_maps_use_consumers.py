"""Maps §14 U2 (docs/satinav-maps-redesign.md §14.6): the consumers read the robot's session.

- dispatcher `_route_in_robot_frame`: waypoints on the session's map go through
  inverse(map_T_session); an unplaced session fails the node ("robot is not placed on map X");
  a map the robot has no session on: the transition fallback (M2 rule, warning), refused once
  SESSIONLESS_FALLBACK is off; an unreadable session: the M2 rule;
- planner: the default map is the session's map, the robot's position goes through
  map_T_session, an unplaced session is a 409 (POST /api/v1/navigate, passed on by the API);
- run recorder: mission_runs.map_id = the session's map (null when mapless);
- bag metadata: the session's map and session_id.
"""
import math
import os
from types import SimpleNamespace

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

import cloud_common.objects as api_objects  # noqa: E402
import cloud_common.objects.mission as mission_object  # noqa: E402
from cloud_common.objects.map import MapObjectV1  # noqa: E402
from packages.controllers.mission import server as dispatch_server  # noqa: E402
from packages.controllers.mission.server import Robot, RouteRefused  # noqa: E402
from packages.database.postgres import PostgresDatabase  # noqa: E402
from packages.services.mission_planner import main as planner_main  # noqa: E402
from packages.services.mission_planner import server as planner_server  # noqa: E402
from packages.services.mission_planner.server import (  # noqa: E402
    MissionPlannerService, RobotNotPlacedError)
from packages.utils import map_geo  # noqa: E402

pytestmark = pytest.mark.unit

T = {"tx": 10.0, "ty": -5.0, "yaw": math.radians(30)}


def session(map_name="shed", aligned=True, transform=T, purpose="operate"):
    return {"session_id": "s1", "map_name": map_name, "purpose": purpose, "aligned": aligned,
            "map_t_session": dict(transform), "datum": None, "placement": None,
            "paused_at": None, "map_geo": None, "map_type": "local"}


# --- dispatcher ----------------------------------------------------------------------------------

def _robot():
    db = AsyncMock(spec=PostgresDatabase)
    db.update_status = AsyncMock()
    server = MagicMock()
    server.push_telemetry = False
    server.mission_ctrl_url = None
    server.disable_request_factsheet = True
    r = Robot("r1", db, MagicMock(), "prefix", server)
    r._robot_object = api_objects.RobotObjectV1(name="r1", status={"online": True})
    return r, db


def _route(map_id="shed"):
    # a waypoint stored in the map frame: where the robot's pose (2, 1, 0.2) lies on the map
    x, y, yaw = map_geo.apply_pose(T, 2.0, 1.0, 0.2)
    return mission_object.MissionRouteNodeV1(waypoints=[
        {"x": x, "y": y, "theta": yaw, "map_id": map_id},
        {"x": 4.0, "y": 4.0, "theta": 0.0, "map_id": ""}])


class TestDispatcherRoute:
    async def test_session_map_goes_through_the_session(self):
        r, db = _robot()
        r._read_open_session = AsyncMock(return_value=session())
        out = await r._route_in_robot_frame(_route())
        w = out.waypoints
        assert (w[0].x, w[0].y, w[0].theta) == pytest.approx((2.0, 1.0, 0.2))
        assert (w[1].x, w[1].y) == (4.0, 4.0)
        db.get_object.assert_not_awaited()  # no map/datum rule needed

    async def test_unplaced_session_is_refused(self):
        r, _ = _robot()
        r._read_open_session = AsyncMock(return_value=session(aligned=False))
        with pytest.raises(RouteRefused, match="not placed on map shed"):
            await r._route_in_robot_frame(_route())

    async def test_other_map_falls_back_with_a_warning(self):
        r, db = _robot()
        r._read_open_session = AsyncMock(return_value=session(map_name="barn"))
        db.get_object = AsyncMock(return_value=MapObjectV1(name="shed", type="local"))
        r.warning = MagicMock()
        route = _route()
        assert await r._route_in_robot_frame(route) is route  # local: identity, as M2
        assert any("transition fallback" in c.args[0] for c in r.warning.call_args_list)

    async def test_no_fallback_after_u6(self):
        r, _ = _robot()
        r._read_open_session = AsyncMock(return_value=None)
        with patch.object(dispatch_server, "SESSIONLESS_FALLBACK", False):
            with pytest.raises(RouteRefused, match="not using map shed"):
                await r._route_in_robot_frame(_route())

    async def test_unreadable_session_uses_the_m2_rule(self):
        r, db = _robot()
        r._read_open_session = AsyncMock(return_value=dispatch_server.SESSION_UNKNOWN)
        db.get_object = AsyncMock(return_value=MapObjectV1(name="shed", type="local"))
        route = _route()
        assert await r._route_in_robot_frame(route) is route

    async def test_mapless_route_reads_nothing(self):
        r, _ = _robot()
        r._read_open_session = AsyncMock()
        route = _route(map_id="")
        assert await r._route_in_robot_frame(route) is route
        r._read_open_session.assert_not_awaited()

    async def test_refused_node_fails_and_nothing_is_sent(self):
        r, _ = _robot()
        r._read_open_session = AsyncMock(return_value=session(aligned=False))
        mission = api_objects.MissionObjectV1(
            name="m1", robot="r1", status={}, timeout=1000,
            mission_tree=[{"name": "0", "parent": "root", "route": _route().dict()}])
        r._missions[mission.name] = mission
        recorded = []
        r._record = lambda hook, *a, **kw: recorded.append((hook, kw))
        await r._try_start_mission()
        assert mission.status.node_status["0"].state == mission_object.MissionStateV1.FAILED
        assert mission.status.state == mission_object.MissionStateV1.FAILED
        assert "not placed on map shed" in mission.status.failure_reason
        orders = [c for c in r._mqtt_client.publish.call_args_list if c.args[0].endswith("/order")]
        assert orders == []
        # the run recorder got the session's map
        assert ("run_started", {"session_map": "shed"}) in recorded
        r._cancel_mission_timeout()

    async def test_placed_node_is_sent_in_the_robot_frame(self):
        r, _ = _robot()
        r._read_open_session = AsyncMock(return_value=session())
        mission = api_objects.MissionObjectV1(
            name="m1", robot="r1", status={}, timeout=1000,
            mission_tree=[{"name": "0", "parent": "root", "route": _route().dict()}])
        r._missions[mission.name] = mission
        await r._try_start_mission()
        orders = [c for c in r._mqtt_client.publish.call_args_list if c.args[0].endswith("/order")]
        assert len(orders) == 1 and mission.status.state == mission_object.MissionStateV1.RUNNING
        import json
        node = json.loads(orders[0].args[1])["nodes"][1]["nodePosition"]  # [0]: robot pose
        assert (node["x"], node["y"]) == pytest.approx((2.0, 1.0))
        r._cancel_mission_timeout()


class TestRecorder:
    def test_run_map_is_the_session_map(self):
        from packages.controllers.mission import fleet_recorder as fr
        rec = fr.FleetRecorder.__new__(fr.FleetRecorder)
        captured = {}
        rec._runs = {}
        rec._clock = lambda: __import__("datetime").datetime(2026, 9, 30,
                                                              tzinfo=__import__("datetime").timezone.utc)
        rec._track = MagicMock(return_value=SimpleNamespace(sw=SimpleNamespace(value=None)))
        rec.policy = MagicMock()
        rec.policy.level_for.return_value = SimpleNamespace(value="full")
        rec.policy.site_for.return_value = None
        rec._submit = lambda job: captured.setdefault("run", job.info)
        mission = api_objects.MissionObjectV1(
            name="m1", robot="r1", status={},
            mission_tree=[{"name": "0", "parent": "root", "route": _route().dict()}])
        robot = api_objects.RobotObjectV1(name="r1", status={"pose": {"map_id": "map"}},
                                          current_map="GEO")
        rec.run_started("r1", mission, robot, session_map=None)
        assert captured["run"].map_id is None  # mapless: not the GEO sentinel, not "map"
        rec._runs, captured = {}, {}
        rec._submit = lambda job: captured.setdefault("run", job.info)
        rec.run_started("r1", mission, robot, session_map="shed")
        assert captured["run"].map_id == "shed"
        rec._runs, captured = {}, {}
        rec._submit = lambda job: captured.setdefault("run", job.info)
        rec.run_started("r1", mission, robot)  # session unknown: the M2 rule
        assert captured["run"].map_id == "GEO"


# --- planner ----------------------------------------------------------------------------------------

@pytest.fixture
def planner():
    with patch("packages.services.mission_planner.server.GraphDatabaseService"), \
         patch("packages.services.mission_planner.server.PostgresDatabase"):
        svc = MissionPlannerService()
    return svc


def _probot(current_map=None, pose=(2.0, 1.0)):
    return SimpleNamespace(name="r1", current_map=current_map, datum=None,
                           status=SimpleNamespace(pose=SimpleNamespace(x=pose[0], y=pose[1])))


class TestPlanner:
    async def test_default_map_is_the_session_map(self, planner):
        planner._open_session = AsyncMock(return_value=session())
        planner.get_robot_status = AsyncMock(return_value=_probot(current_map="old"))
        assert await planner._resolve_map(None, "r1") == "shed"
        assert await planner._resolve_map("yard", "r1") == "yard"

    async def test_current_map_fallback_without_a_session(self, planner):
        planner._open_session = AsyncMock(return_value=None)
        planner.get_robot_status = AsyncMock(return_value=_probot(current_map="old"))
        assert await planner._resolve_map(None, "r1") == "old"
        with patch.object(planner_server, "SESSIONLESS_FALLBACK", False):
            with pytest.raises(planner_server.MapResolutionError):
                await planner._resolve_map(None, "r1")

    async def test_robot_position_through_the_session(self, planner):
        planner._open_session = AsyncMock(return_value=session())
        x, y = await planner._robot_xy_in_map(_probot(), "shed")
        assert (x, y) == pytest.approx(map_geo.apply_transform(T, 2.0, 1.0))

    async def test_unplaced_is_refused(self, planner):
        planner._open_session = AsyncMock(return_value=session(aligned=False))
        with pytest.raises(RobotNotPlacedError, match="not placed"):
            await planner._robot_xy_in_map(_probot(), "shed")
        planner.get_robot_status = AsyncMock(return_value=_probot())
        node, err = await planner.find_closest_node_to_robot("r1", "shed")
        assert node is None and err.startswith(planner_server.NOT_PLACED_PREFIX)
        planner.find_closest_node_to_robot = AsyncMock(return_value=(None, err))
        result = await planner.plan_and_execute_mission("r1", target_x=1.0, target_y=1.0)
        assert result["failed_at"] == "robot_not_placed"
        assert not result["error"].startswith(planner_server.NOT_PLACED_PREFIX)
        out = await planner.find_nearby_nodes("r1", "shed")
        assert out["nodes"] == [] and "not placed" in out["error"]

    async def test_other_map_falls_back(self, planner):
        planner._open_session = AsyncMock(return_value=session(map_name="barn"))
        planner.database.get_object = AsyncMock(return_value=MapObjectV1(name="shed",
                                                                          type="local"))
        assert await planner._robot_xy_in_map(_probot(), "shed") == (2.0, 1.0)

    async def test_navigate_route_is_409(self):
        fake = MagicMock()
        fake.plan_and_execute_mission = AsyncMock(return_value={
            "success": False, "robot_name": "r1", "failed_at": "robot_not_placed",
            "error": "Robot 'r1' is not placed on map 'shed'"})
        with patch.object(planner_main, "service", fake):
            with pytest.raises(HTTPException) as exc:
                await planner_main.navigate(planner_main.NavigationRequest(
                    robot_name="r1", target_x=1.0, target_y=2.0))
        assert exc.value.status_code == 409

    async def test_api_passes_the_409_on(self):
        from packages.api.server import ApiDelegationService
        err = Exception("conflict")
        err.response = SimpleNamespace(status_code=409,
                                       json=lambda: {"detail": "Robot 'r1' is not placed"})
        svc = ApiDelegationService.__new__(ApiDelegationService)
        svc.logger = MagicMock()
        svc.database = MagicMock(get_object=AsyncMock(return_value=object()))
        svc.mission_planner = MagicMock(navigate=AsyncMock(side_effect=err))
        with pytest.raises(HTTPException) as exc:
            await svc.navigate("r1", target_x=1.0, target_y=2.0)
        assert exc.value.status_code == 409 and "not placed" in exc.value.detail


# --- bag metadata ------------------------------------------------------------------------------------

class TestBagMetadata:
    async def test_session_map_and_id(self):
        from packages.api import maps
        from packages.api.server import ApiDelegationService
        svc = ApiDelegationService.__new__(ApiDelegationService)
        svc.logger = MagicMock()
        svc.database = MagicMock(get_object=AsyncMock(return_value=api_objects.RobotObjectV1(
            name="r1", status={}, current_map="GEO")))
        svc.rosbag_db = MagicMock()
        svc.rosbag_db.create_upload_url.return_value = {"upload_url": "u", "expires_in": 60}
        with patch.object(maps, "robot_sessions", AsyncMock(return_value={
                "r1": {"map": "shed", "session_id": "s1"}})):
            out = await svc.create_bag_upload_url("r1")
        kw = svc.rosbag_db.create_upload_url.call_args.kwargs
        assert kw["map_id"] == "shed" and kw["session_id"] == "s1"
        assert out["map_id"] == "shed" and out["session_id"] == "s1"
        with patch.object(maps, "robot_sessions", AsyncMock(return_value={})):
            out = await svc.create_bag_upload_url("r1")
        assert out["map_id"] is None and out["session_id"] is None
