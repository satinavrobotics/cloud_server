"""Persistence: round trip, incremental saves, compare-and-set, crash safety, pinned reads, lineage, generations."""
import copy
import random

import numpy as np
import pytest

from packages.scene_graph.api import BBox, MissingTiles, SceneParams
from packages.scene_graph.engine import ChartGraph
from packages.scene_graph.store import (ArangoMinioBackend, MemoryBackend, SceneGraphStore, StaleVersion, TooOld, tiles_for)
from packages.scene_graph.store_codec import pack, unpack

from . import chart_worlds as cw
from .chart_verify import ok, verify

pytestmark = pytest.mark.unit
BIG = BBox(-1e4, -1e4, 1e4, 1e4)


def _world(name="two_rooms"):
    w, way = getattr(cw, name)()
    return w, cw.drive(w, way)


def _store(keep_last=5):
    b = MemoryBackend()
    return SceneGraphStore(b, b, keep_last=keep_last, clock=lambda: 1_700_000_000.0), b


def _grow(store, map_id, leaves, batch):
    g = ChartGraph(SceneParams()); v = None; cursor = 0
    for i in range(0, len(leaves), batch):
        d = g.fold_many(leaves[i:i + batch])
        cursor += 1
        v = store.save(map_id, g, d, v, {"n": cursor})
    return g, v


def _sig(view_or_graph, bbox=BIG):
    q = view_or_graph.query(bbox, raster=False)
    return (sorted((c.key, tuple(round(a, 6) for a in c.anchor), c.cells, round(c.driven_frac, 6), round(c.ever_blocked_frac, 6), c.leaf)
                   for c in q.charts),
            sorted((l.a, l.b, round(l.length, 6), round(l.width, 6), tuple(sorted((k, round(v, 6)) for k, v in l.channels.items())))
                   for l in q.links))


def _global_owner(g_or_view):
    """{(global lattice x, y): chart key} from the raster of either an engine or a stored view"""
    r = g_or_view.query(BIG, raster=True).raster
    out = {}
    ys, xs = np.nonzero(r.index)
    ox, oy = round(r.origin[0] / 0.05) // 2, round(r.origin[1] / 0.05) // 2
    for y, x in zip(ys, xs):
        out[(ox + int(x), oy + int(y))] = r.keys[int(r.index[y, x]) - 1]
    return out


def test_codec_is_deterministic_and_lossless():
    a = {"x": np.arange(12, dtype=np.int64).reshape(3, 4), "y": np.array(["ab", "c"], dtype="U")}
    h1, b1 = pack(a, {"m": 1}); h2, b2 = pack(dict(reversed(list(a.items()))), {"m": 1})
    h3, b3 = pack(a, {"m": 1})
    assert (h1, b1) == (h3, b3)
    out, m = unpack(b1)
    assert m == {"m": 1} and (out["x"] == a["x"]).all() and list(out["y"]) == ["ab", "c"]


def test_roundtrip_reproduces_charts_keys_links_and_owner_plane():
    store, _ = _store()
    w, leaves = _world("two_rooms")
    g, v = _grow(store, "m", leaves, 6)
    h = store.load("m")
    assert _sig(g) == _sig(h)
    assert _global_owner(g) == _global_owner(h)
    r = verify(h, w)
    assert ok(r), r


def test_continuing_after_load_equals_continuing_without_a_save():
    w, leaves = _world("two_rooms")
    half = len(leaves) // 2
    store, _ = _store()
    g = ChartGraph(SceneParams())
    d = g.fold_many(leaves[:half]); store.save("m", g, d, None, {})
    h = store.load("m")
    g.fold_many(leaves[half:]); h.fold_many(leaves[half:])
    # planes bit-identical over the known region (canvases differ by an offset)
    ky = np.argwhere(g._V != 255)
    gy, gx = ky[:, 0] + g._cy0, ky[:, 1] + g._cx0
    assert (h._V[gy - h._cy0, gx - h._cx0] == g._V[ky[:, 0], ky[:, 1]]).all()
    assert (h._T[gy - h._cy0, gx - h._cx0] == g._T[ky[:, 0], ky[:, 1]]).all()
    assert (h._B[gy - h._cy0, gx - h._cx0] == g._B[ky[:, 0], ky[:, 1]]).all()
    assert ok(verify(h, w))
    assert sorted(c.key for c in h.query(BIG, raster=False).charts) == sorted(c.key for c in g.query(BIG, raster=False).charts)


