"""Read-path hardening of SceneGraphStore: page cache, per-view id lut, single-flight tiles, byte-budget tile cache, leases.
Stores here are synthesised straight into a MemoryBackend (pages, tiles, manifests) so they can be large and cheap."""
import threading
import time

import numpy as np
import pytest

from packages.scene_graph import store as S
from packages.scene_graph.store import MemoryBackend, SceneGraphStore
from packages.scene_graph.store_codec import canonical_json, pack, pack_json

pytestmark = pytest.mark.unit
PARAMS = {"robot_half_side": 0.3, "lattice": 0.1, "chart_radius": 3.0}


def _chart(cid, sx, sy, k, area=1.0):
    ax, ay = (sx * S.SUPER * S.TILE_F + 5 + k) * S.RES, (sy * S.SUPER * S.TILE_F + 5) * S.RES
    return {"key": "m:%d" % cid, "id": cid, "peak": 1.0, "region": 0, "anchor": [ax, ay], "cell": [0, 0], "cells": 10,
            "area": area, "clearance": 0.5, "driven": 0.0, "ever_blocked": 0.0, "leaf": None}


def build(be, map_id, P, per_page, versions=1, changed=None):
    """P x P pages, one tile each (own ids of that page's charts), `per_page` charts per page; version v >= 2 changes the
    area of one chart of page (0,0). Returns {version: {tile hash}}"""
    ids = {}
    gens = {"1": 1}
    for v in range(1, versions + 1):
        pages, tiles, charts = {}, {}, []
        for sx in range(P):
            for sy in range(P):
                cs = [_chart(1 + (sx * P + sy) * per_page + k, sx, sy, k, 1.0 + (v - 1 if (sx, sy, k) == (0, 0, 0) else 0))
                      for k in range(per_page)]
                own = np.zeros((S.TILE_L, S.TILE_L), np.int32)
                for k, c in enumerate(cs): own[:, k::per_page] = c["id"]
                arrs = {"v": np.full((S.TILE_F, S.TILE_F), 255, np.uint8), "t": np.full((S.TILE_F, S.TILE_F), -1, np.int64),
                        "b": np.full((S.TILE_F, S.TILE_F), -1, np.int64), "own": own}
                th, blob = pack(arrs, {})
                be.put(map_id, "g1/t/" + th, blob)
                name = "%d,%d" % (sx * S.SUPER, sy * S.SUPER)
                ph, pblob = pack_json({"tiles": {name: th}, "charts": cs, "links": []})
                be.put(map_id, "g1/p/" + ph, pblob)
                pages["%d,%d" % (sx, sy)] = ph; tiles[name] = th
                charts += cs
        body = {"format": S.FORMAT, "pipeline": S.PIPELINE, "generation": 1, "params": PARAMS, "params_hash": "x",
                "cursor": None, "watermark": [0, ""], "engine_version": v, "canvas": [0, 0, 0, 0], "tile_cells": S.TILE_F,
                "channels": [], "next_id": 10 ** 9, "pages": pages, "order": [c["id"] for c in charts],
                "chart_count": len(charts), "tile_count": len(tiles), "segments": [], "leaf_count": 0, "retired": {},
                "version": v, "created_ms": 0, "content_hash": str(v)}
        be.put(map_id, "g1/manifest-%d.json" % v, canonical_json(body))
        ids[v] = set(tiles.values())
        cur = be.meta(map_id)
        assert be.cas_meta(map_id, cur["version"] if cur else None,
                           {"version": v, "generation": 1, "params": PARAMS, "params_hash": "x", "cursor": None,
                            "watermark": [0, ""], "manifest": "g1/manifest-%d.json" % v, "generations": gens,
                            "created_ms": 0, "epoch": "e"})
    return ids


def _fresh(be, **kw):
    return SceneGraphStore(be, be, **kw)


class Counting(MemoryBackend):
    def __init__(self):
        super().__init__(); self.gets = {}; self.gate = None; self.fail_names = set()

    def get(self, map_id, key):
        self.gets[key] = self.gets.get(key, 0) + 1
        if self.gate is not None and "/t/" in key: self.gate.wait(5)
        if key in self.fail_names: raise IOError("injected " + key)
        return super().get(map_id, key)


# ---------------------------------------------------------------- 1. page cache
def test_page_cache_reparses_only_changed_pages_and_results_are_identical():
    be = Counting(); build(be, "m", 6, 3, versions=2)
    st = _fresh(be)
    m1 = st.manifest("m", 1)
    pages_read = sum(n for k, n in be.gets.items() if "/p/" in k)
    assert pages_read == 36
    m2 = st.manifest("m", 2)
    assert sum(n for k, n in be.gets.items() if "/p/" in k) == 36 + 1      # one changed page
    cold = _fresh(be, page_cache_size=1).manifest("m", 2)                   # a thrashing cache: same result
    assert {k: m2[k] for k in ("tiles", "charts", "links", "key_ids")} == {k: cold[k] for k in ("tiles", "charts", "links", "key_ids")}
    assert m1["charts"][0]["area"] == 1.0 and m2["charts"][0]["area"] == 2.0


def test_page_cache_is_bounded_and_dropped_by_gc():
    be = Counting(); build(be, "m", 4, 2, versions=3)
    st = _fresh(be, page_cache_size=5, keep_last=1)
    for v in (1, 2, 3): st.manifest("m", v)
    assert len(st._pages) == 5
    st2 = _fresh(be, keep_last=1)
    st2.manifest("m", 1); st2.gc("m")
    assert all(k[1] in set(be.list("m")) for k in st2._pages)


