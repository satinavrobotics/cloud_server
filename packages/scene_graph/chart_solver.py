"""The chart solver: canonical, local definition of ownership and of the solve order (verified reference: T2 + sight cap).

  D(a,x)    = octile distance (h * (max + (sqrt2-1) * min) of the cell offsets) for every x that a sees: with cost = length the digital drive
              a->x is itself a shortest 8-neighbour path, so no Dijkstra is needed anywhere in the solve.
  ownership by ROUNDS (no visiting order):  own_0 = anchors.  In round t every unowned cell x joins the nearest (D, then index) anchor j
              whose whole drive j->x (minus x) is owned by j after round t-1, with the diagonal rule (an orthogonal cell of every diagonal step is owned by j).
              A function of (F, A) only; a cell reads only cells on drives that end in it.
  solve     = construct (add at the largest-clearance cell of the lowest-numbered pocket / off-band anchor) then descend: anchors in cell-index order,
              each tries to be removed, else moved to the band medoid; a step is taken iff the set stays feasible and strictly lowers
              (|A|, #dependents, sum of D over owned cells) lexicographically.  Locally minimal for single removals / moves, in that order.
  locality  = a step re-simulates only the cells that can read a changed cell: the closure of (cells seen by the changed anchors) under 'reads'."""
import math
import numpy as np
from scipy import ndimage
from scipy.sparse.csgraph import connected_components
from scipy.sparse import coo_matrix

BIG = 1 << 30
SQ2 = math.sqrt(2.0) - 1.0


def octile(dy, dx, h):
    a = np.abs(dy); b = np.abs(dx)
    return h * (np.maximum(a, b) + SQ2 * np.minimum(a, b))


class Row:
    """everything one anchor cell contributes: the cells it sees, and the tables of its digital drives"""
    __slots__ = ('a', 'T', 'SEG', 'P', 'Q', 'rect', 'D', 'dep', 'chg')


