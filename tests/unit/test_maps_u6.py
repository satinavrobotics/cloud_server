"""Maps U6 (docs/satinav-maps-redesign.md §14.3, §14.14): robot.current_map, the PUT
/robots/{r}/map shim, the GEO/LOCAL sentinels and the §14.6 transition fallbacks are gone. A
robot's map is its open session only (§14.2).

The consumers' refusals (dispatcher, planner, recorder, bags): tests/unit/test_maps_use_consumers.py.
The migration on a copy of the production schema: ~/pg-cutover/scripts/mapsu6.sh --dry-run.
"""
import importlib.util
import os
import pathlib

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from cloud_common.objects.robot import RobotObjectV1, RobotSpecV1  # noqa: E402
from packages import config  # noqa: E402
from packages.api import main, maps  # noqa: E402
from packages.controllers.mission import server as dispatch_server  # noqa: E402
from packages.services.mission_planner import server as planner_server  # noqa: E402
from packages.utils import map_geo  # noqa: E402

pytestmark = pytest.mark.unit

MIGRATION = (pathlib.Path(__file__).resolve().parents[2] / "packages/api/migrations/versions"
             / "20261002_01_drop_current_map.py")


def _migration():
    spec = importlib.util.spec_from_file_location("drop_current_map", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestRobotModel:
    def test_no_current_map_field(self):
        assert "current_map" not in RobotSpecV1.__fields__
        assert "current_map" not in RobotObjectV1(name="r1", status={}).dict()

    def test_a_stored_row_with_current_map_still_parses(self):
        """Rows written before the migration (or by a rolled-back API) carry the key; pydantic
        v1 ignores it."""
        robot = RobotObjectV1(name="r1", status={}, current_map="GEO")
        assert not hasattr(robot, "current_map")
        assert "current_map" not in robot.dict()
        assert "current_map" not in RobotSpecV1(current_map="yard").dict()


class TestRemovedRoute:
    async def test_put_robot_map_is_410(self):
        with pytest.raises(HTTPException) as exc:
            await main.update_robot_map("r1")
        assert exc.value.status_code == 410
        assert "/api/v1/maps/{id}/sessions" in exc.value.detail and "finish" in exc.value.detail

    def test_route_is_registered_as_410(self):
        routes = [r for r in main.app.routes
                  if getattr(r, "path", None) == "/api/v1/robots/{robot_name}/map"]
        assert len(routes) == 1 and routes[0].methods == {"PUT"}
        assert routes[0].status_code == 410

    def test_shim_is_gone(self):
        for name in ("assign_robot_map", "AssignRobotMapRequest", "_create_for_assign"):
            assert not hasattr(maps, name)
        assert not hasattr(maps.SqlStore, "set_current_map")
        assert not hasattr(main, "UpdateRobotMapRequest")


class TestSentinelsAndFallbacks:
    def test_no_sentinels_in_config(self):
        assert not hasattr(config, "GPS_MAP_SENTINEL")
        assert not hasattr(config, "LOCAL_MAP_SENTINEL")

    def test_no_fallbacks(self):
        assert not hasattr(dispatch_server, "SESSIONLESS_FALLBACK")
        assert not hasattr(planner_server, "SESSIONLESS_FALLBACK")
        assert not hasattr(map_geo, "robot_frame_in_map")

    def test_old_sentinel_names_stay_reserved(self):
        """Old missions and mission_runs rows carry them as map ids: never a real map."""
        for name in ("GEO", "LOCAL"):
            with pytest.raises(HTTPException) as exc:
                maps.parse_body(maps.CreateMapRequest, {"name": name, "type": "local"})
            assert exc.value.status_code == 422


class TestMigration:
    def test_chain(self):
        mod = _migration()
        assert mod.revision == "20261002_01_drop_current_map"
        assert mod.down_revision == "20261001_01_run_epochs"

    def test_upgrade_strips_only_the_key(self):
        sql = _migration()._upgrade_sql()
        assert "spec = spec - 'current_map'" in sql and "spec ? 'current_map'" in sql
        assert "lock_timeout" in sql

    def test_downgrade_restores_the_open_session_map_only(self):
        sql = _migration()._downgrade_sql()
        assert "map_sessions" in sql and "ended_at IS NULL" in sql
        assert "GEO" not in sql and "LOCAL" not in sql.replace("SET LOCAL", "")