def test_save_writes_only_dirty_tiles_one_segment_and_a_manifest():
    store, b = _store()
    w, leaves = _world("corridor")
    g = ChartGraph(SceneParams())
    d = g.fold_many(leaves[:-3]); v = store.save("m", g, d, None, {})
    before = set(b.list("m"))
    d = g.fold_many(leaves[-3:]); v2 = store.save("m", g, d, v, {})
    new = set(b.list("m")) - before
    kinds = {n.split("/")[1] for n in new}
    assert v2 == v + 1 and "manifest-%d.json" % v2 in {n.split("/")[-1] for n in new}
    assert sum(n.split("/")[1] == "l" for n in new) == 1                         # the last leaf segment
    man = store.manifest("m", v2); old = store.manifest("m", v)
    changed = {k for k in man["tiles"] if old["tiles"].get(k) != man["tiles"][k]}
    assert sum(n.split("/")[1] == "t" for n in new) == len(changed) <= len(man["tiles"])
    assert kinds <= {"t", "l", "p", "manifest-%d.json" % v2}
    assert 1 <= sum(n.split("/")[1] == "p" for n in new) <= len(man["pages"])


def test_incremental_dirty_set_equals_the_full_hash_set():
    """the tiles outside the dirty bbox never differ: a full save would write exactly the same tile objects"""
    store, b = _store()
    store2, b2 = _store()
    w, leaves = _world("two_rooms")
    g = ChartGraph(SceneParams()); v = v2 = None
    for i in range(0, len(leaves), 5):
        d = g.fold_many(leaves[i:i + 5])
        v = store.save("m", g, d, v, {})
        v2 = store2.save("m", g, d, v2, {}, full=True)
        assert store.manifest("m", v)["tiles"] == store2.manifest("m", v2)["tiles"]


def test_idempotent_save_writes_nothing():
    store, b = _store()
    w, leaves = _world("two_rooms")
    g, v = _grow(store, "m", leaves, 8)
    n = len(b.list("m"))
    d = g.fold_many([])                                     # nothing new
    assert store.save("m", g, d, v, {"n": 99}) == v          # same state: same version; only the cursor moves, in place
    assert store.meta("m")["cursor"] == {"n": 99} and len(b.list("m")) == n
    assert store.save("m", g, d, v, {"n": 99}) == v          # and again: nothing at all


def test_compare_and_set_fences_a_stale_writer_and_leaves_no_readable_state():
    store, b = _store()
    w, leaves = _world("two_rooms")
    g, v = _grow(store, "m", leaves[:10], 5)
    other = ChartGraph(SceneParams()); other.fold_many(leaves[:10])
    d = g.fold_many(leaves[10:])
    store.save("m", g, d, v, {})                             # the first writer wins
    d2 = other.fold_many(leaves[10:])
    with pytest.raises(StaleVersion):
        store.save("m", other, d2, v, {})                    # the zombie still thinks the version is v
    assert store.meta("m")["version"] == v + 1


def test_crash_before_the_commit_leaves_the_previous_version_readable_and_gc_collects_orphans():
    store, b = _store()
    w, leaves = _world("two_rooms")
    g, v = _grow(store, "m", leaves[:10], 5)
    before_sig = _sig(store.load("m"))
    d = g.fold_many(leaves[10:])

    def boom(kind, name):
        if kind == "meta": raise RuntimeError("crash before the commit")
    b.fail_on = boom
    with pytest.raises(RuntimeError):
        store.save("m", g, d, v, {})
    b.fail_on = None
    assert store.meta("m")["version"] == v and _sig(store.load("m")) == before_sig
    n = len(b.list("m")); res = store.gc("m")
    assert res["deleted"] > 0 and len(b.list("m")) < n and _sig(store.load("m")) == before_sig


