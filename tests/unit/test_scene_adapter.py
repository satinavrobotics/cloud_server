import datetime
import io

import numpy as np
import pytest
from PIL import Image

from packages.services.graph_builder import scene_adapter as sa

RECEIPT = datetime.datetime(2026, 10, 12, 12, 0, 0, tzinfo=datetime.timezone.utc)


def png(arr, **kw):
    buf = io.BytesIO()
    Image.fromarray(arr, mode="L").save(buf, format="PNG", **kw)
    return buf.getvalue()


def node(w=8, h=6, **over):
    n = {"_key": "n1", "session_id": "s1", "timestamp": RECEIPT.replace(tzinfo=None).isoformat(),
         "pose": {"x": 1.0, "y": 2.0, "yaw": 0.5},
         "costmap": {"occupancy": {"width": w, "height": h, "resolution": 0.05,
                                   "origin_map": {"x": -1.0, "y": -2.0, "yaw": 0.25},
                                   "costmap_stamp_ms": int(RECEIPT.timestamp() * 1000) - 500}}}
    n.update(over)
    return n


@pytest.mark.parametrize("shape", [(1, 1), (6, 8), (40, 33), (200, 200)])
def test_decode_matches_pillow_for_every_filter(shape):
    rng = np.random.default_rng(1)
    smooth = (np.add.outer(np.arange(shape[0]), np.arange(shape[1])) % 101).astype(np.uint8)
    noisy = rng.integers(0, 256, shape, dtype=np.uint8)
    for arr in (smooth, noisy):
        assert np.array_equal(sa.decode_png_gray8(png(arr)), arr)
        assert np.array_equal(sa.decode_png_gray8(png(arr, optimize=True)), arr)


def test_decode_rejects_other_formats():
    rgb = io.BytesIO()
    Image.fromarray(np.zeros((4, 4, 3), np.uint8), mode="RGB").save(rgb, format="PNG")
    with pytest.raises(sa.PngError):
        sa.decode_png_gray8(rgb.getvalue())
    with pytest.raises(sa.PngError):
        sa.decode_png_gray8(b"nope")
    with pytest.raises(sa.PngError):
        sa.decode_png_gray8(png(np.zeros((4, 4), np.uint8))[:-20])


def test_to_leaf_passes_geometry_through():
    arr = np.arange(48, dtype=np.uint8).reshape(6, 8)
    leaf, why = sa.to_leaf(node(), png(arr), r_d=0.2)
    assert why is None
    assert (leaf.key, leaf.session, leaf.r_d) == ("n1", "s1", 0.2)
    assert (leaf.x, leaf.y, leaf.yaw) == (1.0, 2.0, 0.5)
    assert leaf.origin == (-1.0, -2.0) and leaf.origin_yaw == 0.25 and leaf.resolution == 0.05
    assert np.array_equal(leaf.window_u8, arr)
    assert leaf.stamp_ms == int(RECEIPT.timestamp() * 1000) - 500


def test_depth_layer_is_used_when_it_is_the_only_one():
    n = node()
    n["costmap"] = {"occupancy_depth": n["costmap"]["occupancy"]}
    leaf, why = sa.to_leaf(n, png(np.zeros((6, 8), np.uint8)))
    assert why is None and leaf is not None


@pytest.mark.parametrize("mutate,reason", [
    (lambda n: n.pop("costmap"), sa.SKIP_NO_LAYER),
    (lambda n: n.pop("pose"), sa.SKIP_NO_POSE),
    (lambda n: n["costmap"]["occupancy"].pop("origin_map"), sa.SKIP_NO_ORIGIN),
    (lambda n: n["costmap"]["occupancy"].update(resolution=0), sa.SKIP_RESOLUTION),
    (lambda n: n["costmap"]["occupancy"].update(width=9), sa.SKIP_SIZE),
])
def test_skip_reasons(mutate, reason):
    n = node()
    mutate(n)
    assert sa.to_leaf(n, png(np.zeros((6, 8), np.uint8))) == (None, reason)


def test_bad_png_and_no_stamp():
    assert sa.to_leaf(node(), b"junk") == (None, sa.SKIP_BAD_PNG)
    n = node()
    n.pop("timestamp")
    n["costmap"]["occupancy"].pop("costmap_stamp_ms")
    assert sa.to_leaf(n, png(np.zeros((6, 8), np.uint8))) == (None, sa.SKIP_NO_STAMP)


def test_channels_only_when_same_size():
    ok = png(np.full((6, 8), 7, np.uint8))
    bad = png(np.full((5, 8), 7, np.uint8))
    leaf, _ = sa.to_leaf(node(), png(np.zeros((6, 8), np.uint8)),
                         channels={"depth_occ": ok, "wrong": bad, "junk": b"x"})
    assert list(leaf.channels) == ["depth_occ"]
    assert leaf.channels["depth_occ"].shape == (6, 8)


@pytest.mark.parametrize("length,width,expect", [
    (1.2, 0.6, (0.3, True)), (0.5, 0.9, (0.25, True)),
    (-1, 0.6, (0.25, False)), (1.0, -1, (0.25, False)), (None, 1.0, (0.25, False)),
    ("x", 1.0, (0.25, False)), (float("nan"), 1.0, (0.25, False))])
def test_half_side_is_the_smaller_side(length, width, expect):
    assert sa.half_side(length, width) == expect


def test_person_and_reserved_values_read_as_unknown():
    arr = np.array([[0, 50, 100, 253, 254, 255, 99, 252]], np.uint8)
    out = sa.occupancy_window(png(arr))
    assert out.tolist() == [[0, 50, 100, 255, 255, 255, 99, 252]]
    assert (sa.UNKNOWN, sa.PERSON, sa.RESERVED) == (255, 253, 254)
    ok = np.arange(0, 101, dtype=np.uint8).reshape(1, 101)
    assert np.array_equal(sa.occupancy_window(png(ok)), ok)


def test_person_cells_give_no_vote_in_the_fold():
    from packages.scene_graph.api import SceneParams
    from packages.scene_graph.engine import ChartGraph
    arr = np.zeros((6, 8), np.uint8)
    arr[2:4, 2:4] = 253
    leaf, why = sa.to_leaf(node(), png(arr))
    assert why is None and (leaf.window_u8 == 253).sum() == 0 and (leaf.window_u8 == 255).sum() == 4
    ref = np.zeros((6, 8), np.uint8)
    ref[2:4, 2:4] = 255
    ref_leaf, _ = sa.to_leaf(node(), png(ref))
    g1, g2 = ChartGraph(SceneParams()), ChartGraph(SceneParams())
    g1.fold_many([leaf]); g2.fold_many([ref_leaf])
    assert np.array_equal(g1._V, g2._V)
    assert (g1._V == 255).any() and not ((g1._V >= 100) & (g1._V != 255)).any()
