"""Read-only export of one map's topo nodes and occupancy PNGs from the live stack.

Writes into <outdir>: `nodes.json` (the node documents that have a PNG) and `<node key>.png` (the occupancy window of
each). The PNG is the layer the live worker would use for the node: `occupancy`, else `occupancy_depth` (the backfilled
one), chosen by packages/services/graph_builder/scene_leaf.py (`role_layers`, LAYER_PRIORITY). The layer is not in the
file name, so `branch_viewer/worker.py` reads `<key>.png` as is.

Nodes the export cannot write are printed by key with a count, and the script exits 1:
  * `no_layer`: the node has no stored occupancy-role layer (neither `occupancy` nor `occupancy_depth`);
  * `missing_png`: the layer is stored but its PNG could not be fetched from MinIO.
Such nodes are left out of nodes.json, so the viewer never gets a node without a PNG. Exit 0 means every node with a
costmap was exported.

Runs inside a container that already has ArangoDB and MinIO access (python-arango, minio) and the packages tree
(the graph-builder image, WORKDIR /app, so `python -` finds packages/ on the working directory):

    docker exec -i docker_compose-graph-builder-service-1 python - <map> <outdir> < export_live.py
    docker cp docker_compose-graph-builder-service-1:<outdir>/. tools/scene_graph_sim/data/<map>/

It only reads. Nothing is written to ArangoDB or MinIO.
"""
import json
import os
import sys

from arango import ArangoClient
from minio import Minio

from packages.services.graph_builder import scene_leaf

map_id, outdir = sys.argv[1], sys.argv[2]
os.makedirs(outdir, exist_ok=True)

arango = ArangoClient(hosts=f"http://{os.environ['ARANGO_HOST']}:{os.environ['ARANGO_PORT']}")
db = arango.db("topomap_db", username="root", password=os.environ["ARANGO_PASSWORD"])
minio = Minio(
    f"{os.environ['MINIO_HOST']}:{os.environ['MINIO_PORT']}",
    access_key=os.environ["MINIO_ACCESS_KEY"],
    secret_key=os.environ["MINIO_SECRET_KEY"],
    secure=False,
)

docs = list(db.aql.execute(
    f"FOR d IN nodes_{map_id} FILTER HAS(d, 'costmap') SORT d.session_node_id RETURN d"))
exported, no_layer, missing_png = [], [], []
for d in docs:
    layer = scene_leaf.role_layers(d.get("costmap")).get(scene_leaf.ROLE_OCCUPANCY)
    if layer is None:
        no_layer.append(d["_key"])
        continue
    try:
        minio.fget_object(f"map-{map_id}", f"{d['_key']}/costmap/{layer}.png", f"{outdir}/{d['_key']}.png")
    except Exception as exc:  # the S3 error names the object; the key is what the operator needs
        missing_png.append(d["_key"])
        print("missing PNG", d["_key"], layer, type(exc).__name__, file=sys.stderr)
        continue
    exported.append(d)

with open(f"{outdir}/nodes.json", "w") as f:
    json.dump(exported, f)
print(len(docs), "nodes with a costmap;", len(exported), "exported")
if no_layer:
    print(len(no_layer), "without an occupancy layer (no_layer):", " ".join(no_layer), file=sys.stderr)
if missing_png:
    print(len(missing_png), "with a missing PNG (missing_png):", " ".join(missing_png), file=sys.stderr)
sys.exit(1 if no_layer or missing_png else 0)
