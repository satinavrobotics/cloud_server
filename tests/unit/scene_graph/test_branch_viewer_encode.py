"""The branch viewer's data encodings round-trip (tools/scene_graph_sim/branch_viewer/encode.py)."""
import os
import sys

import numpy as np
import pytest

pytestmark = pytest.mark.unit

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "tools", "scene_graph_sim", "branch_viewer"))
import checks  # noqa: E402
import encode  # noqa: E402


@pytest.mark.parametrize("n", [1, 5, 255, 256, 1000])
def test_rle_round_trip(n):
    rng = np.random.default_rng(n)
    a = rng.integers(0, 4, n).astype(np.uint8)
    assert (encode.unrle(encode.rle(a), n) == a).all()


def test_rle_long_constant_run_splits_at_255():
    a = np.full(600, 7, np.uint8)
    assert (encode.unrle(encode.rle(a), 600) == a).all()


def test_unrle_rejects_wrong_length():
    with pytest.raises(ValueError):
        encode.unrle(encode.rle(np.zeros(10, np.uint8)), 11)


def test_delta_round_trip_applies_only_changes():
    rng = np.random.default_rng(3)
    pa = rng.integers(0, 3, 500).astype(np.uint8)
    pb = rng.integers(0, 3, 500).astype(np.uint8)
    na, nb = pa.copy(), pb.copy()
    na[::37] = 9
    nb[5::53] = 7
    ca, cb = pa.copy(), pb.copy()
    encode.apply_delta(encode.delta(pa, pb, na, nb), ca, cb)
    assert (ca == na).all() and (cb == nb).all()


def test_varint_rejects_negative():
    with pytest.raises(ValueError):
        encode.varint(-1, bytearray())


def test_check_route_flags_nodes_off_navigable_mask():
    route = [[0.0, 0.0], [1.0, 0.0]]
    on_nav = lambda x, y: x < 0.5  # noqa: E731
    fails = checks.check_route(route, 1.0, (0.0, 0.0), (1.0, 0.0), on_nav)
    assert "nodes_nav" in fails and "segments_nav" in fails and "goal_snap" not in fails
