"""packages/api/scene_graph_reads.py and graph_builder/scene_events.py, with a fake graph."""
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pytest
from fastapi import HTTPException

from packages.scene_graph.api import BBox
from packages.services.graph_builder import scene_events


def _load_reads():
    """By file path: importing `packages.api` pulls the whole API service (pydantic v1 only)."""
    path = Path(__file__).resolve().parents[2] / "packages" / "api" / "scene_graph_reads.py"
    spec = importlib.util.spec_from_file_location("scene_graph_reads_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sgr = _load_reads()


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def chart(key, x=0.0, y=0.0):
    return NS(key=key, anchor=(x, y), cells=10, area_m2=0.1, clearance=0.5, driven_frac=1.0,
              ever_blocked_frac=0.0, leaf="L", parent={1: "p:1"})


class FakeGraph:
    def __init__(self, calls):
        self.calls = calls

    def query(self, bbox, level, raster, parents=True):
        self.calls.append((bbox, level, raster))
        r = None
        if raster:
            r = NS(origin=(1.0, 2.0), step=0.1, index=np.array([[0, 1], [2, 1]], dtype=np.uint16),
                   keys=["a:1", "b:1"], dependent=np.array([[0, 0], [1, 0]], dtype=bool))
        return NS(version=3, charts=[chart("a:1"), chart("b:1", 1, 1)],
                  links=[NS(a="a:1", b="b:1", length=2.0, width=0.8, portal=((0, 0), (0, 1)),
                            channels={"c": 1.5})], raster=r)


def reads(meta=None, calls=None, **kw):
    calls = [] if calls is None else calls
    m = {"version": 3, "params": {"lattice": 0.1}} if meta is None else meta
    return sgr.SceneGraphReads(lambda mid: m, lambda mid, v, b: FakeGraph(calls), **kw), calls


def test_parse_bbox_ok_and_bad():
    assert sgr.parse_bbox("0,1,2,3") == BBox(0, 1, 2, 3)
    for bad in (None, "", "1,2,3", "a,b,c,d", "0,0,0,1", "0,0,nan,1", "0,0,inf,1", "5,0,1,1"):
        with pytest.raises(HTTPException) as e:
            sgr.parse_bbox(bad)
        assert e.value.status_code == 422


def test_view_charts_links_and_version():
    r, _ = reads()
    out = run(r.view("m", BBox(0, 0, 5, 5)))
    assert out["available"] and out["version"] == 3 and out["raster"] is None
    assert [c["key"] for c in out["charts"]] == ["a:1", "b:1"]
    assert out["charts"][0]["parent"] == {"1": "p:1"}
    assert out["links"][0]["portal"] == [[0, 0], [0, 1]]


def test_view_not_available_when_never_built():
    r, calls = reads(meta={})
    out = run(r.view("m", BBox(0, 0, 1, 1)))
    assert out == {"available": False, "map_id": "m", "version": None, "charts": [], "links": []}
    assert calls == []


def test_view_with_raster_metadata_and_one_query_for_both():
    r, calls = reads()
    b = BBox(0, 0, 1, 1)
    out = run(r.view("m", b, raster=True))
    assert out["raster"] == {"keys": ["a:1", "b:1"], "shape": [2, 2], "origin": [1.0, 2.0], "step": 0.1}
    body, h = run(r.raster("m", b))
    assert len(calls) == 1          # same (map, version, bbox, level, raster): cached
    assert np.frombuffer(body, dtype="<u2").tolist() == [0, 1, 2, 1]
    assert h["X-Raster-Shape"] == "2,2" and h["X-Raster-Dtype"] == "u16le" and h["X-Scene-Graph-Version"] == "3"


def test_dependent_plane_is_u8():
    r, _ = reads()
    body, h = run(r.raster("m", BBox(0, 0, 1, 1), plane="dependent"))
    assert list(body) == [0, 0, 1, 0] and h["X-Raster-Dtype"] == "u8"


def test_raster_limits_and_errors():
    r, _ = reads(max_cells=100)
    with pytest.raises(HTTPException) as e:
        run(r.raster("m", BBox(0, 0, 100, 100)))
    assert e.value.status_code == 422
    with pytest.raises(HTTPException) as e:
        run(r.view("m", BBox(0, 0, 1, 1), level=99))
    assert e.value.status_code == 422
    with pytest.raises(HTTPException) as e:
        run(r.raster("m", BBox(0, 0, 1, 1), plane="x"))
    assert e.value.status_code == 422
    nr, _ = reads(meta={})
    with pytest.raises(HTTPException) as e:
        run(nr.raster("m", BBox(0, 0, 1, 1)))
    assert e.value.status_code == 404


def test_too_many_charts_is_422():
    r, _ = reads(max_charts=1)
    with pytest.raises(HTTPException) as e:
        run(r.view("m", BBox(0, 0, 1, 1)))
    assert e.value.status_code == 422


def test_version_not_retained_is_404():
    def load(mid, v, b):
        raise LookupError(v)
    r = sgr.SceneGraphReads(lambda mid: {"version": 3}, load)
    with pytest.raises(HTTPException) as e:
        run(r.view("m", BBox(0, 0, 1, 1), version=1))
    assert e.value.status_code == 404


def test_lineage():
    r = sgr.SceneGraphReads(lambda mid: {"version": 5}, lambda *a: None, lambda mid, v: {"a:1": ["a:2"]})
    out = run(r.lineage("m", 2))
    assert out.pop("epoch") and out == {"available": True, "from_version": 2, "version": 5,
                                        "retired": {"a:1": ["a:2"]}}
    none = sgr.SceneGraphReads(lambda mid: {}, lambda *a: None)
    assert run(none.lineage("m", 2))["available"] is False


def test_updated_message_and_truncation():
    d = NS(bbox=BBox(0, 1, 2, 3), changed=["a:1"], retired={"b:1": ["a:1"]})
    m = scene_events.updated_message("m", 7, d)
    assert m == {"type": "scene_graph_updated", "map_id": "m", "version": 7, "bbox": [0, 1, 2, 3],
                 "changed": ["a:1"], "retired": {"b:1": ["a:1"]}, "truncated": False}
    big = NS(bbox=None, changed=[f"k:{i}" for i in range(5)], retired={})
    t = scene_events.updated_message("m", 8, big, max_keys=3)
    assert t["truncated"] and t["changed"] == [] and t["bbox"] is None


def test_make_publish_posts_on_the_loop():
    got = []

    class Pub:
        async def publish(self, map_id, message):
            got.append((map_id, message["version"]))

    loop = asyncio.new_event_loop()
    pub = scene_events.make_publish(Pub(), loop, "m")
    pub(4, NS(bbox=None, changed=[], retired={}))
    loop.run_until_complete(asyncio.sleep(0.01))
    assert got == [("m", 4)]


def test_build_disabled_answers_unavailable():
    r = sgr.build(False)
    assert run(r.view("m", BBox(0, 0, 1, 1)))["available"] is False


def test_from_store_uses_meta_load_and_optional_lineage():
    calls = []
    store = NS(meta=lambda mid: {"version": 2}, load=lambda mid, v, tiles=None: FakeGraph(calls))
    r = sgr.from_store(store)
    assert run(r.view("m", BBox(0, 0, 1, 1)))["version"] == 2
    assert run(r.lineage("m", 1))["available"] is False      # store has no lineage yet


def test_scene_graph_prefix_is_not_a_node():
    from packages.topomap_dbs.node_ids import NON_NODE_PREFIXES, is_node_id
    assert "scene_graph" in NON_NODE_PREFIXES and not is_node_id("scene_graph")


def test_future_version_is_404_and_whole_graph_loads_once_for_many_bboxes():
    r, _ = reads()
    with pytest.raises(HTTPException) as e:
        run(r.view("m", BBox(0, 0, 1, 1), version=9))
    assert e.value.status_code == 404
    loads = []
    store = NS(meta=lambda mid: {"version": 2},
               load=lambda mid, v, tiles=None: loads.append(1) or FakeGraph([]))
    w = sgr.from_store(store)
    for i in range(3):
        run(w.view("m", BBox(i, 0, i + 1, 1)))
    assert len(loads) == 1


@pytest.mark.parametrize("text", [None, "", "1,2,3", "a,b,c,d", "0,0,1,1,2", "nan,0,1,1", "0,0,inf,1",
                                  "1,0,0,1", "0,1,1,1"])
def test_parse_bbox_errors_are_422(text):
    with pytest.raises(HTTPException) as e:
        sgr.parse_bbox(text)
    assert e.value.status_code == 422


@pytest.mark.parametrize("level", [-1, sgr.MAX_LEVEL + 1])
def test_level_out_of_range_is_422_for_view_and_raster(level):
    r, _ = reads()
    for call in (lambda: r.view("m", BBox(0, 0, 1, 1), level=level),
                 lambda: r.raster("m", BBox(0, 0, 1, 1), level=level)):
        with pytest.raises(HTTPException) as e:
            run(call())
        assert e.value.status_code == 422


def test_level_bounds_are_accepted():
    r, _ = reads()
    for level in (0, sgr.MAX_LEVEL):
        assert run(r.view("m", BBox(0, 0, 1, 1), level=level))["available"] is True


class _LeveledGraph(FakeGraph):
    """A view that reports `levels` like ViewResult.levels (the engine and the stored view)."""
    levels = None

    def query(self, bbox, level, raster, parents=True):
        res = super().query(bbox, level, raster, parents)
        if self.levels is not None:
            res.levels = self.levels
        return res


def _counted(count):
    graph = type("G", (_LeveledGraph,), {"levels": count})
    m = {"version": 3, "params": {"lattice": 0.1}}
    return sgr.SceneGraphReads(lambda mid: m, lambda mid, v, b: graph([]))


def test_level_beyond_the_view_level_count_is_422():
    r = _counted(3)
    for level in (0, 1, 2):                   # count - 1 is the top level: fine
        assert run(r.view("m", BBox(0, 0, 1, 1), level=level))["level"] == level
    for level in (3, 4, sgr.MAX_LEVEL, -1):   # count, beyond, and negative: 422, never clamped silently
        for call in (lambda: r.view("m", BBox(0, 0, 1, 1), level=level),
                     lambda: r.raster("m", BBox(0, 0, 1, 1), level=level)):
            with pytest.raises(HTTPException) as e:
                run(call())
            assert e.value.status_code == 422


def test_single_level_map_answers_level_0_only():
    r = _counted(1)
    assert run(r.view("m", BBox(0, 0, 1, 1), level=0))["available"] is True
    with pytest.raises(HTTPException) as e:
        run(r.view("m", BBox(0, 0, 1, 1), level=1))
    assert e.value.status_code == 422


def test_absent_or_zero_levels_keeps_the_plain_range_check():
    for count in (None, 0):
        r = _counted(count)
        assert run(r.view("m", BBox(0, 0, 1, 1), level=sgr.MAX_LEVEL))["available"] is True


def test_area_cap_applies_to_json_and_raster_before_any_load():
    r, calls = reads(max_cells=100)
    for raster in (True, False):
        with pytest.raises(HTTPException) as e:
            run(r.view("m", BBox(0, 0, 100, 100), raster=raster))
        assert e.value.status_code == 422
    assert calls == []


def test_module_limits_are_the_documented_ones():
    assert (sgr.MAX_CELLS, sgr.MAX_CHARTS, sgr.MAX_LEVEL) == (4_000_000, 20_000, 8)
    r = sgr.SceneGraphReads(lambda m: {"version": 1}, lambda *a: None)
    assert (r.max_cells, r.max_charts) == (sgr.MAX_CELLS, sgr.MAX_CHARTS)


def test_negative_version_is_404():
    r, _ = reads()
    with pytest.raises(HTTPException) as e:
        run(r.view("m", BBox(0, 0, 1, 1), version=-1))
    assert e.value.status_code == 404


class FakeTiledStore:
    """meta / tiles_in / view like SceneGraphStore; counts tile loads (a view loads a tile once, lazily)."""

    def __init__(self, side=40, version=3, retained=(3,)):
        self.side, self.version, self.retained = side, version, set(retained)
        self.tile_loads, self.view_opens, self.whole_loads = [], [], 0

    def meta(self, mid):
        return {"version": self.version, "params": {"lattice": 0.1}}

    def tiles_in(self, mid, bbox, version=None):
        if version is not None and version not in self.retained:
            raise FileNotFoundError(version)
        tx0, ty0 = int(bbox.x0 // 6.4), int(bbox.y0 // 6.4)      # 128 cells * 0.05 m
        tx1, ty1 = int(bbox.x1 // 6.4), int(bbox.y1 // 6.4)
        return [(x, y) for x in range(max(tx0, 0), min(tx1, self.side - 1) + 1)
                for y in range(max(ty0, 0), min(ty1, self.side - 1) + 1)]

    def view(self, mid, version=None, tiles=None):
        self.view_opens.append(tuple(sorted(tiles)))
        store, allowed, seen = self, set(tiles), set()

        class V:
            def query(self, bbox, level=0, raster=True):
                for t in store.tiles_in(mid, bbox):
                    if t not in allowed:
                        from packages.scene_graph.api import MissingTiles
                        raise MissingTiles([t])
                    if t not in seen:
                        seen.add(t)
                        store.tile_loads.append(t)
                return FakeGraph([]).query(bbox, level, raster)
        return V()

    def load(self, mid, version=None, tiles=None):
        self.whole_loads += 1
        return FakeGraph([])


def test_tiled_store_small_bbox_on_large_map_loads_few_tiles_and_caches():
    store = FakeTiledStore(side=40)                  # 1600 tiles
    r = sgr.from_store(store)
    b = BBox(1.0, 1.0, 5.0, 5.0)                     # inside tile (0, 0)
    out = run(r.view("m", b, raster=True))
    assert out["available"] and store.whole_loads == 0
    assert store.tile_loads == [(0, 0)]
    run(r.view("m", b, raster=True))                 # result cache
    run(r.view("m", b, level=1, raster=True))        # view cache: same tile-set, same tiles, no reload
    assert store.view_opens == [((0, 0),)] and store.tile_loads == [(0, 0)]
    run(r.view("m", BBox(1.0, 1.0, 13.0, 5.0)))      # wider bbox: 3 tiles only
    assert set(store.tile_loads) == {(0, 0), (1, 0), (2, 0)} and len(store.tile_loads) == 4   # new tile-set, new view


def test_tiled_store_view_cache_is_bounded_lru():
    store = FakeTiledStore()
    r = sgr.from_store(store)
    for i in range(sgr.CACHE_VIEWS + 3):
        run(r.view("m", BBox(i * 6.4 + 0.1, 0.1, i * 6.4 + 0.2, 0.2)))
    assert len(r._views) == sgr.CACHE_VIEWS


def test_tiled_store_missing_tiles_are_retried_with_the_named_tiles():
    store = FakeTiledStore()
    first = store.tiles_in
    calls = {"n": 0}

    def short(mid, bbox, version=None):              # first scan misses a tile the query then asks for
        calls["n"] += 1
        got = first(mid, bbox, version)
        return got[:-1] if calls["n"] == 1 and len(got) > 1 else got
    store.tiles_in = short
    r = sgr.from_store(store)
    out = run(r.view("m", BBox(1.0, 1.0, 13.0, 5.0)))
    assert out["available"] is True
    assert len(store.view_opens) == 2 and len(store.view_opens[1]) == len(store.view_opens[0]) + 1


def test_tiled_store_unretained_version_is_404_and_future_is_404():
    store = FakeTiledStore(version=5, retained=(5,))
    r = sgr.from_store(store)
    with pytest.raises(HTTPException) as e:
        run(r.view("m", BBox(0, 0, 1, 1), version=2))
    assert e.value.status_code == 404
    with pytest.raises(HTTPException) as e:
        run(r.view("m", BBox(0, 0, 1, 1), version=9))
    assert e.value.status_code == 404


def test_tiled_store_pinned_version_is_part_of_the_cache_key():
    store = FakeTiledStore(version=5, retained=(5, 4))
    r = sgr.from_store(store)
    b = BBox(0.1, 0.1, 0.5, 0.5)
    assert run(r.view("m", b))["version"] == 5
    assert run(r.view("m", b, version=4))["version"] == 4
    assert len(store.view_opens) == 2


def test_persistent_missing_tiles_is_409():
    class Bad(FakeTiledStore):
        def view(self, mid, version=None, tiles=None):
            from packages.scene_graph.api import MissingTiles

            class V:
                def query(s, *a):
                    raise MissingTiles([(9, 9)])
            return V()
    r = sgr.from_store(Bad())
    with pytest.raises(HTTPException) as e:
        run(r.view("m", BBox(0, 0, 1, 1)))
    assert e.value.status_code == 409


def test_lineage_too_old_is_409():
    from packages.scene_graph import TooOld

    def lin(mid, v):
        raise TooOld("version 3 is no longer retained")
    r = sgr.SceneGraphReads(lambda mid: {"version": 5}, lambda *a: None, lin)
    with pytest.raises(HTTPException) as e:
        run(r.lineage("m", 2))
    assert e.value.status_code == 409


def test_rebuilt_dirty_is_reported_as_truncated():
    d = NS(bbox=None, changed=[], retired={"a:1": ["b:1"]}, full=True)
    m = scene_events.updated_message("m", 9, d)
    assert m["truncated"] and m["changed"] == [] and m["retired"] == {}


def test_build_event_validates_for_every_kind():
    from packages.events.emit import build_row
    cases = {
        "updated": {"version": 4, "leaves_folded": 12, "charts_changed": None},
        "fold_failed": {"stage": "fold", "detail": "boom", "consecutive": 2},
        "rebuild_started": {"reason": "needs_rebuild"},
        "rebuild_completed": {"reason": "needs_rebuild", "version": 7, "duration_s": 3.2},
        "skipped": {"reason": "map_not_enabled"},
    }
    codes = {}
    for kind, payload in cases.items():
        ev = scene_events.build_event("m1", kind, payload)
        row = build_row(ev, strict=True)
        assert row["payload"]["map_name"] == "m1" and row["robot_name"] is None
        assert row["source"] == "graph_builder"
        assert ev.discriminator.startswith(f"map:m1:scene_graph:{kind}:")
        codes[kind] = row["code"]
    assert codes["updated"] == "MAP.SCENE_GRAPH_UPDATED" and codes["skipped"] == "MAP.SCENE_GRAPH_DISABLED_SKIPPED"
    assert len(set(codes.values())) == 5


# ---------------------------------------------------------------- review fixes
import threading


def _gate_reads(**kw):
    """reads whose load blocks until `go` is set; counts loads."""
    go, loads = threading.Event(), []

    def load(mid, v, b):
        loads.append((mid, v))
        assert go.wait(5)
        return FakeGraph([])
    return sgr.SceneGraphReads(lambda m: {"version": 3, "params": {"lattice": 0.1}}, load, **kw), go, loads


def test_build_enabled_goes_through_from_client(monkeypatch):
    from packages.scene_graph import store as store_mod
    seen = []
    real = store_mod.SceneGraphStore.from_client.__func__

    def spy(cls, client, **kw):
        seen.append(client)
        return real(cls, client, **kw)
    monkeypatch.setattr(store_mod.SceneGraphStore, "from_client", classmethod(spy))

    class Col:
        def get(self, key):
            return {"_key": "meta", "version": 3, "generation": 1, "params": {"lattice": 0.1},
                    "params_hash": "h", "cursor": None, "manifest": "g1/manifest-3.json", "created_ms": 111}

    class Db:
        def has_collection(self, name):
            return name == "sg_meta_known"

        def collection(self, name):
            return Col()
    client = NS(graph=NS(db=Db()), image=NS(client=None, _bucket_name=lambda m: "b", _ensure_map_bucket=lambda m: True))
    r = sgr.build(True, client)
    assert seen == [client]
    assert run(r.view("nope", BBox(0, 0, 1, 1)))["available"] is False       # real store + backend, no meta
    assert run(r._version("known", None))[0] == 3                           # meta + created_ms epoch via the store
    assert run(r._version("known", None))[2] == (111, 1)
    with pytest.raises(ValueError):
        sgr.build(True)


def test_recreated_map_with_the_same_version_is_not_served_from_cache():
    state = {"epoch": 1000, "charts": ["a:1"]}

    def load(mid, v, b):
        g = FakeGraph([])
        orig = g.query

        def q(bbox, level, raster):
            res = orig(bbox, level, raster)
            res.charts = [chart(k) for k in state["charts"]]
            return res
        g.query = q
        return g
    r = sgr.SceneGraphReads(lambda m: {"version": 1, "params": {"lattice": 0.1}}, load,
                            epoch=lambda m, v: state["epoch"])
    b = BBox(0, 0, 1, 1)
    first = run(r.view("m", b, version=1))
    body1, h1 = run(r.raster("m", b, version=1))
    state.update(epoch=2000, charts=["z:1", "y:1"])             # deleted, recreated, back at version 1
    second = run(r.view("m", b, version=1))
    body2, h2 = run(r.raster("m", b, version=1))
    assert [c["key"] for c in first["charts"]] == ["a:1"]
    assert [c["key"] for c in second["charts"]] == ["z:1", "y:1"]
    assert h1["ETag"] != h2["ETag"] and first["epoch"] != second["epoch"]
    for h in (h1, h2):
        assert "immutable" not in h["Cache-Control"]
    # the old client ETag no longer matches: full body, not 304
    body, _ = run(r.raster("m", b, version=1, if_none_match=h1["ETag"]))
    assert body is not None


def test_epoch_falls_back_to_meta_fields_without_an_epoch_function():
    m = {"version": 1, "generation": 1, "params_hash": "a", "cursor": {"c": 1}, "params": {"lattice": 0.1}}
    r = sgr.SceneGraphReads(lambda mid: m, lambda *a: FakeGraph([]))
    _, h1 = run(r.raster("m", BBox(0, 0, 1, 1)))
    m["cursor"] = {"c": 2}
    _, h2 = run(r.raster("m", BBox(0, 0, 1, 1)))
    assert h1["ETag"] != h2["ETag"]


def test_from_store_epoch_uses_created_ms_of_the_manifest():
    created = {"cur": 5000, 2: 4000}
    docs = NS(meta=lambda mid: {"version": 3, "generation": 1, "created_ms": created["cur"]})
    store = NS(meta=lambda mid: {"version": 3, "params": {"lattice": 0.1}}, docs=docs,
               manifest=lambda mid, v: {"created_ms": created[v], "generation": 1},
               load=lambda mid, v, tiles=None: FakeGraph([]))
    r = sgr.from_store(store)
    assert run(r._version("m", None))[2] == (5000, 1)
    assert run(r._version("m", 2))[2] == (4000, 1)


def test_single_flight_json_and_raster_share_one_computation():
    r, go, loads = _gate_reads()
    b = BBox(0, 0, 3, 3)

    async def both():
        t1 = asyncio.ensure_future(r.view("m", b))
        t2 = asyncio.ensure_future(r.raster("m", b))
        t3 = asyncio.ensure_future(r.view("m", b, raster=True))
        await asyncio.sleep(0.2)
        go.set()
        return await asyncio.gather(t1, t2, t3)
    out = run(both())
    assert len(loads) == 1 and out[0]["available"] and out[1][0] is not None


def test_cache_is_thread_safe_under_eviction_pressure():
    r = sgr.SceneGraphReads(lambda m: {"version": 3, "params": {"lattice": 0.1}}, lambda *a: FakeGraph([]))
    r._results.max_items = 2
    errors = []

    def work(i):
        try:
            for j in range(40):
                x = ((i * 7 + j) % 9) * 6.4
                run(r.view("m", BBox(x, 0, x + 1, 1)))
        except Exception as exc:        # noqa: BLE001
            errors.append(exc)
    ts = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert errors == [] and len(r._results) <= 2


def test_result_cache_is_bounded_by_bytes(monkeypatch):
    monkeypatch.setattr(sgr, "CACHE_RESULT_BYTES", 1000)
    r, _ = reads()
    for i in range(10):
        run(r.view("m", BBox(i * 6.4, 0, i * 6.4 + 1, 1), raster=True))
    assert r._results._bytes <= 1000 and 0 < len(r._results) < 10


def test_bbox_snaps_outward_to_tiles_so_pans_hit_the_cache():
    r, calls = reads()
    a = run(r.view("m", BBox(1.0, 1.0, 5.0, 5.0)))
    b = run(r.view("m", BBox(1.5, 0.2, 5.9, 6.0)))
    assert a["bbox"] == b["bbox"] == [0.0, 0.0, 6.4, 6.4]
    assert len(calls) == 1
    assert calls[0][0].x1 < 6.4          # the store is not asked to touch the next tile


def test_raster_applies_the_chart_cap_and_the_u16_guard():
    r, _ = reads(max_charts=1)
    with pytest.raises(HTTPException) as e:
        run(r.raster("m", BBox(0, 0, 1, 1)))
    assert e.value.status_code == 422

    class Many(FakeGraph):
        def query(self, bbox, level, raster):
            res = super().query(bbox, level, raster)
            res.raster.keys = ["k%d" % i for i in range(sgr.MAX_OWNER_KEYS + 1)]
            return res
    big = sgr.SceneGraphReads(lambda m: {"version": 3}, lambda *a: Many([]))
    with pytest.raises(HTTPException) as e:
        run(big.raster("m", BBox(0, 0, 1, 1)))
    assert e.value.status_code == 422
    with pytest.raises(HTTPException):
        run(big.view("m", BBox(0, 0, 1, 1), raster=True))
    assert run(big.view("m", BBox(0, 0, 1, 1)))["available"]          # no plane asked: JSON is fine
    body, _ = run(big.raster("m", BBox(0, 0, 1, 1), plane="dependent"))   # u8 plane has no key limit
    assert body is not None


def test_etag_is_ascii_for_non_ascii_digit_bboxes_and_304_skips_the_query():
    box = sgr.parse_bbox("\u0660,\u0660,\u0661,\u0661")       # arabic-indic digits parse as floats
    assert box == BBox(0, 0, 1, 1)
    r, calls = reads()
    body, h = run(r.raster("m", box, version=3))
    assert body is not None
    h["ETag"].encode("ascii")
    assert len(calls) == 1
    body2, h2 = run(r.raster("m", box, version=3, if_none_match=f'"zzz", W/{h["ETag"]}'))
    assert body2 is None and h2["ETag"] == h["ETag"] and len(calls) == 1
    cold, _ = reads()
    none, _ = run(cold.raster("m", box, version=3, if_none_match=h["ETag"]))
    assert none is None          # same epoch/version/bbox: 304 with nothing computed


def test_cache_control_never_immutable():
    r, _ = reads()
    _, pinned = run(r.raster("m", BBox(0, 0, 1, 1), version=3))
    _, cur = run(r.raster("m", BBox(0, 0, 1, 1)))
    assert "immutable" not in pinned["Cache-Control"] and "max-age" in pinned["Cache-Control"]
    assert cur["Cache-Control"] == "no-cache"


def test_lineage_validates_caps_and_caches():
    calls = []

    def lin(mid, v):
        calls.append(v)
        return {"a:1": ["a:2"]}
    r = sgr.SceneGraphReads(lambda mid: {"version": 5}, lambda *a: None, lin)
    for bad, code in ((-1, 422), (9, 404)):
        with pytest.raises(HTTPException) as e:
            run(r.lineage("m", bad))
        assert e.value.status_code == code
    assert run(r.lineage("m", 2))["retired"] == {"a:1": ["a:2"]}
    run(r.lineage("m", 2))
    assert calls == [2]
    far = sgr.SceneGraphReads(lambda mid: {"version": 10_000}, lambda *a: None, lin)
    with pytest.raises(HTTPException) as e:
        run(far.lineage("m", 1))
    assert e.value.status_code == 409 and calls == [2]


# ---- map without a graph (never built, or dropped after an empty rebuild) ----------------------

def _real_reads():
    from packages.scene_graph.api import SceneParams
    from packages.scene_graph.engine import ChartGraph
    from packages.scene_graph.store import MemoryBackend, SceneGraphStore
    from tests.unit.scene_graph import chart_worlds as cw
    b = MemoryBackend()
    store = SceneGraphStore(b, b, clock=lambda: 1_700_000_000.0)
    w, way = cw.two_rooms()
    leaves = cw.drive(w, way)
    g = ChartGraph(SceneParams())
    store.save("m", g, g.fold_many(leaves), None, {"n": 1})
    return sgr.from_store(store), store


def test_never_built_map_is_available_false_and_raster_404_no_store():
    r, _ = _real_reads()
    b = BBox(0, 0, 10, 10)
    v = run(r.view("none", b))
    assert v == {"available": False, "map_id": "none", "version": None, "charts": [], "links": []}
    assert run(r.view("none", b, version=3))["available"] is False        # a pinned read is no error either
    with pytest.raises(HTTPException) as e:
        run(r.raster("none", b))
    assert e.value.status_code == 404 and e.value.headers["Cache-Control"] == "no-store"
    assert "ETag" not in (e.value.headers or {})
    lin = run(r.lineage("none", 0))
    assert lin["available"] is False and lin["retired"] == {}


def test_drop_after_cached_version_serves_nothing_stale_and_a_rebuild_is_fresh():
    r, store = _real_reads()
    b = BBox(0, 0, 40, 40)
    first = run(r.view("m", b))
    body, headers = run(r.raster("m", b))
    assert first["available"] and first["charts"] and body
    run(r.lineage("m", 1))
    assert len(r._results) and len(r._views)
    store.drop("m")
    assert run(r.view("m", b))["available"] is False
    assert run(r.view("m", b, version=first["version"]))["available"] is False
    with pytest.raises(HTTPException) as e:
        run(r.raster("m", b, version=first["version"], if_none_match=headers["ETag"]))   # old ETag, no 304
    assert e.value.status_code == 404
    assert run(r.lineage("m", 0))["available"] is False
    assert len(r._results) == 0 and len(r._views) == 0 and len(r._lineages) == 0
    # a new incarnation restarts at version 1: new epoch, never the old cached answer or a 304
    from packages.scene_graph.api import SceneParams
    from packages.scene_graph.engine import ChartGraph
    from tests.unit.scene_graph import chart_worlds as cw
    w, way = cw.two_rooms()
    g = ChartGraph(SceneParams())
    store.clock = lambda: 1_700_000_100.0
    store.save("m", g, g.fold_many(cw.drive(w, way)[:6]), None, {"n": 1})
    again = run(r.view("m", b))
    assert again["available"] and again["version"] == first["version"]
    body2, h2 = run(r.raster("m", b, if_none_match=headers["ETag"]))
    assert body2 is not None and h2["ETag"] != headers["ETag"]


def test_fake_cache_is_purged_when_meta_disappears():
    state = {"m": {"version": 3, "params": {"lattice": 0.1}}}
    calls = []
    r = sgr.SceneGraphReads(lambda mid: state["m"], lambda mid, v, b: FakeGraph(calls), whole_graph=True)
    run(r.view("m", BBox(0, 0, 1, 1)))
    assert len(r._results) == 1 and len(r._graphs) == 1
    state["m"] = {}
    assert run(r.view("m", BBox(0, 0, 1, 1)))["available"] is False
    assert len(r._results) == 0 and len(r._graphs) == 0


# ---- review round: snapping, one meta read, ETag/304, epoch pin, 503, parents, bounds ----

@pytest.mark.parametrize("t", [6.4, 0.1, 3.2, 12.8])
def test_snap_is_idempotent_and_outward_across_the_map(t):
    r = sgr.SceneGraphReads(lambda m: {}, lambda *a: None, tile_m=t)
    rng = np.random.default_rng(7)
    vals = list(rng.uniform(-3000.0, 3000.0, 4000)) + [k * t for k in range(-2000, 2000)] + \
        [k * t + d for k in (-7, 0, 3, 41, 999) for d in (-1e-9, 1e-9, -1e-4, 1e-4)]
    for v in vals:
        for box in (BBox(v, v, v + 1.0, v + 1.0), BBox(v - 5.0, v - 2.0, v, v + 0.0001)):
            s = r._snap(box)
            assert r._snap(s) == s, (v, box, s)             # no drift, no extra tile
            assert s.x0 <= box.x0 + 1e-5 and s.x1 >= box.x1 - 1e-5      # outward (to within the 1e-6-tile tolerance)
            assert s.x1 - s.x0 - (box.x1 - box.x0) < 2 * t + 1e-5      # and by less than a tile per side


def test_snap_known_boundary_values_of_the_6_4_grid():
    r, _ = reads()
    for k in (3, 7, 11, 13, 29, 31, 4096):
        edge = round(k * 6.4, 6)
        s = r._snap(BBox(edge, edge, edge + 0.5, edge + 0.5))
        assert s.x0 == edge and s.y0 == edge                         # an edge ON a boundary stays there
        assert s.x1 == round((k + 1) * 6.4, 6)
        assert r._snap(s) == s


def test_one_meta_read_per_request_and_epoch_comes_from_it():
    n = []
    docs_reads = []
    created = {"version": 3, "generation": 1, "created_ms": 777, "params": {"lattice": 0.1}}

    def meta(mid):
        n.append(mid)
        return dict(created)
    store = NS(meta=meta, docs=NS(meta=lambda mid: docs_reads.append(mid) or created),
               load=lambda mid, v, tiles=None: FakeGraph([]))
    r = sgr.from_store(store)
    b = BBox(0, 0, 1, 1)
    run(r.view("m", b, raster=True))
    assert len(n) == 1 and docs_reads == []
    run(r.raster("m", b))
    run(r.owner_plane("m", b))
    assert len(n) == 3 and docs_reads == []       # one per request, never a second one for the epoch
    out = run(r.view("m", b))
    assert out["epoch"] == sgr._tag((777, 1))


def test_json_view_has_an_etag_and_answers_304():
    r, calls = reads()
    out, h = run(r.view_conditional("m", BBox(0, 0, 1, 1), version=3))
    assert out["available"] and h["ETag"].encode("ascii") and h["X-Scene-Graph-Epoch"] == str(out["epoch"])
    assert h["Cache-Control"] == f"public, max-age={sgr.REVALIDATE_S}" and len(calls) == 1
    again, h2 = run(r.view_conditional("m", BBox(0, 0, 1, 1), version=3, if_none_match=f'W/{h["ETag"]}'))
    assert again is None and h2["ETag"] == h["ETag"] and len(calls) == 1
    cold, _ = reads()
    none, _ = run(cold.view_conditional("m", BBox(0, 0, 1, 1), version=3, if_none_match=h["ETag"]))
    assert none is None and cold._results.__len__() == 0         # nothing computed for a 304
    # other params -> other ETag
    _, h3 = run(r.view_conditional("m", BBox(0, 0, 1, 1), version=3, raster=True))
    _, h4 = run(r.view_conditional("m", BBox(0, 0, 1, 1), version=3, level=1))
    assert len({h["ETag"], h3["ETag"], h4["ETag"]}) == 3
    _, cur = run(r.view_conditional("m", BBox(0, 0, 1, 1)))
    assert cur["Cache-Control"] == "no-cache"
    # not available: no ETag, the caller sets no-store
    gone, hg = run(reads(meta={})[0].view_conditional("m", BBox(0, 0, 1, 1), if_none_match="*"))
    assert gone["available"] is False and hg == {}


def test_epoch_pin_409_on_view_raster_and_lineage_carries_the_current_epoch():
    state = {"epoch": 1000}
    r = sgr.SceneGraphReads(lambda m: {"version": 1, "params": {"lattice": 0.1}}, lambda *a: FakeGraph([]),
                            lambda m, v: {}, epoch=lambda m, v: state["epoch"])
    b = BBox(0, 0, 1, 1)
    tag = run(r.view("m", b))["epoch"]
    assert run(r.view("m", b, epoch=tag))["available"]
    assert run(r.raster("m", b, epoch=tag))[0] is not None
    assert run(r.lineage("m", 0, epoch=tag))["epoch"] == tag
    state["epoch"] = 2000                                       # recreated under the same id, back at version 1
    new = sgr._tag(2000)
    for call in (lambda: r.view("m", b, epoch=tag), lambda: r.raster("m", b, epoch=tag),
                 lambda: r.lineage("m", 0, epoch=tag)):
        with pytest.raises(HTTPException) as e:
            run(call())
        assert e.value.status_code == 409
        assert e.value.detail == {"error": "epoch_changed", "epoch": new, "current_epoch": new, "version": 1}
        assert e.value.headers["X-Scene-Graph-Epoch"] == str(new)
    assert run(r.view("m", b, epoch=new))["epoch"] == new       # the restart works
    # a pin against a map that has no graph at all is a mismatch too (epoch null)
    with pytest.raises(HTTPException) as e:
        run(reads(meta={})[0].view("m", b, epoch=5))
    assert e.value.status_code == 409 and e.value.detail["epoch"] is None


def test_transient_store_errors_are_503_with_retry_after_never_no_graph():
    def boom(*a, **k):
        raise ConnectionError("arango down")
    b = BBox(0, 0, 1, 1)
    cases = {
        "meta": sgr.SceneGraphReads(boom, lambda *a: None),
        "load": sgr.SceneGraphReads(lambda m: {"version": 1}, boom),
        "epoch": sgr.SceneGraphReads(lambda m: {"version": 1}, lambda *a: FakeGraph([]), epoch=boom),
    }
    for name, r in cases.items():
        calls = [lambda r=r: r.view("m", b), lambda r=r: r.raster("m", b), lambda r=r: r.owner_plane("m", b)]
        if name != "load":                      # lineage needs no graph load
            calls.append(lambda r=r: r.lineage("m", 0))
        for call in calls:
            with pytest.raises(HTTPException) as e:
                run(call())
            assert e.value.status_code == 503, name
            assert e.value.headers["Retry-After"] == str(sgr.RETRY_AFTER_S)
    lin = sgr.SceneGraphReads(lambda m: {"version": 5}, lambda *a: None, boom)
    with pytest.raises(HTTPException) as e:
        run(lin.lineage("m", 1))
    assert e.value.status_code == 503
    # a failure is not cached: the store comes back and the same reads answer
    flaky = {"down": True}

    def meta(m):
        if flaky["down"]:
            raise TimeoutError("slow")
        return {"version": 1, "params": {"lattice": 0.1}}
    r = sgr.SceneGraphReads(meta, lambda *a: FakeGraph([]))
    with pytest.raises(HTTPException):
        run(r.view("m", b))
    flaky["down"] = False
    assert run(r.view("m", b))["available"]
    # a genuine absence is still the contract, and not retained / unknown stay 404
    assert run(reads(meta={})[0].view("m", b))["available"] is False


def test_parents_false_reaches_the_query_only_for_readers_that_ask_and_has_its_own_cache_key():
    seen = []

    class G:
        def query(self, bbox, level, raster, parents=True):
            seen.append(parents)
            return FakeGraph([]).query(bbox, level, raster)
    r = sgr.SceneGraphReads(lambda m: {"version": 3, "params": {"lattice": 0.1}}, lambda *a: G())
    b = BBox(0, 0, 1, 1)
    run(r.view("m", b))
    run(r.view("m", b, parents=False))
    run(r.view("m", b))                                    # cached
    plane = run(r.owner_plane("m", b))                    # parents=False, cached from above
    assert seen == [True, False]
    assert plane["keys"] == ["a:1", "b:1"] and plane["shape"] == (2, 2) and plane["version"] == 3
    assert plane["index"].dtype == np.uint16 and plane["bbox"] == [0.0, 0.0, 6.4, 6.4]
    assert run(reads(meta={})[0].owner_plane("m", b)) is None


def test_owner_plane_area_cap_is_a_distinguishable_422_and_a_bad_plane_is_refused():
    r, _ = reads(max_cells=100)
    with pytest.raises(sgr.AreaTooLarge) as e:
        run(r.owner_plane("m", BBox(0, 0, 50, 50)))
    assert e.value.status_code == 422

    class Bad(FakeGraph):
        def query(self, bbox, level, raster, parents=True):
            res = super().query(bbox, level, raster)
            res.raster.index = np.array([[0, 9]], dtype=np.uint16)     # names key 9 of 2
            return res
    bad = sgr.SceneGraphReads(lambda m: {"version": 3}, lambda *a: Bad([]))
    with pytest.raises(HTTPException) as e:
        run(bad.raster("m", BBox(0, 0, 1, 1)))
    assert e.value.status_code == 503
    assert run(bad.raster("m", BBox(0, 0, 1, 1), plane="dependent"))[0] is not None
