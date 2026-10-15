"""Read-only check of the stored scene graphs after a generation (or at any time).

For every map: the stored version, generation, format and pipeline, the params and their hash, then the whole graph is loaded
through the store (what the planner does), its invariants are checked, and the charts, links, levels, widths and one route
between the two most distant charts are reported. Nothing is written.

    python -m tools.scene_graph_check_generation --map ID [--map ID ...]
    python -m tools.scene_graph_check_generation --all                  # every map with a stored graph
    python -m tools.scene_graph_check_generation --map ID --expect tools/scene_graph_expected.json

`--expect`: {"<map id>": {"charts": 12, "links": 15, "islands": 0}, ...}: counts of the base level and of the charts outside the
largest link component (islands). A differing count is a failure. The route check runs between the two most distant charts of
the largest component only: islands are free cells no route can reach (kept in the graph, marked, reported in "islands").
Exit code 0: every map passed. Run where ARANGO_* and MINIO_* are set (the graph-builder container).
"""
import argparse
import json
import math
import sys
from typing import Any, Dict, List, Optional

from packages.scene_graph.api import BBox, SceneParams
from packages.scene_graph.store import FORMAT, PIPELINE, PIPELINE_NEAR_VAL, SceneGraphStore

BIG = BBox(-1e9, -1e9, 1e9, 1e9)


def link_components(charts: List[Any], links: List[Any]) -> List[List[str]]:
    """the connected components of the chart graph (charts as nodes, links as edges), largest first, each a sorted key list.
    A chart in a component of its own cannot be reached by any route: its free cells touch the rest only at a corner, and
    routes do not squeeze through corners (4-connectivity)."""
    par = {c.key: c.key for c in charts}

    def find(k):
        while par[k] != k:
            par[k] = par[par[k]]
            k = par[k]
        return k
    for l in links:
        if l.a in par and l.b in par:
            par[find(l.a)] = find(l.b)
    groups: Dict[str, List[str]] = {}
    for c in charts:
        groups.setdefault(find(c.key), []).append(c.key)
    return sorted((sorted(g) for g in groups.values()), key=lambda g: (-len(g), g[0]))


def islands(charts: List[Any], links: List[Any]) -> List[Dict[str, Any]]:
    """the charts outside the largest link component: unreachable free space, reported as a field of the check (kept in the
    graph and marked, not dropped): key, cells, anchor"""
    comps = link_components(charts, links)
    main = set(comps[0]) if comps else set()
    return [{"key": c.key, "cells": c.cells, "anchor": [round(c.anchor[0], 2), round(c.anchor[1], 2)]}
            for c in charts if c.key not in main]


def check_map(store: Any, map_id: str, expect: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """One map's report: `problems` is empty when everything checked out."""
    out: Dict[str, Any] = {"map": map_id, "problems": []}
    meta = store.meta(map_id)
    if not meta:
        out["problems"].append("no stored graph")
        return out
    out.update(version=meta.get("version"), generation=meta.get("generation"), params=meta.get("params"))
    man = store.manifest(map_id)
    out.update(format=man.get("format"), pipeline=man.get("pipeline"))
    if man.get("format") != FORMAT: out["problems"].append(f"format {man.get('format')} != {FORMAT}")
    p = meta.get("params") or {}
    expected = PIPELINE_NEAR_VAL if "near_range_m" in p else PIPELINE
    if man.get("pipeline") != expected:
        out["problems"].append(f"pipeline {man.get('pipeline')} != {expected}"
                               + (" (the earlier rule tokens +near3 / +near4 have no validity plane `val`: the worker rebuilds the map in a new generation)"
                                  if man.get("pipeline") in ("morse/2+near3", "morse/2+near4") else ""))
    try:
        want = SceneGraphStore.params_hash(SceneParams(**{k: p[k] for k in ("robot_half_side", "lattice", "chart_radius",
                                                                           "near_range_m", "near_fov_deg", "near_min_clearance_m") if k in p}))
        if want != meta.get("params_hash"): out["problems"].append("params_hash does not match the stored params")
    except KeyError as e:
        out["problems"].append(f"params lack {e}")
    if meta.get("pending"):
        out["problems"].append("a generation is still pending")
    try:
        g = store.load(map_id)
    except ValueError as err:                  # a pipeline the code does not read (an older token): said, not a traceback
        out["problems"].append(f"load refused: {err}")
        return out
    v = g.query(BIG, level=0, raster=False)
    out.update(charts=len(v.charts), links=len(v.links), levels=v.levels)
    bad = g.check_invariants()
    if bad: out["problems"].append(f"invariants: {bad[:3]}")
    if not v.charts: out["problems"].append("no charts")
    if v.links:
        w = sorted(l.width for l in v.links)
        out.update(width_min=round(w[0], 2), width_median=round(w[len(w) // 2], 2))
    out["driven_frac_mean"] = round(sum(c.driven_frac * c.cells for c in v.charts) / max(sum(c.cells for c in v.charts), 1), 3)
    out["untraversed_charts"] = sum(1 for c in v.charts if c.driven_frac == 0.0)
    isl = islands(v.charts, v.links)
    out["islands"] = {"count": len(isl), "cells": sum(i["cells"] for i in isl), "charts": isl}
    main = set(link_components(v.charts, v.links)[0]) if v.charts else set()
    reach = [c for c in v.charts if c.key in main]
    if len(reach) >= 2:                                       # one route between the two most distant REACHABLE anchors
        best = max(((math.dist(a.anchor, b.anchor), a, b) for i, a in enumerate(reach) for b in reach[i + 1:]), key=lambda t: t[0])
        r = g.route(best[1].key, best[2].key)
        out["route"] = None if r is None else {"charts": len(r.charts), "length": round(r.length, 1), "min_width": round(r.min_width, 2),
                                               "cost": r.cost}
        if r is None: out["problems"].append("no route between the two most distant reachable charts")
    if expect:
        for k, got in (("charts", out["charts"]), ("links", out["links"]), ("islands", out["islands"]["count"])):
            if k in expect and expect[k] != got:
                out["problems"].append(f"{k}: {got}, expected {expect[k]}")
    return out


def main(argv: Optional[List[str]] = None, store: Any = None, maps: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--map", action="append", default=[])
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--expect")
    a = ap.parse_args(argv)
    if store is None:
        from packages.topomap_dbs.client import TopomapDatabaseClient
        client = TopomapDatabaseClient()
        store = SceneGraphStore.from_client(client)
        if a.all:
            maps = sorted(c["name"][len("sg_meta_"):] for c in client.graph.db.collections() if c["name"].startswith("sg_meta_"))
    ids = a.map or maps or []
    if not ids:
        print("no map given (--map ID or --all)", file=sys.stderr)
        return 2
    expect = json.load(open(a.expect)) if a.expect else {}
    failed = 0
    for m in ids:
        try:
            r = check_map(store, m, expect.get(m))
        except Exception as e:                                # noqa: BLE001 - a map that cannot be loaded is a failed map
            r = {"map": m, "problems": [f"load failed: {e!r}"]}
        failed += bool(r["problems"])
        print(json.dumps(r, sort_keys=True))
    print(f"{len(ids) - failed} of {len(ids)} maps passed", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
