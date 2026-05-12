"""Unit tests for cloud_common/objects/map.py — MapObjectV1 model."""

import pytest

from cloud_common.objects.map import MapObjectV1, MapSpecV1, MapStatusV1, MapQueryParamsV1
from cloud_common.objects.object import ObjectLifecycleV1
from cloud_common.objects import ALL_OBJECTS


@pytest.mark.unit
class TestMapSpecV1:
    """Tests for MapSpecV1 Pydantic model."""

    def test_default_values(self):
        spec = MapSpecV1()
        assert spec.description is None
        assert spec.datum_latitude is None
        assert spec.datum_longitude is None
        assert spec.datum_bearing_deg == 0.0

    def test_full_datum(self):
        spec = MapSpecV1(
            description="Main floor",
            datum_latitude=47.3769,
            datum_longitude=8.5417,
            datum_bearing_deg=12.5,
        )
        assert spec.description == "Main floor"
        assert spec.datum_latitude == 47.3769
        assert spec.datum_longitude == 8.5417
        assert spec.datum_bearing_deg == 12.5

    def test_partial_datum_allowed(self):
        """latitude without longitude is allowed at the model level (validation is in the service)."""
        spec = MapSpecV1(datum_latitude=47.0)
        assert spec.datum_latitude == 47.0
        assert spec.datum_longitude is None

    def test_serialisation_roundtrip(self):
        spec = MapSpecV1(datum_latitude=47.0, datum_longitude=8.0, datum_bearing_deg=30.0)
        restored = MapSpecV1(**spec.dict())
        assert restored == spec


@pytest.mark.unit
class TestMapStatusV1:
    """Tests for MapStatusV1 Pydantic model."""

    def test_defaults(self):
        status = MapStatusV1()
        assert status.node_count == 0
        assert status.edge_count == 0

    def test_custom_counts(self):
        status = MapStatusV1(node_count=42, edge_count=100)
        assert status.node_count == 42
        assert status.edge_count == 100


@pytest.mark.unit
class TestMapObjectV1:
    """Tests for MapObjectV1 ApiObject subclass."""

    def test_alias(self):
        assert MapObjectV1.get_alias() == "map"

    def test_table_name(self):
        assert MapObjectV1.table_name() == "mapobjectv1"

    def test_spec_class(self):
        assert MapObjectV1.get_spec_class() is MapSpecV1

    def test_status_class(self):
        assert MapObjectV1.get_status_class() is MapStatusV1

    def test_query_params_class(self):
        assert MapObjectV1.get_query_params() is MapQueryParamsV1

    def test_supports_spec_update(self):
        assert MapObjectV1.supports_spec_update() is True

    def test_default_spec(self):
        spec = MapObjectV1.default_spec()
        assert isinstance(spec, dict)
        assert "datum_latitude" in spec
        assert spec["datum_latitude"] is None

    def test_get_query_map_empty(self):
        assert MapObjectV1.get_query_map() == {}

    def test_create_minimal(self):
        obj = MapObjectV1(name="site_a")
        assert obj.name == "site_a"
        assert obj.lifecycle == ObjectLifecycleV1.ALIVE
        assert obj.status.node_count == 0
        assert obj.datum_latitude is None

    def test_create_with_datum(self):
        obj = MapObjectV1(
            name="site_b",
            datum_latitude=47.3769,
            datum_longitude=8.5417,
            datum_bearing_deg=12.5,
            description="Warehouse level 1",
        )
        assert obj.datum_latitude == 47.3769
        assert obj.datum_longitude == 8.5417
        assert obj.datum_bearing_deg == 12.5
        assert obj.description == "Warehouse level 1"

    def test_spec_property(self):
        obj = MapObjectV1(name="site_c", datum_latitude=1.0, datum_longitude=2.0)
        spec = obj.spec
        assert isinstance(spec, MapSpecV1)
        assert spec.datum_latitude == 1.0
        assert spec.datum_longitude == 2.0

    def test_spec_dict_serialisable(self):
        obj = MapObjectV1(name="site_d", datum_latitude=47.0, datum_longitude=8.0)
        d = obj.spec.dict()
        assert d["datum_latitude"] == 47.0
        assert isinstance(d, dict)

    def test_status_mutation(self):
        obj = MapObjectV1(name="site_e")
        obj.status.node_count = 50
        assert obj.status.node_count == 50


@pytest.mark.unit
class TestMapObjectV1Registration:
    """Tests that MapObjectV1 is correctly registered in the objects registry."""

    def test_in_all_objects(self):
        aliases = [o.get_alias() for o in ALL_OBJECTS]
        assert "map" in aliases

    def test_importable_from_package(self):
        from cloud_common.objects import MapObjectV1 as ImportedMap
        assert ImportedMap is MapObjectV1
