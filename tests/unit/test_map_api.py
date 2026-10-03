"""Unit tests for map CRUD API endpoints and GPS navigation."""

import pytest

from packages.api import maps
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
        service._geo_map_usage = AsyncMock(return_value=(0, 0))
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
        service._geo_map_usage = AsyncMock(return_value=(0, 0))
        result = await service.update_map_datum("ghost", 0.0, 0.0)

        assert result["success"] is False

    @pytest.mark.asyncio
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.RosbagDatabaseService')
    @patch('packages.topomap_dbs.client.ModelDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.api.server.LiveKitClient')
    async def test_update_datum_with_utm_frame(
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
        service._geo_map_usage = AsyncMock(return_value=(0, 0))
        result = await service.update_map_datum(
            "site_a", 47.47946, 19.03238, 0.0, datum_frame="utm", datum_utm_zone=34,
            datum_utm_north=True)

        assert result["datum_frame"] == "utm"
        assert result["datum_utm_zone"] == 34
        spec = mock_db_inst.update_spec.call_args[0][2]
        assert spec.datum_frame == "utm"
        assert spec.datum_utm_zone == 34 and spec.datum_utm_north is True
        assert spec.datum_utm_easting is None

    @pytest.mark.asyncio
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.RosbagDatabaseService')
    @patch('packages.topomap_dbs.client.ModelDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.api.server.LiveKitClient')
    async def test_update_datum_without_frame_resets_to_enu(
        self, mock_lk, mock_mp, mock_db, mock_graph, mock_model, mock_rosbag, mock_image
    ):
        """A datum PUT replaces the whole datum: the old UTM fields must not survive."""
        from packages.api.server import ApiDelegationService

        map_obj = _make_map_obj()
        map_obj.datum_frame = "utm"
        map_obj.datum_utm_zone = 32
        map_obj.datum_utm_easting = 465270.4231
        map_obj.description = "yard"
        mock_db_inst = AsyncMock()
        mock_db_inst.get_object = AsyncMock(return_value=map_obj)
        mock_db_inst.update_spec = AsyncMock()
        mock_db.return_value = mock_db_inst
        mock_graph.return_value = Mock()

        service = ApiDelegationService(arango_password="x", postgres_password="x")
        service._geo_map_usage = AsyncMock(return_value=(0, 0))
        result = await service.update_map_datum("site_a", 47.0, 8.0)

        assert result["datum_frame"] == "enu"
        spec = mock_db_inst.update_spec.call_args[0][2]
        assert spec.datum_frame == "enu"
        assert spec.datum_utm_zone is None and spec.datum_utm_easting is None
        assert spec.description == "yard"


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
    async def test_load_map_on_existing_record_only_updates_when_given_a_datum(
        self, mock_lk, mock_mp, mock_db, mock_graph, mock_model, mock_rosbag, mock_image
    ):
        """If the map already exists in Postgres, a load that carries no datum must
        leave the stored record (and its datum) alone rather than overwrite it."""
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
        mock_db_inst.update_spec.assert_not_called()

        # ...whereas a load that does supply a datum updates the existing record.
        result = await service.load_map(
            map_id="existing", datum_latitude=47.37, datum_longitude=8.54)

        assert result["success"] is True
        mock_db_inst.update_spec.assert_called_once()

    @pytest.mark.asyncio
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.RosbagDatabaseService')
    @patch('packages.topomap_dbs.client.ModelDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.api.server.LiveKitClient')
    async def test_load_map_transform_carries_the_stored_frame(
        self, mock_lk, mock_mp, mock_db, mock_graph, mock_model, mock_rosbag, mock_image
    ):
        """The transform comes from the stored map (e.g. auto-seeded from a UTM robot),
        not from the request."""
        from packages.api.server import ApiDelegationService

        stored = MapObjectV1(
            name="yard", datum_latitude=47.47946, datum_longitude=19.03238,
            datum_frame="utm", datum_utm_zone=34, datum_utm_north=True,
            datum_utm_easting=351756.484938, datum_utm_northing=5260323.440888)
        mock_db_inst = AsyncMock()
        mock_db_inst.create_object = AsyncMock(side_effect=Exception("duplicate"))
        mock_db_inst.get_object = AsyncMock(return_value=stored)
        mock_db.return_value = mock_db_inst

        mock_graph_inst = Mock()
        mock_graph_inst.create_map.return_value = True
        mock_graph_inst.get_map_stats.return_value = {"node_count": 0, "edge_count": 0}
        mock_graph_inst.get_all_nodes.return_value = []
        mock_graph_inst.get_edges.return_value = []
        mock_graph.return_value = mock_graph_inst

        service = ApiDelegationService(arango_password="x", postgres_password="x")
        result = await service.load_map(map_id="yard")

        t = result["transform"]
        assert t["frame"] == "utm"
        assert t["utm_zone"] == 34 and t["utm_north"] is True
        assert t["utm_easting"] == 351756.484938
        assert t["origin_lat"] == 47.47946
        assert t["rotation_rad"] == 0.0

    @pytest.mark.asyncio
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.RosbagDatabaseService')
    @patch('packages.topomap_dbs.client.ModelDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.api.server.LiveKitClient')
    async def test_load_map_accepts_a_frame(
        self, mock_lk, mock_mp, mock_db, mock_graph, mock_model, mock_rosbag, mock_image
    ):
        from packages.api.server import ApiDelegationService

        mock_db_inst = AsyncMock()
        mock_db_inst.create_object = AsyncMock()
        mock_db_inst.get_object = AsyncMock(side_effect=Exception("not reachable"))
        mock_db.return_value = mock_db_inst

        mock_graph_inst = Mock()
        mock_graph_inst.create_map.return_value = True
        mock_graph_inst.get_map_stats.return_value = {"node_count": 0, "edge_count": 0}
        mock_graph_inst.get_all_nodes.return_value = []
        mock_graph_inst.get_edges.return_value = []
        mock_graph.return_value = mock_graph_inst

        service = ApiDelegationService(arango_password="x", postgres_password="x")
        result = await service.load_map(
            map_id="new", datum_latitude=-33.8688, datum_longitude=151.2093,
            datum_frame="utm", datum_utm_zone=56, datum_utm_north=False)

        created = mock_db_inst.create_object.call_args[0][0]
        assert created.datum_frame == "utm"
        assert created.datum_utm_zone == 56 and created.datum_utm_north is False
        # stored map not readable -> transform from the request's values
        assert result["transform"]["frame"] == "utm"
        assert result["transform"]["utm_zone"] == 56


# ---------------------------------------------------------------------------
# ApiDelegationService.delete_map — hands off to the background saga (WP11 F1;
# the saga itself: tests/unit/test_map_delete.py)
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
    async def test_delete_map_marks_deleting_and_does_not_delete_inline(
        self, mock_lk, mock_mp, mock_db, mock_graph, mock_model, mock_rosbag, mock_image
    ):
        from packages.api.server import ApiDelegationService

        mock_db_inst = AsyncMock()
        mock_db.return_value = mock_db_inst
        mock_graph_inst = Mock()
        mock_graph.return_value = mock_graph_inst

        service = ApiDelegationService(arango_password="x", postgres_password="x")
        body = {"success": True, "map_id": "site_a", "lifecycle": "DELETING"}
        service.map_deleter.request = AsyncMock(return_value=body)
        result = await service.delete_map("site_a")

        assert result == body
        service.map_deleter.request.assert_awaited_once_with(
            "site_a", guard=maps.refuse_open_session)
        # Nothing is deleted in the request path any more.
        mock_graph_inst.delete_map.assert_not_called()
        mock_image.return_value.delete_map.assert_not_called()
        mock_db_inst.set_lifecycle.assert_not_called()

    @pytest.mark.asyncio
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.RosbagDatabaseService')
    @patch('packages.topomap_dbs.client.ModelDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.api.server.LiveKitClient')
    async def test_deleter_uses_the_graph_and_image_stores(
        self, mock_lk, mock_mp, mock_db, mock_graph, mock_model, mock_rosbag, mock_image
    ):
        from packages.api.server import ApiDelegationService

        mock_graph.return_value.delete_map.return_value = True
        mock_image.return_value.delete_map.return_value = True
        service = ApiDelegationService(arango_password="x", postgres_password="x")
        assert await service.map_deleter._attempt("site_a") is None
        mock_graph.return_value.delete_map.assert_called_once_with("site_a")
        mock_image.return_value.delete_map.assert_called_once_with("site_a")
        mock_rosbag.return_value.delete_map_bags.assert_not_called()


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

    @pytest.mark.asyncio
    @patch('packages.services.mission_planner.server.GraphDatabaseService')
    @patch('packages.services.mission_planner.server.PostgresDatabase')
    async def test_gps_target_with_utm_datum_is_exact(self, mock_db, mock_graph):
        """A UTM robot's map: the GPS target lands on the grid offset, not ~27 m off at 1 km.
        Golden point from pyproj (tests/unit/test_geo.py BUDAPEST_GRID_CASES)."""
        service = self._make_planner_with_mocks(
            mock_db, mock_graph, datum={"lat": 47.47946, "lon": 19.03238})
        stored = await service.database.get_object(MapObjectV1, "site_a")
        stored.datum_frame = "utm"
        stored.datum_utm_zone = 34
        stored.datum_utm_north = True

        result = await service.plan_and_execute_mission(
            robot_name="robot_1", target_lat=47.4796869358, target_lon=19.0456449132,
            map_id="site_a")

        assert abs(result["target"]["x"] - 1000.0) < 0.01
        assert abs(result["target"]["y"]) < 0.01


# ---------------------------------------------------------------------------
# ApiDelegationService.update_map_approx_location
# ---------------------------------------------------------------------------

def _approx_service(map_obj, mock_db, mock_graph):
    from packages.api.server import ApiDelegationService
    db = AsyncMock()
    if isinstance(map_obj, Exception):
        db.get_object = AsyncMock(side_effect=map_obj)
    else:
        db.get_object = AsyncMock(return_value=map_obj)
    db.update_spec = AsyncMock()
    mock_db.return_value = db
    mock_graph.return_value = Mock()
    return ApiDelegationService(arango_password="x", postgres_password="x"), db


@pytest.mark.unit
class TestApiDelegationApproxLocation:

    @pytest.mark.asyncio
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.RosbagDatabaseService')
    @patch('packages.topomap_dbs.client.ModelDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.api.server.LiveKitClient')
    async def test_sets_on_local_map(
        self, mock_lk, mock_mp, mock_db, mock_graph, mock_model, mock_rosbag, mock_image
    ):
        local = MapObjectV1(name="hall", type="local", description="hall")
        svc, db = _approx_service(local, mock_db, mock_graph)
        result = await svc.update_map_approx_location(
            "hall", 47.5, 19.04, accuracy_m=30.0, source="robot")

        assert result["success"] is True
        assert result["approx_location"]["latitude"] == 47.5
        spec = db.update_spec.call_args[0][2]
        assert spec.approx_location.source == "robot"
        assert spec.approx_location.accuracy_m == 30.0
        assert spec.approx_location.set_at is not None
        assert spec.description == "hall" and spec.type == "local"
        assert spec.datum_latitude is None  # never becomes a datum

    @pytest.mark.asyncio
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.RosbagDatabaseService')
    @patch('packages.topomap_dbs.client.ModelDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.api.server.LiveKitClient')
    async def test_geo_map_rejected_409(
        self, mock_lk, mock_mp, mock_db, mock_graph, mock_model, mock_rosbag, mock_image
    ):
        from fastapi import HTTPException
        svc, db = _approx_service(_make_map_obj(), mock_db, mock_graph)  # real datum: effective geo
        with pytest.raises(HTTPException) as exc:
            await svc.update_map_approx_location("site_a", 47.5, 19.04)
        assert exc.value.status_code == 409
        db.update_spec.assert_not_called()

    @pytest.mark.asyncio
    @patch('packages.topomap_dbs.client.ImageDatabaseService')
    @patch('packages.topomap_dbs.client.RosbagDatabaseService')
    @patch('packages.topomap_dbs.client.ModelDatabaseService')
    @patch('packages.topomap_dbs.client.GraphDatabaseService')
    @patch('packages.api.server.PostgresDatabase')
    @patch('packages.api.server.MissionPlannerClient')
    @patch('packages.api.server.LiveKitClient')
    async def test_not_found_and_null_island(
        self, mock_lk, mock_mp, mock_db, mock_graph, mock_model, mock_rosbag, mock_image
    ):
        from fastapi import HTTPException
        svc, db = _approx_service(Exception("nope"), mock_db, mock_graph)
        assert (await svc.update_map_approx_location("ghost", 1.0, 2.0))["success"] is False
        with pytest.raises(HTTPException) as exc:
            await svc.update_map_approx_location("ghost", 0.0, 0.0)
        assert exc.value.status_code == 422
        db.update_spec.assert_not_called()


@pytest.mark.unit
class TestApproxLocationPassThrough:

    def test_filter_maps_and_map_view_keep_field(self):
        import datetime
        from cloud_common.objects.map import ApproxLocationV1
        loc = ApproxLocationV1(latitude=47.5, longitude=19.0, set_at=datetime.datetime(
            2026, 10, 3, tzinfo=datetime.timezone.utc))
        m = MapObjectV1(name="hall", type="local", approx_location=loc, status=MapStatusV1())
        plain = MapObjectV1(name="yard", type="local", status=MapStatusV1())
        views = {v["name"]: v for v in maps.filter_maps([m, plain])}
        assert views["hall"]["approx_location"]["latitude"] == 47.5
        assert views["hall"]["approx_location"]["source"] == "manual"
        assert views["yard"]["approx_location"] is None
        assert maps.map_view(m)["approx_location"]["longitude"] == 19.0

    @pytest.mark.asyncio
    async def test_route_404_on_unknown_map(self):
        from fastapi import HTTPException
        from packages.api import main
        svc = Mock()
        svc.ensure_map_not_deleting = AsyncMock()
        svc.update_map_approx_location = AsyncMock(
            return_value={"success": False, "error": "Map 'x' not found"})
        with patch.object(main, "service", svc):
            with pytest.raises(HTTPException) as exc:
                await main.update_map_approx_location(
                    "x", main.ApproxLocationRequest(latitude=1.0, longitude=2.0))
        assert exc.value.status_code == 404

    def test_request_validation(self):
        from pydantic import ValidationError
        from packages.api import main
        with pytest.raises(ValidationError):
            main.ApproxLocationRequest(latitude=95.0, longitude=0.0)
        with pytest.raises(ValidationError):
            main.ApproxLocationRequest(latitude=1.0, longitude=1.0, source="gps")
        assert main.ApproxLocationRequest(latitude=1.0, longitude=1.0).source == "manual"
