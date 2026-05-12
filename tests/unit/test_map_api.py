"""Unit tests for map CRUD API endpoints and GPS navigation."""

import pytest
from unittest.mock import AsyncMock, Mock, patch, MagicMock
import uuid

from cloud_common.objects.map import MapObjectV1, MapSpecV1, MapStatusV1
from cloud_common.objects.object import ObjectLifecycleV1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_map_obj(name="site_a", datum_lat=47.37, datum_lon=8.54, bearing=0.0):
    return MapObjectV1(
        name=name,
        datum_latitude=datum_lat,
        datum_longitude=datum_lon,
        datum_bearing_deg=bearing,
        status=MapStatusV1(node_count=5, edge_count=8),
    )


# ---------------------------------------------------------------------------
# ApiDelegationService.get_map
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestApiDelegationGetMap:

    @pytest.mark.asyncio
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.RosbagDatabaseService')
    @patch('packages.topomap_dbs.client.ModelDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.api.server.LiveKitClient')
    async def test_get_map_success(
        self, mock_lk, mock_mp, mock_db, mock_graph, mock_model, mock_rosbag, mock_image
    ):
        from packages.api.server import ApiDelegationService

        map_obj = _make_map_obj()
        mock_db_inst = AsyncMock()
        mock_db_inst.get_object = AsyncMock(return_value=map_obj)
        mock_db.return_value = mock_db_inst

        mock_graph_inst = Mock()
        mock_graph_inst.get_map_stats.return_value = {"node_count": 5, "edge_count": 8}
        mock_graph.return_value = mock_graph_inst

        service = ApiDelegationService(arango_password="x", postgres_password="x")
        result = await service.get_map("site_a")

        assert result["success"] is True
        assert result["map_id"] == "site_a"
        assert result["datum_latitude"] == 47.37
        assert result["node_count"] == 5

    @pytest.mark.asyncio
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.RosbagDatabaseService')
    @patch('packages.topomap_dbs.client.ModelDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.api.server.LiveKitClient')
    async def test_get_map_not_found(
        self, mock_lk, mock_mp, mock_db, mock_graph, mock_model, mock_rosbag, mock_image
    ):
        from packages.api.server import ApiDelegationService

        mock_db_inst = AsyncMock()
        mock_db_inst.get_object = AsyncMock(side_effect=Exception("not found"))
        mock_db.return_value = mock_db_inst
        mock_graph.return_value = Mock()

        service = ApiDelegationService(arango_password="x", postgres_password="x")
        result = await service.get_map("missing")

        assert result["success"] is False
        assert "not found" in result["error"].lower()


# ---------------------------------------------------------------------------
# ApiDelegationService.update_map_datum
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestApiDelegationUpdateDatum:

    @pytest.mark.asyncio
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.RosbagDatabaseService')
    @patch('packages.topomap_dbs.client.ModelDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.api.server.LiveKitClient')
    async def test_update_datum_success(
        self, mock_lk, mock_mp, mock_db, mock_graph, mock_model, mock_rosbag, mock_image
    ):
        from packages.api.server import ApiDelegationService

        map_obj = _make_map_obj()
        mock_db_inst = AsyncMock()
        mock_db_inst.get_object = AsyncMock(return_value=map_obj)
        mock_db_inst.update_spec = AsyncMock()
        mock_db.return_value = mock_db_inst
        mock_graph.return_value = Mock()

        service = ApiDelegationService(arango_password="x", postgres_password="x")
        result = await service.update_map_datum("site_a", 47.999, 8.888, 45.0)

        assert result["success"] is True
        assert result["datum_latitude"] == 47.999
        assert result["datum_longitude"] == 8.888
        assert result["datum_bearing_deg"] == 45.0
        mock_db_inst.update_spec.assert_called_once()

    @pytest.mark.asyncio
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.RosbagDatabaseService')
    @patch('packages.topomap_dbs.client.ModelDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.api.server.LiveKitClient')
    async def test_update_datum_map_not_found(
        self, mock_lk, mock_mp, mock_db, mock_graph, mock_model, mock_rosbag, mock_image
    ):
        from packages.api.server import ApiDelegationService

        mock_db_inst = AsyncMock()
        mock_db_inst.get_object = AsyncMock(side_effect=Exception("not found"))
        mock_db.return_value = mock_db_inst
        mock_graph.return_value = Mock()

        service = ApiDelegationService(arango_password="x", postgres_password="x")
        result = await service.update_map_datum("ghost", 0.0, 0.0)

        assert result["success"] is False


