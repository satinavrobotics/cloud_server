"""Unit tests for the base_models download-url route and robot current_model persistence.

These call the FastAPI route coroutines in packages.api.main directly with a mocked
global ``service`` so no database/MinIO/lifespan is required.
"""
import os

# config.py validates these at import time; provide harmless test values.
for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

import packages.api.main as main


class TestBaseModelDownloadUrlRoute:
    @pytest.mark.asyncio
    async def test_returns_url_when_available(self):
        svc = MagicMock()
        svc.model_db.get_download_url.return_value = "https://minio/presigned"
        with patch.object(main, "service", svc):
            result = await main.get_base_model_download_url("model-123")
        assert result == {"download_url": "https://minio/presigned", "model_id": "model-123"}
        svc.model_db.get_download_url.assert_called_once_with("model-123")

    @pytest.mark.asyncio
    async def test_404_when_not_uploaded_or_missing(self):
        svc = MagicMock()
        svc.model_db.get_download_url.return_value = None
        with patch.object(main, "service", svc):
            with pytest.raises(HTTPException) as exc:
                await main.get_base_model_download_url("missing")
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_503_when_service_uninitialized(self):
        with patch.object(main, "service", None):
            with pytest.raises(HTTPException) as exc:
                await main.get_base_model_download_url("x")
        assert exc.value.status_code == 503


class TestCreateRobotCurrentModel:
    @pytest.mark.asyncio
    async def test_new_robot_persists_current_model(self):
        svc = MagicMock()
        svc.database.get_object = AsyncMock(side_effect=Exception("not found"))
        svc.database.create_object = AsyncMock()
        with patch.object(main, "service", svc):
            result = await main.create_robot({"name": "bot1", "current_model": "detector.onnx"})
        assert result["current_model"] == "detector.onnx"
        svc.database.create_object.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_new_robot_without_current_model_defaults_none(self):
        svc = MagicMock()
        svc.database.get_object = AsyncMock(side_effect=Exception("not found"))
        svc.database.create_object = AsyncMock()
        with patch.object(main, "service", svc):
            result = await main.create_robot({"name": "bot2"})
        assert result["current_model"] is None

    @pytest.mark.asyncio
    async def test_existing_robot_updates_current_model(self):
        existing = main.RobotObjectV1(
            name="bot1",
            status=main.RobotStatusV1(),
            lifecycle=main.ObjectLifecycleV1.ALIVE,
        )
        svc = MagicMock()
        svc.database.get_object = AsyncMock(return_value=existing)
        svc.database.update_spec = AsyncMock()
        svc.database.update_status = AsyncMock()
        with patch.object(main, "service", svc):
            result = await main.create_robot({"name": "bot1", "current_model": "newmodel.onnx"})
        assert result["current_model"] == "newmodel.onnx"
        svc.database.update_spec.assert_awaited()
