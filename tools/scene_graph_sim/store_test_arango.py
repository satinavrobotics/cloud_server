"""Persist a simulated scene graph into the throw-away test ArangoDB and read it back with AQL.

    python store_test_arango.py office8

Writes only to SG_TEST_ARANGO_URL (never the live instance). Collections: sg_anchors, sg_links (edge), sg_meta.
This is the shape the cloud store would take; it is here to prove that the graph answers a weighted
SHORTEST_PATH between any two anchors, which is what the planner needs.
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.dirname(os.path.dirname(HERE))]

from arango_http import TestArango
from leaves import load_leaves
from packages.scene_graph import Params, SceneGraph
from packages.scene_graph.levels import build_levels

map_id = sys.argv[1] if len(sys.argv) > 1 else "office8"
sg = SceneGraph(Params())
for lf in load_leaves(map_id):
    sg.add_leaf(lf)
levels = build_levels(sg)
res = sg.p.res
db = TestArango()
db.ensure_db()
db.recreate_collection("sg_anchors")
db.recreate_collection("sg_links", edge=True)
db.recreate_collection("sg_meta")
key = lambda i, lv=0: f"{map_id}:{lv}:{i}"
db.insert("sg_anchors", [dict(_key=key(n.id, L.index), map=map_id, level=L.index, id=n.id, x=n.x, y=n.y, cells=n.cells,
                              clearance=n.clearance, members=n.members)
                         for L in levels for n in L.nodes])
db.insert("sg_links", [dict(_from=f"sg_anchors/{key(l.a, L.index)}", _to=f"sg_anchors/{key(l.b, L.index)}", map=map_id, level=L.index,
                            cost=l.cost, length=l.length, width=l.width, portal=list(l.portal))
                       for L in levels for l in L.links])
db.insert("sg_meta", [dict(_key=map_id, built_at=time.time(), params=sg.p.__dict__, leaves=len(sg.log))])
ids = [n.id for n in levels[0].nodes]
print("anchors", len(ids), "links", len(levels[0].links), "levels", [len(L.nodes) for L in levels])
a, b = f"sg_anchors/{key(ids[0])}", f"sg_anchors/{key(ids[-1])}"
rows = db.query("""FOR v, e IN ANY SHORTEST_PATH @a TO @b sg_links OPTIONS {weightAttribute: 'cost', defaultWeight: 1}
                   FILTER e == null OR e.level == 0 RETURN {v: v.id, cost: e.cost}""", {"a": a, "b": b})
print("weighted path", ids[0], "->", ids[-1], rows)