# ---------------------------------------------------------------------------
# ApiDelegationService.load_map — Postgres registration
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestApiDelegationLoadMapPostgres:

    @pytest.mark.asyncio
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.RosbagDatabaseService')
    @patch('packages.topomap_dbs.client.ModelDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.api.server.LiveKitClient')
    async def test_load_map_registers_in_postgres(
        self, mock_lk, mock_mp, mock_db, mock_graph, mock_model, mock_rosbag, mock_image
    ):
        from packages.api.server import ApiDelegationService

        mock_db_inst = AsyncMock()
        mock_db_inst.create_object = AsyncMock()
        mock_db.return_value = mock_db_inst

        mock_graph_inst = Mock()
        mock_graph_inst.create_map.return_value = True
        mock_graph_inst.get_map_stats.return_value = {"node_count": 0, "edge_count": 0}
        mock_graph_inst.get_all_nodes.return_value = []
        mock_graph_inst.get_edges.return_value = []
        mock_graph.return_value = mock_graph_inst

        service = ApiDelegationService(arango_password="x", postgres_password="x")
        result = await service.load_map(
            map_id="site_b",
            datum_latitude=47.3769,
            datum_longitude=8.5417,
            datum_bearing_deg=12.5,
        )

        assert result["success"] is True
        mock_db_inst.create_object.assert_called_once()
        # Verify the MapObjectV1 passed to create_object has the right datum
        call_args = mock_db_inst.create_object.call_args[0]
        created_obj = call_args[0]
        assert isinstance(created_obj, MapObjectV1)
        assert created_obj.datum_latitude == 47.3769
        assert created_obj.datum_bearing_deg == 12.5

    @pytest.mark.asyncio
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.RosbagDatabaseService')
    @patch('packages.topomap_dbs.client.ModelDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.api.server.LiveKitClient')
    async def test_load_map_updates_existing_postgres_record(
        self, mock_lk, mock_mp, mock_db, mock_graph, mock_model, mock_rosbag, mock_image
    ):
        """If map already exists in Postgres, update_spec is called instead of create_object."""
        from packages.api.server import ApiDelegationService

        mock_db_inst = AsyncMock()
        mock_db_inst.create_object = AsyncMock(side_effect=Exception("duplicate"))
        mock_db_inst.update_spec = AsyncMock()
        mock_db.return_value = mock_db_inst

        mock_graph_inst = Mock()
        mock_graph_inst.create_map.return_value = True
        mock_graph_inst.get_map_stats.return_value = {"node_count": 3, "edge_count": 2}
        mock_graph_inst.get_all_nodes.return_value = []
        mock_graph_inst.get_edges.return_value = []
        mock_graph.return_value = mock_graph_inst

        service = ApiDelegationService(arango_password="x", postgres_password="x")
        result = await service.load_map(map_id="existing")

        assert result["success"] is True
        mock_db_inst.update_spec.assert_called_once()


