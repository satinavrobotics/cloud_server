"""Unit tests for the /api/v1/settings routes in packages.api.main.

These call the FastAPI route coroutines directly with a mocked global ``service``
so no database/MinIO/lifespan is required — same pattern as test_base_model_routes.py.
"""
import os

# config.py validates these at import time; provide harmless test values.
for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

import packages.api.main as main
from cloud_common.objects.settings import GLOBAL_SETTINGS_NAME, SettingsObjectV1
from cloud_common.objects.object import ObjectLifecycleV1


def _existing_settings(fault_error_types=None):
    return SettingsObjectV1(
        name=GLOBAL_SETTINGS_NAME,
        lifecycle=ObjectLifecycleV1.ALIVE,
        fault_error_types=fault_error_types or [],
    )


class TestGetSettingsRoute:
    @pytest.mark.asyncio
    async def test_returns_existing_settings(self):
        svc = MagicMock()
        svc.database.get_object = AsyncMock(return_value=_existing_settings(["motorStalledError"]))
        with patch.object(main, "service", svc):
            result = await main.get_settings()
        assert result["fault_error_types"] == ["motorStalledError"]
        svc.database.get_object.assert_awaited_once_with(SettingsObjectV1, GLOBAL_SETTINGS_NAME)

    @pytest.mark.asyncio
    async def test_creates_settings_with_defaults_on_first_read(self):
        svc = MagicMock()
        svc.database.get_object = AsyncMock(side_effect=Exception("not found"))
        svc.database.create_object = AsyncMock()
        with patch.object(main, "service", svc):
            result = await main.get_settings()
        assert result["fault_error_types"] == []
        assert result["name"] == GLOBAL_SETTINGS_NAME
        svc.database.create_object.assert_awaited_once()
        created_obj = svc.database.create_object.call_args.args[0]
        assert isinstance(created_obj, SettingsObjectV1)
        assert created_obj.name == GLOBAL_SETTINGS_NAME

    @pytest.mark.asyncio
    async def test_503_when_service_uninitialized(self):
        with patch.object(main, "service", None):
            with pytest.raises(HTTPException) as exc:
                await main.get_settings()
        assert exc.value.status_code == 503

    @pytest.mark.asyncio
    async def test_recovers_from_concurrent_create_race(self):
        # get_object misses, our own create_object loses a race to another
        # concurrent request's create (UniqueViolation -> 400), we should
        # recover by re-fetching rather than surfacing that 400 to the caller.
        svc = MagicMock()
        svc.database.get_object = AsyncMock(
            side_effect=[Exception("not found"), _existing_settings(["fromOtherRequest"])]
        )
        svc.database.create_object = AsyncMock(
            side_effect=HTTPException(400, "Object settings with name global already exists")
        )
        with patch.object(main, "service", svc):
            result = await main.get_settings()
        assert result["fault_error_types"] == ["fromOtherRequest"]
        assert svc.database.get_object.await_count == 2

    @pytest.mark.asyncio
    async def test_reraises_non_conflict_http_exception_from_create(self):
        svc = MagicMock()
        svc.database.get_object = AsyncMock(side_effect=Exception("not found"))
        svc.database.create_object = AsyncMock(side_effect=HTTPException(503, "db unavailable"))
        with patch.object(main, "service", svc):
            with pytest.raises(HTTPException) as exc:
                await main.get_settings()
        # get_settings' own except-Exception wraps everything else as a 500.
        assert exc.value.status_code == 500


