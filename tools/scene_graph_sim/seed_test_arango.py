"""Copy an exported map (export_live.py) into the throw-away test ArangoDB.

    python seed_test_arango.py office8

Only the test instance (SG_TEST_ARANGO_URL) is written. Collection: nodes_<map>.
"""
import json
import os
import sys

from arango_http import TestArango

map_id = sys.argv[1]
here = os.path.dirname(os.path.abspath(__file__))
docs = json.load(open(f"{here}/data/{map_id}/nodes.json"))
for d in docs:
    d.pop("_id", None); d.pop("_rev", None)
db = TestArango()
db.ensure_db()
db.recreate_collection(f"nodes_{map_id}")
db.insert(f"nodes_{map_id}", docs)
print(db.query(f"RETURN LENGTH(nodes_{map_id})"), "nodes in", db.url, db.db)
