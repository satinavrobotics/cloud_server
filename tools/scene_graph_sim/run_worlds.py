"""Run the synthetic reference worlds and write a final frame for each.

    python run_worlds.py [--tau 1.5] [name ...]
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path[:0] = [HERE, ROOT]

from packages.scene_graph import Params, SceneGraph
from packages.scene_graph.metrics import invariants
from render import frame
from tests.unit.scene_graph.synth import drive
from tests.unit.scene_graph.worlds import ALL

ap = argparse.ArgumentParser()
ap.add_argument("names", nargs="*")
ap.add_argument("--tau", type=float, default=Params.tau)
ap.add_argument("--radius", type=float, default=Params.robot_radius)
a = ap.parse_args()
for name in a.names or ALL:
    world, path = ALL[name]()
    leaves = drive(world, path)
    sg = SceneGraph(Params(tau=a.tau, robot_radius=a.radius))
    cells = []
    for lf in leaves:
        cells.append(sg.add_leaf(lf)["cell"])
    links = sg.links()
    out = f"{HERE}/out/worlds"
    os.makedirs(out, exist_ok=True)
    frame(sg, f"{out}/{name}_t{a.tau}.png", f"{name}  tau={a.tau}  anchors={len(sg.anchors)}  links={len(links)}", cells, None, links)
    m = invariants(sg)
    print(name, "leaves", len(leaves), "anchors", [(x.id, x.n) for x in sg.anchors.values()],
          "links", [(l.a, l.b, round(l.cost, 1), round(l.width, 2)) for l in links],
          "dil", m["dilation"], "visUnowned", m["visited_unowned"], "unowned", m["unowned"])
