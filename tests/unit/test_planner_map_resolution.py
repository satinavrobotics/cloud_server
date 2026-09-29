"""A planner request without map_id: the robot's current map, else a clear 400 (never `default`)."""

import os

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

from types import SimpleNamespace  # noqa: E402
from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from packages.services.mission_planner import main as planner_main  # noqa: E402
from packages.services.mission_planner.server import (  # noqa: E402
    MapResolutionError, MissionPlannerService)

pytestmark = pytest.mark.unit


@pytest.fixture
def service():
    with patch("packages.services.mission_planner.server.GraphDatabaseService"), \
         patch("packages.services.mission_planner.server.PostgresDatabase"):
        svc = MissionPlannerService()
    return svc


def robot(current_map):
    return SimpleNamespace(name="r1", current_map=current_map,
                           status=SimpleNamespace(pose=SimpleNamespace(x=0.0, y=0.0)))


class TestResolve:
    async def test_explicit_map_wins(self, service):
        service.get_robot_status = AsyncMock(return_value=robot("other"))
        assert await service._resolve_map("yard", "r1") == "yard"
        service.get_robot_status.assert_not_awaited()

    async def test_robot_current_map(self, service):
        service.get_robot_status = AsyncMock(return_value=robot("yard"))
        assert await service._resolve_map(None, "r1") == "yard"
        assert await service._resolve_map("", "r1") == "yard"

    @pytest.mark.parametrize("current", [None, "", "GEO", "LOCAL"])
    async def test_robot_without_a_real_map_is_an_error(self, service, current):
        service.get_robot_status = AsyncMock(return_value=robot(current))
        with pytest.raises(MapResolutionError, match="r1"):
            await service._resolve_map(None, "r1")

    async def test_sentinel_map_id_is_not_a_map(self, service):
        service.get_robot_status = AsyncMock(return_value=robot("yard"))
        assert await service._resolve_map("GEO", "r1") == "yard"
        with pytest.raises(MapResolutionError):
            service._require_map("LOCAL")

    async def test_no_robot_no_map_is_an_error_not_default(self, service):
        assert service.default_map_id is None
        with pytest.raises(MapResolutionError, match="map_id"):
            await service._resolve_map(None, None)
        with pytest.raises(MapResolutionError):
            service._require_map(None)

    async def test_unknown_robot_is_an_error(self, service):
        service.get_robot_status = AsyncMock(return_value=None)
        with pytest.raises(MapResolutionError):
            await service._resolve_map(None, "ghost")

    async def test_configured_fallback_is_opt_in(self):
        with patch("packages.services.mission_planner.server.GraphDatabaseService"), \
             patch("packages.services.mission_planner.server.PostgresDatabase"):
            svc = MissionPlannerService(default_map_id="fixture")
        assert svc._require_map(None) == "fixture"


class TestPlannerCalls:
    async def test_plan_and_execute_uses_the_robot_map(self, service):
        service.get_robot_status = AsyncMock(return_value=robot("yard"))
        service.find_closest_node_to_robot = AsyncMock(return_value=(None, "stop here"))
        result = await service.plan_and_execute_mission("r1", target_x=1.0, target_y=2.0)
        assert result["failed_at"] == "find_robot_node"
        assert service.find_closest_node_to_robot.await_args.args[1] == "yard"

    async def test_plan_and_execute_without_a_map_fails_clearly(self, service):
        service.get_robot_status = AsyncMock(return_value=robot(None))
        result = await service.plan_and_execute_mission("r1", target_x=1.0, target_y=2.0)
        assert result["success"] is False and result["failed_at"] == "map_resolution"
        assert "map_id" in result["error"]

    async def test_helpers_do_not_query_a_default_map(self, service):
        node, err = await service.find_closest_node_to_target(1.0, 2.0, None)
        assert node is None and "map_id" in err
        path, err = await service.find_path("a", "b", None)
        assert path is None and "map_id" in err
        poses, err = service.get_node_poses(["a"], None)
        assert poses is None and "map_id" in err
        service.graph_db.k_nearest_neighbors.assert_not_called()
        service.graph_db.shortest_path.assert_not_called()

    async def test_find_nearby_nodes_uses_the_robot_map(self, service):
        service.get_robot_status = AsyncMock(return_value=robot("yard"))
        service.graph_db.nodes_in_range.return_value = ([], [])
        service._robot_xy_in_map = AsyncMock(return_value=(0.0, 0.0))
        await service.find_nearby_nodes("r1")
        assert service.graph_db.nodes_in_range.call_args.kwargs["map_id"] == "yard"


class TestRoutes:
    async def test_navigate_route_is_400_without_a_map(self):
        fake = MagicMock()
        fake.plan_and_execute_mission = AsyncMock(return_value={
            "success": False, "robot_name": "r1", "failed_at": "map_resolution",
            "error": "No map_id given and robot 'r1' has no current map: pass map_id"})
        with patch.object(planner_main, "service", fake):
            with pytest.raises(HTTPException) as exc:
                await planner_main.navigate(planner_main.NavigationRequest(
                    robot_name="r1", target_x=1.0, target_y=2.0))
        assert exc.value.status_code == 400 and "map_id" in exc.value.detail

    async def test_plan_route_is_400_without_a_map(self):
        fake = MagicMock()
        fake.get_mission_plan = AsyncMock(side_effect=MapResolutionError("no map: pass map_id"))
        with patch.object(planner_main, "service", fake):
            with pytest.raises(HTTPException) as exc:
                await planner_main.get_mission_plan("m1")
        assert exc.value.status_code == 400


class TestApiPassThrough:
    async def test_call_mission_planner_passes_map_id(self):
        from packages.api.server import ApiDelegationService
        svc = ApiDelegationService.__new__(ApiDelegationService)
        svc.logger = MagicMock()
        svc.mission_planner = MagicMock(navigate=AsyncMock(return_value={"success": True}))
        await svc._call_mission_planner("r1", 1.0, 2.0, "yard")
        assert svc.mission_planner.navigate.await_args.kwargs["map_id"] == "yard"

    async def test_navigate_turns_a_planner_400_into_400(self):
        from packages.api.server import ApiDelegationService
        err = Exception("bad request")
        err.response = SimpleNamespace(status_code=400,
                                       json=lambda: {"detail": "No map_id given: pass map_id"})
        svc = ApiDelegationService.__new__(ApiDelegationService)
        svc.logger = MagicMock()
        svc.database = MagicMock(get_object=AsyncMock(return_value=object()))
        svc.mission_planner = MagicMock(navigate=AsyncMock(side_effect=err))
        with pytest.raises(HTTPException) as exc:
            await svc.navigate("r1", target_x=1.0, target_y=2.0)
        assert exc.value.status_code == 400 and "pass map_id" in exc.value.detail
