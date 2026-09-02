"""Unit tests for cloud_common/objects/settings.py — SettingsObjectV1 model."""

import pytest

from cloud_common.objects.settings import (
    GLOBAL_SETTINGS_NAME,
    SettingsObjectV1,
    SettingsSpecV1,
    SettingsStatusV1,
    SettingsQueryParamsV1,
)
from cloud_common.objects.object import ObjectLifecycleV1
from cloud_common.objects import ALL_OBJECTS, USER_API_OBJECT_DICT


@pytest.mark.unit
class TestSettingsSpecV1:
    """Tests for SettingsSpecV1 Pydantic model."""

    def test_default_is_empty_list(self):
        spec = SettingsSpecV1()
        assert spec.fault_error_types == []

    def test_custom_fault_error_types(self):
        spec = SettingsSpecV1(fault_error_types=["motorStalledError", "batteryFailure"])
        assert spec.fault_error_types == ["motorStalledError", "batteryFailure"]

    def test_serialisation_roundtrip(self):
        spec = SettingsSpecV1(fault_error_types=["motorStalledError"])
        restored = SettingsSpecV1(**spec.dict())
        assert restored == spec


@pytest.mark.unit
class TestSettingsStatusV1:
    """SettingsStatusV1 carries no fields yet — just confirm it constructs cleanly."""

    def test_defaults(self):
        assert SettingsStatusV1().dict() == {}


@pytest.mark.unit
class TestSettingsObjectV1:
    """Tests for SettingsObjectV1 ApiObject subclass."""

    def test_alias(self):
        assert SettingsObjectV1.get_alias() == "settings"

    def test_table_name(self):
        assert SettingsObjectV1.table_name() == "settingsobjectv1"

    def test_spec_class(self):
        assert SettingsObjectV1.get_spec_class() is SettingsSpecV1

    def test_status_class(self):
        assert SettingsObjectV1.get_status_class() is SettingsStatusV1

    def test_query_params_class(self):
        assert SettingsObjectV1.get_query_params() is SettingsQueryParamsV1

    def test_supports_spec_update(self):
        assert SettingsObjectV1.supports_spec_update() is True

    def test_default_spec(self):
        assert SettingsObjectV1.default_spec() == {"fault_error_types": []}

    def test_construct_with_global_name(self):
        settings = SettingsObjectV1(name=GLOBAL_SETTINGS_NAME, lifecycle=ObjectLifecycleV1.ALIVE)
        assert settings.name == GLOBAL_SETTINGS_NAME
        assert settings.fault_error_types == []
        assert settings.spec == SettingsSpecV1(fault_error_types=[])

    def test_registered_in_all_objects(self):
        assert SettingsObjectV1 in ALL_OBJECTS

    def test_registered_in_user_api_object_dict(self):
        assert USER_API_OBJECT_DICT.get("settings") is SettingsObjectV1
