"""Close-evidence rule (SceneParams.near_range_m): a cell reported blocked stays blocked until a FREE report from a capture
pose within range is as new (B > Fn). Off by default and then bit-identical to the latest-value fold."""
import itertools

import numpy as np
import pytest

from packages.scene_graph import store as S
from packages.scene_graph import store_codec as C
from packages.scene_graph.api import BBox, Leaf, SceneParams
from packages.scene_graph.engine import ChartGraph

from . import chart_worlds as cw

pytestmark = pytest.mark.unit

R = 0.05
PATCH = (3.0, 2.5)       # centre of a 0.5 m hazard patch in an 8 x 6 m room
NEAR, FAR = (4.0, 2.5), (6.5, 2.5)   # capture poses: 1.0 m and 3.5 m from the patch


def _leaf(key, t, pose, hazard, yaw=0.0):
    """an 8 x 6 m window at the origin: free room, the patch lethal when `hazard` else free"""
    w = np.zeros((120, 160), np.uint8)
    if hazard: w[int(2.25 / R):int(2.75 / R), int(2.75 / R):int(3.25 / R)] = 100
    return Leaf(key=key, session="s", stamp_ms=t, x=pose[0], y=pose[1], yaw=yaw, resolution=R, origin=(0.0, 0.0), window_u8=w)


