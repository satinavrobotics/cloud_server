"""The close-evidence rule IN THE ENGINE (SceneParams.near_range_m) on a leaf export: prints the numbers that
close_evidence_replay.py gets from its own fold (design: docs/scene_graph/close_evidence_and_turn_cost_design.md, part A).

  python tools/scene_graph_sim/close_evidence_engine_check.py --data <export> [--rdet 1.5] [--fov-deg 0]
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
import accept_route_safety as A  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data", required=True)
    ap.add_argument("--rdet", type=float, default=1.5)
    ap.add_argument("--fov-deg", type=float, default=0.0)
    a = ap.parse_args()
    kw = dict(near_range_m=a.rdet, near_fov_deg=a.fov_deg if a.fov_deg > 0 else None)
    _, _, off = A.build(a.data, 0.25)
    _, _, on = A.build(a.data, 0.25, **kw)
    V, B, Fn = on._V, on._B, on._Fn
    free = (V != 255) & (V < 100)
    flip = free & (B >= 0) & (B > Fn)
    n_off, n_on = int(off._build_F((0, 0))[0].sum()), int(on._build_F((0, 0))[0].sum())
    print("free cells %d | turn blocked %d" % (free.sum(), flip.sum()))
    print("navigable lattice cells (0.1 m): %d -> %d (%.1f%%)" % (n_off, n_on, 100 * (n_on / n_off - 1)))


if __name__ == "__main__":
    main()
