"""The lazy planner graph (`PlanGraph`, read from stored tiles) answers like the restored engine (`store.load(...).route`):
same snap, same route cells and length, same chart sequence, with and without a blocked overlay; `free_cell` is the snap
test of `blocked_free_los`; a corridor over the cap raises CorridorTooLarge."""
import itertools
import math
import random

import numpy as np
import pytest

from packages.scene_graph.api import BBox, Blocked, SceneParams
from packages.scene_graph.engine import ChartGraph
from packages.scene_graph.store import MemoryBackend, SceneGraphStore
from packages.services.mission_planner import lazy_plan
from packages.services.mission_planner.lazy_plan import CorridorTooLarge, PlanGraph

from .scene_graph import chart_worlds as cw

pytestmark = pytest.mark.unit


def _grid_world(nx=4, ny=3, room=6.0, door=1.0):
    """nx x ny rooms of `room` m with `door` wide doors in every inner wall and a pillar in each; a serpentine drive"""
    W, H = nx * room, ny * room
    w = np.full((int(H / cw.R), int(W / cw.R)), 100, np.uint8)
    for i in range(nx):
        for j in range(ny):
            cw._rect(w, i * room + 0.2, j * room + 0.2, (i + 1) * room - 0.1, (j + 1) * room - 0.1, 0)
            cw._rect(w, i * room + 2.4, j * room + 2.4, i * room + 3.4, j * room + 3.4, 100)
    for i in range(1, nx):
        for j in range(ny):
            cw._rect(w, i * room - 0.1, j * room + room / 2 - door / 2, i * room + 0.2, j * room + room / 2 + door / 2, 0)
    for j in range(1, ny):
        for i in range(nx):
            cw._rect(w, i * room + room / 2 - door / 2, j * room - 0.1, i * room + room / 2 + door / 2, j * room + 0.2, 0)
    way = []
    for j in range(ny):
        xs = range(nx) if j % 2 == 0 else reversed(range(nx))
        for i in xs:
            way += [(i * room + 1.0, j * room + 1.0), (i * room + 5.0, j * room + 1.0), (i * room + 5.0, j * room + 5.0)]
    return w, way


def _build(world_way):
    w, way = world_way
    leaves = cw.drive(w, way)
    be = MemoryBackend()
    st = SceneGraphStore(be, be)
    g = ChartGraph(SceneParams())
    v = None
    for i in range(0, len(leaves), 9):
        v = st.save("m", g, g.fold_many(leaves[i:i + 9]), v, {"n": 1})
    return st, st.load("m"), PlanGraph(st.view("m"), max_tiles=10 ** 6)


WORLDS = {"two_rooms": cw.two_rooms, "room_loop": cw.room_loop, "ring": cw.ring, "pillar_room": cw.pillar_room,
          "grid": _grid_world}


@pytest.fixture(scope="module", params=sorted(WORLDS))
def built(request):
    return request.param, _build(WORLDS[request.param]())


def _pairs(g, n=40, seed=3):
    keys = [c.key for c in g.query(BBox(-1e3, -1e3, 1e3, 1e3), raster=False).charts]
    rnd = random.Random(seed)
    allp = [(a, b) for a in keys for b in keys]
    rnd.shuffle(allp)
    return allp[:n]


def _same(r_eng, r_lazy, tie_free=True):
    """equal length, and the same cells: under an overlay the engine searches the whole graph, the lazy reader a subset,
    and scipy's tie-break among EQUAL-length routes may differ there, so the route is then only checked to be a lattice
    path of that length"""
    if r_eng is None:
        assert r_lazy is None
        return
    assert r_lazy is not None
    assert r_lazy.length == pytest.approx(r_eng.length, abs=1e-9)
    if tie_free:
        assert r_lazy.charts == r_eng.charts
        assert [tuple(p) for p in r_lazy.nodes] == [tuple(p) for p in r_eng.nodes]
        return
    steps = [math.dist(p, q) for p, q in zip(r_lazy.nodes[:-1], r_lazy.nodes[1:])]
    assert all(s == pytest.approx(0.1) or s == pytest.approx(0.1 * math.sqrt(2)) for s in steps)
    assert sum(steps) == pytest.approx(r_lazy.length, abs=1e-6)


