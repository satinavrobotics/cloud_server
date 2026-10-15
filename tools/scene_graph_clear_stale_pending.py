"""Clear the stale `pending` record left in a map's scene-graph meta by `commit_generation` before the meta compare-and-set replaced
the document (fix/meta-replace, 2026-10-15). A pending record whose generation is not newer than the live generation describes a
rebuild that is already committed: nothing resumes from it, but the generation checker reports "a generation is still pending".

    python -m tools.scene_graph_clear_stale_pending                 # dry run: list what would be cleared
    python -m tools.scene_graph_clear_stale_pending --apply         # clear (compare-and-set on the meta version, one map at a time)
    python -m tools.scene_graph_clear_stale_pending --map ID ...    # only these maps

Run in the graph-builder container of an image that has the REPLACE meta write (an older image would keep the key on the update).
It never touches a pending record of a newer generation than the live one (a rebuild in progress).
"""
import argparse
import sys
from typing import Any, List, Optional

from packages.scene_graph.store import SceneGraphStore


def stale(meta: dict) -> bool:
    pend = (meta or {}).get("pending")
    return bool(pend) and int(pend.get("generation", 0)) <= int(meta.get("generation", 0))


def main(argv: Optional[List[str]] = None, store: Any = None, maps: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--map", action="append", default=[])
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    if store is None:
        from packages.topomap_dbs.client import TopomapDatabaseClient
        client = TopomapDatabaseClient()
        store = SceneGraphStore.from_client(client)
        maps = sorted(c["name"][len("sg_meta_"):] for c in client.graph.db.collections() if c["name"].startswith("sg_meta_"))
    names = args.map or maps or []
    rc = 0
    for m in names:
        raw = store.docs.meta(m)
        if not raw:
            print("%s: no stored graph" % m)
            continue
        if not stale(raw):
            print("%s: no stale pending (version %s, generation %s%s)" % (
                m, raw.get("version"), raw.get("generation"), ", a rebuild is pending" if raw.get("pending") else ""))
            continue
        pend = raw["pending"]
        print("%s: stale pending generation %s (live generation %s, version %s)%s" % (
            m, pend.get("generation"), raw.get("generation"), raw.get("version"), "" if args.apply else "  [dry run]"))
        if args.apply:
            new = {k: v for k, v in raw.items() if k not in ("pending", "_key", "_id", "_rev")}
            if not store.docs.cas_meta(m, raw["version"], new):
                print("%s: the meta changed under us (compare-and-set lost); run again" % m)
                rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
