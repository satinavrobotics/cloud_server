"""The per-leaf update of the chart solver.  State between frames lives in image positions; everything the update touches is found from the changed cells."""
import time
import numpy as np
from . import chart_solver as L

class Track:
    def __init__(s, r=0.25, mult=1.5, h=0.1, sight_range=None):
        s.r, s.mult, s.h = r, mult, h
        s.sight_range = sight_range
        s.nav = None; s.clr = None
        s.anchors = []          # (y, x) of the anchors, in their order
        s.own_img = None; s.rnd_img = None
        s.img_rows = {}         # (y,x) -> row of an anchor (or of a hypothetical move target) in image positions, valid for the last frame
        s.tried = {}            # (y,x) -> (set of (y,x) of the anchors the last trial used, flat positions it read)
        s.last = {}
        s.kmemo = [{}, {}]      # chart content -> kernel-test and proposal answers, this frame and the previous one (chart_solver._chart)

    def grow(s, shift, shape):
        """The canvas grew: the old image sits at `shift` (rows, cols in lattice cells) inside an image of `shape`.
        Everything this update carries lives in image positions, and drives, rows and ownership depend only on relative
        positions and on F, so it is translated, not recomputed; the new border is outside F, and `step` then finds the
        cells whose F or clearance changed exactly as for any other change."""
        sy, sx = shift
        s.anchors = [(y + sy, x + sx) for y, x in s.anchors]
        if s.nav is None:
            return
        oH, oW = s.nav.shape; H, W = shape

        def pad(a, fill):
            b = np.full(shape, fill, a.dtype); b[sy:sy + oH, sx:sx + oW] = a; return b

        def flat(f):                                    # flat image position in the old image -> in the new one; -1 stays
            f = np.asarray(f)
            y, x = np.divmod(f, oW)
            return np.where(f >= 0, (y + sy) * W + (x + sx), f)

        s.nav = pad(s.nav, False); s.clr = pad(s.clr, 0.0)
        s.own_img = pad(s.own_img, -1); s.rnd_img = pad(s.rnd_img, L.BIG)
        s.img_rows = {(y + sy, x + sx): dict(T=flat(im['T']), SEG=flat(im['SEG']), P=flat(im['P']), Q=flat(im['Q']),
                                             rect=(im['rect'][0] + sy, im['rect'][1] + sy, im['rect'][2] + sx, im['rect'][3] + sx),
                                             dep=flat(im['dep']))
                      for (y, x), im in s.img_rows.items()}
        s.tried = {(y + sy, x + sx): ({(uy + sy, ux + sx) for uy, ux in used}, flat(reads))
                   for (y, x), (used, reads) in s.tried.items()}

    def step(s, nav, clr, local=True):
        t0 = time.perf_counter(); H, W = nav.shape
        if s.nav is None:
            s.nav = np.zeros_like(nav); s.clr = np.zeros_like(clr)
            s.own_img = -np.ones(nav.shape, np.int32); s.rnd_img = np.full(nav.shape, L.BIG, np.int32)
        delta = (nav != s.nav)                              # F membership: what the drives and ownership depend on
        dclr = (np.abs(clr - s.clr) > 1e-9) & nav & s.nav    # clearance: what the band depends on
        t_delta = time.perf_counter() - t0
        sp = L.Local(nav, clr, h=s.h, r=s.r, mult=s.mult, img_rows=s.img_rows, delta=delta, sight_range=s.sight_range, kmemo=s.kmemo)
        t_build = time.perf_counter() - t0 - t_delta
        N = sp.N
        keep = [k for k, (y, x) in enumerate(s.anchors) if sp.idx[y, x] >= 0]
        gone = [k for k in range(len(s.anchors)) if k not in set(keep)]
        gone_keys = {s.anchors[k] for k in gone}
        A = [int(sp.idx[s.anchors[k]]) for k in keep]
        newidx = {k: i for i, k in enumerate(keep)}
        ob = s.own_img.ravel()[sp.flat]; rb = s.rnd_img.ravel()[sp.flat]
        own = np.full(N, -1, np.int32); mapv = np.full(len(s.anchors) + 1, -1, np.int32)
        for k, i in newidx.items(): mapv[k] = i
        sel = ob >= 0; own[sel] = mapv[ob[sel]]
        rnd = np.where(own >= 0, rb, L.BIG).astype(np.int32)
        for i, a in enumerate(A): own[a] = i; rnd[a] = 0
        own_base, rnd_base = own.copy(), rnd.copy(); A_base = list(A)
        # seeds: changed cells in F, the targets of invalid anchors whose sight or drive changed, what removed anchors owned
        dcells = sp.idx[delta & nav]; dcells = dcells[dcells >= 0]
        S0 = [dcells]; rows_changed = set(gone_keys)
        invalid = []
        for k in keep:
            key = s.anchors[k]; im = s.img_rows.get(key)
            if im is None or delta.ravel()[im['dep']].any(): invalid.append(key)
        for key in invalid:
            r_ = sp.row(int(sp.idx[key]))
            if len(r_.chg): rows_changed.add(key); S0.append(r_.chg)
        for k in gone: S0.append(np.nonzero(ob == k)[0])
        S0 = np.unique(np.concatenate(S0)) if S0 else np.zeros(0, np.int64)
        t_seed = time.perf_counter() - t0 - t_delta - t_build
        if len(A):
            own, rnd, S, invo = sp.settle(A, own, rnd, S0, X0=dcells, local=local)
        else:
            S = S0
        cclr = sp.idx[dclr]; cclr = cclr[cclr >= 0]
        added_T = []
        while True:                                          # violations near the change, canonical order
            seeds = np.union1d(S[own[S] < 0] if len(S) else S, cclr[own[cclr] < 0])
            touched = np.unique(np.concatenate([own[S], own[cclr]])) if len(S) or len(cclr) else np.zeros(0, int); touched = touched[touched >= 0].tolist()
            v = sp.violation(A, own, seeds=seeds, anchors=touched)
            if v is None: break
            A, own, rnd, S2, _ = sp.apply(A, own, rnd, ('add', v[1]), local=local)
            S = np.union1d(S, S2); added_T.append(sp.row(v[1]).T)
        # what changed this frame, in cells: F, clearance, candidates (S0, new anchors' sight), owner and round
        Cm = np.zeros(H * W, bool)
        Cm[np.nonzero(delta.ravel())[0]] = True; Cm[np.nonzero(dclr.ravel())[0]] = True
        Cm[sp.flat[S0]] = True
        for t_ in added_T: Cm[sp.flat[t_]] = True
        if len(S):
            old_o = np.where(own_base[S] >= 0, np.array(A_base, np.int64)[np.maximum(own_base[S], 0)] if len(A_base) else -1, -1)
            new_o = np.where(own[S] >= 0, np.array(A, np.int64)[np.maximum(own[S], 0)], -1)
            ch = S[(old_o != new_o) | (rnd_base[S] != rnd[S])]
            Cm[sp.flat[ch]] = True
        # trial memos that survive: every anchor they used still exists unchanged, and they read no changed cell
        tried = {}
        cur_keys = {(int(sp.ys[a]), int(sp.xs[a])) for a in A}
        for key, (used_keys, reads_flat) in s.tried.items():
            if key not in cur_keys: continue
            ok = True
            for u in used_keys:
                if u in rows_changed or u in gone_keys or u not in s.img_rows: ok = False; break
                if u not in cur_keys and delta.ravel()[s.img_rows[u]['dep']].any(): ok = False; break
            if ok and Cm[reads_flat].any(): ok = False
            if ok:
                try: tried[int(sp.idx[key])] = ({int(sp.idx[u]) for u in used_keys if sp.idx[u] >= 0}, sp.flat2idx[reads_flat][sp.flat2idx[reads_flat] < N])
                except Exception: pass
        queue = {a for a in A if a not in tried}
        A, own, rnd, tried = sp.descend(A, own, rnd, queue=queue, local=local, tried=tried)
        dt = time.perf_counter() - t0
        s.kmemo = [{}, s.kmemo[0]]   # entries not used for two frames are dropped
        # persist
        s.nav = nav.copy(); s.clr = clr.copy()
        s.anchors = [(int(sp.ys[a]), int(sp.xs[a])) for a in A]
        s.own_img = -np.ones(nav.shape, np.int32); s.own_img[sp.ys, sp.xs] = own
        s.rnd_img = np.full(nav.shape, L.BIG, np.int32); s.rnd_img[sp.ys, sp.xs] = rnd
        new_rows = sp.export_rows()
        s.tried = {}
        for a, (us, rd) in tried.items():
            if a in A and len(us) and all(u < N for u in us):
                s.tried[(int(sp.ys[a]), int(sp.xs[a]))] = ({(int(sp.ys[u]), int(sp.xs[u])) for u in us}, np.sort(sp.flat[rd[rd < N]]))
        wanted = set(s.anchors).union(*[v[0] for v in s.tried.values()]) if s.tried else set(s.anchors)
        kept_rows = {key: im for key, im in s.img_rows.items() if key in wanted and key not in new_rows}
        kept_rows.update({k_: v_ for k_, v_ in new_rows.items() if k_ in wanted})
        s.img_rows = kept_rows
        s.last = dict(t=dt, t_delta=t_delta, t_build=t_build, t_seed=t_seed, N=N, A=len(A), S0=len(S0), S=len(S), queue=len(queue), stats=sp.stats, invalid=len(invalid), gone=len(gone),
                      dirty_cells=int(delta.sum()), clr_cells=int(dclr.sum()), changed_cells=int(Cm.sum()))
        return sp, A, own, rnd

    def restore(s, nav, clr):
        """Resume from a stored state: `anchors`, `own_img`, `rnd_img` are set by the caller.  No construct and no descend: the
        ownership and rounds are recomputed from the stored anchors by the deterministic re-simulation and returned next to
        the cells where they differ from the stored ones (the caller decides what a difference means)."""
        H, W = nav.shape
        s.nav = np.zeros_like(nav); s.clr = np.zeros_like(clr)
        s.img_rows = {}; s.tried = {}; s.kmemo = [{}, {}]
        sp = L.Local(nav, clr, h=s.h, r=s.r, mult=s.mult, img_rows={}, delta=(nav != s.nav), sight_range=s.sight_range, kmemo=s.kmemo)
        N = sp.N
        ys = np.array([a[0] for a in s.anchors], np.int64); xs = np.array([a[1] for a in s.anchors], np.int64)
        inside = (ys >= 0) & (ys < H) & (xs >= 0) & (xs < W)
        if not inside.all() or (sp.idx[ys, xs] < 0).any():
            return sp, [], None, None, np.zeros(0, np.int64)         # a stored anchor lies outside F: not this state
        A = [int(sp.idx[y, x]) for y, x in s.anchors]
        ob = s.own_img.ravel()[sp.flat]; rb = s.rnd_img.ravel()[sp.flat]
        own_s = ob.astype(np.int32); rnd_s = np.where(ob >= 0, rb, L.BIG).astype(np.int32)
        own = np.full(N, -1, np.int32); rnd = np.full(N, L.BIG, np.int32)
        for i, a in enumerate(A): own[a] = i; rnd[a] = 0
        own, rnd, _, _ = sp.settle(A, own, rnd, np.arange(N, dtype=np.int64), local=False)
        bad = np.nonzero((own != own_s) | (rnd != rnd_s))[0]
        s.nav = nav.copy(); s.clr = clr.copy()
        s.anchors = [(int(sp.ys[a]), int(sp.xs[a])) for a in A]
        s.own_img = -np.ones(nav.shape, np.int32); s.own_img[sp.ys, sp.xs] = own
        s.rnd_img = np.full(nav.shape, L.BIG, np.int32); s.rnd_img[sp.ys, sp.xs] = rnd
        keep = set(s.anchors)
        s.img_rows = {k: v for k, v in sp.export_rows().items() if k in keep}
        return sp, A, own, rnd, sp.flat[bad]
