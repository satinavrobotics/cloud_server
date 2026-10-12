"""Optimisations that must not change what the engine computes: each is checked against the plain definition it replaces."""
import copy

import numpy as np
import pytest
from scipy import ndimage

from packages.scene_graph import chart_solver as L
from packages.scene_graph.api import BBox, SceneParams
from packages.scene_graph.engine import ChartGraph, _squares

from . import chart_worlds as cw
from .test_growth import DOWN_RIGHT, UP_LEFT

pytestmark = pytest.mark.unit
BIG = L.BIG


def _batches(world_fn, way, batch=3, step=0.4, win=40):
    w, _ = world_fn()
    return w, cw.drive(w, way, step=step, win=win), batch


@pytest.mark.parametrize("way", [UP_LEFT, DOWN_RIGHT], ids=["up_left", "down_right"])
def test_local_erosion_is_the_full_erosion_after_every_batch(way):
    w, leaves, batch = _batches(cw.pillar_room, way)
    g = ChartGraph(SceneParams())
    sqs = _squares(g.params.robot_half_side)
    shapes = set()
    for i in range(0, len(leaves), batch):
        g.fold_many(leaves[i:i + batch])
        free = (g._V != 255) & (g._V < 100)
        full = np.zeros_like(free)
        for st in sqs:
            full |= ndimage.binary_erosion(free, structure=st, border_value=0)
        shapes.add(g._V.shape)
        assert (g._ecache[0] == free).all() and (g._ecache[1] == full).all(), i
    assert len(shapes) > 1                                    # the canvas did grow


def _hall(k=5, rw=3.0, rh=3.0, door=0.9):
    """k rooms in a row joined by aligned doors, driven along the door line"""
    W, H = k * rw + 0.4, rh + 0.4
    w = cw._blank(W, H)
    for i in range(1, k):
        x = 0.2 + i * rw
        cw._rect(w, x - 0.075, 0.2, x + 0.075, H - 0.2, 100)
        cw._rect(w, x - 0.075, H / 2 - door / 2, x + 0.075, H / 2 + door / 2, 0)
    return w, [(0.8, H / 2), (W - 0.8, H / 2)]


