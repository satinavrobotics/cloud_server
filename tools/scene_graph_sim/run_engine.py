"""Fold a map's leaves (from the read-only export in data/<map>/) through the chart engine and print what it makes.

    python run_engine.py office8 [--every 6]

Reads data/<map>/nodes.json + <node>.png (export_live.py). No database, no network. Prints per batch: charts, links, time;
at the end the independent verifier's counts (tests/unit/scene_graph/chart_verify.py) on the final state.
"""
import argparse
import datetime
import json
import os
import sys
import time

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path[:0] = [ROOT]
from packages.scene_graph.api import BBox, Leaf, SceneParams  # noqa: E402
from packages.scene_graph.engine import ChartGraph  # noqa: E402


def load(map_id):
    nodes = json.load(open(f"{HERE}/data/{map_id}/nodes.json"))
    nodes = sorted([n for n in nodes if "costmap" in n and "occupancy" in n["costmap"]], key=lambda n: n["session_node_id"])
    out = []
    for n in nodes:
        c = n["costmap"]["occupancy"]
        t = int(datetime.datetime.fromisoformat(n["capture_time"]).timestamp() * 1000)
        w = np.array(Image.open(f"{HERE}/data/{map_id}/{n['_key']}.png"))
        out.append(Leaf(key=n["_key"], session=n.get("session_id", "s"), stamp_ms=t, x=n["pose"]["x"], y=n["pose"]["y"],
                        yaw=n["pose"].get("yaw", 0.0), resolution=c["resolution"],
                        origin=(c["origin_map"]["x"], c["origin_map"]["y"]), window_u8=w, origin_yaw=c["origin_map"].get("yaw", 0.0)))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("map_id")
    ap.add_argument("--every", type=int, default=6)
    ap.add_argument("--verify", action="store_true")
    a = ap.parse_args()
    leaves = load(a.map_id)
    g = ChartGraph(SceneParams())
    big = BBox(-100, -100, 100, 100)
    for i in range(0, len(leaves), a.every):
        t = time.time()
        d = g.fold_many(leaves[i:i + a.every])
        v = g.query(big, raster=False)
        print(f"leaves {min(i + a.every, len(leaves)):3d}  charts {len(v.charts):2d}  links {len(v.links):2d}  "
              f"changed {len(d.changed):2d}  retired {len(d.retired):2d}  {time.time() - t:5.2f} s", flush=True)
    v = g.query(big, raster=False)
    w = [l.width for l in v.links]
    print(f"final: {len(v.charts)} charts, {len(v.links)} links, widths {min(w):.2f}..{max(w):.2f} m" if w else "final: no links")
    for c in v.charts:
        print(f"  {c.key:14s} anchor ({c.anchor[0]:6.2f},{c.anchor[1]:6.2f}) area {c.area_m2:5.2f} m2 driven {c.driven_frac:.2f} "
              f"ever_blocked {c.ever_blocked_frac:.2f} clr {c.clearance:.2f}")
    if a.verify:
        sys.path.insert(0, ROOT)
        from tests.unit.scene_graph.chart_verify import verify
        print(verify(g, np.zeros((1, 1), np.uint8), bbox=big))


if __name__ == "__main__":
    main()
