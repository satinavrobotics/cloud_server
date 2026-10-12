"""Occupancy windows from stored depth, for nodes that have no costmap. DRY RUN ONLY: reads local files, writes local files.

    # nodes.json + <node>_<camera>.png as written by export_live.py-style read-only exports
    python depth_occupancy_backfill.py --nodes nodes.json --depth-dir depth/ --out out/ [--robot-height 0.5]

For every node with a depth record it writes `<out>/<node>.png` (u8: 0 free, 100 occupied, 255 unknown, 200 x 200 at 0.05 m,
centred on the pose and aligned to the lattice) and `<out>/records.json`: the `costmap.occupancy_depth` record to merge on the
node (shaped like ingest.costmap_record_from, plus source="depth" and the generator parameters). Writing to ArangoDB / MinIO is
a separate, reviewed step through `set_node_costmaps` / `store_costmap`; it is not done here.
"""
import argparse
import hashlib
import json
import math
import os
import sys

import numpy as np
from PIL import Image

sys.path[:0] = [os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))]
from packages.scene_graph.depth_occupancy import occupancy_from_depth  # noqa: E402

LAYER = "occupancy_depth"
RES = 0.05
SIZE = 200
VERSION = "depth_to_occupancy/2"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--nodes", required=True)
    ap.add_argument("--depth-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--robot-height", type=float, default=0.5)
    ap.add_argument("--floor-tolerance", type=float, default=0.08)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    nodes = sorted(json.load(open(a.nodes)), key=lambda n: n.get("session_node_id", 0))
    gen = {"version": VERSION, "robot_height_m": a.robot_height, "floor_tolerance_m": a.floor_tolerance,
           "floor": "per-leaf dominant plane near the base", "free": "camera-to-point rays and floor points",
           "unknown": "no depth evidence", "min_points": 1}
    records, skipped = {}, 0
    for n in nodes:
        depth = n.get("depth") or {}
        cam = sorted(depth)[0] if depth else None
        png = os.path.join(a.depth_dir, f"{n['_key']}_{cam}.png") if cam else None
        if not cam or not os.path.exists(png):
            skipped += 1
            continue
        rec = depth[cam]
        px, py = n["pose"]["x"], n["pose"]["y"]
        ox = (math.floor(px / RES) - SIZE // 2) * RES
        oy = (math.floor(py / RES) - SIZE // 2) * RES
        w = occupancy_from_depth(np.array(Image.open(png)), rec["depth_scale"], rec["camera"], rec["camera"]["T_base_cam"],
                                 rec["pose3d_map"], (ox, oy), (SIZE, SIZE), RES, a.robot_height, a.floor_tolerance)
        out = os.path.join(a.out, f"{n['_key']}.png")
        Image.fromarray(w, mode="L").save(out, optimize=True)
        records[n["_key"]] = {
            "layer": LAYER, "width": SIZE, "height": SIZE, "resolution": RES, "origin": {"x": ox, "y": oy, "yaw": 0.0},
            "frame": "map", "costmap_stamp_ms": rec.get("depth_stamp_ms"), "keyframe_stamp_ms": rec.get("rgb_stamp_ms"),
            "origin_map": {"x": ox, "y": oy, "yaw": 0.0}, "session_id": rec.get("session_id") or n.get("session_id"),
            "source": "depth", "generator": gen, "png_sha256": hashlib.sha256(open(out, "rb").read()).hexdigest()}
    json.dump(records, open(os.path.join(a.out, "records.json"), "w"))
    print(f"{len(records)} windows, {skipped} skipped -> {a.out}")


if __name__ == "__main__":
    main()
