"""Full-app HTTP tests of the three scene-graph routes (real `app`, real SceneGraphStore over a
MemoryBackend built by the engine; only the delegation service object is stubbed)."""
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

import httpx  # noqa: E402
import numpy as np  # noqa: E402
import pytest  # noqa: E402

import packages.api.main as main  # noqa: E402
from packages.api import scene_graph_reads as sgr  # noqa: E402
from packages.scene_graph.api import SceneParams  # noqa: E402
from packages.scene_graph.engine import ChartGraph  # noqa: E402
from packages.scene_graph.store import MemoryBackend, SceneGraphStore  # noqa: E402
from tests.unit.scene_graph import chart_worlds as cw  # noqa: E402

pytestmark = pytest.mark.unit

BASE = "/api/v1/maps/m/scene-graph"
BBOX = "bbox=0,0,12,12"


def _store(two_versions=False):
    b = MemoryBackend()
    store = SceneGraphStore(b, b, clock=lambda: 1_700_000_000.0)
    w, way = cw.two_rooms()
    leaves = cw.drive(w, way)
    g = ChartGraph(SceneParams())
    if two_versions:
        store.save("m", g, g.fold_many(leaves[:6]), None, {"n": 1})
        store.save("m", g, g.fold_many(leaves[6:]), 1, {"n": 2})
    else:
        store.save("m", g, g.fold_many(leaves), None, {"n": 1})
    return store


@pytest.fixture
def app_state(monkeypatch):
    """Install real reads over a store into the app; yields the reads."""
    reads = sgr.from_store(_store(two_versions=True))
    monkeypatch.setattr(main, "service", SimpleNamespace(database=object(), topomap_db=None))
    monkeypatch.setattr(main, "_scene_reads", reads)
    return reads


async def _get(path, **kw):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://t") as c:
        return await c.get(path, **kw)


# ---- JSON view ----

async def test_view_200_with_charts_links_and_snapped_bbox(app_state):
    r = await _get(f"{BASE}?bbox=0.5,0.5,7,3&level=0")
    assert r.status_code == 200
    j = r.json()
    assert j["available"] and j["version"] == 2 and j["level"] == 0 and j["charts"]
    assert j["bbox"] == [0.0, 0.0, 12.8, 6.4]          # snapped outward to the 6.4 m tile grid
    assert j["raster"] is None
    # an available answer carries no Cache-Control from the handler
    assert "no-store" not in r.headers.get("cache-control", "")


async def test_view_with_raster_metadata_matches_the_plane(app_state):
    j = (await _get(f"{BASE}?{BBOX}&raster=true")).json()
    meta = j["raster"]
    rr = await _get(f"{BASE}/raster?{BBOX}&version={j['version']}")
    rows, cols = meta["shape"]
    assert rr.headers["x-raster-shape"] == f"{rows},{cols}"
    plane = np.frombuffer(rr.content, dtype="<u2").reshape(rows, cols)
    assert plane.max() <= len(meta["keys"]) and plane.max() > 0


async def test_view_pinned_older_version_and_future_version(app_state):
    assert (await _get(f"{BASE}?{BBOX}&version=1")).json()["version"] == 1
    assert (await _get(f"{BASE}?{BBOX}&version=99")).status_code == 404


async def test_view_level_param_and_bounds(app_state):
    assert (await _get(f"{BASE}?{BBOX}&level=1")).json()["level"] == 1
    assert (await _get(f"{BASE}?{BBOX}&level=9")).status_code == 422
    assert (await _get(f"{BASE}?{BBOX}&level=-1")).status_code == 422
    assert (await _get(f"{BASE}?{BBOX}&level=x")).status_code == 422


@pytest.mark.parametrize("bbox", ["1,2,3", "a,b,c,d", "5,0,1,1", "0,0,nan,1", ""])
async def test_view_and_raster_bad_bbox_422(app_state, bbox):
    assert (await _get(f"{BASE}", params={"bbox": bbox})).status_code == 422
    assert (await _get(f"{BASE}/raster", params={"bbox": bbox})).status_code == 422


async def test_missing_bbox_is_422(app_state):
    assert (await _get(BASE)).status_code == 422


async def test_area_cap_is_a_422_for_json_and_raster(app_state):
    big = "bbox=0,0,5000,5000"
    for path in (BASE, f"{BASE}/raster"):
        r = await _get(f"{path}?{big}")
        assert r.status_code == 422 and "cells" in r.json()["detail"]


async def test_unknown_map_view_is_available_false_no_store(app_state):
    r = await _get(f"/api/v1/maps/nope/scene-graph?{BBOX}")
    assert r.status_code == 200
    assert r.json() == {"available": False, "map_id": "nope", "version": None, "charts": [], "links": []}
    assert r.headers["cache-control"] == "no-store"


# ---- raster ----

