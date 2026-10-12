"""Blob side of ArangoMinioBackend against a THROW-AWAY MinIO (refuses anything but 127.0.0.1 on a non-live port).

Env: SG_TEST_MINIO_ENDPOINT (127.0.0.1:19000), SG_TEST_MINIO_USER, SG_TEST_MINIO_PASSWORD. Documents are in memory here (the
Arango side has its own smoke test). Checks: bucket naming, put / get / exists / list / delete, concurrent put of one key,
a save / load / gc cycle through SceneGraphStore.
"""
import os
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path[:0] = [ROOT]
from minio import Minio  # noqa: E402

from packages.scene_graph.api import BBox, SceneParams  # noqa: E402
from packages.scene_graph.engine import ChartGraph  # noqa: E402
from packages.scene_graph.store import ArangoMinioBackend, MemoryBackend, SceneGraphStore  # noqa: E402
from tests.unit.scene_graph import chart_worlds as cw  # noqa: E402

ep = os.environ["SG_TEST_MINIO_ENDPOINT"]
host, port = ep.rsplit(":", 1)
if host != "127.0.0.1" or port in ("9000", "9001"):
    sys.exit("refusing: not the throw-away MinIO")
mc = Minio(ep, access_key=os.environ["SG_TEST_MINIO_USER"], secret_key=os.environ["SG_TEST_MINIO_PASSWORD"], secure=False)


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
client.graph = type("G", (), {"db": None})()
be = ArangoMinioBackend(client)
MAP = "Smoke_Map_1"
ok = True


def check(name, cond):
    global ok
    ok &= bool(cond)
    print(("PASS " if cond else "FAIL ") + name)


be.put(MAP, "g1/t/abc", b"hello")
check("bucket is lowercased with _ -> -", mc.bucket_exists("map-smoke-map-1") and Image._bucket_name(MAP) == "map-smoke-map-1")
check("object lives under scene_graph/", mc.stat_object("map-smoke-map-1", "scene_graph/g1/t/abc").size == 5)
check("get / exists / missing", be.get(MAP, "g1/t/abc") == b"hello" and be.exists(MAP, "g1/t/abc") and be.get(MAP, "nope") is None
      and not be.exists(MAP, "nope"))
for i in range(3): be.put(MAP, "g1/t/k%d" % i, b"x" * (i + 1))
check("list by prefix", be.list(MAP, "g1/t/") == ["g1/t/abc", "g1/t/k0", "g1/t/k1", "g1/t/k2"])
errs = []
def w():
    try: be.put(MAP, "g1/p/same", b"y" * 100000)
    except Exception as e: errs.append(e)
ts = [threading.Thread(target=w) for _ in range(12)]
[t.start() for t in ts]; [t.join() for t in ts]
check("12 concurrent puts of one key", not errs and be.get(MAP, "g1/p/same") == b"y" * 100000)
be.delete(MAP, ["g1/t/k0", "g1/t/k1"])
check("delete", be.list(MAP, "g1/t/") == ["g1/t/abc", "g1/t/k2"])
check("missing bucket reads as empty", be.list("Other_Map") == [] and be.get("Other_Map", "x") is None)

w_, way = cw.two_rooms(); leaves = cw.drive(w_, way)
store = SceneGraphStore(be, MemoryBackend(), keep_last=2, clock=lambda: 1.0)
M2 = "Smoke_Map_2"
g = ChartGraph(SceneParams()); v = None
for i in range(0, len(leaves), 6):
    v = store.save(M2, g, g.fold_many(leaves[i:i + 6]), v, {"i": i})
before = len(be.list(M2))
res = store.gc(M2)
h = store.load(M2)
big = BBox(-9, -9, 99, 99)
check("save x%d / gc / load round trip on real objects" % v,
      sorted(c.key for c in h.query(big, raster=False).charts) == sorted(c.key for c in g.query(big, raster=False).charts)
      and len(be.list(M2)) == before - res["deleted"] and res["deleted"] > 0)
store.drop(M2)
be.delete(M2, be.list(M2))
check("drop empties the prefix", be.list(M2) == [])
for m in (MAP,): be.delete(m, be.list(m))
sys.exit(0 if ok else 1)