def test_route_between_anchors_equals_the_engine(built):
    name, (st, eng, lazy) = built
    for a, b in _pairs(eng):
        _same(eng.route(a, b), lazy.route(a, b))


def test_point_to_point_route_equals_the_engine(built):
    name, (st, eng, lazy) = built
    rnd = random.Random(5)
    ext = eng.query(BBox(-1e3, -1e3, 1e3, 1e3), raster=False)
    xs = [c.anchor[0] for c in ext.charts]; ys = [c.anchor[1] for c in ext.charts]
    for _ in range(40):
        p = (rnd.uniform(min(xs) - 0.5, max(xs) + 0.5), rnd.uniform(min(ys) - 0.5, max(ys) + 0.5))
        q = (rnd.uniform(min(xs) - 0.5, max(xs) + 0.5), rnd.uniform(min(ys) - 0.5, max(ys) + 0.5))
        sa, sb = eng.snap(*p, 1.0), eng.snap(*q, 1.0)
        la, lb = lazy.snap(*p, 1.0), lazy.snap(*q, 1.0)
        for pt, e, l in ((p, sa, la), (q, sb, lb)):
            # both take the nearest cell in lattice-index distance; cells at an equal index distance may tie-break
            # differently (EDT index vs argmin), and then differ in real distance
            assert (l.key is None) == (e.key is None)
            if e.key is None:
                continue
            idx = lambda s: math.hypot(math.floor(s.x / 0.1) - math.floor(pt[0] / 0.1),
                                       math.floor(s.y / 0.1) - math.floor(pt[1] / 0.1))
            assert idx(l) == pytest.approx(idx(e))
            if (l.x, l.y) == (e.x, e.y):
                assert l.key == e.key and l.crosses_blocked == e.crosses_blocked
        if sa.key is None or sb.key is None:
            continue
        _same(eng.route(sa.key, sb.key, None, (sa.x, sa.y), (sb.x, sb.y)),
              lazy.route(sa.key, sb.key, None, (sa.x, sa.y), (sb.x, sb.y)))


