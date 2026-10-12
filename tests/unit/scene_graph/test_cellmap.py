import numpy as np
import pytest

from packages.scene_graph.cellmap import CHUNK, UNKNOWN, CellMap

pytestmark = pytest.mark.unit


def test_fold_newest_wins_and_unknown_never_overwrites():
    cm = CellMap()
    a = np.full((4, 4), UNKNOWN, np.uint8)
    a[1:3, 1:3] = 100
    cm.fold(10, 20, a)
    b = np.full((4, 4), UNKNOWN, np.uint8)
    b[1, 1] = 0            # newer: free now
    b[2, 2] = UNKNOWN      # unknown must not erase the lethal cell
    new, flips = cm.fold(10, 20, b)
    val = cm.crop(10, 20, 14, 24)[0]
    assert val[1, 1] == 0 and val[2, 2] == 100 and val[0, 0] == UNKNOWN
    assert new == 0 and flips == 1


def test_fold_is_idempotent():
    cm = CellMap()
    w = (np.arange(36, dtype=np.uint8) % 7).reshape(6, 6)
    cm.fold(-3, -3, w)
    before = cm.crop(-3, -3, 3, 3)[0].copy()
    new, flips = cm.fold(-3, -3, w)
    assert new == 0 and flips == 0
    assert (cm.crop(-3, -3, 3, 3)[0] == before).all()


def test_window_across_chunk_borders_and_negative_indices():
    cm = CellMap()
    w = np.zeros((30, 30), np.uint8)
    cm.fold(-CHUNK + 5, -CHUNK + 5, w)          # straddles four chunks
    val = cm.crop(-CHUNK, -CHUNK, CHUNK, CHUNK)[0]
    assert (val[5:35, 5:35] == 0).all() and val[0, 0] == UNKNOWN


def test_owner_is_single_valued_and_not_created_for_unobserved_cells():
    cm = CellMap()
    cm.fold(0, 0, np.zeros((8, 8), np.uint8))
    own = np.full((8, 8), 3, np.int32)
    cm.write_own(0, 0, own)
    cm.write_own(0, 0, np.full((8, 8), 5, np.int32))
    assert (cm.crop(0, 0, 8, 8)[1] == 5).all()
    far = cm.crop(10 * CHUNK, 10 * CHUNK, 10 * CHUNK + 4, 10 * CHUNK + 4)[1]
    cm.write_own(10 * CHUNK, 10 * CHUNK, np.full((4, 4), 1, np.int32))
    assert (far == -1).all() and (cm.crop(10 * CHUNK, 10 * CHUNK, 10 * CHUNK + 4, 10 * CHUNK + 4)[1] == -1).all()


def test_body_and_path_clear_cells_and_visited_marks_only_the_leaf_cell():
    cm = CellMap()
    cm.fold(0, 0, np.full((40, 40), 100, np.uint8))
    cm.clear_path(5, 20, 30, 20, 3)
    cm.mark_visited(30, 20)
    val, _, vis = cm.crop(0, 0, 40, 40)
    assert (val[20, 5:31] == 0).all() and val[0, 0] == 100
    assert vis.sum() == 1 and vis[20, 30]
