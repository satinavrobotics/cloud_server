"""Validated-first planning (docs/scene_graph/close_evidence_and_turn_cost_design.md part G): the lexicographic (unvalidated edges, cost)
optimum through the penalised weights of `packages/scene_graph/validated.py`, the validity plane `val` and its store round trip.

(a) exactness against a brute force over all simple paths and against an independent tuple-distance Dijkstra (no penalty constant),
(b) a validated detour beats a shorter unvalidated shortcut, (c) a world without a validated route still plans, (d) off = today's weights,
(e) the `val` plane through the store and the pipeline token, (f) tile readers see the engine's validity, (g) float64 bound."""
import itertools
import math
import random

import numpy as np
import pytest
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import dijkstra

from packages.scene_graph import store as S
from packages.scene_graph import validated
from packages.scene_graph.api import Safety, SceneParams
from packages.scene_graph.engine import ChartGraph

from . import chart_worlds as cw
from .route_safety_worlds import Reference, engine_of, pairs, random_world

pytestmark = pytest.mark.unit

NEAR = 0.5                    # a small near range: most cells stay unvalidated except along the drive
RULE = SceneParams(near_range_m=NEAR)
R_C = 0.25 * math.sqrt(2)
VF = Safety(0.45, 0.0, validated_first=True)
VF_PRICED = Safety(R_C, 1.0 / (R_C - 0.25), validated_first=True)
VF_TURN = Safety(0.45, 0.0, turn=0.4, validated_first=True)
VF_BOTH = Safety(R_C, 1.0 / (R_C - 0.25), turn=0.4, validated_first=True)


def detour_world():
    """12 x 6 m, a wall with two doors: D1 (y 0.7 - 1.7) is driven through, D2 (y 3.5 - 4.5) is only seen from afar; the start (2, 4) and the
    goal (10, 4) lie on the straight line through D2, which is 8.1 m, against about 12.3 m through D1"""
    W, H = 12.0, 6.0
    w = cw._blank(W, H)
    cw._rect(w, 5.8, 0.2, 6.0, H - 0.2, 100)
    cw._rect(w, 5.8, 0.7, 6.0, 1.7, 0)
    cw._rect(w, 5.8, 3.5, 6.0, 4.5, 0)
    return w, [(2.0, 4.0), (2.0, 1.2), (5.0, 1.2), (7.0, 1.2), (10.0, 1.2), (10.0, 4.0)]


def seen_only_world():
    """two rooms joined by one door; the robot never leaves the left room, the right one is only seen through the door"""
    w = cw.two_rooms(door=0.9)[0]
    return w, [(1.0, 1.5), (2.0, 1.5), (1.0, 0.8), (1.0, 1.5)]


def _cells(r):
    return [tuple(p) for p in r.nodes]


# ---- the pieces ------------------------------------------------------------------------------------------------
def test_validated_nodes_are_a_close_free_report_or_the_body():
    g = engine_of(detour_world(), params=RULE)
    sp = g._solved.sp
    val = g._val
    assert val.shape == g._solved.plane.shape and val.dtype == bool
    ref = Reference(g)
    assert np.array_equal(val, ref.val)                 # the node-by-node reading of the definition
    frac = float(val[sp.ys, sp.xs].mean())
    assert 0.05 < frac < 0.6                            # a driven line and its surroundings, not the whole room
    # a rule-off engine has no validity plane
    off = engine_of(detour_world())
    assert off._val is None and off.export_state().Val is None


