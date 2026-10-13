"""Throw-away ArangoDB only (127.0.0.1:18529, ~/.sg_test_arango.env): does a reader ever see a missing or mixed index while
`put_index` runs? Writer rewrites the per-map index N times; a reader thread polls anchors/links/meta in ONE query and
records the smallest anchor count and any anchors/links version mix it saw. Refuses any other endpoint."""
import os
import sys
import threading
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.dirname(os.path.dirname(HERE))]
from arango import ArangoClient  # noqa: E402

from packages.scene_graph.store_backends import ArangoMinioBackend  # noqa: E402

env = dict(l.strip().split("=", 1) for l in open(os.path.expanduser("~/.sg_test_arango.env")) if "=" in l)
url = env["SG_TEST_ARANGO_URL"]
if "127.0.0.1:18529" not in url:
    sys.exit("refusing: not the throw-away ArangoDB")
db = ArangoClient(hosts=url).db(env.get("SG_TEST_ARANGO_DB", "sg_test"), username=env["SG_TEST_ARANGO_USER"],
                                password=env["SG_TEST_ARANGO_PASSWORD"])


class Graph: pass
class Client: pass
c = Client(); c.graph = Graph(); c.graph.db = db; c.image = None
be = ArangoMinioBackend(c)
m = "sgio" + uuid.uuid4().hex[:6]
N_A, N_L, ROUNDS = int(os.environ.get("N_A", 300)), int(os.environ.get("N_L", 500)), int(os.environ.get("ROUNDS", 25))
anchors = lambda v: [{"key": f"c{i}", "anchor": [i, v], "cells": 10 + i, "area": 1.0, "clearance": .5, "driven": .5, "ever_blocked": 0, "leaf": None}
                     for i in range(N_A)]
links = lambda v: [{"a": f"c{i % N_A}", "b": f"c{(i * 7 + 1) % N_A}", "length": float(v), "width": 1.0, "channels": {}} for i in range(N_L)]

be.cas_meta(m, None, {"version": 1, "generation": 1})
be.put_index(m, 1, anchors(1), links(1))
Q = (f"RETURN {{a: LENGTH(sg_anchors_{m}), l: LENGTH(sg_links_{m}), iv: DOCUMENT('sg_meta_{m}/meta').index_version, "
     f"av: (FOR d IN sg_anchors_{m} COLLECT v = d.version RETURN v), lv: (FOR d IN sg_links_{m} COLLECT v = d.version RETURN v)}}")
stop = threading.Event(); seen = {"n": 0, "min_a": 10 ** 9, "min_l": 10 ** 9, "mixed": 0, "iv_mismatch": 0}


def reader():
    while not stop.is_set():
        r = list(db.aql.execute(Q))[0]
        seen["n"] += 1; seen["min_a"] = min(seen["min_a"], r["a"]); seen["min_l"] = min(seen["min_l"], r["l"])
        live = [v for v in r["av"] if v == r["iv"]]
        if len(r["av"]) > 1 and not live: seen["mixed"] += 1                  # several versions and none is the live one
        if r["iv"] not in r["av"] and r["a"] > 0: seen["iv_mismatch"] += 1    # the live version has no anchor docs
t = threading.Thread(target=reader); t.start()
t0 = time.time()
for v in range(2, 2 + ROUNDS):
    be.put_index(m, v, anchors(v), links(v))
stop.set(); t.join()
final = list(db.aql.execute(Q))[0]
for kind in ("anchors", "links", "meta"):
    if db.has_collection(f"sg_{kind}_{m}"): db.delete_collection(f"sg_{kind}_{m}")
print(f"{ROUNDS} rewrites of {N_A} anchors + {N_L} links in {time.time() - t0:.1f} s; reader polls {seen['n']}; "
      f"min anchors seen {seen['min_a']} (expect {N_A}), min links {seen['min_l']} (expect {N_L}); "
      f"index_version has no docs: {seen['iv_mismatch']}; final {final['a']}/{final['l']} versions {final['av']} live {final['iv']}")
