"""Writes a synthetic map into ArangoDB and MinIO the way graph-builder stores it.

Per leaf: the node document through `GraphDatabaseService.add_node` (pose, `session_id`,
`robot_name`, receipt `timestamp`, `capture_time`), the leaf edge to the previous node, the
costmap PNG through `ImageDatabaseService.store_costmap` (PNG first, as `_save_layers` does), then
the `costmap.occupancy` record through `set_node_costmap`, built by the ingest module's own
`check_costmap_payload` / `costmap_record_from` with an identity map transform. The record write
stamps `costmap_stored_ms` with the database clock, the cursor field of the scene worker.
"""
import base64
import datetime
import io
import time
import uuid
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image

from packages.services.graph_builder import ingest
from packages.utils import map_geo

from . import world as W

LAYER = "occupancy"


def png_gray8(arr: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(arr, dtype=np.uint8), mode="L").save(buf, format="PNG")
    return buf.getvalue()


def node_uuid(map_id: str, index: int) -> str:
    """A canonical lower-case UUID (what `is_node_id` accepts), stable per (map, index)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"sg-e2e/{map_id}/{index}"))


def add_leaf(client: Any, map_id: str, node_id: str, x: float, y: float, yaw: float,
             world: np.ndarray, *, session_id: str, robot: str, index: int,
             stamp_ms: int, layer: str = LAYER, with_costmap: bool = True,
             defer_png: bool = False) -> Optional[bytes]:
    """Store one leaf. `defer_png`: write the node and its costmap RECORD but not the PNG and
    return the PNG bytes (the record being visible before the object is what a slow upload looks
    like to the worker)."""
    graph, image = client.graph, client.image
    meta: Dict[str, Any] = {
        "robot_name": robot, "session_id": session_id, "session_node_id": index,
        "timestamp": datetime.datetime.now().isoformat(),      # the cloud's naive receipt time
        "capture_time": datetime.datetime.fromtimestamp(
            stamp_ms / 1000.0, datetime.timezone.utc).isoformat(),
    }
    assert graph.add_node(node_id=node_id, x=x, y=y, yaw=yaw, map_id=map_id, metadata=meta), \
        f"add_node {node_id} failed"
    if not with_costmap:
        return None
    win, origin = W.window_at(world, x, y)
    png = png_gray8(win)
    payload = {
        "session_node_id": index, "robot_name": robot, "layer": layer,
        "costmap_data": base64.b64encode(png).decode(), "width": int(win.shape[1]),
        "height": int(win.shape[0]), "resolution": W.RES,
        "origin": {"x": origin[0], "y": origin[1], "yaw": 0.0},
        "costmap_stamp_ms": int(stamp_ms), "frame": "map",
    }
    parsed = ingest.check_costmap_payload(payload)
    record = ingest.costmap_record_from(parsed, map_geo.IDENTITY, session_id)
    meta_png = {"layer": layer, "costmap_stamp_ms": stamp_ms, "session_id": session_id}
    if not defer_png:
        assert image.store_costmap(png, node_id, layer, map_id, metadata=meta_png), \
            "store_costmap failed"
    assert graph.set_node_costmap(map_id, node_id, layer, record), "set_node_costmap failed"
    return png if defer_png else None


def seed_map(client: Any, map_id: str, world: np.ndarray, way: Sequence[Tuple[float, float]], *,
             session_id: str = "sess-e2e", robot: str = "e2e-robot", step: float = 0.5,
             base_stamp_ms: Optional[int] = None, stamp_step_ms: int = 1000) -> List[str]:
    """Create the map's collections and every leaf along `way`; returns the node ids in order."""
    assert client.create_map(map_id), "create_map failed"
    base = base_stamp_ms if base_stamp_ms is not None else int(time.time() * 1000) - 600_000
    ids: List[str] = []
    for i, (x, y, yaw) in enumerate(W.drive(list(way), step)):
        nid = node_uuid(map_id, i)
        add_leaf(client, map_id, nid, x, y, yaw, world, session_id=session_id, robot=robot,
                 index=i, stamp_ms=base + i * stamp_step_ms)
        if ids:
            assert client.graph.add_edge(ids[-1], nid, map_id=map_id), "add_edge failed"
        ids.append(nid)
        # `costmap_stored_ms` is the database clock in whole ms and the cursor orders by it, then by
        # key: leaves written within one ms would be ordered by their (random) uuids. The fold's
        # result depends on the order of capture stamps, so a deterministic drive order needs
        # distinct stored times.
        time.sleep(0.003)
    return ids


def unstamp(client: Any, map_id: str, node_ids: Sequence[str]) -> int:
    """Remove `costmap_stored_ms` from nodes: what a map stored before the field existed looks like."""
    aql = ("FOR d IN @@c FILTER d._key IN @keys "
           "REPLACE d WITH UNSET(d, 'costmap_stored_ms') IN @@c RETURN 1")
    cur = client.graph.db.aql.execute(aql, bind_vars={"@c": f"nodes_{map_id}", "keys": list(node_ids)})
    return len(list(cur))
