"""Row tables are the largest live state of a solve (SEG, P, Q per candidate anchor). Two exact economies are pinned here:
a row nobody will read is neither exported (Local.export_rows(keep)) nor retained after its anchor left (Local.descend), and
neither changes any output: the digests below were taken from the commit before the change (charts, links, owner raster)."""
import hashlib

import numpy as np
import pytest

from packages.scene_graph.api import BBox, SceneParams
from packages.scene_graph.engine import ChartGraph

from . import chart_worlds as cw

pytestmark = pytest.mark.unit
PINS = {"two_rooms": "1fc269136f1a646e", "corridor": "72bd5a0014803171", "pillar_room": "ff6941d17778acdb",
        "ring": "44f1a41fba910d48", "island": "b7d976a3d6ca1a27"}


def _digest(name):
    w, way = getattr(cw, name)(); L = cw.drive(w, way)
    g = ChartGraph(SceneParams())
    for i in range(0, len(L), 5): g.fold_many(L[i:i + 5])
    v = g.query(BBox(-50, -50, 100, 100), raster=True)
    h = hashlib.sha1()
    for c in sorted(v.charts, key=lambda c: c.key):
        h.update(repr((c.key, c.anchor, c.cells, round(c.area_m2, 6), round(c.driven_frac, 6))).encode())
    for l in sorted(v.links, key=lambda l: (l.a, l.b, l.length)):
        h.update(repr((l.a, l.b, round(l.length, 6), round(l.width, 6))).encode())
    h.update(v.raster.index.tobytes())
    return g, h.hexdigest()[:16]


@pytest.mark.parametrize("name", list(PINS))
def test_outputs_equal_those_before_the_row_economies(name):
    assert _digest(name)[1] == PINS[name]


def test_export_of_wanted_rows_equals_the_filtered_full_export():
    g, _ = _digest("pillar_room")
    sp = g._solved.sp
    full = sp.export_rows()
    anchors = {(int(sp.ys[a]), int(sp.xs[a])) for a in g._solved.A}
    part = sp.export_rows(anchors)
    assert set(part) == set(full) & anchors and part
    for k, im in part.items():
        for f in ("T", "SEG", "P", "Q", "dep"):
            assert np.array_equal(im[f], full[k][f])
        assert im["rect"] == full[k]["rect"]


def test_only_rows_that_can_be_asked_for_again_survive_a_solve():
    g, _ = _digest("island")
    sp, A = g._solved.sp, g._solved.A
    wanted = {(int(sp.ys[a]), int(sp.xs[a])) for a in A}
    for us, _ in g._track.tried.values(): wanted |= us
    live = {(int(sp.ys[a]), int(sp.xs[a])) for a in sp.rows}
    assert live <= wanted                                    # nothing else is retained after the solve
    assert {(int(sp.ys[a]), int(sp.xs[a])) for a in A} <= live | set(g._track.img_rows)