def test_width_cache_is_exact_and_notices_a_changed_cell():
    w, way = _hall()
    leaves, batch = cw.drive(w, way, step=0.5, win=60), 2
    g = ChartGraph(SceneParams())
    reused = 0
    for i in range(0, len(leaves), batch):
        before = set(g._wcache)
        g.fold_many(leaves[i:i + batch])
        s = g._solved
        if s is None: continue
        reused += len(before & set(g._wcache))
        for l in s.links:
            assert l["width"] == _widest_union_find(s.sp, s.full, s.A, l["a"], l["b"])
    assert reused > 0                                         # untouched pairs are served from the memo
    s = g._solved
    l = s.links[0]
    a, b = l["a"], l["b"]
    base = g._width(s.sp, s.full, s.A, a, b, {})
    sp2 = copy.copy(s.sp)
    cells = np.nonzero((s.full == a) | (s.full == b))[0]
    sp2.clr = s.sp.clr.copy(); sp2.clr[cells[len(cells) // 2]] += 0.05
    before = dict(g._wcache); new = {}
    w2 = g._width(sp2, s.full, s.A, a, b, new)
    assert set(new) - set(before)                             # the changed clearance is a new key
    assert w2 == g._widest_strict(sp2, s.full, s.A, a, b) and base == l["width"]


def _widest_union_find(sp, full, A, a, b):
    """the definition the maximum-spanning-tree form replaced: add cells by decreasing clearance, diagonal steps need both orthogonal cells"""
    cells = np.nonzero((full == a) | (full == b))[0]
    order = cells[np.argsort(-sp.clr[cells], kind="stable")]
    par = {}
    ys, xs, idx, H, W = sp.ys, sp.xs, sp.idx, sp.H, sp.W

    def find(x):
        while par[x] != x:
            par[x] = par[par[x]]; x = par[x]
        return x

    def union(u, v):
        ru, rv = find(u), find(v)
        if ru != rv: par[ru] = rv

    def at(y, x):
        if 0 <= y < H and 0 <= x < W:
            i = int(idx[y, x]); return i if i in par else None
        return None
    ia, ib = int(A[a]), int(A[b])
    for u in order.tolist():
        par[u] = u; y, x = int(ys[u]), int(xs[u])
        for dy, dx in ((0, 1), (0, -1), (1, 0), (-1, 0)):
            v = at(y + dy, x + dx)
            if v is not None: union(u, v)
        for dy in (-1, 1):
            for dx in (-1, 1):
                o = at(y + dy, x + dx); p = at(y + dy, x); q = at(y, x + dx)
                if o is not None and p is not None and q is not None:
                    union(u, o); union(p, q)
        if ia in par and ib in par and find(ia) == find(ib):
            return 2.0 * float(sp.clr[u])
    return 0.0


@pytest.mark.parametrize("name", ["pillar_room", "ring", "island", "two_rooms", "corridor"])
def test_widest_path_by_spanning_tree_equals_the_union_find_definition(name):
    w, way = getattr(cw, name)()
    g = ChartGraph(SceneParams())
    g.fold_many(cw.drive(w, way))
    s = g._solved
    k = len(s.A)
    n_pairs = 0
    for a in range(k):
        for b in range(a + 1, k):                      # every pair, adjacent or not
            assert g._widest_strict(s.sp, s.full, s.A, a, b) == _widest_union_find(s.sp, s.full, s.A, a, b), (name, a, b)
            n_pairs += 1
    assert n_pairs > 0 or k < 2


def _reference_resim(s, A, S, own, rnd):
    """the round-by-round definition (every drive of every anchor tested in every round), the code `resim` replaced"""
    N = s.N
    own[S] = -1; rnd[S] = BIG
    for j, a in enumerate(A): own[a] = j; rnd[a] = 0
    own_e = np.append(own, -2); rnd_e = np.append(rnd, BIG)
    parts = []
    for j, a in enumerate(A):
        r_ = s.row(a)
        if not len(r_.T): continue
        pos = np.searchsorted(r_.T, S); pos[pos >= len(r_.T)] = 0
        hit = (r_.T[pos] == S) & (S != a)
        if not hit.any(): continue
        pj = pos[hit]
        nl = np.maximum(np.abs(s.ys[S[hit]] - s.ys[a]), np.abs(s.xs[S[hit]] - s.xs[a]))
        o_ = np.argsort(nl, kind='stable'); pj = pj[o_]
        parts.append([j, S[hit][o_], r_.SEG[pj], r_.P[pj], r_.Q[pj], r_.D[pj], nl[o_]])
    if not parts: return [], np.zeros(0, np.int64)
    invo = [A[p[0]] for p in parts]
    rd_ = np.zeros(N + 1, bool)
    for part in parts:
        rd_[part[2].ravel()] = True; rd_[part[3].ravel()] = True; rd_[part[4].ravel()] = True; rd_[part[1]] = True
    reads = np.nonzero(rd_[:N])[0]
    tmax = 0
    for part in parts:
        for arr in (part[2], part[3], part[4]):
            v_ = rnd_e[arr]; v_ = v_[v_ < BIG]
            if len(v_): tmax = max(tmax, int(v_.max()))
    bestD = np.full(N + 1, np.inf); bestJ = np.full(N + 1, -1, np.int64)
    t = 0
    while True:
        t += 1
        touched = []
        for part in parts:
            j, cells, SEG, P, Q, Dj, nl = part
            if not len(cells): continue
            hi = int(np.searchsorted(nl, t, side='right'))
            if hi == 0: continue
            cs_, SEGh, Ph, Qh, Dh = cells[:hi], SEG[:hi], P[:hi], Q[:hi], Dj[:hi]
            oseg = own_e[SEGh]; mine = oseg == j
            ready = (mine & (rnd_e[SEGh] < t)).all(1)
            dead = ((oseg >= 0) & ~mine).any(1)
            if Ph.shape[1]:
                op = own_e[Ph]; oq = own_e[Qh]
                ready &= (((op == j) & (rnd_e[Ph] < t)) | ((oq == j) & (rnd_e[Qh] < t))).all(1)
            ready &= own_e[cs_] < 0
            if ready.any():
                rc = cs_[ready]; rd = Dh[ready]
                m_ = rd < bestD[rc]; rc = rc[m_]; rd = rd[m_]
                bestD[rc] = rd; bestJ[rc] = j; touched.append(rc)
            keep_h = (~dead) & (own_e[cs_] < 0)
            if not keep_h.all():
                kf = np.ones(len(cells), bool); kf[:hi] = keep_h
                part[1], part[2], part[3], part[4], part[5], part[6] = cells[kf], SEG[kf], P[kf], Q[kf], Dj[kf], nl[kf]
        if not touched:
            if t > tmax: break
            continue
        cs = np.unique(np.concatenate(touched))
        own_e[cs] = bestJ[cs]; rnd_e[cs] = t
        bestD[cs] = np.inf; bestJ[cs] = -1
    own[:] = own_e[:N]; rnd[:] = rnd_e[:N]
    return invo, reads


@pytest.mark.parametrize("name", ["pillar_room", "ring", "island"])
def test_resim_equals_the_round_by_round_definition(name):
    w, way = getattr(cw, name)()
    g = ChartGraph(SceneParams())
    g.fold_many(cw.drive(w, way))
    s = g._solved
    sp, A = s.sp, list(s.A)
    own0 = s.own.astype(np.int32).copy()
    rnd0 = g._track.rnd_img[sp.ys, sp.xs].astype(np.int32)
    rng = np.random.default_rng(7)
    N = sp.N
    for trial in range(25):
        B = list(A)
        if trial % 3 == 1 and len(B) > 1: B.pop(int(rng.integers(len(B))))            # a removed anchor
        if trial % 3 == 2: B.append(int(rng.integers(N)))                              # a hypothetical anchor
        B = list(dict.fromkeys(B))
        S = np.unique(rng.integers(0, N, int(rng.integers(5, max(6, N // 3))))).astype(np.int64)
        o1, r1 = own0.copy(), rnd0.copy(); o2, r2 = own0.copy(), rnd0.copy()
        if len(o1) and len(B):
            o1[o1 >= len(B)] = -1; o2[o2 >= len(B)] = -1
        sp.row_cache = None
        inv_ref, reads_ref = _reference_resim(sp, B, S, o1, r1)
        inv = sp.resim(B, S, o2, r2)
        assert (o1 == o2).all() and (r1 == r2).all(), trial
        assert list(inv) == list(inv_ref)
        if len(reads_ref): assert (sp._reads == reads_ref).all()


def _state(g):
    """{chart key: (anchor map position, frozenset of its cells in map coordinates)} from the public view only"""
    v = g.query(BBox(-50, -50, 50, 50), raster=True)
    r = v.raster
    out = {}
    for i, k in enumerate(r.keys, start=1):
        ys, xs = np.nonzero(r.index == i)
        out[k] = frozenset(zip((np.round(r.origin[0] + (xs + 0.5) * r.step, 3)).tolist(), (np.round(r.origin[1] + (ys + 0.5) * r.step, 3)).tolist()))
    pos = {c.key: (round(c.anchor[0], 3), round(c.anchor[1], 3)) for c in v.charts}
    return {k: (pos[k], out[k]) for k in out}


def test_dirty_changed_is_exact_when_the_canvas_grows_to_the_left_and_top():
    w, leaves, batch = _batches(cw.pillar_room, UP_LEFT)
    g = ChartGraph(SceneParams())
    old = {}
    grew = 0
    for i in range(0, len(leaves), batch):
        had, c0 = g._V is not None, (g._cx0, g._cy0)
        d = g.fold_many(leaves[i:i + batch])
        grew += int(had and (g._cx0, g._cy0) != c0)
        new = _state(g)
        truth = {k for k, v in new.items() if old.get(k) != v}
        assert set(d.changed) == truth, (i, sorted(d.changed), sorted(truth))
        old = new
    assert grew > 0