# ---------------------------------------------------------------------------
# ApiDelegationService.delete_map — Postgres cleanup
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestApiDelegationDeleteMapPostgres:

    @pytest.mark.asyncio
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.RosbagDatabaseService')
    @patch('packages.topomap_dbs.client.ModelDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.api.server.LiveKitClient')
    async def test_delete_map_removes_postgres_record(
        self, mock_lk, mock_mp, mock_db, mock_graph, mock_model, mock_rosbag, mock_image
    ):
        from packages.api.server import ApiDelegationService

        mock_db_inst = AsyncMock()
        mock_db_inst.set_lifecycle = AsyncMock()
        mock_db.return_value = mock_db_inst

        mock_graph_inst = Mock()
        mock_graph_inst.delete_map.return_value = {"success": True}
        mock_graph.return_value = mock_graph_inst
        mock_image.return_value.delete_map = Mock(return_value={"success": True})

        service = ApiDelegationService(arango_password="x", postgres_password="x")
        result = await service.delete_map("site_a")

        assert result["success"] is True
        mock_db_inst.set_lifecycle.assert_called_once()
        call_args = mock_db_inst.set_lifecycle.call_args[0]
        assert call_args[0] is MapObjectV1
        assert call_args[1] == "site_a"
        assert call_args[2] == ObjectLifecycleV1.DELETED

    @pytest.mark.asyncio
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.RosbagDatabaseService')
    @patch('packages.topomap_dbs.client.ModelDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.api.server.LiveKitClient')
    async def test_delete_map_postgres_failure_does_not_block(
        self, mock_lk, mock_mp, mock_db, mock_graph, mock_model, mock_rosbag, mock_image
    ):
        """A Postgres failure on delete should be logged but not cause an error response."""
        from packages.api.server import ApiDelegationService

        mock_db_inst = AsyncMock()
        mock_db_inst.set_lifecycle = AsyncMock(side_effect=Exception("Postgres down"))
        mock_db.return_value = mock_db_inst

        mock_graph_inst = Mock()
        mock_graph_inst.delete_map.return_value = {"success": True}
        mock_graph.return_value = mock_graph_inst
        mock_image.return_value.delete_map = Mock(return_value={"success": True})

        service = ApiDelegationService(arango_password="x", postgres_password="x")
        result = await service.delete_map("site_a")

        # ArangoDB + MinIO succeeded, so overall success even if Postgres had an error
        assert result["success"] is True


# ---------------------------------------------------------------------------
# ApiDelegationService.navigate — GPS passthrough
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestApiDelegationNavigateGps:

    @pytest.mark.asyncio
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.RosbagDatabaseService')
    @patch('packages.topomap_dbs.client.ModelDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.api.server.LiveKitClient')
    async def test_navigate_gps_forwarded_to_planner(
        self, mock_lk, mock_mp, mock_db, mock_graph, mock_model, mock_rosbag, mock_image
    ):
        from packages.api.server import ApiDelegationService
        from cloud_common.objects.robot import RobotObjectV1

        mock_db_inst = AsyncMock()
        mock_db_inst.get_object = AsyncMock(return_value=Mock(spec=RobotObjectV1))
        mock_db.return_value = mock_db_inst
        mock_graph.return_value = Mock()

        mock_mp_inst = AsyncMock()
        mock_mp_inst.navigate.return_value = {"success": True, "mission_name": "nav_001"}
        mock_mp.return_value = mock_mp_inst

        service = ApiDelegationService(arango_password="x", postgres_password="x")
        result = await service.navigate(
            robot_name="robot_1",
            target_lat=47.3770,
            target_lon=8.5420,
            map_id="site_a",
        )

        assert result["success"] is True
        call_kwargs = mock_mp_inst.navigate.call_args[1]
        assert call_kwargs["target_lat"] == 47.3770
        assert call_kwargs["target_lon"] == 8.5420

    @pytest.mark.asyncio
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.RosbagDatabaseService')
    @patch('packages.topomap_dbs.client.ModelDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.api.server.LiveKitClient')
    async def test_navigate_xy_still_works(
        self, mock_lk, mock_mp, mock_db, mock_graph, mock_model, mock_rosbag, mock_image
    ):
        from packages.api.server import ApiDelegationService
        from cloud_common.objects.robot import RobotObjectV1

        mock_db_inst = AsyncMock()
        mock_db_inst.get_object = AsyncMock(return_value=Mock(spec=RobotObjectV1))
        mock_db.return_value = mock_db_inst
        mock_graph.return_value = Mock()

        mock_mp_inst = AsyncMock()
        mock_mp_inst.navigate.return_value = {"success": True, "mission_name": "nav_002"}
        mock_mp.return_value = mock_mp_inst

        service = ApiDelegationService(arango_password="x", postgres_password="x")
        result = await service.navigate(
            robot_name="robot_1",
            target_x=10.0,
            target_y=20.0,
        )

        assert result["success"] is True
        call_kwargs = mock_mp_inst.navigate.call_args[1]
        assert call_kwargs["target_x"] == 10.0
        assert call_kwargs["target_y"] == 20.0
        assert call_kwargs.get("target_lat") is None


