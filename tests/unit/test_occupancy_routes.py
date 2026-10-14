"""Full-app HTTP tests of the occupancy routes (plan §4, L-2): the real `app` and the real `packages/api/occupancy_reads.py`
over the `FakeStore` of test_occupancy_reads (manifest + tile_v_plane). Only the delegation service object is stubbed."""
import importlib
import os
from types import SimpleNamespace

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

import httpx  # noqa: E402
import pytest  # noqa: E402

import packages.api.main as main  # noqa: E402
from packages.api import occupancy_reads as occ  # noqa: E402
from tests.unit.test_occupancy_reads import MAP, FakeStore, plane  # noqa: E402

pytestmark = pytest.mark.unit

BASE = "/api/v1/maps/%s/scene-graph/occupancy" % MAP


async def _get(path, **kw):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://t") as c:
        return await c.get(path, **kw)


@pytest.fixture
def occ_state(monkeypatch):
    """v1: chunk (0,0) = tile (0,0), chunk (1,0) = tile (8,0). v2 changes only tile (8,0), so chunk (0,0) is unchanged."""
    store = FakeStore()
    store.save({(0, 0): plane(1), (8, 0): plane(2)})
    store.save({(0, 0): plane(1), (8, 0): plane(3)})
    reads = occ.OccupancyReads(store)
    # main does `from packages.config import ...` at call time, so patch whatever module sys.modules holds NOW
    # (test_config drops packages.config from sys.modules, which leaves the import-time `config` stale).
    monkeypatch.setattr(importlib.import_module("packages.config"), "SCENE_GRAPH_WORKER_ENABLED", True)
    monkeypatch.setattr(main, "service", SimpleNamespace(database=object(), topomap_db=object()))
    monkeypatch.setattr(main, "_occ_reads", reads)
    return store


# ---- extent ----

async def test_extent_200_shape(occ_state):
    r = await _get(BASE + "/extent")
    assert r.status_code == 200
    j = r.json()
    assert set(j) == {"version", "epoch", "generation", "cell_m", "chunk_m", "chunks"}
    assert j["version"] == 2 and j["cell_m"] == occ.CELL_M and j["chunk_m"] == occ.CHUNK_M
    assert [(c["cx"], c["cy"], c["tiles"]) for c in j["chunks"]] == [(0, 0, 1), (1, 0, 1)]
    assert all(set(c) == {"cx", "cy", "digest", "tiles"} and len(c["digest"]) == 16 for c in j["chunks"])
    assert r.headers["cache-control"] == "no-cache"


async def test_extent_pinned_version_is_immutable(occ_state):
    r = await _get(BASE + "/extent?version=1")
    assert r.status_code == 200 and r.json()["version"] == 1
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"


async def test_extent_400_malformed_version(occ_state):
    for bad in ("abc", "1.5", "0", "-1", ""):
        r = await _get(BASE + "/extent?version=" + bad)
        assert r.status_code == 400, bad


async def test_extent_404_unknown_map_and_unknown_version(occ_state):
    r = await _get("/api/v1/maps/other/scene-graph/occupancy/extent")
    assert r.status_code == 404
    r = await _get(BASE + "/extent?version=99")
    assert r.status_code == 404


async def test_extent_413_over_limit(occ_state, monkeypatch):
    monkeypatch.setattr(occ, "MAX_EXTENT_CHUNKS", 1)     # the limit is read at call time; the map has 2 chunks
    r = await _get(BASE + "/extent")
    assert r.status_code == 413


# ---- chunk ----

async def test_chunk_200_body_parses_and_etag(occ_state):
    ext = (await _get(BASE + "/extent")).json()
    d = {(c["cx"], c["cy"]): c["digest"] for c in ext["chunks"]}
    r = await _get(BASE + "/chunk/1/0?version=2")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/octet-stream")
    assert r.headers["etag"] == '"occ.1.0.%s"' % d[(1, 0)]
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"
    header, planes = occ.unpack(r.content)
    assert header["format"] == occ.FORMAT and (header["cx"], header["cy"]) == (1, 0)
    h = occ_state.manifest(MAP, 2)["tiles"]["8,0"]
    assert header["slots"] == [[0, h, 0]]        # tile (8,0) is local slot (0,0) of chunk (1,0)
    assert planes[0] == occ_state.blobs[h].tobytes()


