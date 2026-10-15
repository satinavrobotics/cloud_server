"""The close-evidence rule IN THE ENGINE (SceneParams.near_range_m) on a leaf export: prints the numbers that
close_evidence_replay.py gets from its own fold (design: docs/scene_graph/close_evidence_and_turn_cost_design.md, part A).

  python tools/scene_graph_sim/close_evidence_engine_check.py --data <export> [--rdet 1.5] [--fov-deg 0] [--min-clearance 0.25] [--old-rule]
"""
import argparse
import os
import sys

import numpy as np
from scipy import ndimage

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
import accept_route_safety as A  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data", required=True)
    ap.add_argument("--rdet", type=float, default=1.5)
    ap.add_argument("--fov-deg", type=float, default=0.0)
    ap.add_argument("--min-clearance", type=float, default=None,
                    help="the away-from-walls filter (SceneParams.near_min_clearance_m, m): only held components with a cell farther "
                         "than this from every other blocked or unknown cell stay held")
    ap.add_argument("--old-rule", action="store_true",
                    help="hold by the latest lethal time from ANY distance (B), the rule before the lethal-from-within-range fix: "
                         "for comparison only (the engine's Bn plane is replaced by B)")
    a = ap.parse_args()
    kw = dict(near_range_m=a.rdet, near_fov_deg=a.fov_deg if a.fov_deg > 0 else None, near_min_clearance_m=a.min_clearance)
    _, _, off = A.build(a.data, 0.25)
    _, _, on = A.build(a.data, 0.25, **kw)
    if a.old_rule: on._Bn = on._B
    V, Bn, Fn = on._V, on._Bn, on._Fn
    free = (V != 255) & (V < 100)
    flip = free & (Bn >= 0) & (Bn > Fn)
    n_off, n_on = int(off._build_F((0, 0))[0].sum()), int(on._build_F((0, 0))[0].sum())
    print("rule: %s" % ("OLD (lethal from any distance)" if a.old_rule else "lethal from within %.2f m" % a.rdet))
    print("free cells %d | held by the rule %d" % (free.sum(), flip.sum()))
    if a.min_clearance is not None:
        lab, n = ndimage.label(on._hold, structure=np.ones((3, 3)))
        _, nc = ndimage.label(flip, structure=np.ones((3, 3)))
        print("away from walls (> %.2f m): %d of %d components kept, %d cells held" % (a.min_clearance, n, nc, on._hold.sum()))
    print("navigable lattice cells (0.1 m): %d -> %d (%.1f%%)" % (n_off, n_on, 100 * (n_on / n_off - 1)))


if __name__ == "__main__":
    main()
