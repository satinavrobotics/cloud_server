"""Read-only check of the stored scene graphs after a generation (or at any time).

For every map: the stored version, generation, format and pipeline, the params and their hash, then the whole graph is loaded
through the store (what the planner does), its invariants are checked, and the charts, links, levels, widths and one route
between the two most distant charts are reported. Nothing is written.

    python -m tools.scene_graph_check_generation --map ID [--map ID ...]
    python -m tools.scene_graph_check_generation --all                  # every map with a stored graph
    python -m tools.scene_graph_check_generation --map ID --expect expected.json

`expected.json`: {"<map id>": {"charts": 12, "links": 15}, ...} (counts of the base level; a differing count is a failure).
Exit code 0: every map passed. Run where ARANGO_* and MINIO_* are set (the graph-builder container).
"""
import argparse
import json
import math
import sys
from typing import Any, Dict, List, Optional

from packages.scene_graph.api import BBox, SceneParams
from packages.scene_graph.store import FORMAT, PIPELINE, SceneGraphStore

BIG = BBox(-1e9, -1e9, 1e9, 1e9)


def check_map(store: Any, map_id: str, expect: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
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
    if man.get("pipeline") != PIPELINE: out["problems"].append(f"pipeline {man.get('pipeline')} != {PIPELINE}")
    p = meta.get("params") or {}
    try:
        want = SceneGraphStore.params_hash(SceneParams(robot_half_side=p["robot_half_side"], lattice=p["lattice"],
                                                       chart_radius=p["chart_radius"]))
        if want != meta.get("params_hash"): out["problems"].append("params_hash does not match the stored params")
    except KeyError as e:
        out["problems"].append(f"params lack {e}")
    if meta.get("pending"):
        out["problems"].append("a generation is still pending")
    g = store.load(map_id)
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
    if len(v.charts) >= 2:                                    # one route between the two most distant anchors
        best = max(((math.dist(a.anchor, b.anchor), a, b) for i, a in enumerate(v.charts) for b in v.charts[i + 1:]),
                   key=lambda t: t[0])
        r = g.route(best[1].key, best[2].key)
        out["route"] = None if r is None else {"charts": len(r.charts), "length": round(r.length, 1), "min_width": round(r.min_width, 2)}
        if r is None: out["problems"].append("no route between the two most distant charts")
    if expect:
        for k, got in (("charts", out["charts"]), ("links", out["links"])):
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
