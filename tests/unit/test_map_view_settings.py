"""Per-map view settings (packages/api/map_view_settings.py): validation, the store, the routes, the migration."""
import datetime
import importlib.util
import json
import os
import pathlib
from unittest.mock import MagicMock, patch

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from packages.api import map_delete, map_view_settings as mv  # noqa: E402

pytestmark = pytest.mark.unit

T0 = datetime.datetime(2026, 10, 17, 12, 0, tzinfo=datetime.timezone.utc)


class FakeCursor:
    def __init__(self, row):
        self._row = row

    async def fetchone(self):
        return self._row


class FakeDb:
    """In-memory map_view_settings and mapobjectv1 behind the `connection()` the store uses."""

    def __init__(self, maps=None):
        self.maps = {"lab": "ALIVE", **(maps or {})}
        self.rows = {}
        self.sql = []

    def connection(self):
        db = self

        class Ctx:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def execute(self, sql, params=()):
                db.sql.append(sql)
                if sql == mv.MAP_SQL:
                    state = db.maps.get(params[0])
                    return FakeCursor((state,) if state else None)
                if sql == mv.GET_SQL:
                    row = db.rows.get(params[0])
                    return FakeCursor(row)
                if sql == mv.UPSERT_SQL:
                    db.rows[params[0]] = (json.loads(params[1]), T0)
                    return FakeCursor(db.rows[params[0]])
                raise AssertionError(sql)
        return Ctx()


class TestValidation:
    def test_accepts_a_full_document_and_normalises_the_classes(self):
        s = mv.parse({"ignored_classes": ["wall", "floor", "wall"], "height_min_m": 0.3, "height_max_m": 2.5})
        assert s.dict() == {"ignored_classes": ["floor", "wall"], "height_min_m": 0.3, "height_max_m": 2.5}

    def test_defaults_and_null_sides(self):
        assert mv.parse({}).dict() == {"ignored_classes": [], "height_min_m": None, "height_max_m": None}
        assert mv.parse(None).height_max_m is None
        assert mv.parse({"height_max_m": None, "height_min_m": 1}).height_max_m is None

    @pytest.mark.parametrize("body", [
        {"height_max_m": -1}, {"height_max_m": 31}, {"height_min_m": float("nan")},
        {"height_min_m": 2, "height_max_m": 2}, {"height_min_m": 3, "height_max_m": 1},
        {"ignored_classes": [""]}, {"ignored_classes": ["x" * 81]}, {"ignored_classes": [str(i) for i in range(201)]},
        {"ignored_classes": "wall"}, {"unknown": 1},
    ])
    def test_rejects_with_422(self, body):
        with pytest.raises(HTTPException) as e:
            mv.parse(body)
        assert e.value.status_code == 422


class TestStore:
    async def test_unset_map_has_no_settings(self):
        out = await mv.ViewSettingsStore(FakeDb()).get("lab")
        assert out == {"map_name": "lab", "settings": None, "updated_at": None}

    async def test_put_stores_and_get_returns_it(self):
        db = FakeDb()
        store = mv.ViewSettingsStore(db)
        put = await store.put("lab", {"ignored_classes": ["wall"], "height_max_m": 3})
        assert put["settings"] == {"ignored_classes": ["wall"], "height_min_m": None, "height_max_m": 3.0}
        assert put["updated_at"] == "2026-10-17T12:00:00Z"
        assert (await store.get("lab")) == put
        # the body replaces the whole document
        again = await store.put("lab", {"height_min_m": 0.5})
        assert again["settings"] == {"ignored_classes": [], "height_min_m": 0.5, "height_max_m": None}

    async def test_unknown_and_deleting_maps(self):
        store = mv.ViewSettingsStore(FakeDb({"gone": "DELETING"}))
        for call in (store.get("nope"), store.put("nope", {})):
            with pytest.raises(HTTPException) as e:
                await call
            assert e.value.status_code == 404 and e.value.detail["code"] == "map_not_found"
        with pytest.raises(HTTPException) as e:
            await store.put("gone", {})
        assert e.value.status_code == 409 and e.value.detail["code"] == "map_deleting"

    async def test_a_bad_body_never_reaches_the_database(self):
        db = FakeDb()
        with pytest.raises(HTTPException):
            await mv.ViewSettingsStore(db).put("lab", {"height_max_m": 99})
        assert db.sql == []


class TestHttp:
    @pytest.fixture
    def app(self):
        import packages.api.main as main
        svc = MagicMock()
        svc.view_settings = mv.ViewSettingsStore(FakeDb())
        with patch.object(main, "service", svc):
            yield main.app

    async def test_get_put_and_errors(self, app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            assert (await c.get("/api/v1/maps/lab/view_settings")).json()["settings"] is None
            r = await c.put("/api/v1/maps/lab/view_settings",
                            json={"ignored_classes": ["wall"], "height_min_m": 0.2, "height_max_m": 4})
            assert r.status_code == 200 and r.json()["settings"]["height_max_m"] == 4.0
            assert (await c.get("/api/v1/maps/lab/view_settings")).json()["settings"]["ignored_classes"] == ["wall"]
            assert (await c.put("/api/v1/maps/lab/view_settings", json={"height_max_m": 99})).status_code == 422
            assert (await c.get("/api/v1/maps/nope/view_settings")).status_code == 404


def _migration():
    path = (pathlib.Path(__file__).resolve().parents[2] / "packages/api/migrations/versions"
            / "20261017_01_map_view_settings.py")
    spec = importlib.util.spec_from_file_location("m_view", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestMigrationAndDelete:
    def test_chain_and_sql(self):
        m = _migration()
        assert (m.revision, m.down_revision) == ("20261017_01_map_view_settings", "20261015_01_map_semantics")
        assert "CREATE TABLE IF NOT EXISTS map_view_settings" in m.UPGRADE_SQL and "map_name    text PRIMARY KEY" in m.UPGRADE_SQL
        assert "DROP TABLE IF EXISTS map_view_settings" in m.DOWNGRADE_SQL

    def test_the_map_delete_removes_the_row(self):
        assert map_delete.VIEW_SETTINGS_SQL == "DELETE FROM map_view_settings WHERE map_name = %s"
