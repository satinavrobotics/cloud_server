"""PUT /maps/{id}/datum on geo maps: origin (spec.geo) fixed once there is data; kept in step
otherwise."""

import os

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

from unittest.mock import AsyncMock, MagicMock, Mock  # noqa: E402

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from cloud_common.objects.map import MapGeoV1, MapObjectV1, MapStatusV1  # noqa: E402
from packages.api.server import ApiDelegationService  # noqa: E402
from packages.utils import map_geo  # noqa: E402

pytestmark = pytest.mark.unit

GEO = {"utm_zone": 34, "utm_north": True, "origin_e": 352397.3, "origin_n": 5262357.8}


def make_service(map_obj, sessions=0, nodes=0):
    svc = ApiDelegationService.__new__(ApiDelegationService)
    svc.logger = MagicMock()
    svc.database = MagicMock()
    svc.database.get_object = AsyncMock(return_value=map_obj)
    svc.database.update_spec = AsyncMock()
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchone = AsyncMock(return_value=(sessions,))
    conn.execute = AsyncMock(return_value=cursor)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    svc.database.connection = MagicMock(return_value=cm)
    svc.graph_db = Mock()
    svc.graph_db.get_map_stats.return_value = {"node_count": nodes}
    return svc


def geo_map(geo=GEO):
    return MapObjectV1(name="yard", type="geo", geo=MapGeoV1(**geo) if geo else None,
                       datum_latitude=47.4979, datum_longitude=19.0402,
                       status=MapStatusV1())


def local_map():
    return MapObjectV1(name="hall", type="local", status=MapStatusV1())


NEW = dict(datum_latitude=47.6, datum_longitude=19.1, datum_bearing_deg=0.0)


async def test_geo_map_with_sessions_is_refused():
    svc = make_service(geo_map(), sessions=1, nodes=0)
    with pytest.raises(HTTPException) as exc:
        await svc.update_map_datum("yard", **NEW)
    assert exc.value.status_code == 409 and "origin" in exc.value.detail
    svc.database.update_spec.assert_not_called()


async def test_geo_map_with_nodes_is_refused():
    svc = make_service(geo_map(), sessions=0, nodes=5)
    with pytest.raises(HTTPException) as exc:
        await svc.update_map_datum("yard", **NEW)
    assert exc.value.status_code == 409 and "5 node" in exc.value.detail
    svc.database.update_spec.assert_not_called()


async def test_empty_geo_map_moves_its_origin_with_the_datum():
    svc = make_service(geo_map())
    result = await svc.update_map_datum("yard", **NEW)
    assert result["success"] is True
    spec = svc.database.update_spec.call_args[0][2]
    expected = map_geo.geo_from_datum(map_geo.map_datum(spec))
    assert spec.geo.dict() == pytest.approx(expected)
    assert spec.geo.origin_e != GEO["origin_e"]  # the origin followed the datum


async def test_empty_geo_map_without_origin_stays_without():
    svc = make_service(geo_map(geo=None))
    await svc.update_map_datum("yard", **NEW)
    assert svc.database.update_spec.call_args[0][2].geo is None


async def test_local_map_is_unchanged_and_not_queried():
    svc = make_service(local_map(), sessions=3, nodes=9)
    result = await svc.update_map_datum("hall", **NEW)
    assert result["success"] is True
    svc.database.connection.assert_not_called()
    svc.graph_db.get_map_stats.assert_not_called()
    spec = svc.database.update_spec.call_args[0][2]
    assert spec.geo is None and spec.datum_latitude == 47.6
