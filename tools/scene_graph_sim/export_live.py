"""Read-only export of one map's topo nodes and costmap PNGs from the live stack.

Runs inside a container that already has ArangoDB and MinIO access (python-arango, minio):

    docker exec -i docker_compose-graph-builder-service-1 python - <map> <outdir> < export_live.py
    docker cp docker_compose-graph-builder-service-1:<outdir>/. tools/scene_graph_sim/data/<map>/

It only reads. Nothing is written to ArangoDB or MinIO.
"""
import json
import os
import sys

from arango import ArangoClient
from minio import Minio

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
for d in docs:
    minio.fget_object(f"map-{map_id}", f"{d['_key']}/costmap/occupancy.png", f"{outdir}/{d['_key']}.png")
with open(f"{outdir}/nodes.json", "w") as f:
    json.dump(docs, f)
print(len(docs), "nodes exported")
