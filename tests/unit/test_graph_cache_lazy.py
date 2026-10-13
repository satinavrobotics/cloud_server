"""GraphCache in lazy mode (SCENE_GRAPH_PLANNER_MODE=lazy): opens a `PlanGraph` (manifest only), caches it per version,
`blocked_free_los` asks it `free_cell`, and a graph that could not read a tile is dropped so the next `get` re-opens the
map's current version."""
import pytest

from packages.scene_graph.api import SceneParams
from packages.scene_graph.engine import ChartGraph
from packages.scene_graph.store import MemoryBackend, SceneGraphStore
from packages.services.mission_planner import scene_route as sr
from packages.services.mission_planner.lazy_plan import PlanGraph

from .scene_graph import chart_worlds as cw

pytestmark = pytest.mark.unit


class _World:
    def __init__(self):
        self.be = MemoryBackend()
        self.store = SceneGraphStore(self.be, self.be)
        self.g = ChartGraph(SceneParams())
        w, way = cw.two_rooms()
        self.leaves = cw.drive(w, way)
        self.version = None
        self.n = 0

    def save(self, upto):
        batch = self.leaves[self.n:upto]
        self.n = upto
        self.version = self.store.save("m", self.g, self.g.fold_many(batch), self.version, {"n": upto})
        return self.version


def _cache(world, **kw):
    return sr.GraphCache(lambda: world.store, lazy=True, lazy_kw=dict(max_tiles=10 ** 6),
                         meta_ttl_s=0.0, **kw)


async def test_lazy_mode_opens_a_plan_graph_and_caches_it_per_version():
    w = _World()
    v1 = w.save(len(w.leaves) // 2)
    cache = _cache(w)
    try:
        g1, ver1 = await cache.get("m")
        assert isinstance(g1, PlanGraph) and ver1 == v1 and g1.version == v1
        assert not g1.view._cache                      # the manifest only: no tile read yet
        g1b, _ = await cache.get("m")
        assert g1b is g1                               # cached
        v2 = w.save(len(w.leaves))
        assert await cache.warm("m") == v2             # a newer version loads in the background; warm waits for it
        g2, ver2 = await cache.get("m")
        assert ver2 == v2 > v1 and isinstance(g2, PlanGraph) and g2 is not g1
        assert (await cache.get("m"))[0] is g2
    finally:
        cache.close()


async def test_blocked_free_los_uses_free_cell_not_snap():
    w = _World()
    w.save(len(w.leaves))
    cache = _cache(w)
    try:
        g, _ = await cache.get("m")
        calls = {"free": 0}
        real = g.free_cell

        def free_cell(i, j):
            calls["free"] += 1
            return real(i, j)

        def no_snap(*a, **k):
            raise AssertionError("snap must not be used by the lazy graph's line of sight")
        g.free_cell, g.snap = free_cell, no_snap
        visible = sr.blocked_free_los(g, ([], []))
        a = (1.0, 1.0)
        assert isinstance(visible(a, (1.5, 1.0)), bool)
        assert calls["free"] > 0
    finally:
        cache.close()


async def test_an_unavailable_tile_drops_the_graph_and_the_next_get_reopens():
    w = _World()
    v = w.save(len(w.leaves))
    cache = _cache(w)
    try:
        g1, _ = await cache.get("m")
        saved = {k: bytes(b) for k, b in w.be.blobs["m"].items() if "/t/" in k}
        for k in saved:                                # a garbage-collected version: its tiles are gone
            del w.be.blobs["m"][k]
        ids = iter("n%d" % i for i in range(10 ** 4))
        with pytest.raises(sr.ChartFallback) as err:
            sr.plan_on_graph(g1, "m", (1.0, 1.0), (5.0, 1.0), ([], []), lambda x, y: next(ids), 0.5, 1.5, 3.0)
        assert err.value.reason == "tile_unavailable"
        assert g1.unavailable
        w.be.blobs["m"].update(saved)                  # the current version is readable again
        g2, ver = await cache.get("m")
        assert ver == v and g2 is not g1 and not g2.unavailable
        assert cache.stats()["graphs"] == 1
        assert (await cache.get("m"))[0] is g2
    finally:
        cache.close()
