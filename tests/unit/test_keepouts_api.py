"""Keep-out zone routes (packages/api/keepouts.py): CRUD, validation, `applies`, the cap, errors, the migration."""
import datetime
import importlib.util
import json
import os
import pathlib
from unittest.mock import MagicMock, patch

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

import httpx  # noqa: E402
import numpy as np  # noqa: E402
import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from packages.api import keepouts as api  # noqa: E402
from packages.api import map_view_settings  # noqa: E402
from packages.utils import keepouts as ko  # noqa: E402

pytestmark = pytest.mark.unit

T0 = datetime.datetime(2026, 10, 25, 12, 0, tzinfo=datetime.timezone.utc)
SQUARE = [[0, 0], [2, 0], [2, 2], [0, 2]]


class Cursor:
    def __init__(self, rows):
        self._rows = rows

    async def fetchone(self):
        return self._rows[0] if self._rows else None

    async def fetchall(self):
        return list(self._rows)


class FakeDb:
    """map_keepouts and mapobjectv1 in memory, behind the `connection().execute(sql, params)` the store uses."""

    def __init__(self, maps=None):
        self.maps = {"lab": "ALIVE", **(maps or {})}
        self.rows = []                       # dicts of the table
        self.tick = 0
        self.sql = []

    def _tuple(self, r):
        return tuple(r[c] for c in ko.COLUMNS)

    def connection(self):
        db = self

        class Ctx:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def execute(self, sql, params=()):
                db.sql.append(sql)
                if sql == map_view_settings.MAP_SQL:
                    state = db.maps.get(params[0])
                    return Cursor([(state,)] if state else [])
                if sql == ko.LIST_SQL:
                    return Cursor([db._tuple(r) for r in db.rows if r["map_name"] == params[0]])
                if sql == ko.GET_SQL:
                    return Cursor([db._tuple(r) for r in db.rows if (r["map_name"], r["id"]) == tuple(params)])
                if sql == ko.INSERT_SQL:
                    id_, map_, kind, label, spec, map2, cap = params
                    if sum(1 for r in db.rows if r["map_name"] == map2) >= cap:
                        return Cursor([])
                    db.tick += 1
                    row = {"id": id_, "map_name": map_, "kind": kind, "label": label, "enabled": True, "spec": json.loads(spec),
                           "created_at": T0 + datetime.timedelta(seconds=db.tick), "updated_at": T0 + datetime.timedelta(seconds=db.tick)}
                    db.rows.append(row)
                    return Cursor([db._tuple(row)])
                if sql == ko.UPDATE_SQL:
                    enabled, label, map_, id_ = params
                    for r in db.rows:
                        if (r["map_name"], r["id"]) == (map_, id_):
                            db.tick += 1
                            r.update(enabled=enabled, label=label, updated_at=T0 + datetime.timedelta(seconds=100 + db.tick))
                            return Cursor([db._tuple(r)])
                    return Cursor([])
                if sql == ko.DELETE_SQL:
                    keep = [r for r in db.rows if (r["map_name"], r["id"]) != tuple(params)]
                    hit = len(keep) != len(db.rows)
                    db.rows = keep
                    return Cursor([(params[1],)] if hit else [])
                raise AssertionError(sql)
        return Ctx()


class FakeReads:
    """What the store asks of the scene-graph read side: the topology and the owner plane."""

    def __init__(self, keys=("a:0", "b:0", "c:0"), links=(("a:0", "b:0"),), plane=None, fail=False):
        self.topo = {"version": 3, "keys": set(keys), "links": {frozenset(x) for x in links}}
        self.plane, self.fail = plane, fail

    async def topology(self, map_id):
        if self.fail:
            raise RuntimeError("store down")
        return self.topo

    async def owner_plane(self, map_id, bbox, level=0):
        if self.fail:
            raise RuntimeError("store down")
        return self.plane


def store(db=None, reads=None):
    db = db or FakeDb()
    return api.KeepoutStore(db, (lambda: reads) if reads is not None else None), db


async def code_of(awaitable):
    with pytest.raises(HTTPException) as e:
        await awaitable
    return e.value.status_code, e.value.detail["code"]