class Local:
    def __init__(s, nav, clr, h=0.1, r=0.25, mult=1.5, eps=None, rows_cache=None, img_rows=None, delta=None, sight_range=None):
        s.h, s.r = h, r
        # sight_range (m): an anchor sees a cell only within this Euclidean distance (centre to centre); None = unbounded.
        # It must reach a ball of the place scale, else an anchor could not even see the ball it was put in for.
        assert sight_range is None or sight_range >= mult * r, 'sight_range below the place scale'
        s.cap2 = None if sight_range is None else (sight_range / h) ** 2
        s.scale = mult * r
        s.eps = (h / s.scale) if eps is None else eps
        s.band = 1.0 - s.eps
        s.nav = nav; s.H, s.W = nav.shape
        s.ys, s.xs = np.nonzero(nav); s.N = N = len(s.ys)
        s.idx = -np.ones(nav.shape, np.int64); s.idx[s.ys, s.xs] = np.arange(N)
        s.idxp = np.where(s.idx >= 0, s.idx, N)
        s.clrI = clr; s.clr = clr[s.ys, s.xs]
        s.flat = s.ys * s.W + s.xs                       # image position of every cell (stable across frames)
        lab = np.zeros(nav.shape, np.int32)
        l4, n4 = ndimage.label(nav, structure=np.ones((3, 3)))   # F-components for the lattice graph: 8-neighbour with the corner rule is a subset; use 4-conn labels of drives below
        # components under the drive rule (8-neighbour, no corner cutting): build the edge list once, vectorised
        rows, cols = [], []
        for dy, dx in ((0, 1), (1, 0), (1, 1), (1, -1)):
            y2, x2 = s.ys + dy, s.xs + dx
            ok = (y2 >= 0) & (y2 < s.H) & (x2 >= 0) & (x2 < s.W)
            ok[ok] &= nav[y2[ok], x2[ok]]
            a = np.nonzero(ok)[0]; b = s.idx[y2[a], x2[a]]
            if dy and dx:
                keep = nav[s.ys[a], x2[a]] & nav[y2[a], s.xs[a]]; a, b = a[keep], b[keep]
            rows.append(a); cols.append(b)
        a = np.concatenate(rows); b = np.concatenate(cols)
        s.ncomp, s.comp = connected_components(coo_matrix((np.ones(len(a)), (a, b)), shape=(N, N)), directed=False)
        s.compmax = np.zeros(s.ncomp); np.maximum.at(s.compmax, s.comp, s.clr)
        s.rows = {} if rows_cache is None else rows_cache       # anchor cell -> Row (this frame)
        s.img_rows = {} if img_rows is None else img_rows       # (y,x) -> rows of the previous frame in image positions
        s.delta = delta                                          # bool image: cells whose F membership changed since the previous frame
        s.delta_flat = None if delta is None else np.nonzero(delta.ravel())[0]
        s.flat2idx = np.full(nav.size + 1, N, np.int64); s.flat2idx[s.flat] = np.arange(N)   # image position -> cell (N = not in F)
        s._pick = {}
        s.stats = dict(rows=0, resim=0, resim_cells=0, closure_iter=0, trials=0)

    def _near(s, a, T):
        """mask of the cells T within the sight range of anchor cell a (all True when there is no cap)"""
        if s.cap2 is None: return np.ones(len(T), bool)
        dy = (s.ys[T] - s.ys[a]).astype(np.int64); dx = (s.xs[T] - s.xs[a]).astype(np.int64)
        return dy * dy + dx * dx <= s.cap2

    # ---- digital drive
    def rast(s, y0, x0, y1, x1):
        dy = y1 - y0; dx = x1 - x0
        n = np.maximum(np.abs(dy), np.abs(dx))
        k = np.arange(int(n.max()) + 1)[None, :]
        kk = np.minimum(k, n[:, None]); nn = np.maximum(n[:, None], 1)
        off = lambda dd: np.sign(dd)[:, None] * ((2 * np.abs(dd)[:, None] * kk + nn) // (2 * nn))
        return y0[:, None] + off(dy), x0[:, None] + off(dx), n

    @staticmethod
    def corners(rr, cc, M):
        dg = (rr[:, 1:] != rr[:, :-1]) & (cc[:, 1:] != cc[:, :-1])
        a = M[rr[:, :-1], cc[:, 1:]] | ~dg
        b = M[rr[:, 1:], cc[:, :-1]] | ~dg
        return (a & b).all(1)

    def _chunks(s, m, L):
        step = max(1, int(3_000_000 // max(L, 1)))
        for i in range(0, m, step): yield slice(i, min(m, i + step))

    def drives_ok(s, y0, x0, y1, x1):
        out = np.zeros(len(y0), bool)
        for sl in s._chunks(len(y0), 128):
            rr, cc, n = s.rast(y0[sl], x0[sl], y1[sl], x1[sl])
            out[sl] = s.nav[rr, cc].all(1) & s.corners(rr, cc, s.nav)
        return out

    def row(s, a):
        r_ = s.rows.get(a)
        if r_ is not None: return r_
        key = (int(s.ys[a]), int(s.xs[a]))
        im = s.img_rows.get(key)
        if im is not None:
            if s.delta is None or not s.delta.ravel()[im['dep']].any():
                r_ = Row(); r_.a = a; r_.chg = np.zeros(0, np.int64)
                r_.T = s.flat2idx[im['T']]
                r_.SEG = s._toidx(im['SEG']); r_.P = s._toidx(im['P']); r_.Q = s._toidx(im['Q'])
                r_.rect = im['rect']; r_.dep = im['dep']
                r_.D = octile(s.ys[r_.T] - s.ys[a], s.xs[r_.T] - s.xs[a], s.h)
                s.rows[a] = r_; s.stats['rows_reused'] = s.stats.get('rows_reused', 0) + 1
                return r_
            r_ = s.row_update(a, im)
            if r_ is not None: return r_
        s.stats['rows'] += 1
        T = np.nonzero(s.comp == s.comp[a])[0]
        T = T[s._near(a, T)]
        ya = np.full(len(T), s.ys[a]); xa = np.full(len(T), s.xs[a])
        ok = np.zeros(len(T), bool)
        ymin = ymax = int(s.ys[a]); xmin = xmax = int(s.xs[a]); depf = []
        for sl in s._chunks(len(T), 128):
            rr, cc, n = s.rast(ya[sl], xa[sl], s.ys[T][sl], s.xs[T][sl])
            navp = s.nav[rr, cc]
            ok[sl] = navp.all(1) & s.corners(rr, cc, s.nav)
            first = np.cumsum(~navp, axis=1) <= 1             # cells up to and including the first blocker: what this drive depends on
            depf.append(np.unique(rr[first] * s.W + cc[first]))
            ymin = min(ymin, int(rr[first].min())); ymax = max(ymax, int(rr[first].max()))
            xmin = min(xmin, int(cc[first].min())); xmax = max(xmax, int(cc[first].max()))
        back = s.drives_ok(s.ys[T], s.xs[T], ya, xa)
        Tv = T[ok & back]
        r_ = Row(); r_.a = a; r_.T = Tv; r_.rect = (ymin, ymax, xmin, xmax); r_.chg = T
        dd = np.unique(np.concatenate(depf)) if depf else np.zeros(0, np.int64)       # forward-visible region and its first blockers
        m_ = np.zeros((s.H + 2, s.W + 2), bool); m_[dd // s.W + 1, dd % s.W + 1] = True
        m_ = ndimage.binary_dilation(m_, structure=np.ones((3, 3)))                     # the reverse drive is the same segment digitised from the other end
        yy, xx = np.nonzero(m_[1:-1, 1:-1]); r_.dep = np.sort(yy * s.W + xx)
        # tables of the drives a -> x (target replaced by the anchor, which is always its own)
        ya = np.full(len(Tv), s.ys[a]); xa = np.full(len(Tv), s.xs[a])
        rr, cc, n = s.rast(ya, xa, s.ys[Tv], s.xs[Tv])
        ci = s.idxp[rr, cc]
        ci = np.where(ci == Tv[:, None], a, ci).astype(np.int32)
        dg = (rr[:, 1:] != rr[:, :-1]) & (cc[:, 1:] != cc[:, :-1])
        p = np.where(dg, s.idxp[rr[:, :-1], cc[:, 1:]], a).astype(np.int32)
        q = np.where(dg, s.idxp[rr[:, 1:], cc[:, :-1]], a).astype(np.int32)
        r_.SEG, r_.P, r_.Q = ci, p, q
        r_.D = octile(s.ys[Tv] - s.ys[a], s.xs[Tv] - s.xs[a], s.h)
        s.rows[a] = r_
        return r_

    @staticmethod
    def _on_drive(wy, wx, uy, ux):
        """is the cell u (relative to the origin) on the integer-DDA drive from the origin to w, for every w: the rounding of rast(), inverted"""
        ay_, ax_ = np.abs(wy), np.abs(wx)
        n = np.maximum(ay_, ax_)
        xmaj = ax_ >= ay_
        k = np.where(xmaj, abs(ux), abs(uy))
        sgn_ok = np.where(xmaj, (ux == 0) | (np.sign(ux) == np.sign(wx)), (uy == 0) | (np.sign(uy) == np.sign(wy)))
        nn = np.maximum(n, 1)
        oy = np.sign(wy) * ((2 * ay_ * k + nn) // (2 * nn)); ox = np.sign(wx) * ((2 * ax_ * k + nn) // (2 * nn))
        return (k <= n) & sgn_ok & (oy == uy) & (ox == ux)

    def _toidx(s, f):
        return np.where(f < 0, s.N, s.flat2idx[np.maximum(f, 0)]).astype(np.int32)

    def _drive_tables(s, a, Tv):
        ya = np.full(len(Tv), s.ys[a]); xa = np.full(len(Tv), s.xs[a])
        rr, cc, n = s.rast(ya, xa, s.ys[Tv], s.xs[Tv])
        ci = s.idxp[rr, cc]
        ci = np.where(ci == Tv[:, None], a, ci).astype(np.int32)
        dg = (rr[:, 1:] != rr[:, :-1]) & (cc[:, 1:] != cc[:, :-1])
        p = np.where(dg, s.idxp[rr[:, :-1], cc[:, 1:]], a).astype(np.int32)
        q = np.where(dg, s.idxp[rr[:, 1:], cc[:, :-1]], a).astype(np.int32)
        return ci, p, q

    def row_update(s, a, im):
        """the row of an anchor whose old row depended on a changed cell: only the targets whose drive can pass through a changed cell are
        re-examined (a drive is a digital segment: the target lies in the thin wedge behind the changed cell as seen from the anchor)"""
        s.stats['rows_updated'] = s.stats.get('rows_updated', 0) + 1
        W = s.W
        dd = s.delta_flat[np.isin(s.delta_flat, im['dep'])]
        ay, ax = int(s.ys[a]), int(s.xs[a])
        Tc = np.nonzero(s.comp == s.comp[a])[0]
        Tc = Tc[s._near(a, Tc)]
        # exact: the targets whose digital drive (either direction), or the orthogonal cells it needs, contain a changed cell
        dy_ = dd // W; dx_ = dd % W
        cy = np.concatenate([dy_, dy_ + 1, dy_ - 1, dy_, dy_]); cx = np.concatenate([dx_, dx_, dx_, dx_ + 1, dx_ - 1])
        u = np.unique(np.stack([cy, cx], 1), axis=0)
        wy = (s.ys[Tc] - ay).astype(np.int32); wx = (s.xs[Tc] - ax).astype(np.int32)
        C = np.zeros(len(Tc), bool)
        for (uy_, ux_) in u.tolist():
            C |= s._on_drive(wy, wx, uy_ - ay, ux_ - ax)                       # a -> x
            C |= s._on_drive(-wy, -wx, uy_ - ay - wy, ux_ - ax - wx)           # x -> a: origin x, cell relative to x = (cell - a) - (x - a)
        inC = np.zeros(s.N + 1, bool); inC[Tc[C]] = True
        newf = s.flat2idx[s.delta_flat]; newf = newf[newf < s.N]
        inC[newf] = True
        Cc = np.nonzero(inC[:s.N] & (s.comp == s.comp[a]))[0]
        Cc = Cc[s._near(a, Cc)]
        if len(Cc):
            ya = np.full(len(Cc), ay); xa = np.full(len(Cc), ax)
            ok = np.zeros(len(Cc), bool); depf = []
            for sl in s._chunks(len(Cc), 128):
                rr, cc, n = s.rast(ya[sl], xa[sl], s.ys[Cc][sl], s.xs[Cc][sl])
                navp = s.nav[rr, cc]
                ok[sl] = navp.all(1) & s.corners(rr, cc, s.nav)
                first = np.cumsum(~navp, axis=1) <= 1
                depf.append(np.unique(rr[first] * W + cc[first]))
            back = s.drives_ok(s.ys[Cc], s.xs[Cc], ya, xa)
            vC = Cc[ok & back]
        else:
            vC = np.zeros(0, np.int64); depf = []
        Told = s.flat2idx[im['T']]
        tn = np.minimum(Told, s.N - 1)
        keep = (Told < s.N) & ~inC[np.minimum(Told, s.N)] & (s.comp[tn] == s.comp[a])    # still in F, same piece of free space, drive untouched
        Tk = Told[keep]
        SEGk = s._toidx(im['SEG'][keep]); Pk = s._toidx(im['P'][keep]); Qk = s._toidx(im['Q'][keep])
        def padw(x, w, fill):
            if x.shape[1] == w: return x
            return np.concatenate([x, np.full((x.shape[0], w - x.shape[1]), fill, x.dtype)], 1)
        if len(vC):
            ci, p, q = s._drive_tables(a, vC)
            Lw = max(SEGk.shape[1] if len(Tk) else 0, ci.shape[1]); Pw = max(Pk.shape[1] if len(Tk) else 0, p.shape[1])
            SEG = np.concatenate([padw(SEGk, Lw, a), padw(ci, Lw, a)]) if len(Tk) else ci
            P = np.concatenate([padw(Pk, Pw, a), padw(p, Pw, a)]) if len(Tk) else p
            Q = np.concatenate([padw(Qk, Pw, a), padw(q, Pw, a)]) if len(Tk) else q
            T = np.concatenate([Tk, vC])
        else:
            SEG, P, Q, T = SEGk, Pk, Qk, Tk
        o = np.argsort(T, kind='stable')
        r_ = Row(); r_.a = a; r_.T = T[o]; r_.SEG = SEG[o]; r_.P = P[o]; r_.Q = Q[o]
        r_.D = octile(s.ys[r_.T] - ay, s.xs[r_.T] - ax, s.h)
        dep = im['dep']
        if depf:
            dn = np.unique(np.concatenate(depf))
            m_ = np.zeros((s.H + 2, s.W + 2), bool); m_[dn // W + 1, dn % W + 1] = True
            m_ = ndimage.binary_dilation(m_, structure=np.ones((3, 3)))
            yy, xx = np.nonzero(m_[1:-1, 1:-1]); dep = np.union1d(dep, yy * W + xx)
        r_.dep = dep
        ry0, ry1, rx0, rx1 = im['rect']
        if len(dep): ry0 = min(ry0, int(dep.min() // W)); ry1 = max(ry1, int(dep.max() // W)); rx0 = min(rx0, int((dep % W).min())); rx1 = max(rx1, int((dep % W).max()))
        r_.rect = (ry0, ry1, rx0, rx1)
        # seeds: only the targets whose status or tables really changed
        if len(Cc):
            was = np.isin(Cc, Told[Told < s.N]); now = np.isin(Cc, vC)
            chg = was ^ now
            both = was & now
            if both.any():
                xs_ = Cc[both]
                fo = s.flat[xs_]; po = np.searchsorted(im['T'], fo)
                so = s._toidx(im['SEG'][po]); pp = s._toidx(im['P'][po]); qq = s._toidx(im['Q'][po])
                pn = np.searchsorted(r_.T, xs_)
                sn = r_.SEG[pn]; pn_ = r_.P[pn]; qn_ = r_.Q[pn]
                w = max(so.shape[1], sn.shape[1]); wp = max(pp.shape[1], pn_.shape[1])
                same = (padw(so, w, a) == padw(sn, w, a)).all(1) & (padw(pp, wp, a) == padw(pn_, wp, a)).all(1) & (padw(qq, wp, a) == padw(qn_, wp, a)).all(1)
                chg[np.nonzero(both)[0][~same]] = True
            r_.chg = Cc[chg]
        else:
            r_.chg = Cc
        s.rows[a] = r_
        return r_

    def export_rows(s):
        """rows used in this frame, in image positions (for the next frame)"""
        out = {}
        N = s.N
        for a, r_ in s.rows.items():
            f = np.append(s.flat, -1)
            out[(int(s.ys[a]), int(s.xs[a]))] = dict(T=s.flat[r_.T], SEG=f[r_.SEG], P=f[r_.P], Q=f[r_.Q], rect=r_.rect, dep=r_.dep)
        return out

    # ---- ownership by rounds on a set S of cells, the rest frozen
    def resim(s, A, S, own, rnd):
        """recompute (own, rnd) of the cells S (in place); cells outside S and the anchors keep theirs"""
        N = s.N
        s.stats['resim'] += 1; s.stats['resim_cells'] += len(S)
        own[S] = -1; rnd[S] = BIG
        for j, a in enumerate(A): own[a] = j; rnd[a] = 0
        own_e = np.append(own, -2); rnd_e = np.append(rnd, BIG)
        parts = []
        for j, a in enumerate(A):
            r_ = s.row(a)
            if not len(r_.T): continue
            pos = np.searchsorted(r_.T, S)
            pos[pos >= len(r_.T)] = 0
            hit = (r_.T[pos] == S) & (S != a)
            if not hit.any(): continue
            pj = pos[hit]
            nl = np.maximum(np.abs(s.ys[S[hit]] - s.ys[a]), np.abs(s.xs[S[hit]] - s.xs[a]))      # drive length: a cell at distance d cannot be assigned before round d
            o_ = np.argsort(nl, kind='stable'); pj = pj[o_]
            parts.append([j, S[hit][o_], r_.SEG[pj], r_.P[pj], r_.Q[pj], r_.D[pj], nl[o_]])
        if not parts: s._reads = np.zeros(0, np.int64); return []
        invo = [A[p[0]] for p in parts]
        rd_ = np.zeros(N + 1, bool)
        for part in parts:
            rd_[part[2].ravel()] = True; rd_[part[3].ravel()] = True; rd_[part[4].ravel()] = True; rd_[part[1]] = True
        s._reads = np.nonzero(rd_[:N])[0]
        tmax = 0                                      # frozen cells become readable at their own round: do not stop before the last one that is read
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
                oseg = own_e[SEGh]
                mine = oseg == j
                ready = (mine & (rnd_e[SEGh] < t)).all(1)
                dead = ((oseg >= 0) & ~mine).any(1)               # a cell of the drive belongs to another anchor: this drive can never be ready
                if Ph.shape[1]:
                    op = own_e[Ph]; oq = own_e[Qh]
                    ready &= (((op == j) & (rnd_e[Ph] < t)) | ((oq == j) & (rnd_e[Qh] < t))).all(1)
                ready &= own_e[cs_] < 0
                if ready.any():
                    rc = cs_[ready]; rd = Dh[ready]
                    m_ = rd < bestD[rc]
                    rc = rc[m_]; rd = rd[m_]
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
        return invo

    def readers(s, A, X):
        """cells that have a cell of X on a drive (or orthogonal cell of a drive) of some anchor that sees them.
        Only anchors whose dependency set meets X can have such a cell (every drive cell of a visible target is in it)."""
        if not len(X): return np.zeros(0, np.int64)
        out = []
        Xm = np.zeros(s.H * s.W, bool); Xm[s.flat[X]] = True
        for a in A:
            r_ = s.row(a)
            if not len(r_.T) or not Xm[r_.dep].any(): continue
            h = np.isin(r_.SEG, X).any(1)
            if r_.P.shape[1]: h |= np.isin(r_.P, X).any(1) | np.isin(r_.Q, X).any(1)
            if h.any(): out.append(r_.T[h])
        return np.unique(np.concatenate(out)) if out else np.zeros(0, np.int64)

    def settle(s, A, own_base, rnd_base, S0, X0=None, local=True):
        """exact ownership under A: own_base/rnd_base are the state before the change with the change applied structurally
        (removed anchors' cells unowned, anchor indices remapped); S0 = cells whose candidates changed; X0 = cells whose frozen value changed"""
        N = s.N
        S = np.unique(S0)
        if not local: S = np.arange(N, dtype=np.int64)
        X0 = np.zeros(0, np.int64) if X0 is None else np.unique(X0)
        invo = set()
        while True:
            s.stats['closure_iter'] += 1
            own = own_base.copy(); rnd = rnd_base.copy()
            invo |= set(s.resim(A, S, own, rnd)); reads = s._reads
            ch = (own[S] != own_base[S]) | (rnd[S] != rnd_base[S])
            X = np.union1d(S[ch], X0)
            R = s.readers(A, X)
            new = np.setdiff1d(R, S, assume_unique=False)
            if not len(new): s._reads = reads; return own, rnd, S, invo
            S = np.union1d(S, new)

    # ---- pockets: unowned cells (8-connected) holding a ball of radius s
    def pocket_from(s, own, seeds):
        """components of unowned cells containing a seed; returns the list of (min cell, argmax clr cell) of those holding a ball >= scale"""
        seen = set(); out = []
        s._pk_seen = seen
        for c0 in seeds.tolist():
            if own[c0] >= 0 or c0 in seen: continue
            comp = [c0]; seen.add(c0); st = [c0]; best = c0
            while st:
                c = st.pop(); y, x = int(s.ys[c]), int(s.xs[c])
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        yy, xx = y + dy, x + dx
                        if 0 <= yy < s.H and 0 <= xx < s.W:
                            n_ = int(s.idx[yy, xx])
                            if n_ >= 0 and n_ not in seen and own[n_] < 0:
                                seen.add(n_); st.append(n_); comp.append(n_)
            comp = np.array(comp)
            if s.clr[comp].max() >= s.scale:
                mx = comp[s.clr[comp] == s.clr[comp].max()].min()
                out.append((int(comp.min()), int(mx)))
        return sorted(out)

    # ---- anchor band of one chart (memoised on the chart's cells)
    def pick(s, mine):
        key = mine.tobytes()
        v = s._pick.get(key)
        if v is None:
            peak = s.clr[mine].max()
            cand = mine[s.clr[mine] >= s.band * peak - 1e-9]
            mk = np.zeros((s.H, s.W), bool); mk[s.ys[mine], s.xs[mine]] = True
            ym, xm = s.ys[mine], s.xs[mine]
            keep = []
            for y in cand:
                d2 = (ym - s.ys[y]) ** 2 + (xm - s.xs[y]) ** 2
                order = np.argsort(-d2, kind='stable'); ok = True; pos = 0; step = 16
                while ok and pos < len(order):
                    sel = order[pos:pos + step]; pos += step; step *= 2
                    yy = np.full(len(sel), s.ys[y]); xx = np.full(len(sel), s.xs[y])
                    rr, cc, n = s.rast(yy, xx, ym[sel], xm[sel])
                    g = mk[rr, cc].all(1) & s.corners(rr, cc, s.nav)
                    dg = (rr[:, 1:] != rr[:, :-1]) & (cc[:, 1:] != cc[:, :-1])
                    a_ = mk[rr[:, :-1], cc[:, 1:]] | ~dg; b_ = mk[rr[:, 1:], cc[:, :-1]] | ~dg
                    g &= (a_ | b_).all(1)
                    if not g.all(): ok = False; break
                    if not s.drives_ok(ym[sel], xm[sel], yy, xx).all(): ok = False
                if ok: keep.append(int(y))
            band = np.array(keep, int)
            if not len(band): v = (None, band)
            else:
                dmat = octile(s.ys[band][:, None] - ym[None, :], s.xs[band][:, None] - xm[None, :], s.h)
                v = (int(band[np.argmin(dmat.mean(1))]), band)
            s._pick[key] = v
        return v

    # ---- state of a solution
    def full_state(s, A):
        own = -np.ones(s.N, np.int32); rnd = np.full(s.N, BIG, np.int32)
        own, rnd, _, _ = s.settle(A, own, rnd, np.arange(s.N), local=False)
        return own, rnd

    def objective(s, A, own, cells=None):
        """(#dependents among cells of anchored components, sum of D over owned cells), over `cells` (default all)"""
        if cells is None: cells = np.arange(s.N)
        anchored = np.zeros(s.ncomp, bool)
        if len(A): anchored[s.comp[np.array(A, int)]] = True
        o = own[cells]
        dep = int(((o < 0) & anchored[s.comp[cells]]).sum())
        m = o >= 0
        arr = np.array(A, int)
        d = octile(s.ys[cells][m] - s.ys[arr[o[m]]], s.xs[cells][m] - s.xs[arr[o[m]]], s.h)
        return dep, float(d.sum())

    def violation(s, A, own, seeds=None, anchors=None):
        """first violation in canonical order, or None.  ('pocket', cell) | ('band', cell)"""
        if seeds is None: seeds = np.nonzero(own < 0)[0]
        pk = s.pocket_from(own, seeds)
        if pk: return ('pocket', pk[0][1])
        inA = set(A)
        order = range(len(A)) if anchors is None else sorted(anchors, key=lambda j: A[j])
        for j in sorted(order, key=lambda j: A[j]):
            mine = np.nonzero(own == j)[0]
            b, band = s.pick(mine)
            if b is None or A[j] not in set(band.tolist()):
                for c in mine[np.argsort(-s.clr[mine], kind='stable')]:
                    if int(c) not in inA: return ('band', int(c))
        return None

    def apply(s, A, own, rnd, op, local=True):
        """(A', own', rnd', changed cells) after one operation: ('add', c) | ('remove', j) | ('move', j, b)"""
        A = list(A); own_b = own.copy(); rnd_b = rnd.copy(); X0 = []
        if op[0] == 'add':
            c = op[1]; S0 = s.row(c).T
            A.append(c); own_b[c] = len(A) - 1; rnd_b[c] = 0; X0 = [c]
        elif op[0] == 'remove':
            j = op[1]; a = A[j]
            S0 = np.union1d(s.row(a).T, [a])
            own_b[own_b == j] = -1
            own_b[own_b > j] -= 1
            rnd_b[a] = BIG; own_b[a] = -1; X0 = [a]
            del A[j]
        else:
            j, b = op[1], op[2]; a = A[j]
            S0 = np.union1d(np.union1d(s.row(a).T, s.row(b).T), [a, b])
            own_b[own_b == j] = -1
            rnd_b[a] = BIG; own_b[a] = -1
            A[j] = b; own_b[b] = j; rnd_b[b] = 0; X0 = [a, b]
        o2, r2, S, invo = s.settle(A, own_b, rnd_b, S0, X0=np.array(X0, np.int64), local=local)
        s._apply_reads = s._reads
        return A, o2, r2, S, invo

    def feasible_after(s, A2, own2, S, own_old=None):
        """F1/F2 of the changed neighbourhood only (the old state was feasible): pockets touching a cell of S, and the band of every chart touching S"""
        seeds = S[own2[S] < 0]
        if s.pocket_from(own2, seeds): return False
        touched = np.unique(own2[S]); touched = touched[touched >= 0]
        for j in touched.tolist():
            mine = np.nonzero(own2 == j)[0]
            b, band = s.pick(mine)
            if b is None or A2[j] not in set(band.tolist()): return False
        return True

    # ---- construct and descend, canonical order
    def construct(s, A0=(), local=True):
        A = []
        for a in A0:
            if a not in A: A.append(int(a))
        own, rnd = s.full_state(A) if len(A) else (-np.ones(s.N, np.int32), np.full(s.N, BIG, np.int32))
        for _ in range(s.N + 1):
            v = s.violation(A, own)
            if v is None: return A, own, rnd
            A, own, rnd, _, _ = s.apply(A, own, rnd, ('add', v[1]), local=local)
        raise RuntimeError('construct did not terminate')

    def descend(s, A, own, rnd, queue=None, local=True, tried=None):
        """worklist, smallest chart first (ties by cell index).  tried[a] = (anchors the trial used, cells it read): a commit re-queues exactly
        the anchors whose trial used a changed anchor or read a cell whose owner or round changed."""
        A = list(A)
        pending = set(A) if queue is None else set(queue)
        tried = {} if tried is None else tried
        while pending:
            cnt = np.bincount(own[own >= 0], minlength=len(A))
            sz = {p_: int(cnt[A.index(p_)]) for p_ in pending if p_ in A}
            if not sz: break
            a = min(sz, key=lambda p_: (sz[p_], p_))
            pending.discard(a)
            j = A.index(a); s.stats['trials'] += 1
            done = None; used = {a}; reads = [np.zeros(0, np.int64)]
            if len(A) > 1:
                A2, o2, r2, S, invo = s.apply(A, own, rnd, ('remove', j), local=local); used |= invo; reads.append(s._apply_reads); reads.append(S)
                ok = s.feasible_after(A2, o2, S)
                reads.append(np.fromiter(s._pk_seen, np.int64, len(s._pk_seen)))
                if ok: done = (A2, o2, r2, S)
            if done is None:
                mine = np.nonzero(own == j)[0]
                b, band = s.pick(mine)
                reads.append(mine)
                if b is not None and b != a and b not in A:
                    used.add(b)
                    A2, o2, r2, S, invo = s.apply(A, own, rnd, ('move', j, b), local=local); used |= invo; reads.append(s._apply_reads); reads.append(S)
                    ok = s.feasible_after(A2, o2, S)
                    reads.append(np.fromiter(s._pk_seen, np.int64, len(s._pk_seen)))
                    if ok:
                        aff = np.union1d(S, np.nonzero((own == j) | (o2 == j))[0])
                        old = s.objective(A, own, aff); new = s.objective(A2, o2, aff)
                        reads.append(aff)
                        if new < (old[0], old[1] - 1e-9):
                            done = (A2, o2, r2, S)
            if done is None:
                tried[a] = (used, np.unique(np.concatenate(reads)))
                continue
            A2, o2, r2, S = done
            tried.pop(a, None)
            if getattr(s, 'log', None) is not None: s.log.append(('commit', a, len(A2)))
            old_owner = np.where(own[S] >= 0, np.array(A, np.int64)[np.maximum(own[S], 0)], -1)
            new_owner = np.where(o2[S] >= 0, np.array(A2, np.int64)[np.maximum(o2[S], 0)], -1)
            chg = np.zeros(s.N + 1, bool)
            chg[S[(old_owner != new_owner) | (rnd[S] != r2[S])]] = True
            gone_or_new = (set(A) ^ set(A2))                  # the anchor removed or moved away, the anchor moved to
            for g_ in gone_or_new: chg[g_] = True
            A, own, rnd = A2, o2, r2
            for k, (uk, rk) in list(tried.items()):
                if k not in A or k in pending: continue
                if (uk & gone_or_new) or chg[rk].any(): pending.add(k); tried.pop(k, None)
            for ak in A:                                   # anchors without a trial since their own sight changed
                if ak not in tried and ak not in pending: pending.add(ak)
        return A, own, rnd, tried

    def solve(s, prev_cells=(), local=True):
        A, own, rnd = s.construct(prev_cells, local=local)
        A, own, rnd, _ = s.descend(A, own, rnd, local=local)
        return A, own, rnd