def test_overlay_route_equals_the_engine(built):
    name, (st, eng, lazy) = built
    n = 0
    for a, b in _pairs(eng, 25, seed=7):
        free = eng.route(a, b)
        if free is None or len(free.nodes) < 6:
            continue
        mid = free.nodes[len(free.nodes) // 2]
        for r in (0.3, 0.8):
            bl = Blocked(discs=[(mid[0], mid[1], r)], segments=[])
            e, l = eng.route(a, b, bl), lazy.route(a, b, bl)
            n += 1
            _same(e, l, tie_free=False)
            assert lazy.last["exact"]
            if l is not None:
                assert all(math.hypot(x - mid[0], y - mid[1]) > r for (x, y) in l.nodes)
    assert n


def test_min_width_matches_where_the_pair_has_a_link(built):
    name, (st, eng, lazy) = built
    bad = 0
    n = 0
    for a, b in _pairs(eng, 30, seed=9):
        e, l = eng.route(a, b), lazy.route(a, b)
        if e is None:
            continue
        n += 1
        bad += abs(e.min_width - l.min_width) > 1e-9
    assert bad <= max(1, n // 10), (name, bad, n)


def test_free_cell_is_the_snap_test(built):
    name, (st, eng, lazy) = built
    h = lazy.params.lattice
    rnd = random.Random(2)
    for _ in range(300):
        i, j = rnd.randint(-5, 130), rnd.randint(-5, 100)
        s = eng.snap((i + 0.5) * h, (j + 0.5) * h, 0.5 * h)
        assert lazy.free_cell(i, j) == (s.key is not None and s.distance <= 0.5 * h)


def test_corridor_cap_raises_and_the_tile_cache_is_bounded():
    st, eng, _ = _build(_grid_world())
    small = PlanGraph(st.view("m"), max_tiles=1)
    keys = [c.key for c in eng.query(BBox(-1e3, -1e3, 1e3, 1e3), raster=False).charts]
    far = max(itertools.product(keys, keys), key=lambda p: (eng.route(*p).length if eng.route(*p) else -1))
    with pytest.raises(CorridorTooLarge):
        small.route(*far)
    tight = PlanGraph(st.view("m"), max_tiles=10 ** 6, tile_cache=8)
    tight.route(*far)
    assert len(tight.view._cache) <= 8


def test_plan_on_graph_gives_the_same_plan_on_the_lazy_graph(built):
    """the whole planner path (snap, route, overlay, simplify with LOS, leaf ids) on both graphs"""
    from packages.services.mission_planner import scene_route as sr
    name, (st, eng, lazy) = built
    ext = eng.query(BBox(-1e3, -1e3, 1e3, 1e3), raster=False)
    pts = [c.anchor for c in ext.charts]
    rnd = random.Random(21)
    served = 0
    for _ in range(25):
        p, q = rnd.choice(pts), rnd.choice(pts)
        for overlay in (([], []), ([((p[0] + q[0]) / 2, (p[1] + q[1]) / 2, 0.4)], [])):
            out = []
            for g in (eng, lazy):
                ids = iter("n%d" % i for i in range(10 ** 4))
                seen = {}
                try:
                    out.append(sr.plan_on_graph(g, "m", p, q, overlay, lambda x, y: seen.setdefault((round(x, 3), round(y, 3)), next(ids)),
                                                0.5, 1.5, 3.0))
                except sr.ChartFallback as err:
                    out.append(err.reason)
            a, b = out
            if isinstance(a, str) or isinstance(b, str):
                assert a == b or (isinstance(a, str) and isinstance(b, str))
                continue
            served += 1
            assert a.goal_xy == b.goal_xy
            if not overlay[0]:                  # an overlay may tie-break equal-length routes differently
                assert [(w.x, w.y) for w in a.waypoints] == [(w.x, w.y) for w in b.waypoints]
                assert a.charts == b.charts
    assert served


# ---- hardening: single flight, far overlay, unavailable tiles ---------------------------------------------

class _CountingBackend(MemoryBackend):
    """counts and slows the tile reads (names `g<n>/t/<hash>`), can fail them"""

    def __init__(self):
        super().__init__()
        self.reads = {}
        self.delay = 0.0
        self.failing = None            # exception to raise on a tile read
        self._mu = __import__("threading").Lock()

    def get(self, map_id, key):
        if "/t/" in key:
            with self._mu:
                self.reads[key] = self.reads.get(key, 0) + 1
            if self.delay:
                __import__("time").sleep(self.delay)
            if self.failing is not None:
                raise self.failing
        return super().get(map_id, key)


def _counting_world():
    w, way = _grid_world()
    leaves = cw.drive(w, way)
    be = _CountingBackend()
    st = SceneGraphStore(be, be)
    g = ChartGraph(SceneParams())
    v = None
    for i in range(0, len(leaves), 9):
        v = st.save("m", g, g.fold_many(leaves[i:i + 9]), v, {"n": 1})
    return be, st


def test_the_same_tile_is_fetched_once_and_different_tiles_in_parallel():
    from concurrent.futures import ThreadPoolExecutor
    be, st = _counting_world()
    pg = PlanGraph(st.view("m"), max_tiles=10 ** 6)
    names = sorted(k.split(",") for k in pg.view.man["tiles"])
    tiles = [tuple(int(v) for v in k.split(",")) for k in pg.view.man["tiles"]]
    assert len(tiles) >= 4
    be.reads.clear()
    be.delay = 0.05
    work = tiles * 8                                   # every tile requested by 8 threads at once
    import time
    t0 = time.perf_counter()
    with ThreadPoolExecutor(32) as ex:
        out = list(ex.map(lambda t: pg._read(*t), work))
    took = time.perf_counter() - t0
    assert all(n == 1 for n in be.reads.values()) and len(be.reads) == len(tiles)
    assert took < 0.05 * len(tiles) * 0.7              # not serialised
    assert all(o is not None for o in out)


def test_a_failed_tile_read_reaches_every_waiter_and_is_not_cached():
    from concurrent.futures import ThreadPoolExecutor
    be, st = _counting_world()
    pg = PlanGraph(st.view("m"), max_tiles=10 ** 6)
    tile = tuple(int(v) for v in sorted(pg.view.man["tiles"])[0].split(","))
    be.reads.clear()
    be.delay = 0.1
    be.failing = FileNotFoundError("gone")
    with ThreadPoolExecutor(6) as ex:
        futs = [ex.submit(pg._read, *tile) for _ in range(6)]
        errs = [f.exception() for f in futs]
    assert all(isinstance(e, lazy_plan.TileUnavailable) and isinstance(e.cause, FileNotFoundError) for e in errs)
    assert sum(be.reads.values()) < 6                  # waiters shared the leader's failure
    assert pg.unavailable and tile not in pg.view._cache
    be.failing = None
    be.delay = 0.0
    assert pg._read(*tile) is not None                 # not cached: the next read retries and succeeds


def test_concurrent_routes_equal_serial_routes():
    from concurrent.futures import ThreadPoolExecutor
    st, eng, _ = _build(_grid_world())
    pg = PlanGraph(st.view("m"), max_tiles=10 ** 6, tile_cache=16)     # a tight cache evicts while others read
    pairs = _pairs(eng, 24, seed=11)
    want = [eng.route(a, b) for a, b in pairs]
    with ThreadPoolExecutor(8) as ex:
        got = list(ex.map(lambda p: pg.route(*p), pairs))
    for e, l in zip(want, got):
        _same(e, l)


def test_far_overlay_keeps_the_whole_graph_answer():
    """The engine searches EVERY chart under a non-empty overlay, so a disc that touches nothing can still give a
    shorter route than the no-overlay corridor search: the ellipse step may not be skipped for 'untouched' routes."""
    st, eng, lazy = _build(_grid_world())
    bl = Blocked(discs=[(-500.0, -500.0, 0.1)], segments=[])
    hits = 0
    for a, b in _pairs(eng, 200, seed=1):
        free = eng.route(a, b)
        if free is None:
            continue
        e = eng.route(a, b, bl)
        l = lazy.route(a, b, bl)
        _same(e, l, tie_free=False)
        assert lazy.last["exact"]
        hits += e.length < free.length - 1e-9
    assert hits, "the grid world must contain a pair whose whole-graph route is shorter than its corridor route"


def test_bounded_overlay_search_returns_the_corridor_route_as_the_bound():
    """limit= never excludes the answer: with a disc across the corridor the route is the engine's, with limit on"""
    st, eng, lazy = _build(_grid_world())
    n = 0
    for a, b in _pairs(eng, 60, seed=13):
        free = eng.route(a, b)
        if free is None or len(free.nodes) < 8:
            continue
        for frac in (0.25, 0.5, 0.75):
            mid = free.nodes[int(len(free.nodes) * frac)]
            bl = Blocked(discs=[(mid[0], mid[1], 0.5)], segments=[((mid[0] - 0.7, mid[1]), (mid[0] + 0.7, mid[1] + 0.3), 0.15)])
            _same(eng.route(a, b, bl), lazy.route(a, b, bl), tie_free=False)
            n += 1
    assert n


def test_plan_on_graph_reports_tile_unavailable():
    from packages.services.mission_planner import scene_route as sr
    be, st = _counting_world()
    pg = PlanGraph(st.view("m"), max_tiles=10 ** 6)
    be.failing = FileNotFoundError("gc'd")
    ids = iter("n%d" % i for i in range(10 ** 4))
    with pytest.raises(sr.ChartFallback) as err:
        sr.plan_on_graph(pg, "m", (1.0, 1.0), (23.0, 5.0), ([], []), lambda x, y: next(ids), 0.5, 1.5, 3.0)
    assert err.value.reason == "tile_unavailable"
    assert pg.unavailable
