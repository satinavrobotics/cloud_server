"""Replay of the close-evidence rule on a leaf export (design: docs/scene_graph/close_evidence_and_turn_cost_design.md, part A).

Per cell, over the leaves in capture order: `V` latest known value (today's fold), `B` latest blocked time, `Fnear` latest free
time from a capture pose within r_det. A cell is blocked by the rule when it is free today and B > Fnear. Reports the cells that
turn blocked, the navigable (production `_build_F`) change, and for every component with a cell farther than --away m from a
blocked cell: its size, place, the lethal share of its close and its far captures, the share of its cells never seen free from
close, and the nearest robot pose. Read-only; NOT the engine's fold and not a format.

  python tools/scene_graph_sim/close_evidence_replay.py --data <export> [--rdet 1.5] [--away 0.25]
"""
import argparse
import os
import sys

import numpy as np
from scipy import ndimage

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
import accept_route_safety as A  # noqa: E402

RES = 0.05


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data", required=True)
    ap.add_argument("--rdet", type=float, default=1.5)
    ap.add_argument("--away", type=float, default=0.25)
    ap.add_argument("--half-side", type=float, default=0.25)
    ap.add_argument("--fov-deg", type=float, default=0.0,
                    help="if > 0, a free report counts as close evidence only inside this horizontal field of view around the base yaw "
                         "(the robot's ground-support channel only judges cells its camera swept; the export has no camera view, "
                         "so a forward camera is ASSUMED)")
    a = ap.parse_args()
    leaves, _, eng = A.build(a.data, a.half_side)
    H, W = eng._V.shape
    cx0, cy0 = eng._cx0, eng._cy0
    recs = sorted(((lf.stamp_ms, lf.x, lf.y) + eng._fold_leaf(lf)[:3] + (lf.yaw,) for lf in leaves), key=lambda r: r[0])
    sh = (H, W)
    V = np.full(sh, 255, np.uint8); T = -np.ones(sh, np.int64)
    B = -np.ones(sh, np.int64); Fn = -np.ones(sh, np.int64)
    ncl = np.zeros(sh, np.int32); ncb = np.zeros(sh, np.int32); nfa = np.zeros(sh, np.int32); nfb = np.zeros(sh, np.int32)
    for t, x, y, gx, gy, w, yaw in recs:
        h, wd = w.shape
        oy, ox = gy - cy0, gx - cx0
        y0, x0, y1, x1 = max(0, oy), max(0, ox), min(H, oy + h), min(W, ox + wd)
        if y0 >= y1 or x0 >= x1:
            continue
        sub = w[y0 - oy:y1 - oy, x0 - ox:x1 - ox]
        d = np.hypot(((np.arange(y0, y1) + cy0 + 0.5) * RES - y)[:, None], ((np.arange(x0, x1) + cx0 + 0.5) * RES - x)[None, :])
        sl = (slice(y0, y1), slice(x0, x1))
        kn, bl, close = sub != 255, sub >= 100, d <= a.rdet
        if a.fov_deg > 0:
            bear = np.arctan2((np.arange(y0, y1) + cy0 + 0.5)[:, None] * RES - y, (np.arange(x0, x1) + cx0 + 0.5)[None, :] * RES - x)
            dev = np.abs((bear - yaw + np.pi) % (2 * np.pi) - np.pi)
            close &= (dev <= np.radians(a.fov_deg) / 2) | (d < 0.3)
        Tc, Vc = T[sl], V[sl]
        win = kn & ((Tc < 0) | (t > Tc) | ((t == Tc) & (sub > Vc)))
        Tc[win] = t; Vc[win] = sub[win]
        Bc, Fc = B[sl], Fn[sl]
        Bc[kn & bl] = np.maximum(Bc[kn & bl], t)
        Fc[kn & ~bl & close] = np.maximum(Fc[kn & ~bl & close], t)
        ncl[sl] += kn & close; ncb[sl] += kn & bl & close; nfa[sl] += kn & ~close; nfb[sl] += kn & bl & ~close
    free = (V != 255) & (V < 100)
    flip = free & (B >= 0) & (B > Fn)
    assert (V == eng._V).all(), "replay differs from the engine's folded V"
    keep_old = eng._build_F((0, 0))[0]
    Vs = eng._V
    eng._V = np.where(flip, np.uint8(100), Vs).astype(np.uint8)
    try:
        keep_new = eng._build_F((0, 0))[0]
    finally:
        eng._V = Vs
    dist = ndimage.distance_transform_edt(~(V >= 100)) * RES
    lab, n = ndimage.label(flip, structure=np.ones((3, 3)))
    far = np.array(ndimage.maximum(dist, lab, range(1, n + 1))) if n else np.array([])
    rim = int((flip & (dist <= 0.10)).sum())
    print("leaves %d  span %.2f h  r_det %.2f m  fov %s" % (len(leaves), (recs[-1][0] - recs[0][0]) / 3.6e6, a.rdet, "any" if a.fov_deg <= 0 else "%.0f deg (assumed forward)" % a.fov_deg))
    print("free cells %d | turn blocked %d (%.1f%%), %d on a wall rim (<= 10 cm), components %d" % (
        free.sum(), flip.sum(), 100 * flip.sum() / free.sum(), rim, n))
    print("navigable lattice cells (0.1 m): %d -> %d (%.1f%%)" % (keep_old.sum(), keep_new.sum(),
                                                                 100 * (keep_new.sum() / keep_old.sum() - 1)))
    poses = np.array([(l.x, l.y) for l in leaves])
    rows = []
    for c in np.nonzero(far > a.away)[0] + 1:
        m = lab == c
        ys, xs = np.nonzero(m)
        px, py = (xs.mean() + cx0) * RES, (ys.mean() + cy0) * RES
        k = m.sum()
        rows.append((k, px, py, far[c - 1], ncl[m].sum() / k, ncb[m].sum() / max(ncl[m].sum(), 1), nfa[m].sum() / k,
                     nfb[m].sum() / max(nfa[m].sum(), 1), float((Fn[m] < 0).mean()), float(np.hypot(poses[:, 0] - px, poses[:, 1] - py).min())))
    rows.sort(key=lambda r: -r[0])
    print("components with a cell > %.2f m from a blocked cell: %d, %d cells (%.2f m2)" % (
        a.away, len(rows), sum(r[0] for r in rows), sum(r[0] for r in rows) * RES * RES))
    print("%2s %5s %6s %6s %5s | close caps/cell lethal | far caps/cell lethal | never seen free from close | nearest pose" % ("#", "cells", "x", "y", "maxd"))
    for i, r in enumerate(rows, 1):
        print("%2d %5d %6.1f %6.1f %5.2f | %9.1f %11.2f | %8.1f %9.2f | %26.2f | %.2f m" % ((i,) + r))


if __name__ == "__main__":
    main()
