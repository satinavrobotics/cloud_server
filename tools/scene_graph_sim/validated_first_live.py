"""Validated-first planning on the LIVE stored maps (design G.5). Read-only: it loads each map's stored scene graph and routes in memory.

The stores of the live stack are still made under the pipeline token the code before validated-first wrote (morse/2+near3|near4: no `val`
plane). This script reads them anyway for the measurement: it lets `load` accept the stored token and rebuilds the validity nodes from the
stored close-evidence planes (`Fn`) and the leaf poses, exactly what the writer would store under morse/2+near5. Nothing is written to
ArangoDB or MinIO, no map is rebuilt.

    docker run --rm --network host --env-file <(docker exec docker_compose-graph-builder-service-1 printenv | grep -E '^(ARANGO|MINIO)_') \\
        -v $PWD:/w:ro -w /w -e PYTHONPATH=/w -e PYTHONDONTWRITEBYTECODE=1 graph_builder_service:latest \\
        python tools/scene_graph_sim/validated_first_live.py [--pairs 100] [--seed 7] [--map NAME ...]

For each map, --pairs random start/goal lattice cells >= 3 m apart that the baseline route connects, and two configurations: `length` (no
clearance price: the unpriced route against the validated-first one) and `priced` (the planner's default hinge price, kappa 1, against the
same price validated-first). Columns: pairs; same route; changed (median / max length ratio after / before); start or goal node
unvalidated; unroutable after (route before, none after); routes with unvalidated edges before -> after; unvalidated edges and metres
left in the route before -> after (sums over the pairs); mean engine route time after / before (ms).
"""
import argparse
import dataclasses
import math
import os
import sys
import time

import numpy as np

sys.path[:0] = [os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))]

from packages.scene_graph import store as store_mod  # noqa: E402
from packages.scene_graph import validated  # noqa: E402
from packages.scene_graph.api import Safety  # noqa: E402
from packages.scene_graph.store import SceneGraphStore  # noqa: E402
from packages.services.mission_planner import scene_route as sr  # noqa: E402
from packages.topomap_dbs.client import TopomapDatabaseClient  # noqa: E402

H = 0.1


def load_any(store, map_id):
    """the stored graph under whatever pipeline token it carries (read-only measurement of a store not yet rebuilt under near5)"""
    man = store.manifest(map_id)
    real = store_mod._pipeline
    store_mod._pipeline = lambda params: man.get("pipeline")
    try:
        return store.load(map_id, verify=False), man.get("pipeline")
    finally:
        store_mod._pipeline = real


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", type=int, default=100)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--map", action="append", default=[])
    args = ap.parse_args()
    client = TopomapDatabaseClient()
    store = SceneGraphStore.from_client(client)
    maps = args.map or sorted(c["name"][len("sg_meta_"):] for c in client.graph.db.collections() if c["name"].startswith("sg_meta_"))
    rows = {"length": [], "priced": []}
    shares = []
    for m in maps:
        if not store.meta(m):
            continue
        g, token = load_any(store, m)
        s = g._solved
        if s is None or g._val is None:
            print("%s: token %s, no close-evidence rule: no validity plane" % (m, token))
            continue
        sp = s.sp
        val = g._val[sp.ys, sp.xs]
        # the largest connected piece of validated nodes: the space the plans can use without any penalty
        from scipy import ndimage
        lab, nl = ndimage.label(g._val & (s.plane >= 0), structure=np.ones((3, 3)))
        big = int(np.bincount(lab[lab > 0]).max()) if nl else 0
        shares.append((m, token, sp.N, float(val.mean()), big / sp.N, len(s.keys)))
        price = sr.safety_for(g, None, None, 1.0, 0.0, False)
        configs = {"length": (None, Safety(1.0, 0.0, validated_first=True)),
                   "priced": (price, dataclasses.replace(price, validated_first=True))}
        xy = lambda c: g._cell_xy(int(sp.ys[c]), int(sp.xs[c]))
        rng = np.random.default_rng(args.seed)
        pairs, tries = [], 0
        while len(pairs) < args.pairs and tries < 200000:
            tries += 1
            a, b = (int(v) for v in rng.integers(0, sp.N, 2))
            if math.hypot(int(sp.ys[a]) - int(sp.ys[b]), int(sp.xs[a]) - int(sp.xs[b])) * H < 3.0:
                continue
            ka, kb = s.keys[int(s.full[a])], s.keys[int(s.full[b])]
            if g.route(ka, kb, None, xy(a), xy(b)) is None:
                continue
            pairs.append((a, b, ka, kb))

        def left(r):
            """(unvalidated edges, metres) of a route's node list from the stored validity"""
            ij = [g._lat_of(x, y) for x, y in r.nodes]
            v = np.array([bool(g._val[i, j]) for i, j in ij])
            ys = np.array([i for i, _ in ij])
            xs = np.array([j for _, j in ij])
            return validated.path_summary(v, ys, xs, H)
        for name, (before_sf, after_sf) in configs.items():
            same = chg = lost = unr = tb = ta = 0
            ratios, nb, na, mb, ma, t_b, t_a = [], 0, 0, 0.0, 0.0, [], []
            for a, b, ka, kb in pairs:
                kw = {} if before_sf is None else {"safety": before_sf}
                t0 = time.perf_counter()
                r0 = g.route(ka, kb, None, xy(a), xy(b), **kw)
                t1 = time.perf_counter()
                r1 = g.route(ka, kb, None, xy(a), xy(b), safety=after_sf)
                t2 = time.perf_counter()
                t_b.append(t1 - t0); t_a.append(t2 - t1)
                lost += (not val[a]) or (not val[b])
                if r1 is None:
                    unr += 1
                    continue
                c0, m0 = left(r0)
                nb += c0; mb += m0; na += r1.unvalidated; ma += r1.unvalidated_m
                tb += c0 > 0; ta += r1.unvalidated > 0
                if [tuple(p) for p in r0.nodes] == [tuple(p) for p in r1.nodes]:
                    same += 1
                else:
                    chg += 1
                    ratios.append(r1.length / r0.length)
            rows[name].append((m, len(pairs), same, chg, float(np.median(ratios)) if ratios else 1.0, max(ratios, default=1.0),
                               lost, unr, tb, ta, nb, na, mb, ma, 1e3 * float(np.mean(t_a)), 1e3 * float(np.mean(t_b))))
    print("validated share of F (nodes), largest validated 8-connected piece, charts")
    for m, token, n, v, bg, k in shares:
        print("  %-12s %-16s %6d nodes %3d charts  validated %5.1f%%  largest validated piece %5.1f%%" % (m, token, n, 100 * v, 100 * bg, k))
    for name, rs in rows.items():
        print("\nconfig %s (before -> after validated-first)" % name)
        print("%-12s %5s | %5s | %4s %-11s | %9s | %8s | %-11s | %-17s | %-19s | %-11s" % (
            "map", "pairs", "same", "chg", "(len x med/max)", "S/G unval", "unroutable", "routes w/ unval", "unval edges", "unval metres", "ms after/before"))
        for r in rs:
            print("%-12s %5d | %5d | %4d (%.3f/%.3f) | %9d | %8d | %4d -> %-4d | %6d -> %-6d | %8.1f -> %-8.1f | %.1f/%.1f" % r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
