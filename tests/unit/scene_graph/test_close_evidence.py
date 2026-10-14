"""Close-evidence rule (SceneParams.near_range_m): a cell reported blocked stays blocked until a FREE report from a capture
pose within range is as new (B > Fn). Off by default and then bit-identical to the latest-value fold."""
import itertools

import numpy as np
import pytest

from packages.scene_graph import store as S
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