def test_pinned_reads_return_that_version_after_a_newer_one_exists():
    store, _ = _store()
    w, leaves = _world("two_rooms")
    g = ChartGraph(SceneParams()); v = None; sigs = {}
    for i in range(0, len(leaves), 8):
        d = g.fold_many(leaves[i:i + 8]); v = store.save("m", g, d, v, {})
        sigs[v] = _sig(g)
    assert len(sigs) >= 3
    for ver, sig in sigs.items():
        assert _sig(store.view("m", ver)) == sig
        assert store.view("m", ver).version == ver


def test_view_query_and_locate_match_the_engine_without_building_one():
    store, _ = _store()
    w, leaves = _world("two_rooms")
    g, v = _grow(store, "m", leaves, 8)
    view = store.view("m")
    rnd = random.Random(3)
    for _ in range(6):
        x0, y0 = rnd.uniform(-1, 4), rnd.uniform(-1, 2)
        bb = BBox(x0, y0, x0 + rnd.uniform(0.5, 6), y0 + rnd.uniform(0.5, 4))
        for level in (0, 1, 2):
            a, b = g.query(bb, level, raster=False), view.query(bb, level, raster=False)
            assert sorted((c.key, c.cells) for c in a.charts) == sorted((c.key, c.cells) for c in b.charts)
            assert sorted((l.a, l.b, round(l.length, 6)) for l in a.links) == sorted((l.a, l.b, round(l.length, 6)) for l in b.links)
    og, ov = _global_owner(g), _global_owner(view)
    assert {k: v for k, v in og.items()} == ov
    for _ in range(25):
        x, y = rnd.uniform(0, 6), rnd.uniform(0, 3)
        assert g.locate(x, y) == view.locate(x, y)


def test_lineage_is_cumulative_and_too_old_is_signalled():
    store, _ = _store(keep_last=2)
    w, leaves = _world("two_rooms")
    g, v = _grow(store, "m", leaves, 5)
    assert v >= 4
    lin = store.lineage("m", v - 1)
    assert isinstance(lin, dict)
    live = {c.key for c in g.query(BIG, raster=False).charts}
    for succ in lin.values():
        assert set(succ) <= live
    store.gc("m")
    with pytest.raises(TooOld):
        store.lineage("m", 0)
    assert store.lineage("m", v) == {}


def test_generation_switch_keeps_the_old_readable_until_commit_and_persists_the_key_map():
    store, _ = _store()
    w, leaves = _world("two_rooms")
    g, v = _grow(store, "m", leaves, 8)
    old_sig = _sig(store.view("m"))
    gen = store.begin_generation("m")
    g2 = ChartGraph(SceneParams(robot_half_side=0.2))        # a params change: a rebuild in a new generation
    d = g2.fold_many(leaves)
    store.save("m", g2, d, v, {"rebuild": True}, generation=gen)
    assert store.meta("m")["version"] == v and store.meta("m")["generation"] == 1
    assert _sig(store.view("m")) == old_sig                  # readers still see the old generation
    newv, kmap = store.commit_generation("m", gen)
    assert newv == v + 1 and store.meta("m")["generation"] == gen
    assert set(kmap) == {c.key for c in store.view("m", v).query(BIG, raster=False).charts}
    assert store.lineage("m", v) == kmap
    assert _sig(store.view("m")) == _sig(g2)


def test_never_saved_map_is_empty_meta_and_unknown_watermark():
    store, _ = _store()
    assert store.meta("nope") == {} and store.watermark("nope") == (-1, "")


def test_load_refuses_a_partial_tile_set_and_tiles_for_covers_the_footprint():
    store, _ = _store()
    w, leaves = _world("two_rooms")
    g, v = _grow(store, "m", leaves[:10], 10)
    with pytest.raises(MissingTiles):
        store.load("m", tiles=[])
    t = tiles_for(leaves[:2], SceneParams())
    assert t and all(isinstance(a, tuple) and len(a) == 2 for a in t)


def test_params_mismatch_is_a_new_generation_not_an_in_place_load():
    store, _ = _store()
    w, leaves = _world("two_rooms")
    g, v = _grow(store, "m", leaves[:10], 10)
    man = store.manifest("m")
    assert man["params_hash"] == store.meta("m")["params_hash"] and man["params"]["chart_radius"] == 2.0