# ---------------------------------------------------------------------------
# MissionPlannerService._get_map_datum
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestMissionPlannerGetMapDatum:

    @pytest.mark.asyncio
    @patch('packages.services.mission_planner.server.GraphDatabaseService')
    @patch('packages.services.mission_planner.server.PostgresDatabase')
    async def test_returns_datum_when_set(self, mock_db, mock_graph):
        from packages.services.mission_planner.server import MissionPlannerService

        map_obj = _make_map_obj(datum_lat=47.3769, datum_lon=8.5417, bearing=12.5)
        mock_db_inst = AsyncMock()
        mock_db_inst.get_object = AsyncMock(return_value=map_obj)
        mock_db.return_value = mock_db_inst
        mock_graph.return_value = Mock()

        service = MissionPlannerService()
        datum = await service._get_map_datum("site_a")

        assert datum is not None
        assert datum["lat"] == 47.3769
        assert datum["lon"] == 8.5417
        assert datum["bearing_deg"] == 12.5

    @pytest.mark.asyncio
    @patch('packages.services.mission_planner.server.GraphDatabaseService')
    @patch('packages.services.mission_planner.server.PostgresDatabase')
    async def test_returns_none_when_no_datum(self, mock_db, mock_graph):
        from packages.services.mission_planner.server import MissionPlannerService

        map_obj = MapObjectV1(name="undated")  # no datum set
        mock_db_inst = AsyncMock()
        mock_db_inst.get_object = AsyncMock(return_value=map_obj)
        mock_db.return_value = mock_db_inst
        mock_graph.return_value = Mock()

        service = MissionPlannerService()
        datum = await service._get_map_datum("undated")

        assert datum is None

    @pytest.mark.asyncio
    @patch('packages.services.mission_planner.server.GraphDatabaseService')
    @patch('packages.services.mission_planner.server.PostgresDatabase')
    async def test_returns_none_when_map_missing(self, mock_db, mock_graph):
        from packages.services.mission_planner.server import MissionPlannerService

        mock_db_inst = AsyncMock()
        mock_db_inst.get_object = AsyncMock(side_effect=Exception("not found"))
        mock_db.return_value = mock_db_inst
        mock_graph.return_value = Mock()

        service = MissionPlannerService()
        datum = await service._get_map_datum("ghost")

        assert datum is None


