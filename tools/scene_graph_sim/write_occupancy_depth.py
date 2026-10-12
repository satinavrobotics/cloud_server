"""Write generated `occupancy_depth` layers to a map's nodes. Runs INSIDE the graph-builder container (it has the credentials),
with the worktree's graph_db on PYTHONPATH so `set_node_costmaps` stamps `costmap_stored_ms`.

    python write_occupancy_depth.py <map_id> <dir with <node>.png + records.json> [--apply]

Default is a dry run (checks only, writes nothing). With --apply, per node: PNG first (`{node}/costmap/occupancy_depth.png`),
then the record via set_node_costmaps (merge, other layers kept). Idempotent: a node that already has the layer with the same
PNG sha256 is skipped. After each map: read every node and object back and compare (record, sha256, other layers unchanged).
Stops at the first failure. Never touches the `occupancy` layer.
"""
import hashlib
import io
import json
import os
import sys

from packages.topomap_dbs.client import TopomapDatabaseClient

LAYER = "occupancy_depth"
map_id, folder = sys.argv[1], sys.argv[2]
apply_ = "--apply" in sys.argv
records = json.load(open(os.path.join(folder, "records.json")))
c = TopomapDatabaseClient()
col = c.graph.db.collection(f"nodes_{map_id}")
bucket = c.image._bucket_name(map_id)
todo = skipped = 0
before = {}
for key in records:
    doc = col.get(key)
    if doc is None:
        sys.exit(f"STOP: node {key} not found in nodes_{map_id}")
    cm = doc.get("costmap") or {}
    before[key] = {k: v for k, v in cm.items() if k != LAYER}
    if LAYER in cm and cm[LAYER].get("png_sha256") == records[key]["png_sha256"]:
        skipped += 1
    else:
        if "occupancy" in cm and False:
            pass
        todo += 1
print(f"{map_id}: {len(records)} nodes, to write {todo}, already done {skipped}, apply={apply_}", flush=True)
if not apply_:
    sys.exit(0)
for key, rec in records.items():
    png = open(os.path.join(folder, key + ".png"), "rb").read()
    assert hashlib.sha256(png).hexdigest() == rec["png_sha256"], f"checksum mismatch {key}"
    if not c.image.store_costmap(png, key, LAYER, map_id, metadata={"source": "depth", "layer": LAYER}):
        sys.exit(f"STOP: store_costmap failed for {key}")
    if not c.graph.set_node_costmaps(map_id, key, {LAYER: rec}):
        sys.exit(f"STOP: set_node_costmaps failed for {key}")
bad = 0
for key, rec in records.items():
    doc = col.get(key)
    cm = doc.get("costmap") or {}
    obj = c.image.client.get_object(bucket, f"{key}/costmap/{LAYER}.png")
    data = obj.read(); obj.close(); obj.release_conn()
    ok = (cm.get(LAYER, {}).get("png_sha256") == rec["png_sha256"] and cm[LAYER].get("source") == "depth"
          and hashlib.sha256(data).hexdigest() == rec["png_sha256"] and doc.get("costmap_stored_ms")
          and {k: v for k, v in cm.items() if k != LAYER} == before[key])
    if not ok:
        bad += 1
        print("VERIFY FAIL", key, flush=True)
print(f"{map_id}: written {len(records)}, verify failures {bad}", flush=True)
sys.exit(1 if bad else 0)