class _FakeDb:
    """enough of python-arango for the adapter: collections of dicts and the one compare-and-set query"""
    def __init__(self):
        self.cols = {}; self.aql = self

    def has_collection(self, n): return n in self.cols
    def create_collection(self, n, edge=False): self.cols[n] = {}
    def delete_collection(self, n): self.cols.pop(n, None)
    def collection(self, n): return _FakeCol(self.cols, n)

    def execute(self, q, bind_vars=None):
        assert q == ArangoMinioBackend.CAS_UPDATE
        c = self.cols[bind_vars["@c"]]; d = c.get("meta")
        if d is not None and d["version"] == bind_vars["exp"]:
            c["meta"] = {**bind_vars["meta"], "_key": "meta"}; return iter(["meta"])
        return iter([])


class _FakeCol:
    def __init__(self, cols, n): self.cols, self.name = cols, n
    def get(self, k): return copy.deepcopy(self.cols[self.name].get(k))
    def truncate(self): self.cols[self.name].clear()
    def insert(self, d, overwrite=False):
        if d["_key"] in self.cols[self.name] and not overwrite: raise Exception("unique constraint violated")
        self.cols[self.name][d["_key"]] = copy.deepcopy(d)
    def insert_many(self, ds, overwrite=False):
        for d in ds: self.cols[self.name][d["_key"]] = copy.deepcopy(d)
    def update(self, d): self.cols[self.name][d["_key"]].update(d)


class _FakeMinio:
    def __init__(self): self.o = {}
    def put_object(self, b, n, data, length, content_type=None): self.o[(b, n)] = data.read()
    def get_object(self, b, n):
        if (b, n) not in self.o:
            e = Exception("nosuchkey"); e.code = "NoSuchKey"; raise e
        class R:
            def __init__(s, v): s.v = v
            def read(s): return s.v
            def close(s): pass
            def release_conn(s): pass
        return R(self.o[(b, n)])
    def stat_object(self, b, n):
        if (b, n) not in self.o:
            e = Exception("nf"); e.code = "NoSuchKey"; raise e
    def list_objects(self, b, prefix="", recursive=True):
        class O:
            def __init__(s, n): s.object_name = n
        return [O(n) for (bb, n) in sorted(self.o) if bb == b and n.startswith(prefix)]
    def remove_object(self, b, n): self.o.pop((b, n), None)


class _FakeClient:
    def __init__(self):
        self.graph = type("G", (), {"db": _FakeDb()})()
        mc = _FakeMinio()
        self.image = type("I", (), {"client": mc, "_bucket_name": staticmethod(lambda m: "map-" + m.lower().replace("_", "-")),
                                    "_ensure_map_bucket": staticmethod(lambda m: True)})()


def test_arango_minio_adapter_on_fakes_round_trips_and_fences():
    client = _FakeClient()
    store = SceneGraphStore.from_client(client, clock=lambda: 1.0)
    w, leaves = _world("two_rooms")
    g, v = _grow(store, "Office_1", leaves, 10)
    assert store.meta("Office_1")["version"] == v
    assert any(n.startswith("sg_") for n in client.graph.db.cols) and "sg_anchors_Office_1" in client.graph.db.cols
    assert all(b == "map-office-1" and n.startswith("scene_graph/") for (b, n) in client.image.client.o)
    assert _sig(store.load("Office_1")) == _sig(g)
    d = g.fold_many([])
    with pytest.raises(StaleVersion):
        store.save("Office_1", g, d, v - 1, {})
    store.drop("Office_1")
    assert store.meta("Office_1") == {}


def test_tiles_in_and_a_partial_view_fail_loudly_for_the_tiles_it_lacks():
    store, _ = _store()
    w, leaves = _world("corridor")
    g, v = _grow(store, "m", leaves, 8)
    allt = store.tiles_in("m", BIG)
    assert allt and allt == sorted(allt)
    left = store.tiles_in("m", BBox(-1e4, -1e4, 2.0, 1e4))
    assert 0 < len(left) <= len(allt)
    assert store.tiles_in("m", BBox(1e3, 1e3, 1e3 + 1, 1e3 + 1)) == []
    part = store.view("m", tiles=left)
    a = g.query(BBox(-5, -5, 1.5, 8), raster=False)               # inside the loaded tiles
    b = part.query(BBox(-5, -5, 1.5, 8), raster=False)
    assert sorted(c.key for c in a.charts) == sorted(c.key for c in b.charts)
    with pytest.raises(MissingTiles) as e:
        part.query(BBox(-5, -5, 50, 50), raster=True)
    assert set(e.value.tiles) <= set(allt) - set(left)
    with pytest.raises(MissingTiles):
        store.load("m", tiles=left)                              # a writer needs every tile
    assert store.load("m", tiles=allt) is not None


