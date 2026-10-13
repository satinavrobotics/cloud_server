"""The canvas grows in every direction while leaves arrive and the result still satisfies every invariant (judged by the
independent verifier); persistent keys survive the growth."""
import numpy as np
import pytest

from packages.scene_graph.api import SceneParams
from packages.scene_graph.engine import ChartGraph

from . import chart_worlds as cw
from .chart_verify import ok, verify

pytestmark = pytest.mark.unit

# 2 m windows (the canvas margin is 2 m too), so the canvas grows every few leaves.
# UP_LEFT starts at the bottom right and goes left and up: growth with a shift (rows / cols != 0).
# DOWN_RIGHT starts at the top left and goes right and down: growth without a shift (padding only).
UP_LEFT = [(5.0, 4.2), (0.8, 4.2), (0.8, 0.8), (5.2, 0.8)]
DOWN_RIGHT = [(0.8, 0.8), (5.2, 0.8), (5.2, 4.2), (0.8, 4.2)]


def _anchors(g):
    s = g._solved
    return sorted((g._cx0 // 2 + int(s.sp.xs[a]), g._cy0 // 2 + int(s.sp.ys[a])) for a in s.A) if s is not None else []


def _run(world, leaves, batch=3, check=True):
    g = ChartGraph(SceneParams())
    shifts, trace = [], []
    for i in range(0, len(leaves), batch):
        had, before = g._V is not None, (g._cx0, g._cy0)
        g.fold_many(leaves[i:i + batch])
        shifts.append((before[0] - g._cx0, before[1] - g._cy0) if had else (0, 0))
        trace.append((_anchors(g), [c.key for c in g.query(cw_all(world), raster=False).charts]))
        if check and g._solved is not None:
            r = verify(g, world)
            assert ok(r), (i, r)
    return g, shifts, trace


def cw_all(world):
    from packages.scene_graph.api import BBox
    return BBox(-5.0, -5.0, world.shape[1] * 0.05 + 5.0, world.shape[0] * 0.05 + 5.0)


@pytest.fixture(scope="module", params=["up_left", "down_right"])
def room(request):
    w, _ = cw.pillar_room()
    way = UP_LEFT if request.param == "up_left" else DOWN_RIGHT
    return w, cw.drive(w, way, step=0.4, win=40), request.param


def test_growth_in_all_four_directions_keeps_every_invariant(room):
    w, leaves, kind = room
    g, shifts, _ = _run(w, leaves)
    xs = [s[0] for s in shifts]; ys = [s[1] for s in shifts]
    if kind == "up_left":
        assert any(x > 0 for x in xs) and any(y > 0 for y in ys), "the canvas never grew to the left and to the top"
    else:
        assert all(x == 0 for x in xs) and all(y == 0 for y in ys) and g._V.shape[0] > 2 * 40


def test_keys_persist_across_growth(room):
    w, leaves, _ = room
    _, _, tr = _run(w, leaves, check=False)
    seen_retired_then_back = False
    live_prev = set()
    retired = set()
    for _, keys in tr:
        cur = set(keys)
        assert not (cur & retired)                           # a retired key never comes back
        retired |= live_prev - cur
        live_prev = cur
    assert len(set().union(*[set(k) for _, k in tr])) <= 3 * max(len(k) for _, k in tr)
