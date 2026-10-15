"""Command line of the branch viewer (see driver.py and worker.py).

    python tools/scene_graph_sim/branch_viewer/run.py --map office8 --map office7 --ref main --ref bbf7227 \
        --data tools/scene_graph_sim/data

Needs the maps exported first (tools/scene_graph_sim/export_live.py): <data>/<map>/nodes.json and the PNG windows.
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--map", action="append", default=[], help="a map folder under --data (repeat)")
    ap.add_argument("--ref", action="append", default=[], help="a git ref to play (repeat; default HEAD)")
    ap.add_argument("--data", default=os.path.join(HERE, "..", "data"), help="folder with <map>/nodes.json and the PNG windows")
    ap.add_argument("--exponential", action="store_true", help="the exponential cost profile of the design (not in main)")
    ap.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--tree", help=argparse.SUPPRESS)
    ap.add_argument("--map-dir", help=argparse.SUPPRESS)
    ap.add_argument("--out", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.worker:
        import worker
        return worker.run(args.tree, args.map_dir, args.out, args.exponential)
    if not args.map:
        ap.error("--map is required")
    import driver
    driver.drive(args.ref or ["HEAD"], args.map, args.data, args.exponential)


if __name__ == "__main__":
    main()
