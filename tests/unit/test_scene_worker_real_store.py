"""The worker with the REAL engine and the REAL store (in-memory backends): a restart on a caught-up store.

What the e2e harness checks against ArangoDB / MinIO, in one process: a new worker (empty memory, fresh cursor store) loads
the saved state through `store.load`, re-reads the replay margin and must neither change the graph nor publish a new version,
whether it skips the leaves the store holds (default) or folds them again (`skip_folded=False`)."""
import pytest

from packages.scene_graph.api import BBox, SceneParams
from packages.scene_graph.engine import ChartGraph
from packages.scene_graph.store import MemoryBackend, SceneGraphStore, StaleVersion
from packages.services.graph_builder import scene_host
from packages.services.graph_builder import scene_worker as sw

from tests.unit.test_scene_smoke import Guard, Source, T0, cw, leaves_of, node_of, png

BIG = BBox(-5, -5, 30, 30)
MAP = "m"


def _worker(store, src, leaves, params, events=None, published=None, **cfg):
    return sw.SceneWorker(
        MAP, source=src, fetch_png=lambda m, k, layer: png(next(l for l in leaves if l.key == k).window_u8),
        footprint=lambda robot: (0.8, 0.5),
        engine_factory=lambda v: store.load(MAP, v) if v else ChartGraph(params),
        store=store, cursor_store=sw.MemoryCursorStore(), guard=Guard(),
        config=sw.WorkerConfig(batch_size=10, replay_margin_ms=10 ** 12, save_every_leaves=1, min_save_interval_s=0,
                               save_when_idle=True, **cfg),
        now_ms=lambda: T0 + 10_000_000, new_engine=lambda: ChartGraph(params), fence_errors=(StaleVersion,),
        stored_leaves=lambda version: scene_host.stored_leaf_index(store, MAP, version),
        on_event=(lambda kind, payload: events.append((kind, payload))) if events is not None else None,
        publish=(lambda version, dirty: published.append(version)) if published is not None else None)


def _run(worker, n=400):
    for _ in range(n):
        res = worker.step()
        if res.state == sw.IDLE:
            return res
        assert res.state in (sw.WORKED, sw.IDLE), res
    raise AssertionError("worker did not reach idle")


def _view(store):
    v = store.view(MAP).query(BIG, raster=False)
    return (sorted((c.key, c.cells, c.anchor, round(c.driven_frac, 6)) for c in v.charts),
            sorted((l.a, l.b, round(l.length, 6), round(l.width, 6)) for l in v.links))


@pytest.mark.parametrize("skip_folded", [True, False])
@pytest.mark.parametrize("world", [cw.two_rooms, cw.pillar_room])
def test_restart_on_a_caught_up_store_changes_nothing(world, skip_folded):
    leaves = leaves_of(world)
    be = MemoryBackend()
    params = SceneParams()
    first_store = SceneGraphStore(be, be)
    src = Source([node_of(l, i) for i, l in enumerate(leaves)])
    w1 = _worker(first_store, src, leaves, params)
    _run(w1)
    meta1, state1 = first_store.meta(MAP), _view(first_store)
    assert meta1["version"] >= 1 and w1.stats.folded == len(leaves)
    objects = len(be.list(MAP))

    # a new process: new store object on the same backend, new worker, empty memory, stored stamps are on the nodes
    store2 = SceneGraphStore(be, be)
    events, published = [], []
    w2 = _worker(store2, src, leaves, params, events=events, published=published, skip_folded=skip_folded)
    _run(w2)
    meta2 = store2.meta(MAP)
    assert meta2["version"] == meta1["version"], "a restart replay published a new version"
    assert _view(store2) == state1
    assert len(be.list(MAP)) == objects, "a restart replay wrote objects"
    if skip_folded:
        assert w2.stats.already_folded == len(leaves) and w2.stats.folded == 0
    else:
        assert w2.stats.folded == len(leaves)          # folded again, idempotently
    # nothing changed, so nothing is announced: a version that did not move is not an 'updated' event or a websocket message
    assert published == [], f"a replay that changed nothing published versions {published}"
    assert [p for k, p in events if k == "updated"] == [], "a replay that changed nothing emitted an 'updated' event"
