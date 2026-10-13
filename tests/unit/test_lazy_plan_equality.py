"""Equality of the lazy planner graph (`open_plan` -> `PlanGraph`) with the restored engine (`store.load(...).route`)
on the five test worlds, for seeded anchor-to-anchor pairs, random point pairs and pairs whose points straddle a
tile boundary (tiles are 64 lattice cells = 6.4 m), without an overlay and with a blocked disc / a blocked segment.

Compared: Route.length, Route.cost, the chart-key sequence and the cells (`nodes`), and reachability (lazy None iff the
engine's is None). Both planners use the canonical rule (minimal integer cost, then lexicographic minimum (y, x) cell
sequence), so equal-length routes are identical, with or without an overlay."""
import math
import random

import numpy as np
import pytest

from packages.scene_graph.api import BBox, Blocked
from packages.services.mission_planner.lazy_plan import CorridorTooLarge, PlanGraph, open_plan

from .scene_graph import chart_worlds as cw
from .test_lazy_plan import WORLDS, _build

pytestmark = pytest.mark.unit

TILE_M = 6.4
SHIFT = (3.3, 4.6)             # m; moves each (small, one-tile) world across the x = 6.4 and y = 6.4 tile boundaries


def _shifted(fn):
    """the same world translated by SHIFT (padded with occupied cells; shift is a whole number of lattice cells)"""
    def make():
        w, way = fn()
        px, py = int(round(SHIFT[0] / cw.R)), int(round(SHIFT[1] / cw.R))
        return np.pad(w, ((py, 0), (px, 0)), constant_values=100), [(x + SHIFT[0], y + SHIFT[1]) for x, y in way]
    return make


ALL = {**{n: f for n, f in WORLDS.items()}, **{n + "+shift": _shifted(f) for n, f in WORLDS.items() if n != "grid"}}


def _extent(eng):
    cs = eng.query(BBox(-1e3, -1e3, 1e3, 1e3), raster=False).charts
    xs = [c.anchor[0] for c in cs]
    ys = [c.anchor[1] for c in cs]
    return [c.key for c in cs], (min(xs) - 0.5, min(ys) - 0.5, max(xs) + 0.5, max(ys) + 0.5)


def _point_pairs(eng, ext, seed):
    """(p, q) point pairs: uniform, and straddling a tile boundary line (x or y = k * 6.4)"""
    rnd = random.Random(seed)
    x0, y0, x1, y1 = ext
    out = []
    for _ in range(40):
        out.append(((rnd.uniform(x0, x1), rnd.uniform(y0, y1)), (rnd.uniform(x0, x1), rnd.uniform(y0, y1))))
    kx = list(range(math.ceil(x0 / TILE_M), math.floor(x1 / TILE_M) + 1))
    ky = list(range(math.ceil(y0 / TILE_M), math.floor(y1 / TILE_M) + 1))
    for _ in range(50):
        if kx and (not ky or rnd.random() < 0.5):
            bx, t = rnd.choice(kx) * TILE_M, rnd.uniform(y0, y1)
            d1, d2 = rnd.uniform(0.02, 1.5), rnd.uniform(0.02, 1.5)
            out.append(((bx - d1, t), (bx + d2, t + rnd.uniform(-2, 2))))
        elif ky:
            by, t = rnd.choice(ky) * TILE_M, rnd.uniform(x0, x1)
            d1, d2 = rnd.uniform(0.02, 1.5), rnd.uniform(0.02, 1.5)
            out.append(((t, by - d1), (t + rnd.uniform(-2, 2), by + d2)))
    return out