class TestStore:
    async def test_create_list_patch_delete(self):
        s, db = store(reads=FakeReads())
        made = await s.create("lab", {"kind": "region", "label": "wet floor", "region": {"chart_key": "a:0"}})
        assert made["kind"] == "region" and made["enabled"] is True and made["label"] == "wet floor"
        assert made["region"] == {"chart_key": "a:0"} and made["applies"] == "applied" and made["created_at"].endswith("Z")
        poly = await s.create("lab", {"kind": "region", "region": {"polygon": SQUARE}})
        edge = await s.create("lab", {"kind": "edge", "edge": {"from": "a:0", "to": "b:0"}})
        listed = await s.list_all("lab")
        assert [k["id"] for k in listed["keepouts"]] == [made["id"], poly["id"], edge["id"]]
        assert listed["map_id"] == "lab" and listed["revision"] == ko.revision(
            [ko.row_dict(db._tuple(r)) for r in db.rows])
        off = await s.patch("lab", made["id"], {"enabled": False})
        assert off["enabled"] is False and off["label"] == "wet floor"
        assert (await s.patch("lab", made["id"], {"label": None}))["label"] is None
        assert (await s.list_all("lab"))["revision"] != listed["revision"]
        assert await s.delete("lab", made["id"]) is None
        assert [k["id"] for k in (await s.list_all("lab"))["keepouts"]] == [poly["id"], edge["id"]]

    async def test_the_revision_changes_on_each_edit(self):
        s, _ = store(reads=FakeReads())
        revs = [(await s.list_all("lab"))["revision"]]
        made = await s.create("lab", {"kind": "region", "region": {"polygon": SQUARE}})
        revs.append((await s.list_all("lab"))["revision"])
        await s.patch("lab", made["id"], {"enabled": False})
        revs.append((await s.list_all("lab"))["revision"])
        await s.patch("lab", made["id"], {"enabled": True})
        revs.append((await s.list_all("lab"))["revision"])
        await s.delete("lab", made["id"])
        revs.append((await s.list_all("lab"))["revision"])
        assert len(set(revs[:4])) == 4 and revs[4] == revs[0]

    async def test_unknown_and_deleting_maps(self):
        s, _ = store(FakeDb({"gone": "DELETING"}), FakeReads())
        for call in (s.list_all("nope"), s.create("nope", {"kind": "region", "region": {"polygon": SQUARE}}),
                     s.patch("nope", "x", {"enabled": True}), s.delete("nope", "x")):
            assert await code_of(call) == (404, "map_not_found")
        assert await code_of(s.create("gone", {"kind": "region", "region": {"polygon": SQUARE}})) == (409, "map_deleting")

    async def test_unknown_keepout(self):
        s, _ = store(reads=FakeReads())
        assert await code_of(s.patch("lab", "zz", {"enabled": False})) == (404, "keepout_not_found")
        assert await code_of(s.delete("lab", "zz")) == (404, "keepout_not_found")

    async def test_other_maps_are_separate(self):
        s, db = store(FakeDb({"hall": "ALIVE"}), FakeReads())
        made = await s.create("lab", {"kind": "region", "region": {"polygon": SQUARE}})
        assert (await s.list_all("hall"))["keepouts"] == []
        assert await code_of(s.delete("hall", made["id"])) == (404, "keepout_not_found")

    async def test_bad_bodies_never_reach_the_database(self):
        s, db = store(reads=FakeReads())
        for body in ({"kind": "region", "region": {"polygon": SQUARE[:2]}}, {"kind": "x"}, {},
                     {"kind": "region", "region": {"polygon": [[0, 0], [1, 0], [1, float("nan")]]}}):
            status, _ = await code_of(s.create("lab", body))
            assert status == 422
        assert db.sql == []

    async def test_the_cap_and_duplicates(self, monkeypatch):
        monkeypatch.setattr(ko, "MAX_PER_MAP", 2)
        s, _ = store(reads=FakeReads())
        first = await s.create("lab", {"kind": "region", "region": {"chart_key": "a:0"}})
        assert await code_of(s.create("lab", {"kind": "region", "region": {"chart_key": "a:0"}})) == (409, "keepout_exists")
        await s.create("lab", {"kind": "region", "region": {"chart_key": "b:0"}})
        assert await code_of(s.create("lab", {"kind": "region", "region": {"chart_key": "c:0"}})) == (409, "keepout_limit")
        await s.delete("lab", first["id"])
        await s.create("lab", {"kind": "region", "region": {"chart_key": "c:0"}})

    async def test_unknown_charts_and_missing_links_are_refused_when_the_graph_is_known(self):
        s, _ = store(reads=FakeReads())
        assert await code_of(s.create("lab", {"kind": "region", "region": {"chart_key": "zz:9"}})) == (422, "chart_not_found")
        assert await code_of(s.create("lab", {"kind": "edge", "edge": {"from": "a:0", "to": "zz:9"}})) == (422, "chart_not_found")
        assert await code_of(s.create("lab", {"kind": "edge", "edge": {"from": "a:0", "to": "c:0"}})) == (422, "no_link_between_charts")
        assert await code_of(s.create("lab", {"kind": "edge", "edge": {"from": [5, 5], "to": "a:0"}})) == (422, "point_off_graph")

    async def test_points_of_an_edge_become_chart_keys(self):
        # an owner plane: two 5 x 5 cells, 1 m each: key "a:0" left, "b:0" right
        idx = np.array([[1] * 5 + [2] * 5] * 5, np.uint16)
        plane = {"origin": (0.0, 0.0), "step": 0.1, "shape": idx.shape, "keys": ["a:0", "b:0"], "index": idx}
        s, _ = store(reads=FakeReads(plane=plane))
        made = await s.create("lab", {"kind": "edge", "edge": {"from": [0.2, 0.2], "to": [0.8, 0.2]}})
        assert made["edge"] == {"from": "a:0", "to": "b:0"} and made["applies"] == "applied"

    async def test_without_a_graph_points_are_kept_and_chart_zones_are_unresolved(self):
        s, _ = store(reads=FakeReads(fail=True))
        made = await s.create("lab", {"kind": "edge", "edge": {"from": [1, 1], "to": [2, 2]}})
        assert made["edge"] == {"from": [1.0, 1.0], "to": [2.0, 2.0]}
        region = await s.create("lab", {"kind": "region", "region": {"chart_key": "a:0"}})
        listed = {k["id"]: k for k in (await s.list_all("lab"))["keepouts"]}
        assert listed[region["id"]]["applies"] == "unresolved" and listed[region["id"]]["reason"] == "no_scene_graph"

    async def test_applies_follows_the_current_graph(self):
        reads = FakeReads()
        s, _ = store(reads=reads)
        chart = await s.create("lab", {"kind": "region", "region": {"chart_key": "a:0"}})
        edge = await s.create("lab", {"kind": "edge", "edge": {"from": "a:0", "to": "b:0"}})
        poly = await s.create("lab", {"kind": "region", "region": {"polygon": SQUARE}})
        assert all(k["applies"] == "applied" for k in (await s.list_all("lab"))["keepouts"])
        reads.topo = {"version": 4, "keys": {"b:0"}, "links": set()}               # a:0 retired, no link left
        got = {k["id"]: (k["applies"], k.get("reason")) for k in (await s.list_all("lab"))["keepouts"]}
        assert got[chart["id"]] == ("unresolved", "chart_not_found") and got[edge["id"]] == ("unresolved", "chart_not_found")
        assert got[poly["id"]] == ("applied", None)
        reads.topo = {"version": 5, "keys": {"a:0", "b:0"}, "links": set()}
        assert {k["id"]: k.get("reason") for k in (await s.list_all("lab"))["keepouts"]}[edge["id"]] == "no_link_between_charts"

    async def test_a_disabled_zone_keeps_its_applies_and_shows_enabled_false(self):
        s, _ = store(reads=FakeReads())
        made = await s.create("lab", {"kind": "region", "region": {"polygon": SQUARE}})
        off = await s.patch("lab", made["id"], {"enabled": False})
        assert off["enabled"] is False and off["applies"] == "applied"


