"""Unit tests for robot routes in packages.api.main.

These call the FastAPI route coroutines directly with a mocked global ``service``
-- same pattern as test_settings_api.py.
"""
import os

# config.py validates these at import time; provide harmless test values.
for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

import packages.api.main as main
from cloud_common.objects.robot import RobotObjectV1


def _robot():
    robot = RobotObjectV1(name="r1", status={})
    robot.status.online = True
    robot.status.battery_level = 77.0
    return robot


@pytest.mark.unit
class TestGetRobotStatusRoute:
    async def test_returns_the_status_object_not_the_whole_robot(self):
        """Regression: this returned robot.dict(). sati-client PUT that back as
        {"status": <whole robot>}; RobotStatusV1 ignored the unknown keys, so every
        real status field (pose, online, battery, factsheet) reset to its default."""
        svc = MagicMock()
        svc.database.get_object = AsyncMock(return_value=_robot())
        with patch.object(main, "service", svc):
            result = await main.get_robot_status("r1")
        assert result["online"] is True
        assert result["battery_level"] == 77.0
        assert "status" not in result and "name" not in result

    async def test_database_404_is_not_rewrapped(self):
        svc = MagicMock()
        svc.database.get_object = AsyncMock(
            side_effect=HTTPException(status_code=404, detail="no such robot"))
        with patch.object(main, "service", svc):
            with pytest.raises(HTTPException) as exc:
                await main.get_robot_status("ghost")
        assert exc.value.status_code == 404
        assert exc.value.detail == "no such robot"


@pytest.mark.unit
class TestForceCancelRobotOrderRoute:
    async def test_sets_the_one_shot_flag_on_the_robot_spec(self):
        svc = MagicMock()
        svc.database.get_object = AsyncMock(return_value=_robot())
        svc.database.update_spec = AsyncMock()
        with patch.object(main, "service", svc):
            result = await main.force_cancel_robot_order("r1")
        assert result["success"] is True
        spec = svc.database.update_spec.await_args.args[2]
        assert spec.needs_order_cancel is True

    async def test_unknown_robot_is_a_404_not_a_400(self):
        svc = MagicMock()
        svc.database.get_object = AsyncMock(
            side_effect=HTTPException(status_code=404, detail="no such robot"))
        with patch.object(main, "service", svc):
            with pytest.raises(HTTPException) as exc:
                await main.force_cancel_robot_order("ghost")
        assert exc.value.status_code == 404


@pytest.mark.unit
class TestGetImageRoute:
    async def test_missing_image_is_a_404_not_a_500(self):
        svc = MagicMock()
        svc.get_image = AsyncMock(return_value=None)
        with patch.object(main, "service", svc):
            with pytest.raises(HTTPException) as exc:
                await main.get_image("map", "node", "img")
        assert exc.value.status_code == 404