async def test_chunk_304_on_matching_if_none_match(occ_state):
    r = await _get(BASE + "/chunk/1/0?version=2")
    again = await _get(BASE + "/chunk/1/0?version=2", headers={"If-None-Match": r.headers["etag"]})
    assert again.status_code == 304 and again.content == b""
    assert again.headers["etag"] == r.headers["etag"]
    listed = await _get(BASE + "/chunk/1/0?version=2", headers={"If-None-Match": 'W/"x", ' + r.headers["etag"]})
    assert listed.status_code == 304


async def test_chunk_unchanged_across_versions_is_304(occ_state):
    v1 = await _get(BASE + "/chunk/0/0?version=1")
    assert v1.status_code == 200
    v2 = await _get(BASE + "/chunk/0/0?version=2", headers={"If-None-Match": v1.headers["etag"]})
    assert v2.status_code == 304
    changed = await _get(BASE + "/chunk/1/0?version=1")
    v2c = await _get(BASE + "/chunk/1/0?version=2", headers={"If-None-Match": changed.headers["etag"]})
    assert v2c.status_code == 200 and v2c.headers["etag"] != changed.headers["etag"]


async def test_chunk_400_bad_coordinates_and_missing_version(occ_state):
    for path in ("/chunk/abc/0?version=2", "/chunk/0/1.5?version=2", "/chunk/0/0?version=x"):
        r = await _get(BASE + path)
        assert r.status_code == 400, path
    r = await _get(BASE + "/chunk/0/0")
    assert r.status_code == 400


async def test_chunk_404_unknown_map_and_version(occ_state):
    r = await _get("/api/v1/maps/other/scene-graph/occupancy/chunk/0/0?version=1")
    assert r.status_code == 404
    r = await _get(BASE + "/chunk/0/0?version=99")
    assert r.status_code == 404


async def test_chunk_410_collected_version_body(occ_state):
    occ_state.collect(1)
    r = await _get(BASE + "/chunk/0/0?version=1")
    assert r.status_code == 410
    assert r.json()["detail"] == {"error": "version_gone", "current_version": 2}


async def test_absent_chunk_is_200_with_no_slots(occ_state):
    r = await _get(BASE + "/chunk/5/5?version=2")
    assert r.status_code == 200
    header, planes = occ.unpack(r.content)
    assert header["slots"] == [] and planes == {}


# ---- 503 when the flag is off ----

async def test_flag_off_is_503(monkeypatch):
    monkeypatch.setattr(importlib.import_module("packages.config"), "SCENE_GRAPH_WORKER_ENABLED", False)
    monkeypatch.setattr(main, "_occ_reads", None)
    monkeypatch.setattr(main, "service", SimpleNamespace(database=object(), topomap_db=object()))
    for path in ("/extent", "/chunk/0/0?version=1"):
        r = await _get(BASE + path)
        assert r.status_code == 503, path
        assert r.headers["retry-after"] and r.headers["cache-control"] == "no-store"
        assert r.json()["detail"]["error"] == "scene_graph_unavailable"


async def test_store_failure_is_503_not_500(occ_state, monkeypatch):
    """A transient store failure (Arango / MinIO down) answers 503 with Retry-After, like the scene-graph routes."""
    def down(*_a, **_k):
        raise ConnectionError("store down")
    monkeypatch.setattr(occ_state, "manifest", down)
    for path in ("/extent", "/chunk/0/0?version=1"):
        r = await _get(BASE + path)
        assert r.status_code == 503, path
        assert r.headers["retry-after"] and r.headers["cache-control"] == "no-store"
        assert r.json()["detail"]["error"] == "scene_graph_unavailable"