class TestHttp:
    @pytest.fixture
    def app(self):
        import packages.api.main as main
        svc = MagicMock()
        svc.keepouts = api.KeepoutStore(FakeDb(), lambda: FakeReads())
        with patch.object(main, "service", svc):
            yield main.app

    async def test_the_four_routes(self, app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/api/v1/maps/lab/keepouts")
            assert r.status_code == 200 and r.json()["keepouts"] == [] and set(r.json()) == {"map_id", "keepouts", "revision"}
            r = await c.post("/api/v1/maps/lab/keepouts", json={"kind": "region", "label": "x", "region": {"polygon": SQUARE}})
            assert r.status_code == 201
            k = r.json()
            assert {"id", "kind", "label", "enabled", "created_at", "region", "applies"} <= set(k)
            r = await c.patch(f"/api/v1/maps/lab/keepouts/{k['id']}", json={"enabled": False, "label": None})
            assert r.status_code == 200 and r.json()["enabled"] is False and r.json()["label"] is None
            assert (await c.get("/api/v1/maps/lab/keepouts")).json()["keepouts"][0]["enabled"] is False
            r = await c.delete(f"/api/v1/maps/lab/keepouts/{k['id']}")
            assert r.status_code == 204 and r.content == b""
            assert (await c.delete(f"/api/v1/maps/lab/keepouts/{k['id']}")).status_code == 404

    async def test_errors_use_the_usual_shape(self, app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/api/v1/maps/nope/keepouts")
            assert r.status_code == 404 and r.json()["detail"]["code"] == "map_not_found"
            r = await c.post("/api/v1/maps/lab/keepouts", json={"kind": "region", "region": {"polygon": [[0, 0], [1, 1]]}})
            assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_polygon"
            r = await c.patch("/api/v1/maps/lab/keepouts/zz", json={"enabled": True})
            assert r.status_code == 404 and r.json()["detail"]["code"] == "keepout_not_found"
            r = await c.patch("/api/v1/maps/lab/keepouts/zz", json={})
            assert r.status_code == 422


def test_the_migration_follows_the_newest_one_and_is_idempotent(monkeypatch):
    try:
        import alembic  # noqa: F401
    except ImportError:                                  # only the module-level `from alembic import op` is needed here
        import sys
        import types
        fake = types.ModuleType("alembic")
        fake.op = types.SimpleNamespace(execute=lambda sql: None)
        monkeypatch.setitem(sys.modules, "alembic", fake)
    path = pathlib.Path(__file__).resolve().parents[2] / "packages/api/migrations/versions/20261025_01_map_keepouts.py"
    spec = importlib.util.spec_from_file_location("m_keepouts", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert (m.revision, m.down_revision) == ("20261025_01_map_keepouts", "20261023_01_map_person_removal")
    assert "CREATE TABLE IF NOT EXISTS map_keepouts" in m.UPGRADE_SQL and "CREATE INDEX IF NOT EXISTS" in m.UPGRADE_SQL
    assert "expires" not in m.UPGRADE_SQL
    assert "DROP TABLE IF EXISTS map_keepouts" in m.DOWNGRADE_SQL
    for col in ko.COLUMNS:
        assert col in m.UPGRADE_SQL
    versions = pathlib.Path(path).parent
    downs = {}
    for f in versions.glob("2026*.py"):
        text = f.read_text()
        rev = [l for l in text.splitlines() if l.startswith("revision = ")]
        down = [l for l in text.splitlines() if l.startswith("down_revision = ")]
        downs[rev[0].split('"')[1]] = down[0].split('"')[1] if '"' in down[0] else None
    heads = set(downs) - set(downs.values())
    assert len(heads) == 1 and "20261025_01_map_keepouts" in downs   # one head (newer migrations may follow ours)


def test_the_map_delete_removes_the_zones():
    from packages.api import map_delete
    assert map_delete.KEEPOUTS_SQL == "DELETE FROM map_keepouts WHERE map_name = %s"


class TestTopologyRead:
    async def test_chart_keys_and_links_come_from_the_manifest_and_are_cached_per_version(self):
        from packages.api.scene_graph_reads import SceneGraphReads
        meta = {"version": 2, "generation": 1, "params_hash": "h", "cursor": None}
        calls = []

        def manifest(map_id, version):
            calls.append((map_id, version))
            return {"charts": [{"key": "a:0"}, {"key": "b:0"}], "links": [{"a": "a:0", "b": "b:0"}]}
        reads = SceneGraphReads(lambda m: dict(meta), lambda *a: None, manifest=manifest)
        topo = await reads.topology("m")
        assert topo == {"version": 2, "keys": {"a:0", "b:0"}, "links": {frozenset(("a:0", "b:0"))}}
        await reads.topology("m")
        assert calls == [("m", 2)]
        meta["version"] = 3
        await reads.topology("m")
        assert calls == [("m", 2), ("m", 3)]

    async def test_no_graph_or_no_manifest_is_none(self):
        from packages.api.scene_graph_reads import SceneGraphReads
        assert await SceneGraphReads(lambda m: {}, lambda *a: None, manifest=lambda *a: {}).topology("m") is None
        assert await SceneGraphReads(lambda m: {"version": 1}, lambda *a: None).topology("m") is None