async def test_raster_200_headers_and_body(app_state):
    r = await _get(f"{BASE}/raster?{BBOX}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/octet-stream"
    h = r.headers
    assert h["x-scene-graph-version"] == "2" and h["x-raster-dtype"] == "u16le"
    assert h["cache-control"] == "no-cache"
    assert h["etag"].startswith('"') and "immutable" not in h["cache-control"]
    rows, cols = (int(v) for v in h["x-raster-shape"].split(","))
    assert len(r.content) == rows * cols * 2
    float(h["x-raster-step"])
    ox, oy = (float(v) for v in h["x-raster-origin"].split(","))
    assert (ox, oy) == (0.0, 0.0)


async def test_raster_pinned_version_is_public_max_age(app_state):
    r = await _get(f"{BASE}/raster?{BBOX}&version=2")
    assert r.status_code == 200
    assert r.headers["cache-control"] == f"public, max-age={sgr.REVALIDATE_S}"


async def test_raster_etag_304_revalidation(app_state):
    r = await _get(f"{BASE}/raster?{BBOX}")
    etag = r.headers["etag"]
    r2 = await _get(f"{BASE}/raster?{BBOX}", headers={"If-None-Match": etag})
    assert r2.status_code == 304 and r2.content == b""
    assert r2.headers["etag"] == etag and r2.headers["cache-control"] == "no-cache"
    r3 = await _get(f"{BASE}/raster?{BBOX}", headers={"If-None-Match": f"W/{etag}, \"other\""})
    assert r3.status_code == 304
    r4 = await _get(f"{BASE}/raster?{BBOX}", headers={"If-None-Match": '"stale"'})
    assert r4.status_code == 200
    # the ETag is per plane / level / bbox / version
    other = await _get(f"{BASE}/raster?{BBOX}&plane=dependent")
    assert other.headers["etag"] != etag and other.headers["x-raster-dtype"] == "u8"
    pinned = await _get(f"{BASE}/raster?{BBOX}&version=1", headers={"If-None-Match": etag})
    assert pinned.status_code == 200 and pinned.headers["etag"] != etag


async def test_raster_snapped_bbox_shares_the_etag(app_state):
    a = await _get(f"{BASE}/raster?bbox=0.5,0.5,7,3")
    b = await _get(f"{BASE}/raster?bbox=1,1,8,5")
    assert a.headers["etag"] == b.headers["etag"]


async def test_raster_bad_plane_and_level_422(app_state):
    assert (await _get(f"{BASE}/raster?{BBOX}&plane=x")).status_code == 422
    assert (await _get(f"{BASE}/raster?{BBOX}&level=99")).status_code == 422


async def test_raster_no_graph_404_no_store(app_state):
    r = await _get(f"/api/v1/maps/nope/scene-graph/raster?{BBOX}")
    assert r.status_code == 404 and "etag" not in r.headers
    assert r.json()["detail"] == "no scene graph for this map"


async def test_raster_no_graph_404_carries_no_store(app_state):
    r = await _get(f"/api/v1/maps/nope/scene-graph/raster?{BBOX}")
    assert r.headers.get("cache-control") == "no-store"


# ---- lineage ----

async def test_lineage_200_and_unknown_map(app_state):
    r = await _get(f"{BASE}/lineage?from_version=1")
    assert r.status_code == 200
    j = r.json()
    assert j["available"] and j["from_version"] == 1 and j["version"] == 2 and isinstance(j["retired"], dict)
    same = (await _get(f"{BASE}/lineage?from_version=2")).json()
    assert same["retired"] == {}
    n = await _get("/api/v1/maps/nope/scene-graph/lineage?from_version=0")
    assert n.status_code == 200 and n.json()["available"] is False
    assert n.headers["cache-control"] == "no-store"


async def test_lineage_errors(app_state):
    assert (await _get(f"{BASE}/lineage?from_version=-1")).status_code == 422
    assert (await _get(f"{BASE}/lineage?from_version=3")).status_code == 404
    assert (await _get(f"{BASE}/lineage")).status_code == 422


# ---- flag disabled ----

async def test_flag_disabled_answers_unavailable(monkeypatch):
    import packages.config as config
    monkeypatch.setattr(config, "SCENE_GRAPH_WORKER_ENABLED", False)
    monkeypatch.setattr(main, "_scene_reads", None)
    monkeypatch.setattr(main, "service", SimpleNamespace(database=object(), topomap_db=None))
    v = await _get(f"{BASE}?{BBOX}")
    assert v.status_code == 200 and v.json()["available"] is False
    assert v.headers["cache-control"] == "no-store"
    r = await _get(f"{BASE}/raster?{BBOX}")
    assert r.status_code == 404
    lin = await _get(f"{BASE}/lineage?from_version=0")
    assert lin.status_code == 200 and lin.json()["available"] is False
    assert lin.headers["cache-control"] == "no-store"


async def test_service_not_up_is_503(monkeypatch):
    monkeypatch.setattr(main, "service", None)
    for p in (f"{BASE}?{BBOX}", f"{BASE}/raster?{BBOX}", f"{BASE}/lineage?from_version=0"):
        assert (await _get(p)).status_code == 503


# ---- blocked-nodes with_chart_keys ----

def _blocked_rows():
    return [{"graph_node_id": "n1", "x": 2.0, "y": 1.5, "robot_x": 1.0, "robot_y": 1.5},
            {"graph_node_id": "@40.0,40.0"}]


@pytest.fixture
def blocked(monkeypatch):
    monkeypatch.setattr(main.blocked_nodes, "fetch_active", AsyncMock(side_effect=lambda *a: _blocked_rows()))
    monkeypatch.setattr(main.blocked_nodes, "resolve_run_ids", AsyncMock())


async def test_blocked_nodes_with_chart_keys_adds_keys(app_state, blocked):
    r = await _get("/api/v1/maps/m/blocked-nodes?with_chart_keys=true")
    assert r.status_code == 200
    rows = r.json()["blocked_nodes"]
    assert rows[0]["chart_keys"], rows
    assert all(isinstance(k, str) for k in rows[0]["chart_keys"])
    assert rows[1]["chart_keys"] == []          # outside every chart


async def test_blocked_nodes_without_flag_has_no_chart_keys(app_state, blocked):
    rows = (await _get("/api/v1/maps/m/blocked-nodes")).json()["blocked_nodes"]
    assert rows and all("chart_keys" not in r for r in rows)


async def test_blocked_nodes_chart_keys_degrade_without_graph(blocked, monkeypatch):
    import packages.config as config
    monkeypatch.setattr(config, "SCENE_GRAPH_WORKER_ENABLED", False)
    monkeypatch.setattr(main, "_scene_reads", None)
    monkeypatch.setattr(main, "service", SimpleNamespace(database=object(), topomap_db=None))
    r = await _get("/api/v1/maps/m/blocked-nodes?with_chart_keys=true")
    assert r.status_code == 200
    assert all("chart_keys" not in row for row in r.json()["blocked_nodes"])


async def test_blocked_nodes_chart_keys_degrade_on_reads_error(app_state, blocked, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("store down")
    monkeypatch.setattr(app_state, "owner_plane", boom)
    r = await _get("/api/v1/maps/m/blocked-nodes?with_chart_keys=true")
    assert r.status_code == 200
    assert all("chart_keys" not in row for row in r.json()["blocked_nodes"])


# ---- review round: JSON ETag/304, epoch pin, 503 ----

async def test_json_view_etag_304_and_epoch_pin(app_state):
    r = await _get(f"{BASE}?{BBOX}&version=2")
    assert r.status_code == 200 and r.headers["etag"] and r.json()["epoch"]
    assert r.headers["x-scene-graph-epoch"] == str(r.json()["epoch"])
    again = await _get(f"{BASE}?{BBOX}&version=2", headers={"If-None-Match": r.headers["etag"]})
    assert again.status_code == 304 and again.headers["etag"] == r.headers["etag"] and again.content == b""
    ok = await _get(f"{BASE}?{BBOX}&epoch={r.json()['epoch']}")
    assert ok.status_code == 200
    bad = await _get(f"{BASE}?{BBOX}&epoch=123")
    assert bad.status_code == 409
    assert bad.json()["detail"] == {"error": "epoch_changed", "epoch": r.json()["epoch"],
                                    "current_epoch": r.json()["epoch"], "version": 2}
    assert isinstance(r.json()["epoch"], int) and bad.headers["x-scene-graph-epoch"] == str(r.json()["epoch"])
    assert (await _get(f"{BASE}?{BBOX}&epoch=abc")).status_code == 422
    assert (await _get(f"{BASE}/raster?{BBOX}&epoch=123")).status_code == 409
    assert (await _get(f"{BASE}/lineage?from_version=0&epoch=123")).status_code == 409
    assert (await _get(f"{BASE}/lineage?from_version=0&epoch={r.json()['epoch']}")).status_code == 200


async def test_store_down_is_503_retry_after_not_a_no_graph_200(monkeypatch):
    def boom(m):
        raise ConnectionError("arango down")
    store = NS_store = SimpleNamespace(meta=boom, load=lambda *a, **k: None)
    monkeypatch.setattr(main, "service", SimpleNamespace(database=object(), topomap_db=None))
    monkeypatch.setattr(main, "_scene_reads", sgr.from_store(store))
    for p in (f"{BASE}?{BBOX}", f"{BASE}/raster?{BBOX}", f"{BASE}/lineage?from_version=0"):
        r = await _get(p)
        assert r.status_code == 503 and r.headers["retry-after"] == str(sgr.RETRY_AFTER_S), p
        assert r.json()["detail"]["error"] == "scene_graph_unavailable"


async def test_reads_not_built_yet_is_503_not_unavailable_false(monkeypatch):
    import packages.config as config
    monkeypatch.setattr(config, "SCENE_GRAPH_WORKER_ENABLED", True)
    monkeypatch.setattr(main, "_scene_reads", None)
    monkeypatch.setattr(main, "service", SimpleNamespace(database=object(), topomap_db=None))
    r = await _get(f"{BASE}?{BBOX}")
    assert r.status_code == 503 and r.headers["retry-after"]