def _nav(g, p=PATCH):
    f = g._build_F((0, 0))[0]
    return bool(f[int(p[1] / R - g._cy0) // 2, int(p[0] / R - g._cx0) // 2])


def _run(events, **kw):
    """events: [(t, pose, hazard)] folded one batch each, in the given arrival order"""
    g = ChartGraph(SceneParams(near_range_m=1.5, **kw))
    for i, (t, pose, hz) in enumerate(events):
        g.fold_many([_leaf("k%d" % i, t, pose, hz)])
    return g


def test_far_free_does_not_clear_a_close_lethal():
    g = _run([(1000, NEAR, True), (2000, FAR, False)])
    assert g._V[int(PATCH[1] / R) - g._cy0, int(PATCH[0] / R) - g._cx0] == 0          # latest value is free ...
    assert not _nav(g)                                                              # ... but the rule holds it blocked


def test_close_free_after_lethal_clears_it():
    assert _nav(_run([(1000, NEAR, True), (2000, FAR, False), (3000, NEAR, False)]))
    assert _nav(_run([(1000, FAR, True), (2000, NEAR, False)]))                      # lethal from far, cleared from close


def test_cell_never_lethal_is_unaffected():
    g = _run([(1000, FAR, False), (2000, NEAR, False)])
    assert _nav(g) and (g._B < 0).all()


def test_time_order_not_arrival_order():
    # free near 2000, free far 3000, an OLDER lethal (1000) arriving last: B < Fn -> free
    assert _nav(_run([(2000, NEAR, False), (3000, FAR, False), (1000, NEAR, True)]))
    # lethal 3000 first, far free 4000, older near free 1000 arrives last: B > Fn -> blocked; a newer near free frees it
    ev = [(3000, NEAR, True), (4000, FAR, False), (1000, NEAR, False)]
    assert not _nav(_run(ev))
    assert _nav(_run(ev + [(5000, NEAR, False)]))
    # a tie goes to blocked
    assert not _nav(_run([(1000, NEAR, True), (1000, NEAR, False)]))


def test_fov_cone_requires_facing():
    # NEAR is east of the patch: yaw 0 looks away from it, yaw pi looks at it
    g = ChartGraph(SceneParams(near_range_m=1.5, near_fov_deg=90.0))
    g.fold_many([_leaf("a", 1000, NEAR, True), _leaf("b", 2000, NEAR, False, yaw=0.0)])
    assert not _nav(g)
    g.fold_many([_leaf("c", 3000, NEAR, False, yaw=np.pi)])
    assert _nav(g)


def _two_pass():
    w, way = cw.pillar_room()
    lethal = w.copy(); lethal[int(1.0 / R):int(2.0 / R), int(1.0 / R):int(2.0 / R)] = 100
    return cw.drive(lethal, way, step=0.4, win=60, session="a") + cw.drive(w, way[::-1], step=0.4, win=60, session="b", t0=100000)


def _norm(g):
    """planes cropped to their non-default content, in global coordinates (the canvas origin depends on arrival order)"""
    act = (g._V != 255) | (g._B >= 0)
    ys, xs = np.nonzero(act)
    sl = (slice(ys.min(), ys.max() + 1), slice(xs.min(), xs.max() + 1))
    out = {n: a[sl].copy() for n, a in (("V", g._V), ("T", g._T), ("B", g._B))}
    if g._Fn is not None: out["Fn"] = g._Fn[sl].copy()
    if g._hold is not None: out["hold"] = g._hold[sl].copy()
    return (g._cx0 + xs.min(), g._cy0 + ys.min()), out


def _cells(g):
    v = g.query(BBox(-50, -50, 50, 50), raster=True).raster
    return sorted(sorted(zip(np.round(v.origin[0] + (xs + 0.5) * v.step, 3).tolist(), np.round(v.origin[1] + (ys + 0.5) * v.step, 3).tolist()))
                  for ys, xs in (np.nonzero(v.index == i) for i in range(1, len(v.keys) + 1)))


def test_rule_off_is_the_plain_fold():
    leaves = _two_pass()
    a, b = ChartGraph(SceneParams()), ChartGraph(SceneParams(near_range_m=None))
    for g in (a, b):
        for i in range(0, len(leaves), 5): g.fold_many(leaves[i:i + 5])
    assert a._Fn is None and b._Fn is None
    (oa, pa), (ob, pb) = _norm(a), _norm(b)
    assert oa == ob and all((pa[k] == pb[k]).all() for k in pa)
    assert (a._build_F((0, 0))[0] == b._build_F((0, 0))[0]).all()
    st = a.export_state()
    assert st.Fn is None and set(st.params) == {"robot_half_side", "lattice", "chart_radius"}
    assert all("fn" not in (S.tile_arrays(st, tx, ty, None) or {}) for tx in range(-1, 2) for ty in range(-1, 2))
    assert S.SceneGraphStore.params_hash(SceneParams()) == S.params_hash(st.params, S.PIPELINE)


def test_order_independence_and_idempotence():
    leaves = _two_pass()
    ref = None
    orders = [list(leaves), list(reversed(leaves)), leaves[::2] + leaves[1::2], list(leaves) + leaves[:7]]    # last: duplicates
    for order, bs in itertools.product(orders, (1, 4, 11)):
        g = ChartGraph(SceneParams(near_range_m=1.0))
        for i in range(0, len(order), bs): g.fold_many(order[i:i + bs])
        got = (_norm(g), _cells(g), int(g._build_F((0, 0))[0].sum()))
        if ref is None:
            ref = got
            assert (ref[0][1]["Fn"] >= 0).any() and (ref[0][1]["B"] >= 0).any()
            continue
        assert got[0][0] == ref[0][0] and all((got[0][1][k] == ref[0][1][k]).all() for k in ref[0][1])
        assert got[1] == ref[1] and got[2] == ref[2]


def test_store_round_trip_and_params_mismatch():
    g = _run([(1000, NEAR, True), (2000, FAR, False)])
    be = S.MemoryBackend(); st = S.SceneGraphStore(be, be)
    st.save("m", g, g.fold_many([_leaf("z", 2500, FAR, False)]), None, {"n": 1})
    h = st.load("m")
    assert h.params.near_range_m == 1.5 and _norm(h)[0] == _norm(g)[0]
    assert all((_norm(h)[1][k] == _norm(g)[1][k]).all() for k in ('V', 'T', 'B', 'Fn'))
    assert not _nav(h) and int(h._build_F((0, 0))[0].sum()) == int(g._build_F((0, 0))[0].sum())
    assert st.meta("m")["params_hash"] == S.SceneGraphStore.params_hash(g.params)
    assert st.manifest("m")["pipeline"] == S.PIPELINE_NEAR
    with pytest.raises(ValueError, match="near_range_m"):       # an engine without the rule refuses the state
        ChartGraph(SceneParams()).import_state(g.export_state())
    # the reverse: a store made without the rule loads as an engine without it, and a rule-on engine refuses its state
    g0 = ChartGraph(SceneParams()); g0.fold_many([_leaf("a", 1000, NEAR, True)])
    be0 = S.MemoryBackend(); st0 = S.SceneGraphStore(be0, be0)
    st0.save("m", g0, g0.fold_many([]), None, {"n": 1})
    h0 = st0.load("m")
    assert h0.params.near_range_m is None and h0._Fn is None
    with pytest.raises(ValueError, match="near_range_m"):
        ChartGraph(SceneParams(near_range_m=1.5)).import_state(g0.export_state())


# ---- readers that work from tiles see the rule --------------------------------------------------------------
def test_effective_v_holds_the_cells_the_engine_holds():
    v = np.array([[0, 0, 100, 255]], np.uint8)
    b = np.array([[5, 5, 5, 5]], np.int64)
    t = {"v": v, "b": b, "own": np.zeros((1, 4), np.int32)}
    assert C.effective_v(t) is v                                       # no `fn` plane: the stored plane itself
    t["fn"] = np.array([[9, 3, 3, 3]], np.int64)                         # cell 0: close free newer than blocked: free
    out = C.effective_v(t)                                              # cell 1: blocked newer: held; blocked and unknown stay
    assert out.tolist() == [[0, 100, 100, 255]] and v.tolist() == [[0, 0, 100, 255]]
    assert C.read_planes(t)["v"].tolist() == out.tolist() and "b" not in C.read_planes(t)
    t["b"] = -np.ones((1, 4), np.int64)                                  # never blocked: nothing is held
    assert C.effective_v(t) is v


def test_lazy_reader_and_tile_plane_agree_with_the_engine_under_the_rule():
    from packages.services.mission_planner.lazy_plan import open_plan
    g = _run([(1000, NEAR, True), (2000, FAR, False)])
    be = S.MemoryBackend(); st = S.SceneGraphStore(be, be)
    st.save("m", g, g.fold_many([_leaf("z", 2500, FAR, False)]), None, {"n": 1})
    eng = st.load("m")
    held = (eng._V < 100) & (eng._B >= 0) & (eng._B > eng._Fn)
    assert eng._Fn is not None and held.any()                           # the rule holds cells that the latest value calls free
    lazy = open_plan(st, "m", max_tiles=10 ** 6)
    sp = eng._solved.sp
    ix0, iy0 = eng._cx0 // 2, eng._cy0 // 2
    n = 0
    for margin in (0.3, 1.0):
        for y, x in zip(sp.ys.tolist(), sp.xs.tolist()):
            e = eng.deficit_cell(x + ix0, y + iy0, margin)
            assert e is not None and lazy.deficit_cell(x + ix0, y + iy0, margin) == e     # clearance counts the held cells as blocked
            n += 1
    assert n > 0
    # the occupancy a tile reader gets (tile_v_plane) holds blocked what _build_F removed from free space
    free_eff = np.zeros(eng._V.shape, bool)
    man = st.manifest("m")
    for name in man["tiles"]:
        tx, ty = (int(c) for c in name.split(","))
        p = st.tile_v_plane("m", st.meta("m")["version"], tx, ty)
        y0, x0 = ty * S.TILE_F - eng._cy0, tx * S.TILE_F - eng._cx0
        ys, xs = max(0, y0), max(0, x0)
        ye, xe = min(eng._V.shape[0], y0 + S.TILE_F), min(eng._V.shape[1], x0 + S.TILE_F)
        if ys < ye and xs < xe:
            free_eff[ys:ye, xs:xe] = ((p != 255) & (p < 100))[ys - y0:ye - y0, xs - x0:xe - x0]
    held = (eng._V < 100) & (eng._B >= 0) & (eng._B > eng._Fn)
    assert held.any() and not free_eff[held].any()


def test_the_checker_accepts_a_store_made_with_the_rule():
    from tools import scene_graph_check_generation as chk
    w, way = cw.two_rooms(W=12.0)
    g = ChartGraph(SceneParams(near_range_m=1.5, near_fov_deg=120.0))
    be = S.MemoryBackend(); st = S.SceneGraphStore(be, be)
    st.save("m", g, g.fold_many(cw.drive(w, way)), None, {})
    r = chk.check_map(st, "m")
    assert r["problems"] == [] and r["pipeline"] == S.PIPELINE_NEAR


def test_the_graph_builder_reads_the_rule_from_config(monkeypatch):
    from packages import config
    monkeypatch.setenv("SCENE_GRAPH_NEAR_RANGE_M", "1.5")
    monkeypatch.setenv("SCENE_GRAPH_NEAR_FOV_DEG", "")
    assert config._opt_float("SCENE_GRAPH_NEAR_RANGE_M") == 1.5 and config._opt_float("SCENE_GRAPH_NEAR_FOV_DEG") is None
    assert config.SCENE_GRAPH_NEAR_RANGE_M is None or isinstance(config.SCENE_GRAPH_NEAR_RANGE_M, float)   # unset in the test env


# ---- the away-from-walls filter (SceneParams.near_min_clearance_m) ------------------------------------------
# A 12 x 6 m room (240 x 120 cells, tile edge at column 128 = 6.4 m) ringed by a lethal wall two cells thick. Hazards are boxes
# (row0, row1, col0, col1) lethal in the NEAR leaf (t 1000) and free in the FAR leaf (t 2000, posed beyond range): held by the rule.
THIN = (2, 4, 100, 132)         # 2 cells thick on the south wall, columns 100..131: left of the tile edge, a wall rim by itself
THICK = (2, 12, 132, 152)       # 10 cells (0.5 m) deep, right of the tile edge, joined to THIN: reaches 0.5 m from the wall
RIM = (116, 118, 40, 80)        # 2 cells thick on the north wall: within 0.1 m of it everywhere
MIDPATCH = (50, 60, 40, 50)       # 0.5 m square in the middle of the room (2.4 m from the nearest wall)
NEAR_P, FAR_P = (6.4, 1.5), (9.5, 3.0)
MC = 0.25


def _room(hazards=(), shape=(120, 240)):
    w = np.zeros(shape, np.uint8)
    w[:2] = 100; w[-2:] = 100; w[:, :2] = 100; w[:, -2:] = 100
    for r0, r1, c0, c1 in hazards: w[r0:r1, c0:c1] = 100
    return w


def _wleaf(key, t, pose, w, origin=(0.0, 0.0)):
    return Leaf(key=key, session="s", stamp_ms=t, x=pose[0], y=pose[1], yaw=0.0, resolution=R, origin=origin, window_u8=w)


def _leaves(hazards):
    return [_wleaf("n", 1000, NEAR_P, _room(hazards)), _wleaf("f", 2000, FAR_P, _room())]


def _engine(hazards, mc=MC, batch=2, **kw):
    g = ChartGraph(SceneParams(near_range_m=1.5, near_min_clearance_m=mc, **kw))
    lv = _leaves(hazards)
    for i in range(0, len(lv), batch): g.fold_many(lv[i:i + batch])
    return g


def _held(g):
    ys, xs = np.nonzero(g._hold)
    return {(int(y) + g._cy0, int(x) + g._cx0) for y, x in zip(ys, xs)}


def _box(b):
    return {(r, c) for r in range(b[0], b[1]) for c in range(b[2], b[3])}


def test_filter_params_are_validated_and_stored_only_when_set():
    with pytest.raises(ValueError, match="needs near_range_m"):
        SceneParams(near_min_clearance_m=0.25)
    for bad in (0.0, -1.0, float("inf")):
        with pytest.raises(ValueError, match="near_min_clearance_m"):
            SceneParams(near_range_m=1.5, near_min_clearance_m=bad)
    assert "near_min_clearance_m" not in ChartGraph(SceneParams(near_range_m=1.5)).export_state().params
    g = ChartGraph(SceneParams(near_range_m=1.5, near_fov_deg=90.0, near_min_clearance_m=0.25))
    assert g.export_state().params == dict(robot_half_side=0.25, lattice=0.1, chart_radius=2.0, near_range_m=1.5,
                                           near_fov_deg=90.0, near_min_clearance_m=0.25)
    assert S.SceneGraphStore.params_hash(SceneParams(near_range_m=1.5)) != S.SceneGraphStore.params_hash(
        SceneParams(near_range_m=1.5, near_min_clearance_m=0.25))


def test_filter_semantics_on_a_synthetic_room():
    hz = (THIN, THICK, RIM, MIDPATCH)
    off = _engine(hz, mc=None)                       # the rule alone holds every candidate, wall rims included
    on = _engine(hz)
    assert off._hold is None                         # no filter: no hold plane
    cand = (off._V < 100) & (off._B >= 0) & (off._B > off._Fn)
    ys, xs = np.nonzero(cand)
    assert {(int(y) + off._cy0, int(x) + off._cx0) for y, x in zip(ys, xs)} == _box(THIN) | _box(THICK) | _box(RIM) | _box(MIDPATCH)
    # a hazard patch 2.4 m from any wall is held; a rim strip beside a wall is released; a component that touches the wall but
    # reaches 0.5 m from it is kept whole, its rim cells included
    assert _held(on) == _box(THIN) | _box(THICK) | _box(MIDPATCH)
    assert not (_held(on) & _box(RIM))
    # the released rim reads as without the rule: F equals the rule-on F of a world that has no RIM hazard
    ref = _engine((THIN, THICK, MIDPATCH))
    assert (on._build_F((0, 0))[0] == ref._build_F((0, 0))[0]).all()
    # exactly at the threshold is not "farther": a 5-cell deep strip (0.25 m) is released, a 6-cell one (0.30 m) is kept
    assert _held(_engine(((2, 7, 100, 110),))) == set()
    assert _held(_engine(((2, 8, 100, 110),))) == _box((2, 8, 100, 110))
    # a smaller threshold keeps the thin rim strip too
    assert _held(_engine((RIM,), mc=0.05)) == _box(RIM)


def test_filter_off_is_the_rule_alone():
    hz = (THIN, THICK, RIM, MIDPATCH)
    a, b = _engine(hz, mc=None), ChartGraph(SceneParams(near_range_m=1.5))
    for lf in _leaves(hz): b.fold_many([lf])
    assert a._hold is None and b._hold is None and a.export_state().Hold is None
    assert (a._build_F((0, 0))[0] == b._build_F((0, 0))[0]).all()
    st = a.export_state()
    tiles = [S.tile_arrays(st, tx, ty, None) for tx in range(-1, 3) for ty in range(-1, 2)]
    assert all(set(t) <= {"v", "t", "b", "fn", "own"} for t in tiles if t)
    be = S.MemoryBackend(); store = S.SceneGraphStore(be, be)
    store.save("m", a, a.fold_many([]), None, {"n": 1})
    man = store.manifest("m")
    assert man["pipeline"] == S.PIPELINE_NEAR
    assert man["params"] == {"robot_half_side": 0.25, "lattice": 0.1, "chart_radius": 2.0, "near_range_m": 1.5}


def test_filter_component_across_a_tile_edge_is_held_consistently_in_engine_and_readers():
    from packages.services.mission_planner.lazy_plan import open_plan
    hz = (THIN, THICK, RIM, MIDPATCH)
    g = _engine(hz)
    be = S.MemoryBackend(); store = S.SceneGraphStore(be, be)
    store.save("m", g, g.fold_many([]), None, {"n": 1})
    man = store.manifest("m")
    # the stored hold plane: the left tile (columns < 128) holds THIN cells although nothing in that tile is farther than 0.25 m
    # from a wall: only the component, which reaches into the right tile, keeps them
    tl = store._tile("m", man, 0, 0)
    assert "hold" in tl and tl["hold"].dtype == np.uint8
    left = {(int(r), int(c)) for r, c in zip(*np.nonzero(tl["hold"]))}
    assert (2, 100) in left and (3, 127) in left and not (left & _box(RIM))
    assert all(c < S.TILE_F for _, c in left)
    eng = store.load("m")                           # import verifies the owner plane and the hold plane against the rebuild
    assert _held(eng) == _held(g)
    lazy = open_plan(store, "m", max_tiles=10 ** 6)
    sp = eng._solved.sp
    ix0, iy0 = eng._cx0 // 2, eng._cy0 // 2
    n = 0
    for margin in (0.3, 1.0):
        for y, x in zip(sp.ys.tolist(), sp.xs.tolist()):
            e = eng.deficit_cell(x + ix0, y + iy0, margin)
            assert e is not None and lazy.deficit_cell(x + ix0, y + iy0, margin) == e
            n += 1
    assert n > 0
    # the occupancy a tile reader gets: the rim strip is free (released), the held cells are blocked
    v = {}
    for name in man["tiles"]:
        tx, ty = (int(c) for c in name.split(","))
        v[(tx, ty)] = store.tile_v_plane("m", man["version"], tx, ty)
    at = lambda r, c: v[(c // S.TILE_F, r // S.TILE_F)][r % S.TILE_F, c % S.TILE_F]
    assert all(at(r, c) < 100 for r, c in _box(RIM))
    assert all(at(r, c) >= 100 for r, c in _box(THIN) | _box(THICK) | _box(MIDPATCH))


def test_filter_dirty_box_covers_a_component_that_changes_outside_the_folded_windows():
    g = ChartGraph(SceneParams(near_range_m=1.5, near_min_clearance_m=MC))
    thin_w = _room((THIN,))[0:20, 90:140]
    far_w = _room()[0:20, 90:140]
    g.fold_many([_wleaf("n1", 1000, NEAR_P, thin_w, (90 * R, 0.0)), _wleaf("f1", 2000, FAR_P, far_w, (90 * R, 0.0))])
    assert g._hold is not None and not g._hold.any()                 # THIN alone is a wall rim: released
    # a later batch whose windows cover only THICK: the component THIN + THICK now has a far cell and THIN becomes held
    tw = _room((THICK,))[0:20, 132:170]
    fw = _room()[0:20, 132:170]
    d = g.fold_many([_wleaf("n2", 1100, NEAR_P, tw, (132 * R, 0.0)), _wleaf("f2", 2100, FAR_P, fw, (132 * R, 0.0))])
    assert _held(g) == _box(THIN) | _box(THICK)
    assert d.bbox is not None and d.bbox.x0 <= 100 * R + 1e-9       # THIN lies outside both windows of this batch


# ---- order, idempotence, store, readers, checker ------------------------------------------------------------
def test_filter_order_independence_and_idempotence():
    leaves = _two_pass()
    ref = None
    orders = [list(leaves), list(reversed(leaves)), leaves[::2] + leaves[1::2], list(leaves) + leaves[:7]]
    for order, bs in itertools.product(orders, (1, 4, 11)):
        g = ChartGraph(SceneParams(near_range_m=0.3, near_min_clearance_m=0.1))
        for i in range(0, len(order), bs): g.fold_many(order[i:i + bs])
        got = (_norm(g), _cells(g), int(g._build_F((0, 0))[0].sum()))
        if ref is None:
            ref = got
            assert ref[0][1]["hold"].any() and (ref[0][1]["Fn"] >= 0).any()
            continue
        assert got[0][0] == ref[0][0] and all((got[0][1][k] == ref[0][1][k]).all() for k in ref[0][1])
        assert got[1] == ref[1] and got[2] == ref[2]


def test_filter_store_round_trip_and_params_mismatch():
    hz = (THIN, THICK, RIM, MIDPATCH)
    g = _engine(hz)
    be = S.MemoryBackend(); store = S.SceneGraphStore(be, be)
    store.save("m", g, g.fold_many([]), None, {"n": 1})
    h = store.load("m")
    assert h.params.near_min_clearance_m == MC and _norm(h)[0] == _norm(g)[0]
    assert all((_norm(h)[1][k] == _norm(g)[1][k]).all() for k in ("V", "T", "B", "Fn", "hold"))
    assert int(h._build_F((0, 0))[0].sum()) == int(g._build_F((0, 0))[0].sum())
    man = store.manifest("m")
    assert man["pipeline"] == S.PIPELINE_NEAR_HOLD and man["params"]["near_min_clearance_m"] == MC
    assert store.meta("m")["params_hash"] == S.SceneGraphStore.params_hash(g.params)
    # an engine with the rule but no filter refuses the state, and the filter engine refuses a rule-only state
    with pytest.raises(ValueError, match="near_min_clearance_m"):
        ChartGraph(SceneParams(near_range_m=1.5)).import_state(g.export_state())
    r = _engine(hz, mc=None)
    with pytest.raises(ValueError, match="near_min_clearance_m"):
        ChartGraph(SceneParams(near_range_m=1.5, near_min_clearance_m=MC)).import_state(r.export_state())
    # a stored hold plane that is not what the planes give is refused
    st = g.export_state()
    st.Hold = ~st.Hold
    with pytest.raises(S.StateMismatch, match="hold"):
        ChartGraph(SceneParams(near_range_m=1.5, near_min_clearance_m=MC)).import_state(st)


def test_filter_the_checker_accepts_a_store_made_with_it():
    from tools import scene_graph_check_generation as chk
    w, way = cw.two_rooms(W=12.0)
    g = ChartGraph(SceneParams(near_range_m=1.5, near_min_clearance_m=0.25))
    be = S.MemoryBackend(); st = S.SceneGraphStore(be, be)
    st.save("m", g, g.fold_many(cw.drive(w, way)), None, {})
    r = chk.check_map(st, "m")
    assert r["problems"] == [] and r["pipeline"] == S.PIPELINE_NEAR_HOLD
    assert r["params"]["near_min_clearance_m"] == 0.25


def test_filter_effective_v_uses_the_stored_mask():
    v = np.array([[0, 0, 100, 255, 0]], np.uint8)
    t = {"v": v, "b": np.array([[5, 5, 5, 5, 5]], np.int64), "fn": np.array([[3, 3, 3, 3, 3]], np.int64)}
    assert C.effective_v(t).tolist() == [[100, 100, 100, 255, 100]]              # b > fn alone: cells 0, 1, 4 held
    t["hold"] = np.array([[0, 1, 1, 1, 0]], np.uint8)                              # the mask releases 0 and 4; blocked/unknown stay
    assert C.effective_v(t).tolist() == [[0, 100, 100, 255, 0]] and v.tolist() == [[0, 0, 100, 255, 0]]
    assert C.read_planes(t)["v"].tolist() == [[0, 100, 100, 255, 0]]


def test_filter_reads_the_setting_from_config(monkeypatch):
    from packages import config
    monkeypatch.setenv("SCENE_GRAPH_NEAR_MIN_CLEARANCE_M", "0.25")
    assert config._opt_float("SCENE_GRAPH_NEAR_MIN_CLEARANCE_M") == 0.25
    monkeypatch.setenv("SCENE_GRAPH_NEAR_MIN_CLEARANCE_M", "")
    assert config._opt_float("SCENE_GRAPH_NEAR_MIN_CLEARANCE_M") is None
    assert config.SCENE_GRAPH_NEAR_MIN_CLEARANCE_M is None or isinstance(config.SCENE_GRAPH_NEAR_MIN_CLEARANCE_M, float)
