"""PATCH /api/v1/maps/{id} with `slam_map` (packages/api/maps.py patch_map): allowed on a local
map with no open mapping session and no pending SLAM save; MAP.SLAM_CHANGED; the body stays
strict."""
import os

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

from types import SimpleNamespace  # noqa: E402
from unittest.mock import patch  # noqa: E402

import pytest  # noqa: E402

from packages.api import maps  # noqa: E402
from packages.events.codes import EventCode  # noqa: E402
from tests.unit import test_maps_m1 as m1  # noqa: E402
from tests.unit.test_maps_m2 import ShimDb  # noqa: E402

pytestmark = pytest.mark.unit

PUB = m1.PUB


@pytest.fixture
def db():
    d = ShimDb()
    with patch.object(maps, "open_store", d.store), patch.object(maps, "_utcnow", m1.Clock()):
        yield d


async def _status(coro):
    try:
        await coro
    except maps.HTTPException as exc:
        return exc.status_code, exc.detail
    raise AssertionError("no HTTPException")


def _switch(pending=()):
    return SimpleNamespace(slam_save_pending=lambda robot: robot in pending)


class TestSlamPatch:
    async def test_round_trip_on_a_local_map(self, db):
        db.add_map("yard", type="local")
        out = await maps.patch_map(None, "yard", {"slam_map": True}, PUB, _switch(), "op")
        assert out["slam_map"] is True and db.maps["yard"]["spec"]["slam_map"] is True
        assert out["type"] == "local"
        ev = db.events[-1]
        assert ev["code"] == EventCode.MAP_SLAM_CHANGED
        assert ev["payload"] == {"map_name": "yard", "slam_map": True, "actor": "op"}
        out = await maps.patch_map(None, "yard", {"slam_map": False}, PUB, _switch())
        assert out["slam_map"] is False and db.maps["yard"]["spec"]["slam_map"] is False
        assert len(db.events) == 2

    async def test_same_value_is_no_event(self, db):
        db.add_map("yard", type="local", slam_map=True)
        out = await maps.patch_map(None, "yard", {"slam_map": True}, PUB)
        assert out["slam_map"] is True and db.events == []

    async def test_description_and_slam_together(self, db):
        db.add_map("yard", type="local")
        out = await maps.patch_map(None, "yard", {"description": "d", "slam_map": True}, PUB)
        assert out["description"] == "d" and out["slam_map"] is True

    async def test_geo_map_refused(self, db):
        db.add_map("g", type="geo", geo={"utm_zone": 34, "utm_north": True, "origin_e": 1.0,
                                          "origin_n": 2.0, "bearing_deg": 0.0})
        code, detail = await _status(maps.patch_map(None, "g", {"slam_map": True}, PUB))
        assert code == 409 and "geo map" in detail and db.events == []
        assert "slam_map" not in db.maps["g"]["spec"] or not db.maps["g"]["spec"]["slam_map"]

    async def test_open_mapping_session_refused(self, db):
        db.add_map("yard", type="local", status={"state": "mapping"})
        db.add_session("yard", "r1", kind="mapping", ended=False)
        code, detail = await _status(maps.patch_map(None, "yard", {"slam_map": True}, PUB))
        assert code == 409 and "open mapping session" in detail and db.events == []

    async def test_pending_save_refused(self, db):
        db.add_map("yard", type="local", slam_map=True)
        db.add_session("yard", "r1", kind="mapping", ended=True)
        code, detail = await _status(
            maps.patch_map(None, "yard", {"slam_map": False}, PUB, _switch({"r1"})))
        assert code == 409 and "still saving" in detail
        assert db.maps["yard"]["spec"]["slam_map"] is True and db.events == []
        # another robot's save does not matter
        out = await maps.patch_map(None, "yard", {"slam_map": False}, PUB, _switch({"r2"}))
        assert out["slam_map"] is False

    async def test_null_is_422(self, db):
        db.add_map("yard", type="local")
        assert (await _status(maps.patch_map(None, "yard", {"slam_map": None}, PUB)))[0] == 422

    async def test_unknown_fields_still_forbidden(self, db):
        db.add_map("yard", type="local")
        code, detail = await _status(
            maps.patch_map(None, "yard", {"slam_map": True, "type": "geo"}, PUB))
        assert code == 422 and detail[0]["loc"] == ["body", "type"]
        assert "slam_map" not in db.maps["yard"]["spec"]