def _overlays(free, rnd):
    """a disc and a segment around a point of the free route (so the overlay really cuts it)"""
    mid = free.nodes[len(free.nodes) // 2]
    ang = rnd.uniform(0, math.pi)
    dx, dy = 0.8 * math.cos(ang), 0.8 * math.sin(ang)
    return {"disc": Blocked(discs=[(mid[0], mid[1], rnd.choice((0.3, 0.6)))], segments=[]),
            "segment": Blocked(discs=[], segments=[((mid[0] - dx, mid[1] - dy), (mid[0] + dx, mid[1] + dy), 0.15)])}


def _lazy(lazy, a, b, bl, s, g):
    try:
        return lazy.route(a, b, bl, s, g)
    except CorridorTooLarge as e:                       # the fixture graph has no practical cap: any raise is a failure
        return e


@pytest.fixture(scope="module", params=sorted(ALL))
def cases(request):
    """all (kind, overlay-kind, engine route, lazy route) records of one world, computed once"""
    name = request.param
    st, eng, lazy = _build(ALL[name]())
    lazy = open_plan(st, "m", max_tiles=10 ** 6)
    keys, ext = _extent(eng)
    rnd = random.Random(1234 + len(name))
    pairs = []                                          # (kind, a, b, start, goal)
    allp = [(a, b) for a in keys for b in keys]
    rnd.shuffle(allp)
    pairs += [("anchor", a, b, None, None) for a, b in allp[:60]]
    pairs += [("anchor_same", a, a, None, None) for a in keys[:5]]
    for p, q in _point_pairs(eng, ext, 99 + len(name)):
        sa, sb = eng.snap(*p, 1.0), eng.snap(*q, 1.0)
        if sa.key is None or sb.key is None:
            continue
        pairs.append(("straddle" if (math.floor(p[0] / TILE_M) != math.floor(q[0] / TILE_M)
                                     or math.floor(p[1] / TILE_M) != math.floor(q[1] / TILE_M)) else "point",
                      sa.key, sb.key, (sa.x, sa.y), (sb.x, sb.y)))
    recs = []
    n_ov = 0
    for kind, a, b, s, g in pairs:
        free = eng.route(a, b, None, s, g)
        recs.append(dict(world=name, kind=kind, ov="none", a=a, b=b, s=s, g=g, e=free, l=_lazy(lazy, a, b, None, s, g)))
        if free is not None and len(free.nodes) >= 6 and n_ov < 40:
            n_ov += 1
            for ovk, bl in _overlays(free, rnd).items():
                recs.append(dict(world=name, kind=kind, ov=ovk, a=a, b=b, s=s, g=g, bl=bl,
                                 e=eng.route(a, b, bl, s, g), l=_lazy(lazy, a, b, bl, s, g)))
    return name, recs


def _ident(r):
    return "%s/%s/%s->%s" % (r["world"], r["ov"], r["a"], r["b"])


def _sel(recs, ov):
    sel = [r for r in recs if r["ov"] in ov]
    assert sel
    return sel


def _check_length(r):
    e, l = r["e"], r["l"]
    assert not isinstance(l, CorridorTooLarge), (_ident(r), str(l))
    assert (e is None) == (l is None), "reachability differs: " + _ident(r)
    if e is not None:
        assert l.length == pytest.approx(e.length, abs=1e-9), \
            "LENGTH DIVERGENCE %s kind=%s engine=%r lazy=%r" % (_ident(r), r["kind"], e.length, l.length)


def _check_seq(r):
    e, l = r["e"], r["l"]
    if e is None:
        return
    assert l.charts == e.charts, "chart sequence differs: %s kind=%s" % (_ident(r), r["kind"])


def _check_cells(r):
    e, l = r["e"], r["l"]
    if e is None:
        return
    assert [tuple(p) for p in l.nodes] == [tuple(p) for p in e.nodes], \
        "cells differ: %s kind=%s (len %d vs %d)" % (_ident(r), r["kind"], len(l.nodes), len(e.nodes))


# ---- no overlay: everything must hold today ------------------------------------------------------------------
def test_no_overlay_length_and_reachability_equal(cases):
    name, recs = cases
    sel = _sel(recs, ("none",))
    kinds = {r["kind"] for r in sel}
    assert {"anchor", "point"} <= kinds
    if name == "grid" or name.endswith("+shift"):       # the other worlds sit inside one tile: nothing to straddle
        assert "straddle" in kinds
    for r in sel:
        _check_length(r)


def test_no_overlay_chart_sequence_equal(cases):
    for r in _sel(cases[1], ("none",)):
        _check_seq(r)


def test_no_overlay_cells_equal(cases):
    for r in _sel(cases[1], ("none",)):
        _check_cells(r)


# ---- overlay: length, chart sequence and cells all equal (canonical tie rule) -------------------------------
@pytest.mark.parametrize("ov", ["disc", "segment"])
def test_overlay_length_and_reachability_equal(cases, ov):
    for r in _sel(cases[1], (ov,)):
        _check_length(r)


@pytest.mark.parametrize("ov", ["disc", "segment"])
def test_overlay_chart_sequence_equal(cases, ov):
    for r in _sel(cases[1], (ov,)):
        _check_length(r)
        _check_seq(r)


@pytest.mark.parametrize("ov", ["disc", "segment"])
def test_overlay_cells_equal(cases, ov):
    for r in _sel(cases[1], (ov,)):
        _check_length(r)
        _check_cells(r)


# ---- the cap: CorridorTooLarge only where the corridor really needs more tiles than the cap -----------------
def test_corridor_too_large_only_where_the_cap_applies(cases):
    name, recs = cases
    st, eng, big = _build(ALL[name]())
    cap = 3
    small = PlanGraph(st.view("m"), max_tiles=cap)
    raised = ok = 0
    for r in [r for r in recs if r["ov"] == "none"][:60]:
        try:
            got = small.route(r["a"], r["b"], None, r["s"], r["g"])
        except CorridorTooLarge as exc:
            raised += 1
            assert exc.tiles > cap
            big.route(r["a"], r["b"], None, r["s"], r["g"])
            assert big.last["tiles"] > cap, "raised under the cap: " + _ident(r)
            continue
        ok += 1
        assert (got is None) == (r["e"] is None)
        if got is not None:
            assert got.length == pytest.approx(r["e"].length, abs=1e-9)
    assert raised + ok