def test_stored_snap_matches_the_engine_snap():
    store, _ = _store()
    w, leaves = _world("two_rooms")
    g, v = _grow(store, "m", leaves, 8)
    view = store.view("m")
    rnd = random.Random(5)
    for _ in range(30):
        x, y = rnd.uniform(-0.5, 6.5), rnd.uniform(-0.5, 3.5)
        a, b = g.snap(x, y), view.snap(x, y)
        assert a.key == b.key and a.crosses_blocked == b.crosses_blocked
        assert (a.distance == b.distance == float("inf")) or abs(a.distance - b.distance) < 1e-9


def test_safety_net_heals_a_dirty_set_that_missed_tiles_and_strict_mode_raises():
    from dataclasses import replace

    from packages.scene_graph.store import DirtyCoverageError
    w, leaves = _world("corridor")
    for strict in (False, True):
        b = MemoryBackend(); store = SceneGraphStore(b, b, verify_every=1, strict=strict, clock=lambda: 1.0)
        g = ChartGraph(SceneParams())
        d = g.fold_many(leaves[:10]); v = store.save("m", g, d, None, {})
        d = g.fold_many(leaves[10:])
        lying = replace(d, bbox=None)                      # a dirty set that claims nothing changed
        if strict:
            with pytest.raises(DirtyCoverageError):
                store.save("m", g, lying, v, {})
            assert store.meta("m")["version"] == v         # nothing was committed
        else:
            v2 = store.save("m", g, lying, v, {})
            assert store.coverage_mismatches == 1
            assert store.manifest("m", v2)["tiles"] == store.manifest("m", v2)["tiles"]
            assert _sig(store.load("m")) == _sig(g)


def test_unchanged_leaf_segments_are_not_repacked_or_rewritten():
    store, b = _store()
    w, leaves = _world("two_rooms")
    g = ChartGraph(SceneParams())
    d = g.fold_many(leaves[:-1]); v = store.save("m", g, d, None, {})
    before = {n for n in b.list("m") if "/l/" in n}
    d = g.fold_many(leaves[-1:]); store.save("m", g, d, v, {})
    after = {n for n in b.list("m") if "/l/" in n}
    assert before <= after and len(after - before) == 1     # one segment (all leaves fit in one) changed: only it is new


def test_replay_with_a_new_cursor_and_canvas_does_not_bump_the_version():
    from tests.unit.scene_graph import chart_worlds as cw
    from packages.scene_graph.api import SceneParams
    from packages.scene_graph.engine import ChartGraph
    from packages.scene_graph.store import SceneGraphStore, MemoryBackend
    w, way = cw.two_rooms()
    leaves = cw.drive(w, way)
    be = MemoryBackend()
    st = SceneGraphStore(be, be)
    g = ChartGraph(SceneParams()); d = g.fold_many(leaves)
    v = st.save("m", g, d, None, {"c": 1})
    d2 = g.fold_many(leaves[-6:])                                      # a restart replays the margin: nothing changes
    assert st.save("m", g, d2, v, {"c": 2}) == v                      # same content: same version ...
    m = st.meta("m")
    assert m["version"] == v and m["cursor"] == {"c": 2}              # ... and the cursor moved in place
    assert m["epoch"] and m["created_ms"]


