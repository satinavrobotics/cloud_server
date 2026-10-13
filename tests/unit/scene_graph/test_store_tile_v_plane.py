"""SceneGraphStore.tile_v_plane (C-1 of the occupancy view proposal): the stored occupancy plane of one tile, read-only,
None for a tile the version does not hold, safe under concurrent reads."""
import threading

import numpy as np
import pytest

from packages.scene_graph.api import SceneParams
from packages.scene_graph.engine import ChartGraph
from packages.scene_graph.store import MemoryBackend, SceneGraphStore, TILE_F
from tests.unit.scene_graph import chart_worlds as cw

pytestmark = pytest.mark.unit


def _saved():
    w, way = cw.two_rooms(W=12.0)
    g = ChartGraph(SceneParams())
    d = g.fold_many(cw.drive(w, way))
    be = MemoryBackend()
    st = SceneGraphStore(be, be)
    st.save("m", g, d, None, {})
    return st, g




def test_a_present_tile_is_the_saved_occupancy_plane_and_read_only():
    st, g = _saved()
    man = st.manifest("m")
    tx, ty = (int(k) for k in next(iter(man["tiles"])).split(","))
    v = st.tile_v_plane("m", man["version"], tx, ty)
    assert v is not None and v.shape == (TILE_F, TILE_F) and v.dtype == np.uint8
    assert not v.flags.writeable
    with pytest.raises(ValueError):
        v[0, 0] = 1
    # the plane equals what the store wrote for this tile: the engine's occupancy window, cut at the tile's origin
    ox, oy = tx * TILE_F - g._cx0, ty * TILE_F - g._cy0
    want = np.full((TILE_F, TILE_F), 255, np.uint8)
    H, W = g._V.shape
    r0, c0 = max(0, oy), max(0, ox)
    r1, c1 = min(H, oy + TILE_F), min(W, ox + TILE_F)
    if r1 > r0 and c1 > c0:
        want[r0 - oy:r1 - oy, c0 - ox:c1 - ox] = g._V[r0:r1, c0:c1]
    assert np.array_equal(v, want)


def test_an_absent_tile_is_none():
    st, _ = _saved()
    man = st.manifest("m")
    assert st.tile_v_plane("m", man["version"], 10_000, 10_000) is None
    assert st.tile_v_plane("m", man["version"], -10_000, 0) is None


def test_concurrent_reads_of_one_tile_agree_and_do_not_fail():
    st, _ = _saved()
    man = st.manifest("m")
    tx, ty = (int(k) for k in next(iter(man["tiles"])).split(","))
    ref = st.tile_v_plane("m", man["version"], tx, ty).copy()
    out, errs = [], []

    def read():
        try:
            for _ in range(20):
                out.append(np.array_equal(st.tile_v_plane("m", man["version"], tx, ty), ref))
        except Exception as e:           # noqa: BLE001 - any failure is a test failure
            errs.append(e)
    th = [threading.Thread(target=read) for _ in range(8)]
    [t.start() for t in th]
    [t.join() for t in th]
    assert not errs and out and all(out)
