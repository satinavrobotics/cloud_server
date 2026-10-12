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

    def query(self, bbox, level, raster):
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
    assert run(r.lineage("m", 2)) == {"available": True, "from_version": 2, "version": 5,
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


def test_raster_too_large_is_422_but_json_view_is_not_cell_limited():
    r, _ = reads(max_cells=100)
    with pytest.raises(HTTPException) as e:
        run(r.view("m", BBox(0, 0, 100, 100), raster=True))
    assert e.value.status_code == 422
    assert run(r.view("m", BBox(0, 0, 100, 100)))["available"] is True


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
    class TooOld(Exception):
        pass

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
