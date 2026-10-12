"""Smoke test of ArangoMinioBackend's document side against the THROW-AWAY ArangoDB (never the live port); blobs in memory.

Reads ~/.sg_test_arango.env (SG_TEST_ARANGO_URL / _USER / _PASSWORD / _DB), refuses port 8529, uses a map id of its own and
drops its collections at the end.
"""
import os
import sys
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path[:0] = [ROOT, HERE, os.path.join(ROOT, "tests", "unit", "scene_graph", "..", "..", "..")]
from arango import ArangoClient  # noqa: E402

from arango_http import _load_env_file  # noqa: E402
from packages.scene_graph.api import SceneParams  # noqa: E402
from packages.scene_graph.engine import ChartGraph  # noqa: E402
from packages.scene_graph.store import ArangoMinioBackend, SceneGraphStore, StaleVersion  # noqa: E402
from tests.unit.scene_graph import chart_worlds as cw  # noqa: E402
from tests.unit.scene_graph.test_store import _FakeMinio  # noqa: E402

env = _load_env_file(os.path.expanduser("~/.sg_test_arango.env"))
url = env["SG_TEST_ARANGO_URL"]
if urlparse(url).port == 8529:
    sys.exit("refusing the live port")
db = ArangoClient(hosts=url).db(env.get("SG_TEST_ARANGO_DB", "sg_test"), username=env["SG_TEST_ARANGO_USER"],
                                password=env["SG_TEST_ARANGO_PASSWORD"])
mc = _FakeMinio()
client = type("C", (), {})()
client.graph = type("G", (), {"db": db})()
client.image = type("I", (), {"client": mc, "_bucket_name": staticmethod(lambda m: "map-" + m.lower().replace("_", "-")),
                              "_ensure_map_bucket": staticmethod(lambda m: True)})()
MAP = "smoke_store_map"
store = SceneGraphStore.from_client(client)
store.drop(MAP)
w, way = cw.two_rooms(); leaves = cw.drive(w, way)
g = ChartGraph(SceneParams()); v = None
for i in range(0, len(leaves), 8):
    v = store.save(MAP, g, g.fold_many(leaves[i:i + 8]), v, {"i": i})
print("saved version", v, "index lag:", store.index_error)
meta = store.meta(MAP)
print("meta", {k: meta[k] for k in ("version", "generation")}, "index_version", store.docs.index_version(MAP))
print("anchors docs", len(list(db.collection(f"sg_anchors_{MAP}").all())), "links docs", len(list(db.collection(f"sg_links_{MAP}").all())))
try:
    store.save(MAP, g, g.fold_many([]), v - 1, {})
    print("FENCING FAILED")
except StaleVersion:
    print("stale writer fenced by the compare-and-set")
h = store.load(MAP)
print("reloaded charts", len(h.query(__import__("packages.scene_graph.api", fromlist=["BBox"]).BBox(-9, -9, 99, 99), raster=False).charts))
store.drop(MAP)
print("dropped:", [n for n in ("anchors", "links", "meta") if db.has_collection(f"sg_{n}_{MAP}")] == [])
