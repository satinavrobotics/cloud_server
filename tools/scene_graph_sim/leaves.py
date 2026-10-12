"""Leaves of one map, read from the test ArangoDB (poses, costmap placement) and the exported PNGs."""
import os

import numpy as np
from PIL import Image

from arango_http import TestArango
from packages.scene_graph import Leaf

HERE = os.path.dirname(os.path.abspath(__file__))


def load_leaves(map_id: str, res: float = 0.05, limit: int = None):
    docs = TestArango().query(
        f"FOR d IN nodes_{map_id} FILTER HAS(d, 'costmap') SORT d.session_node_id "
        f"RETURN {{k: d._key, sid: d.session_id, x: d.pose.x, y: d.pose.y, ox: d.costmap.occupancy.origin_map.x, "
        f"oy: d.costmap.occupancy.origin_map.y}}")
    leaves = []
    for d in docs[:limit]:
        win = np.array(Image.open(f"{HERE}/data/{map_id}/{d['k']}.png"))
        leaves.append(Leaf(d["k"], d["x"], d["y"], round(d["ox"] / res), round(d["oy"] / res), win, d.get("sid")))
    return leaves
