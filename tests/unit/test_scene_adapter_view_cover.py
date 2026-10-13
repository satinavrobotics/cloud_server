"""The adapter gives every leaf of a depth-carrying node its camera view, and cover() then answers from it
(rehearsal finding F9: Leaf.view was None on every leaf, so cover() returned nothing on real maps)."""
import datetime

import numpy as np

from packages.scene_graph.api import BBox, SceneParams
from packages.scene_graph.engine import ChartGraph
from packages.services.graph_builder import scene_adapter as sa
from tests.unit.scene_graph import chart_worlds as cw

RECEIPT = datetime.datetime(2026, 10, 12, 12, 0, 0, tzinfo=datetime.timezone.utc)
T_BASE_CAM = {"x": 0.117, "y": 0.033, "z": 0.26, "qx": -0.4987, "qy": 0.5014, "qz": -0.5041, "qw": 0.4958}   # office8's


def _node(lf, i, with_depth=True):
    stamp = int(RECEIPT.timestamp() * 1000) + 1000 * i
    node = {"_key": lf.key, "session_id": lf.session,
            "timestamp": (RECEIPT + datetime.timedelta(seconds=i + 1)).replace(tzinfo=None).isoformat(),
            "pose": {"x": lf.x, "y": lf.y, "yaw": lf.yaw},
            "costmap": {"occupancy": {"layer": "occupancy", "width": lf.window_u8.shape[1], "height": lf.window_u8.shape[0],
                                      "resolution": lf.resolution, "costmap_stamp_ms": stamp,
                                      "origin_map": {"x": lf.origin[0], "y": lf.origin[1], "yaw": 0.0}}}}
    if with_depth:
        node["depth"] = {"primary": {"camera": {"fx": 174.2, "width": 448, "valid_range_m": [0.2, 10.0],
                                                "T_base_cam": T_BASE_CAM}}}
    return node


def _through_the_adapter(with_depth):
    w, way = cw.two_rooms()
    out = []
    for i, lf in enumerate(cw.drive(w, way)):
        info = {}
        leaf, why = sa.to_leaf(_node(lf, i, with_depth), b"", window=lf.window_u8, view_info=info)
        assert why is None, why
        out.append((leaf, info["view"]))
    return out


def test_view_is_built_from_the_depth_record_and_cover_answers():
    built = _through_the_adapter(True)
    assert all(state == sa.VIEW_OK and leaf.view is not None for leaf, state in built)
    v = built[0][0].view
    assert 0.0 < v.rng <= 10.0 and abs(v.fov - 2 * np.arctan(448 / (2 * 174.2))) < 1e-9
    g = ChartGraph(SceneParams())
    g.fold_many([leaf for leaf, _ in built])
    keys = [c.key for c in g.query(BBox(-5, -5, 20, 20), raster=False).charts]
    leaves, ceiling, optimal = g.cover(keys, k=1)
    assert leaves and ceiling > 0.5 and optimal
    assert set(leaves) <= {leaf.key for leaf, _ in built}


def test_a_map_without_depth_folds_without_views_and_cover_is_empty_not_an_error():
    built = _through_the_adapter(False)
    assert all(state == sa.VIEW_NO_DEPTH and leaf.view is None for leaf, state in built)
    g = ChartGraph(SceneParams())
    g.fold_many([leaf for leaf, _ in built])
    keys = [c.key for c in g.query(BBox(-5, -5, 20, 20), raster=False).charts]
    leaves, ceiling, _ = g.cover(keys, k=1)
    assert leaves == []


def test_a_damaged_camera_block_never_skips_the_leaf():
    w, way = cw.two_rooms()
    lf = cw.drive(w, way)[0]
    node = _node(lf, 0)
    node["depth"]["primary"]["camera"]["fx"] = "nan?"
    info = {}
    leaf, why = sa.to_leaf(node, b"", window=lf.window_u8, view_info=info)
    assert why is None and leaf.view is None and info["view"] == sa.VIEW_BAD_CAMERA
