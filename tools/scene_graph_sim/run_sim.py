"""Replay the leaves of a map through the scene graph and write PNG frames and a metrics line.

    python run_sim.py office8 --tau 1.5 --every 5 --out out/office8_t15
"""
import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.dirname(os.path.dirname(HERE))]

from leaves import load_leaves
from packages.scene_graph import Params, SceneGraph
from render import frame, pipeline


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("map")
    ap.add_argument("--tau", type=float, default=Params.tau)
    ap.add_argument("--radius", type=float, default=Params.robot_radius)
    ap.add_argument("--budget", type=int, default=Params.budget)
    ap.add_argument("--every", type=int, default=0, help="write a frame every N leaves (0 = first, last)")
    ap.add_argument("--out", default=None)
    ap.add_argument("--shuffle", type=int, default=None, help="seed: replay the leaves in random order")
    a = ap.parse_args()
    out = a.out or f"{HERE}/out/{a.map}"
    os.makedirs(out, exist_ok=True)
    leaves = load_leaves(a.map)
    if a.shuffle is not None:
        import random
        random.Random(a.shuffle).shuffle(leaves)
    sg = SceneGraph(Params(tau=a.tau, robot_radius=a.radius, budget=a.budget))
    cells, t0 = [], time.time()
    csv = ["leaf,anchors,new_cells,class_flips,promoted,removed,moved,owner_churn,ms"]
    prev = None
    for k, lf in enumerate(leaves):
        t1 = time.time()
        rec = sg.add_leaf(lf)
        ms = int(1000 * (time.time() - t1))
        cells.append(rec["cell"])
        own = sg.cells.crop(*sg.cells.known_box())[1]
        churn = 0.0
        if prev is not None and prev[0] == sg.cells.known_box():
            both = (prev[1] >= 0) & (own >= 0)
            churn = float(((prev[1] != own) & both).sum() / max(both.sum(), 1))
        prev = (sg.cells.known_box(), own)
        csv.append(",".join(str(x) for x in (k, rec["anchors"], rec["new_cells"], rec["flips"], len(rec["promoted"]),
                                              len(rec["removed"]), len(rec["moved"]), round(churn, 4), ms)))
        if (a.every and k % a.every == 0) or k in (0, len(leaves) - 1):
            note = (f"leaf {k}  new cells {rec['new_cells']}  class flips {rec['flips']}  "
                    f"promoted {rec['promoted']}  removed {rec['removed']}  moved {rec['moved']}")
            frame(sg, f"{out}/leaf_{k:03d}.png", f"{a.map}  tau={a.tau}  leaf {k}/{len(leaves)}  anchors={len(sg.anchors)}",
                  cells, rec["cell"], note=note)
    open(f"{out}/metrics.csv", "w").write("\n".join(csv) + "\n")
    links = sg.links()
    pipeline(sg, f"{out}/pipeline.png", f"{a.map}  tau={a.tau}  anchors={len(sg.anchors)}  links={len(links)}", cells, links)
    frame(sg, f"{out}/final.png", f"{a.map}  tau={a.tau}  final  anchors={len(sg.anchors)}  links={len(links)}",
          cells, None, links)
    print(json.dumps(dict(map=a.map, tau=a.tau, leaves=len(leaves), seconds=round(time.time() - t0, 1),
                          anchors={i: dict(cell=(x.ci, x.cj), n=x.n) for i, x in sg.anchors.items()},
                          links=[(l.a, l.b, round(l.cost, 2), round(l.width, 2)) for l in links])))


if __name__ == "__main__":
    main()