def test_a_close_lethal_report_is_not_validation():
    """validity comes from Fn (a close FREE report) and the driven body only: a node whose block has no Fn >= 0 is validated only by S"""
    g = engine_of(detour_world(), params=RULE)
    seen = g._Fn >= 0
    close_free = seen.reshape(seen.shape[0] // 2, 2, seen.shape[1] // 2, 2).any(axis=(1, 3))
    body_only = g._val & ~close_free
    assert (body_only <= g._pl["S"]).all()
    # a close lethal report (Bn) sets no validity: planes derived from Bn alone leave val unchanged
    assert not (g._Bn >= 0).all() and ((g._val & ~g._pl["S"]) <= close_free).all()


def test_penalty_exceeds_every_simple_path_and_the_bound_is_checked():
    W = np.array([5.0, 7.0, 3.0])
    p = validated.penalty(W.sum(), 4, 0.0)
    assert p == 16                                       # 1 + the sum of the weights
    assert validated.penalty(W.sum(), 4, 10.0) == 16 + 2 * 10
    Wp, p2 = validated.penalise(W, np.array([True, False, True]), 4)
    assert p2 == 16 and list(Wp) == [21.0, 7.0, 19.0]
    with pytest.raises(ValueError, match="exact range"):
        validated.penalise(np.full(10, 2.0 ** 50), np.zeros(10, bool), 10 ** 6)
    assert validated.split(2 * 16 + 9, 16, 2) == 9
    with pytest.raises(AssertionError):
        validated.split(2 * 16 + 9, 16, 1)


# ---- (a) exactness ---------------------------------------------------------------------------------------------
def _grid_graph(rnd, h, w):
    """a random 8-neighbour grid graph on h x w nodes (cells row-major), integer weights, node validity"""
    idx = lambda y, x: y * w + x
    R, C, Wt = [], [], []
    for y in range(h):
        for x in range(w):
            for dy, dx in ((0, 1), (1, 0), (1, 1), (1, -1)):
                yy, xx = y + dy, x + dx
                if 0 <= yy < h and 0 <= xx < w and rnd.random() < 0.8:
                    R.append(idx(y, x)); C.append(idx(yy, xx)); Wt.append(float(rnd.randint(1, 30)))
    val = np.array([rnd.random() < 0.6 for _ in range(h * w)])
    return np.array(R), np.array(C), np.array(Wt), val


def _canonical(g, d, src, dst):
    path = [src]
    while path[-1] != dst:
        u = path[-1]
        lo, hi = g.indptr[u], g.indptr[u + 1]
        nb = g.indices[lo:hi]
        path.append(int(nb[(g.data[lo:hi] + d[nb]) == d[u]].min()))
    return path


def _simple_paths(adj, src, dst):
    out, stack = [], [(src, [src])]
    while stack:
        u, path = stack.pop()
        if u == dst:
            out.append(path)
            continue
        for v in adj[u]:
            if v not in path:
                stack.append((v, path + [v]))
    return out


@pytest.mark.parametrize("seed", range(6))
def test_a_penalised_dijkstra_is_the_brute_force_lexicographic_optimum(seed):
    """all simple paths of a small random graph: the minimum (unvalidated edges, J) and, among ties, the lexicographic minimum of the node
    sequence, on every pair, against the production weights (`validated.penalise`) and the plain canonical walk"""
    rnd = random.Random(seed)
    h, w = 3, 4
    R, C, Wt, val = _grid_graph(rnd, h, w)
    n = h * w
    unval = validated.edge_flags(val, R, C)
    Wp, P = validated.penalise(Wt, unval, n)
    g = coo_matrix((np.concatenate([Wp, Wp]), (np.concatenate([R, C]), np.concatenate([C, R]))), shape=(n, n)).tocsr()
    adj = {u: [] for u in range(n)}
    key = {}
    for r, c, wt, uv in zip(R.tolist(), C.tolist(), Wt.tolist(), unval.tolist()):
        adj[r].append(c); adj[c].append(r)
        key[(r, c)] = key[(c, r)] = (int(uv), int(wt))
    checked = 0
    for src, dst in itertools.permutations(range(n), 2):
        paths = _simple_paths(adj, src, dst)
        d = dijkstra(g, directed=False, indices=dst)
        if not paths:
            assert not np.isfinite(d[src])
            continue
        score = lambda p: (sum(key[(a, b)][0] for a, b in zip(p[:-1], p[1:])), sum(key[(a, b)][1] for a, b in zip(p[:-1], p[1:])))
        best = min(paths, key=lambda p: (score(p), p))
        got = _canonical(g, d, src, dst)
        assert got == best
        c, j = score(best)
        assert validated.split(d[src], P, c) == j
        checked += 1
    assert checked > 60


def _check_world_against_the_reference(g, safeties, n_pairs, seed):
    ref = Reference(g)
    done = 0
    for a, b in pairs(g, n_pairs, seed):
        src, dst = ref.endpoints(a, b)
        for sf in safeties:
            if sf.turn > 0:
                score, path = ref.lex_dijkstra_turn(src, dst, sf)
            else:
                score, path = ref.lex_dijkstra(src, dst, sf if sf.rate > 0 else None)
            r = g.route(a, b, None, None, None, safety=sf)
            assert (r is None) == (score is None)
            if r is None:
                continue
            assert ref.cells_of(r) == path
            assert (r.unvalidated, r.cost) == score
            assert r.unvalidated == ref.unvalidated_of(path)[0]
            assert r.unvalidated_m == pytest.approx(ref.unvalidated_of(path)[1])
            done += 1
    return done


@pytest.mark.parametrize("seed", [3, 8, 14])
def test_a_engine_route_is_the_independent_lexicographic_optimum_on_random_worlds(seed):
    g = engine_of(random_world(seed), params=RULE)
    assert 0.0 < float(g._val[g._solved.sp.ys, g._solved.sp.xs].mean()) < 1.0
    assert _check_world_against_the_reference(g, [VF, VF_PRICED], 14, seed) >= 10


@pytest.mark.parametrize("seed", [5, 11])
def test_a_engine_route_with_a_turn_cost_is_the_lexicographic_optimum(seed):
    g = engine_of(random_world(seed), params=RULE)
    assert _check_world_against_the_reference(g, [VF_TURN, VF_BOTH], 8, seed) >= 6


def test_a_ties_break_by_the_row_major_cell_sequence():
    """a symmetric world (two equal validated ways round a pillar): the route is the lexicographic (y, x) minimum, as without the flag"""
    g = engine_of(cw.pillar_room(), params=SceneParams(near_range_m=3.0))
    ref = Reference(g)
    n = 0
    for a, b in pairs(g, 12, 2):
        src, dst = ref.endpoints(a, b)
        score, path = ref.lex_dijkstra(src, dst, None)
        r = g.route(a, b, None, None, None, safety=VF)
        assert ref.cells_of(r) == path
        n += 1
    assert n


# ---- (b) a validated detour beats a shorter unvalidated shortcut --------------------------------------------------
def test_b_the_plan_takes_the_validated_detour_over_the_unvalidated_shortcut():
    g = engine_of(detour_world(), params=RULE)
    a, b = g.snap(2.0, 4.0), g.snap(10.0, 4.0)
    args = (a.key, b.key, None, (a.x, a.y), (b.x, b.y))
    plain = g.route(*args)
    vf = g.route(*args, safety=VF)
    ref = Reference(g)
    assert plain.unvalidated is None and vf.unvalidated == 0 and vf.unvalidated_m == 0.0
    assert ref.unvalidated_of(ref.cells_of(plain))[0] > 30           # the shortcut through D2 is mostly unvalidated
    assert ref.unvalidated_of(ref.cells_of(vf)) == (0, 0.0)
    assert plain.length < 8.5 < 12.0 < vf.length                     # shorter, but unvalidated
    ys = [p[1] for p in vf.nodes]
    assert min(ys) < 1.8                                             # it went through D1, the driven door
    # the priced / turn variants keep the property and J stays the cost of the route they return
    for sf in (VF_PRICED, VF_TURN, VF_BOTH):
        r = g.route(*args, safety=sf)
        assert r.unvalidated == 0 and min(p[1] for p in r.nodes) < 1.8


# ---- (c) no validated route: the plan still exists -------------------------------------------------------------------
def test_c_a_world_without_a_validated_route_still_plans_and_reports_the_count():
    g = engine_of(seen_only_world(), params=SceneParams(near_range_m=0.3))
    ref = Reference(g)
    keys = g._solved.keys
    far = [k for k in keys if g.route(keys[0], k) is not None]
    assert len(far) >= 2
    worst = 0
    for k in far:
        plain, vf = g.route(keys[0], k), g.route(keys[0], k, safety=VF)
        assert vf is not None                                         # nothing becomes unroutable
        assert vf.unvalidated == ref.unvalidated_of(ref.cells_of(vf))[0]
        assert vf.unvalidated <= ref.unvalidated_of(ref.cells_of(plain))[0]
        worst = max(worst, vf.unvalidated)
    assert worst > 0                                                  # the far room is only seen, so the count is reported, not refused
    # and an unvalidated start or goal: the route exists and the count includes the edges at its ends
    a = g.snap(5.0, 1.5)                                              # the far room
    b = g.snap(1.0, 1.5)
    r = g.route(a.key, b.key, None, (a.x, a.y), (b.x, b.y), safety=VF)
    assert r is not None and r.unvalidated > 0


# ---- (d) off: today's weights ---------------------------------------------------------------------------------------
def test_d_off_the_plans_are_those_of_the_unchanged_weights():
    g = engine_of(random_world(8), params=RULE)
    ref = Reference(g)
    n = 0
    for a, b in pairs(g, 12, 4):
        src, dst = ref.endpoints(a, b)
        cost, path = ref.dijkstra(src, dst, None)
        for sf in (None, Safety(0.45, 0.0), Safety(0.45, 0.0, validated_first=False)):
            r = g.route(a, b, None, None, None, **({} if sf is None else {"safety": sf}))
            assert r.cost == cost and ref.cells_of(r) == path and r.unvalidated is None and r.unvalidated_m is None
        psf = Safety(R_C, 1.0 / (R_C - 0.25))
        pcost, ppath = ref.dijkstra(src, dst, psf)
        r = g.route(a, b, None, None, None, safety=psf)
        assert r.cost == pcost and ref.cells_of(r) == ppath and r.unvalidated is None
        n += 1
    assert n
    # a store without the rule has no validity plane: the flag changes nothing
    h = engine_of(random_world(8))
    for a, b in pairs(h, 8, 4):
        base = h.route(a, b)
        r = h.route(a, b, safety=VF)
        assert _cells(r) == _cells(base) and r.cost == base.cost and r.unvalidated is None


def test_d_the_weights_off_are_the_integer_lengths():
    g = engine_of(cw.two_rooms(), params=RULE)
    s = g._solved
    a, b = s.keys[0], s.keys[-1]
    g.route(a, b)
    expected = np.maximum(np.ceil(s.edges[2] / 1e-4 - 1e-6), 1.0)
    assert np.array_equal(g._cache["route_w"], expected) and "route_wvf" not in g._cache


# ---- (e) store ---------------------------------------------------------------------------------------------------
def _stored(world, params=RULE, strict=True, batch=9):
    w, way = world
    leaves = cw.drive(w, way)
    be = S.MemoryBackend()
    st = S.SceneGraphStore(be, be, verify_every=1, strict=strict)     # every save is checked against a full hash
    g = ChartGraph(params)
    v = None
    for i in range(0, len(leaves), batch):
        v = st.save("m", g, g.fold_many(leaves[i:i + batch]), v, {"n": i})
    return st, g, v


def test_e_the_val_plane_round_trips_and_incremental_saves_cover_it():
    st, g, _ = _stored(detour_world())                                # strict + verify_every=1: a missed dirty tile raises
    man = st.manifest("m")
    assert man["pipeline"] == S.PIPELINE_NEAR_VAL == "morse/2+near5"
    t = st._tile("m", man, 0, 0)
    assert t["val"].dtype == np.uint8 and t["val"].shape == (64, 64)
    h = st.load("m")                                                  # import_state verifies the stored plane against the rebuilt one
    ys, xs = h._solved.sp.ys, h._solved.sp.xs
    gy, gx = ys + h._cy0 // 2, xs + h._cx0 // 2
    wy, wx = g._solved.sp.ys + g._cy0 // 2, g._solved.sp.xs + g._cx0 // 2
    assert sorted(zip(gy.tolist(), gx.tolist())) == sorted(zip(wy.tolist(), wx.tolist()))
    hv = {(int(y), int(x)): bool(v) for y, x, v in zip(gy, gx, h._val[ys, xs])}
    wv = {(int(y), int(x)): bool(v) for y, x, v in zip(wy, wx, g._val[g._solved.sp.ys, g._solved.sp.xs])}
    assert hv == wv


def test_e_a_rule_off_store_carries_no_val_plane_and_keeps_its_token():
    st, _, _ = _stored(detour_world(), params=SceneParams())
    man = st.manifest("m")
    assert man["pipeline"] == S.PIPELINE == "morse/2"
    assert "val" not in st._tile("m", man, 0, 0)
    assert st.load("m").export_state().Val is None


def test_e_the_other_planes_of_a_rule_store_are_unchanged():
    """val is added; fn, bn and hold are what they were"""
    st, g, _ = _stored(detour_world(), params=SceneParams(near_range_m=NEAR, near_min_clearance_m=0.25))
    t = st._tile("m", st.manifest("m"), 0, 0)
    assert set(t) == {"v", "t", "b", "fn", "bn", "hold", "val", "own"}
    st2, _, _ = _stored(detour_world())
    assert set(st2._tile("m", st2.manifest("m"), 0, 0)) == {"v", "t", "b", "fn", "bn", "val", "own"}


def test_e_the_checker_accepts_near5_and_an_older_token_is_refused_clearly(monkeypatch):
    import importlib.util
    import pathlib
    spec = importlib.util.spec_from_file_location("sg_check", pathlib.Path(__file__).resolve().parents[3] / "tools" / "scene_graph_check_generation.py")
    chk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(chk)
    st, g, _ = _stored(detour_world())
    r = chk.check_map(st, "m")
    assert r["problems"] == [] and r["pipeline"] == "morse/2+near5"
    # a store written under the earlier token: the checker names it, load refuses it with the reason, the params hash differs
    for old in ("morse/2+near3", "morse/2+near4"):
        be = S.MemoryBackend(); st_old = S.SceneGraphStore(be, be)
        gg = ChartGraph(RULE)
        gg.fold_many(cw.drive(*detour_world())[:9])
        with monkeypatch.context() as m:
            m.setattr(S, "PIPELINE_NEAR_VAL", old)
            st_old.save("m", gg, gg.fold_many([]), None, {"n": 1})
        assert st_old.manifest("m")["pipeline"] == old
        with pytest.raises(ValueError, match=r"\+near5 adds the validated-node plane `val`.*rebuilt"):
            st_old.load("m")
        rep = chk.check_map(st_old, "m")
        assert any("pipeline %s != morse/2+near5" % old in p and "validity plane" in p for p in rep["problems"])
        assert st_old.meta("m")["params_hash"] != S.SceneGraphStore.params_hash(gg.params)
    # an engine state with the rule but without val still resumes (val is derived); a wrong stored val is a state mismatch
    stt = g.export_state()
    stt.Val = ~stt.Val
    with pytest.raises(S.StateMismatch, match="validity plane"):
        ChartGraph(RULE).import_state(stt)


def test_e_the_worker_rebuilds_a_rule_map_on_the_new_token():
    """the params hash carries the token: every stored rule-on map (near3 / near4) differs from what the worker expects now, a rule-off map does not"""
    on = S.SceneGraphStore.params_hash(RULE)
    from packages.scene_graph.store_codec import params_hash
    from packages.scene_graph.engine import _state_params
    d = _state_params(RULE)
    assert on == params_hash(d, "morse/2+near5")
    assert on != params_hash(d, "morse/2+near3") and on != params_hash(_state_params(SceneParams(near_range_m=NEAR, near_min_clearance_m=0.25)), "morse/2+near4")
    assert S.SceneGraphStore.params_hash(SceneParams()) == params_hash(_state_params(SceneParams()), "morse/2")


# ---- (f) tile readers ---------------------------------------------------------------------------------------------
def test_f_tile_readers_equal_the_engine_on_validity_at_a_tile_edge():
    from packages.services.mission_planner.lazy_plan import PlanGraph
    shifted = cw.drive(*detour_world())
    for lf in shifted:
        lf.x += 6.37; lf.origin = (lf.origin[0] + 6.37, lf.origin[1])           # the wall and the doors straddle tile edges (6.4 m)
    be = S.MemoryBackend(); st = S.SceneGraphStore(be, be)
    g = ChartGraph(RULE)
    v = None
    for i in range(0, len(shifted), 9):
        v = st.save("m", g, g.fold_many(shifted[i:i + 9]), v, {"n": i})
    lazy = PlanGraph(st.view("m"), max_tiles=10 ** 6)
    man = st.manifest("m")
    tiles = sorted(tuple(int(q) for q in n.split(",")) for n in man["tiles"])
    assert len({t[0] for t in tiles}) >= 2
    key, ch, ys, xs, vld = lazy._cells(tiles)
    sp = g._solved.sp
    want = {(int(y) + g._cy0 // 2, int(x) + g._cx0 // 2): bool(g._val[y, x]) for y, x in zip(sp.ys, sp.xs)}
    got = {(int(y), int(x)): bool(v) for y, x, v in zip(ys, xs, vld)}
    assert got == want and 0 < sum(want.values()) < len(want)
    edge = [c for c in want if c[1] % 64 in (0, 63) or c[0] % 64 in (0, 63)]
    assert edge and any(want[c] for c in edge) and not all(want[c] for c in edge)


def test_f_a_store_without_the_rule_reads_every_node_as_validated():
    from packages.services.mission_planner.lazy_plan import PlanGraph
    st, _, _ = _stored(detour_world(), params=SceneParams())
    lazy = PlanGraph(st.view("m"), max_tiles=10 ** 6)
    tiles = sorted(tuple(int(q) for q in n.split(",")) for n in st.manifest("m")["tiles"])
    assert lazy._cells(tiles)[4].all() and not lazy._has_val       # nothing to read: every node counts as validated


# ---- (g) the bound ------------------------------------------------------------------------------------------------
def test_g_the_float64_bound_on_a_117k_cell_synthetic_graph():
    n = 117_000
    w_sum = n * (2 * 1000 + 2 * 1415) * 1.0              # 4 edges a node, two orthogonal and two diagonal, unpriced
    p = validated.penalty(w_sum, n, 0.0)
    assert p * (n + 1) + w_sum < validated.EXACT
    tmax = float(np.ceil(5.0 * math.pi / 1e-4))           # the largest turn price Safety allows (5 m / rad)
    p = validated.penalty(w_sum, n, tmax)
    assert p * (n + 1) + w_sum < validated.EXACT / 3      # the bound still holds, with a factor 3 to spare, at the largest turn price
    p = validated.penalty(10 * w_sum, n, tmax)            # and with every edge priced ten times its length
    assert p * (n + 1) + 10 * w_sum < validated.EXACT