# ---------------------------------------------------------------- 2. lut once per view
def test_window_lut_is_built_once_per_view_and_snap_is_unchanged():
    be = MemoryBackend(); build(be, "m", 3, 4)
    v = _fresh(be).view("m", 1)
    idx1, _ = v._window(0, 0, 64, 64)
    lut = v._lut
    idx2, _ = v._window(0, 0, 64, 64)
    assert v._lut is lut and (idx1 == idx2).all()
    expect = {c["id"]: j for j, c in enumerate(v.man["charts"])}
    assert all(lut[i] == j for i, j in expect.items()) and (lut >= 0).sum() == len(expect)
    assert set(np.unique(idx1[idx1 >= 0]).tolist()) == {0, 1, 2, 3}
    s = v.snap(0.4, 0.4)
    assert s.key is not None and s.distance < 1.0


# ---------------------------------------------------------------- 3. single flight tiles
def test_concurrent_readers_fetch_a_tile_once_and_failures_propagate_uncached():
    be = Counting(); build(be, "m", 1, 2)
    st = _fresh(be)
    man = st.manifest("m")
    h = man["tiles"]["0,0"]; name = "g1/t/" + h
    be.gate = threading.Event()
    out, errs = [], []

    def go():
        try: out.append(st._tile("m", man, 0, 0))
        except Exception as e: errs.append(e)       # noqa: BLE001
    ts = [threading.Thread(target=go) for _ in range(10)]
    [t.start() for t in ts]
    while be.gets.get(name, 0) < 1: time.sleep(0.001)
    time.sleep(0.05); be.gate.set(); [t.join() for t in ts]
    assert be.gets[name] == 1 and len(out) == 10 and not errs and all(o is out[0] for o in out)

    # failure: every waiter sees it, nothing is cached, the next call retries and succeeds
    st = _fresh(be); man = st.manifest("m")
    be.gets.clear(); be.fail_names = {name}; be.gate = threading.Event(); out.clear()
    ts = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in ts]
    while be.gets.get(name, 0) < 1: time.sleep(0.001)
    time.sleep(0.05); be.gate.set(); [t.join() for t in ts]
    assert len(errs) == 6 and not out and be.gets[name] == 1 and not st._tile_flight and not st._tiles
    be.fail_names = set(); be.gate = None
    assert st._tile("m", man, 0, 0) is not None and be.gets[name] == 2


# ---------------------------------------------------------------- 4. byte budget
def test_tile_cache_is_lru_by_bytes_and_read_only():
    be = Counting(); build(be, "m", 3, 2)
    one = sum(a.nbytes for a in S.unpack(be.get("m", "g1/t/" + next(iter(_fresh(be).manifest("m")["tiles"].values()))))[0].values())
    st = _fresh(be, tile_cache_bytes=int(2.5 * one))
    man = st.manifest("m"); names = sorted(man["tiles"]); be.gets.clear()
    xy = [tuple(int(v) for v in n.split(",")) for n in names]
    ref = [st._tile("m", man, *c) for c in xy[:3]]
    assert len(st._tiles) == 2 and st._tile_bytes <= st.tile_cache_bytes       # oldest evicted
    before = dict(be.gets)
    st._tile("m", man, *xy[2])                                                 # hit
    assert be.gets == before
    st._tile("m", man, *xy[0])                                                 # evicted: refetched, identical
    assert be.gets["g1/t/" + man["tiles"][names[0]]] == 2
    again = st._tile("m", man, *xy[0])
    assert all((again[k] == ref[0][k]).all() for k in again)
    with pytest.raises(ValueError):
        again["own"][0, 0] = 5
    off = _fresh(be, tile_cache_bytes=0)
    off._tile("m", man, *xy[0]); assert not off._tiles and off._tile_bytes == 0


def test_load_style_reads_do_not_fill_the_cache_and_views_share_tiles():
    be = Counting(); build(be, "m", 2, 2)
    st = _fresh(be); man = st.manifest("m")
    with st._no_fill(): st._tile("m", man, 0, 0)
    assert not st._tiles
    a, b = st.view("m", 1), st.view("m", 1)
    assert a._own(0, 0) is b._own(0, 0)


# ---------------------------------------------------------------- 5. leases
def test_leased_version_survives_gc_and_is_reclaimed_after_release():
    be = MemoryBackend(); build(be, "m", 2, 2, versions=3)
    st = _fresh(be, keep_last=1)
    with st.lease("m", 1):
        with st.lease("m", 1):                          # re-entrant: counted
            pass
        res = st.gc("m")
        assert res["kept_versions"] == [1, 3]
        assert st.view("m", 1).query(S.BBox(-1e3, -1e3, 1e3, 1e3), raster=False).charts
        with pytest.raises(FileNotFoundError):          # v2 was not leased: collected
            st.view("m", 2)
    assert not st._leases
    st.gc("m")
    with pytest.raises(FileNotFoundError):
        st.view("m", 1)
    assert st.view("m", 3).version == 3


def test_gc_without_leases_is_as_before_and_explicit_leased_still_works():
    be = MemoryBackend(); build(be, "m", 2, 2, versions=3)
    st = _fresh(be, keep_last=2)
    assert st.gc("m", leased=[1])["kept_versions"] == [1, 2, 3]
    assert st.gc("m")["kept_versions"] == [2, 3]
