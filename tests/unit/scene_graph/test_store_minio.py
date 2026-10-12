"""The store's blob side against a REAL MinIO server (the adapter, not a fake). Skipped unless a throw-away MinIO is given:

    docker run -d --rm -p 127.0.0.1:19000:9000 -e MINIO_ROOT_USER=u -e MINIO_ROOT_PASSWORD=p12345678 minio/minio server /data
    SG_TEST_MINIO_ENDPOINT=127.0.0.1:19000 SG_TEST_MINIO_USER=u SG_TEST_MINIO_PASSWORD=p12345678 pytest -k store_minio

Refuses anything that is not 127.0.0.1 on a port other than 9000/9001 (the live stack's)."""
import os
import threading

import pytest

from packages.scene_graph.api import BBox, SceneParams
from packages.scene_graph.engine import ChartGraph
from packages.scene_graph.store import ArangoMinioBackend, MemoryBackend, SceneGraphStore

from . import chart_worlds as cw

pytestmark = [pytest.mark.unit, pytest.mark.requires_docker]
EP = os.environ.get("SG_TEST_MINIO_ENDPOINT")


@pytest.fixture(scope="module")
def blobs():
    if not EP:
        pytest.skip("no throw-away MinIO (SG_TEST_MINIO_ENDPOINT)")
    host, port = EP.rsplit(":", 1)
    if host != "127.0.0.1" or port in ("9000", "9001"):
        pytest.skip("refusing: not a throw-away MinIO")
    minio = pytest.importorskip("minio")
    mc = minio.Minio(EP, access_key=os.environ["SG_TEST_MINIO_USER"], secret_key=os.environ["SG_TEST_MINIO_PASSWORD"], secure=False)

    class Image:
        client = mc
        @staticmethod
        def _bucket_name(m): return "map-" + m.lower().replace("_", "-")
        @staticmethod
        def _ensure_map_bucket(m):
            b = Image._bucket_name(m)
            if not mc.bucket_exists(b): mc.make_bucket(b)
            return True

    client = type("C", (), {})()
    client.image = Image
    return ArangoMinioBackend(client)


def test_blob_roundtrip_listing_and_concurrent_put(blobs):
    m = "Sg_Test_Map"                                           # upper case and underscores: the bucket name is derived
    blobs.put(m, "scene_graph/g1/x", b"abc")
    assert blobs.get(m, "scene_graph/g1/x") == b"abc" and blobs.exists(m, "scene_graph/g1/x")
    assert "scene_graph/g1/x" in blobs.list(m)
    ts = [threading.Thread(target=blobs.put, args=(m, "scene_graph/g1/same", b"v")) for _ in range(12)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert blobs.get(m, "scene_graph/g1/same") == b"v"
    blobs.delete(m, ["scene_graph/g1/x", "scene_graph/g1/same"])
    assert blobs.get(m, "scene_graph/g1/x") is None


def test_save_load_gc_on_real_objects(blobs):
    docs = MemoryBackend()                                      # the document side has its own smoke test (ArangoDB)
    st = SceneGraphStore(blobs, docs)
    w, way = cw.two_rooms(); leaves = cw.drive(w, way)
    g = ChartGraph(SceneParams()); v = None
    for i in range(0, len(leaves), 6):
        v = st.save("sg_test_real", g, g.fold_many(leaves[i:i + 6]), v, {"i": i})
    again = st.load("sg_test_real")
    big = BBox(-5, -5, 20, 20)
    assert sorted(c.key for c in again.query(big, raster=False).charts) == sorted(c.key for c in g.query(big, raster=False).charts)
    st.gc("sg_test_real")
    assert st.meta("sg_test_real")["version"] == v