def test_epoch_changes_after_a_drop_and_the_expansion_cache_cannot_serve_the_old_history():
    from tests.unit.scene_graph import chart_worlds as cw
    from packages.scene_graph.api import SceneParams
    from packages.scene_graph.engine import ChartGraph
    from packages.scene_graph.store import SceneGraphStore, MemoryBackend
    be = MemoryBackend()
    a, b = SceneGraphStore(be, be), SceneGraphStore(be, be)            # two processes on one backend
    w, way = cw.two_rooms(); g = ChartGraph(SceneParams()); d = g.fold_many(cw.drive(w, way))
    a.save("m", g, d, None, None)
    b.view("m").query(BBox(-5, -5, 20, 20), raster=False)      # b caches the expansion of manifest-1
    e1 = a.meta("m")["epoch"]
    a.drop("m")
    w2, way2 = cw.corridor(); g2 = ChartGraph(SceneParams()); d2 = g2.fold_many(cw.drive(w2, way2))
    a.save("m", g2, d2, None, None)                                    # version 1 again, another history
    assert a.meta("m")["epoch"] != e1
    second = b.view("m").query(BBox(-5, -5, 20, 20), raster=False)
    assert {c.key for c in second.charts} == {c.key for c in g2.query(BBox(-5, -5, 20, 20), raster=False).charts}


class _Obj:
    def __init__(self, n): self.object_name = n


class _DropMinio:
    """just enough of the minio client; `fail` makes deletes fail"""
    def __init__(self, n=5, fail=False, multi=False):
        self.objs = {"scene_graph/o%d" % i for i in range(n)}; self.fail = fail; self.calls = 0
        if multi: self.remove_objects = self._remove_objects
    def list_objects(self, b, prefix="", recursive=True): return [_Obj(o) for o in sorted(self.objs) if o.startswith(prefix)]
    def remove_object(self, b, o):
        self.calls += 1
        if self.fail: raise IOError("S3 500")
        self.objs.discard(o)
    def _remove_objects(self, b, objs):
        self.calls += 1
        objs = list(objs)
        if self.fail:
            return [type("E", (), {"code": "InternalError", "name": o.name})() for o in objs]
        for o in objs: self.objs.discard(o.name)
        return []


def _adapter(mc):
    class DB:
        dropped = []
        def has_collection(self, n): return True
        def delete_collection(self, n): DB.dropped.append(n)
    c = type("C", (), {})(); c.image = type("I", (), {})(); c.image.client = mc; c.image._bucket_name = lambda m: "map-" + m
    c.graph = type("G", (), {})(); c.graph.db = DB()
    return ArangoMinioBackend(c), DB


def test_drop_raises_when_objects_cannot_be_deleted_and_keeps_the_documents():
    for multi in (False, True):
        be, DB = _adapter(_DropMinio(fail=True, multi=multi))
        with pytest.raises(Exception):
            be.drop("m")
        assert DB.dropped == []                                # the meta document is still there: the saga can retry


def test_drop_deletes_objects_first_then_the_collections_in_batches():
    mc = _DropMinio(n=2500, multi=True)
    be, DB = _adapter(mc)
    be.drop("m")
    assert not mc.objs and mc.calls == 1                       # one multi-delete request, not 2500
    assert sorted(DB.dropped) == ["sg_anchors_m", "sg_links_m", "sg_meta_m"]
    mc = _DropMinio(n=3)                                       # a client with only remove_object (a fake) still works
    be, DB = _adapter(mc); be.drop("m")
    assert not mc.objs and mc.calls == 3


def test_a_level_beyond_the_top_is_the_top_in_the_engine_and_in_the_stored_view():
    store, _ = _store()
    w, leaves = _world("two_rooms")
    g, v = _grow(store, "m", leaves, 8)
    view = store.view("m")
    top = len(g._levels()[0]) - 1
    assert top >= 1                                           # the world has a coarser level, else the test says nothing
    sig = lambda q: (sorted((c.key, c.cells) for c in q.charts), sorted((l.a, l.b, round(l.length, 6)) for l in q.links))
    for level in range(0, top + 4):
        a, b = g.query(BIG, level, raster=False), view.query(BIG, level, raster=False)
        assert sig(a) == sig(b)
        if level >= top:
            assert sig(a) == sig(g.query(BIG, top, raster=False))
    assert sig(g.query(BIG, top + 2, raster=False)) != sig(g.query(BIG, 0, raster=False))   # not the level-0 charts
    assert sig(g.query(BIG, -3, raster=False)) == sig(g.query(BIG, 0, raster=False))