# ---------------------------------------------------------------------------
# MissionPlannerService.plan_and_execute_mission — GPS path
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestMissionPlannerGpsNavigation:
    """Tests for the GPS → local conversion step in plan_and_execute_mission."""

    def _make_planner_with_mocks(self, mock_db, mock_graph, datum=None, robot_pose=(10.0, 20.0)):
        from packages.services.mission_planner.server import MissionPlannerService
        from cloud_common.objects.robot import RobotObjectV1
        from cloud_common.objects import common

        # Robot
        mock_pose = Mock(spec=common.Pose2D)
        mock_pose.x, mock_pose.y = robot_pose
        mock_status = Mock()
        mock_status.pose = mock_pose
        mock_robot = Mock(spec=RobotObjectV1)
        mock_robot.status = mock_status
        mock_robot.position_mode = 'local'

        # Datum map object
        map_obj = _make_map_obj(
            datum_lat=datum["lat"] if datum else None,
            datum_lon=datum["lon"] if datum else None,
            bearing=datum.get("bearing_deg", 0.0) if datum else 0.0,
        ) if datum else MapObjectV1(name="no_datum")

        mock_db_inst = AsyncMock()
        mock_db_inst.get_object = AsyncMock(side_effect=lambda cls, name: (
            mock_robot if cls is RobotObjectV1 else map_obj
        ))
        mock_db_inst.create_object = AsyncMock()
        mock_db.return_value = mock_db_inst

        mock_graph_inst = Mock()
        mock_graph_inst.k_nearest_neighbors.return_value = (
            [{'node_id': 'n1', 'x': 10.5, 'y': 20.5, 'yaw': 0.0}], [0.7]
        )
        mock_graph_inst.nodes_in_range.return_value = (
            [{'node_id': 'n2', 'x': 50.0, 'y': 60.0, 'yaw': 0.0}], [1.0]
        )
        mock_graph_inst.shortest_path.return_value = ["n1", "n2"]
        mock_graph_inst.get_node.side_effect = [
            {'node_id': 'n1', 'x': 10.5, 'y': 20.5, 'yaw': 0.0},
            {'node_id': 'n2', 'x': 50.0, 'y': 60.0, 'yaw': 0.0},
        ]
        mock_graph.return_value = mock_graph_inst

        return MissionPlannerService()

    @pytest.mark.asyncio
    @patch('packages.services.mission_planner.server.GraphDatabaseService')
    @patch('packages.services.mission_planner.server.PostgresDatabase')
    async def test_gps_no_datum_returns_error(self, mock_db, mock_graph):
        service = self._make_planner_with_mocks(mock_db, mock_graph, datum=None)
        result = await service.plan_and_execute_mission(
            robot_name="robot_1",
            target_lat=47.377,
            target_lon=8.542,
            map_id="no_datum_map",
        )
        assert result["success"] is False
        assert result["failed_at"] == "gps_conversion"
        assert "datum" in result["error"].lower()

    @pytest.mark.asyncio
    @patch('packages.services.mission_planner.server.GraphDatabaseService')
    @patch('packages.services.mission_planner.server.PostgresDatabase')
    async def test_no_coordinates_returns_error(self, mock_db, mock_graph):
        service = self._make_planner_with_mocks(
            mock_db, mock_graph, datum={"lat": 47.0, "lon": 8.0}
        )
        result = await service.plan_and_execute_mission(robot_name="robot_1")
        assert result["success"] is False
        assert result["failed_at"] == "validation"

    @pytest.mark.asyncio
    @patch('packages.services.mission_planner.server.GraphDatabaseService')
    @patch('packages.services.mission_planner.server.PostgresDatabase')
    async def test_gps_with_datum_converts_and_plans(self, mock_db, mock_graph):
        """GPS coords + valid datum should convert to local and reach the planning steps."""
        datum = {"lat": 47.3769, "lon": 8.5417, "bearing_deg": 0.0}
        service = self._make_planner_with_mocks(mock_db, mock_graph, datum=datum)

        result = await service.plan_and_execute_mission(
            robot_name="robot_1",
            target_lat=47.3770,
            target_lon=8.5420,
            map_id="site_a",
        )

        # Conversion happened — target should now be set in result
        assert "target" in result
        assert result["target"]["x"] is not None
        assert result["target"]["y"] is not None
        # The converted coordinates are in the right ball park (~22 m east, ~11 m north)
        assert 15 < result["target"]["x"] < 30
        assert 5 < result["target"]["y"] < 20

    @pytest.mark.asyncio
    @patch('packages.services.mission_planner.server.GraphDatabaseService')
    @patch('packages.services.mission_planner.server.PostgresDatabase')
    async def test_xy_navigation_unaffected(self, mock_db, mock_graph):
        """Plain x/y navigation must still work exactly as before."""
        datum = {"lat": 47.3769, "lon": 8.5417, "bearing_deg": 0.0}
        service = self._make_planner_with_mocks(mock_db, mock_graph, datum=datum)

        result = await service.plan_and_execute_mission(
            robot_name="robot_1",
            target_x=50.0,
            target_y=60.0,
            map_id="site_a",
        )

        assert result["target"]["x"] == 50.0
        assert result["target"]["y"] == 60.0