class TestUpdateSettingsRoute:
    @pytest.mark.asyncio
    async def test_updates_fault_error_types_on_existing_settings(self):
        svc = MagicMock()
        svc.database.get_object = AsyncMock(
            side_effect=[_existing_settings(["oldError"]), _existing_settings(["newError"])]
        )
        svc.database.update_spec = AsyncMock()
        with patch.object(main, "service", svc):
            result = await main.update_settings({"fault_error_types": ["newError"]})
        assert result["fault_error_types"] == ["newError"]
        svc.database.update_spec.assert_awaited_once()
        # update_spec's third positional arg is the spec passed to the DB layer.
        spec_arg = svc.database.update_spec.call_args.args[2]
        assert spec_arg.fault_error_types == ["newError"]

    @pytest.mark.asyncio
    async def test_creates_settings_first_if_missing_then_updates(self):
        svc = MagicMock()
        svc.database.get_object = AsyncMock(
            side_effect=[Exception("not found"), _existing_settings(["newError"])]
        )
        svc.database.create_object = AsyncMock()
        svc.database.update_spec = AsyncMock()
        with patch.object(main, "service", svc):
            result = await main.update_settings({"fault_error_types": ["newError"]})
        assert result["fault_error_types"] == ["newError"]
        svc.database.create_object.assert_awaited_once()
        svc.database.update_spec.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unknown_fields_are_422_listing_them(self):
        # WP11 F2: no longer silently dropped. Nothing is read or written.
        svc = MagicMock()
        svc.database.get_object = AsyncMock()
        svc.database.update_spec = AsyncMock()
        with patch.object(main, "service", svc):
            with pytest.raises(HTTPException) as exc:
                await main.update_settings({"not_a_real_field": "whatever",
                                            "fault_error_types": ["e"], "another": 1})
        assert exc.value.status_code == 422
        assert [err["loc"] for err in exc.value.detail] == [
            ["body", "another"], ["body", "not_a_real_field"]]
        assert all(err["type"] == "value_error.extra" for err in exc.value.detail)
        svc.database.update_spec.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_field_422_even_with_a_valid_recording_level(self):
        svc = MagicMock()
        svc.database.update_spec = AsyncMock()
        with patch.object(main, "service", svc):
            with pytest.raises(HTTPException) as exc:
                await main.update_settings({"telemetry_recording": "full", "typo": True})
        assert exc.value.status_code == 422
        assert exc.value.detail[0]["loc"] == ["body", "typo"]
        svc.database.update_spec.assert_not_called()

    @pytest.mark.asyncio
    async def test_bad_value_is_422(self):
        svc = MagicMock()
        svc.database.get_object = AsyncMock(return_value=_existing_settings([]))
        svc.database.update_spec = AsyncMock()
        with patch.object(main, "service", svc):
            with pytest.raises(HTTPException) as exc:
                await main.update_settings({"fault_error_types": "not-a-list"})
        assert exc.value.status_code == 422
        assert exc.value.detail[0]["loc"][:2] == ["body", "fault_error_types"]
        svc.database.update_spec.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_is_partial(self):
        # Only the keys sent change: a stored telemetry_recording survives a
        # fault_error_types update, and no RECORDING_CHANGED hook is attached.
        stored = _existing_settings(["old"])
        stored.telemetry_recording = "full"
        svc = MagicMock()
        svc.database.get_object = AsyncMock(side_effect=[stored, stored])
        svc.database.update_spec = AsyncMock()
        with patch.object(main, "service", svc):
            await main.update_settings({"fault_error_types": ["new"]})
        spec_arg = svc.database.update_spec.call_args.args[2]
        assert spec_arg.fault_error_types == ["new"]
        assert spec_arg.telemetry_recording == "full"
        assert "before_commit" not in svc.database.update_spec.call_args.kwargs

    @pytest.mark.asyncio
    async def test_telemetry_recording_update_keeps_its_hook(self):
        svc = MagicMock()
        svc.database.get_object = AsyncMock(
            side_effect=[_existing_settings(["keep"]), _existing_settings(["keep"])])
        svc.database.update_spec = AsyncMock()
        with patch.object(main, "service", svc):
            await main.update_settings({"telemetry_recording": "off"})
        spec_arg = svc.database.update_spec.call_args.args[2]
        assert spec_arg.telemetry_recording == "off"
        assert spec_arg.fault_error_types == ["keep"]
        assert callable(svc.database.update_spec.call_args.kwargs["before_commit"])

    @pytest.mark.asyncio
    async def test_accepts_the_get_body_back(self):
        # A client may PUT what GET returned: name/status/lifecycle are accepted and ignored.
        svc = MagicMock()
        svc.database.get_object = AsyncMock(
            side_effect=[_existing_settings([]), _existing_settings(["x"])])
        svc.database.update_spec = AsyncMock()
        body = {**_existing_settings(["x"]).dict(), "lifecycle": "ALIVE"}
        with patch.object(main, "service", svc):
            result = await main.update_settings(body)
        assert result["fault_error_types"] == ["x"]
        assert svc.database.update_spec.call_args.args[2].fault_error_types == ["x"]

    @pytest.mark.asyncio
    async def test_ignores_name_status_lifecycle_even_if_provided(self):
        svc = MagicMock()
        svc.database.get_object = AsyncMock(
            side_effect=[_existing_settings([]), _existing_settings([])]
        )
        svc.database.update_spec = AsyncMock()
        with patch.object(main, "service", svc):
            await main.update_settings({"name": "not-global", "lifecycle": "DELETED"})
        updated_name_arg = svc.database.update_spec.call_args.args[1]
        assert updated_name_arg == GLOBAL_SETTINGS_NAME

    @pytest.mark.asyncio
    async def test_503_when_service_uninitialized(self):
        with patch.object(main, "service", None):
            with pytest.raises(HTTPException) as exc:
                await main.update_settings({"fault_error_types": []})
        assert exc.value.status_code == 503
