"""Backfill `costmap_stored_ms` on topomap nodes stored before the field existed.

The scene-graph worker reads leaves by the server-side cursor `(costmap_stored_ms, _key)`
(GraphDatabaseService.costmap_nodes_since): a node whose costmap record has no such field is
invisible to it. Both write paths of costmap records (`set_node_costmap`, `set_node_costmaps`)
stamp it with DATE_NOW() already; this tool sets it, once, on the older nodes. One-off, explicit,
never run automatically, dry run by default.

    python -m tools.scene_costmap_stored_backfill [--map ID ...] [--no-minio]            # dry run
    python -m tools.scene_costmap_stored_backfill [--map ID ...] --apply [--batch 500]

Run where the API's environment is set (ARANGO_*, MINIO_*), e.g. inside the API container.

Which nodes: those with a non-empty `costmap` object and no `costmap_stored_ms`. Nothing else is
written, and only when the field is still null at write time (a live writer's DB-clock stamp is
never overwritten), so a second run changes nothing and an interrupted run is resumed by running
it again.

Which time, per node, best first (the `source` of the report):
  minio_last_modified  newest last-modified of the node's costmap PNGs in MinIO (the store time
                       on the MinIO host's clock; skipped with --no-minio)
  node_timestamp       the node's `timestamp` (graph-builder receipt), else `created_at`; naive
                       values are read as UTC, like scene_leaf does
  capture_time         the robot's `capture_time`
  costmap_stamp_ms     the robot's stamp inside the costmap record
All sources of one map are put on ONE axis so the relative order is the real one: the reference
source is the best one any node to stamp has; a node that lacks it gets its best other source shifted by the median (reference - other) over the nodes that
have both (source `<name>+offset`; `<name>` unshifted when no node has both: `uncalibrated`).
Values are clamped to [1, run start]. A node with no usable time is skipped and counted: it is
not stamped, so the cursor does not see it (it is not given "now", which would put an old leaf
after everything newer).

IMPORTANT (cursor): stamps are in the past. A scene worker whose cursor for the map is already
past them will not read the backfilled nodes; run this before the worker first ingests the map,
or trigger a rebuild of that map's scene graph afterwards.
"""
import argparse
import datetime
import statistics
import sys
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from packages.services.graph_builder import scene_leaf
from packages.utils import capture_time

SRC_MINIO = "minio_last_modified"
SRC_NODE = "node_timestamp"
SRC_CAPTURE = "capture_time"
SRC_RECORD = "costmap_stamp_ms"
PRIORITY = (SRC_MINIO, SRC_NODE, SRC_CAPTURE, SRC_RECORD)

PROJECTION = ("{_key: n._key, timestamp: n.timestamp, created_at: n.created_at, "
              "capture_time: n.capture_time, costmap_stored_ms: n.costmap_stored_ms, "
              "costmap: n.costmap}")
SELECT_AQL = ("FOR n IN @@col FILTER n._key > @after AND IS_OBJECT(n.costmap) "
              "AND LENGTH(ATTRIBUTES(n.costmap)) > 0 SORT n._key LIMIT @limit RETURN " + PROJECTION)
# Only while the field is still null: never overwrites a stamp a live write set meanwhile.
STAMP_AQL = ("FOR u IN @items FOR d IN @@col FILTER d._key == u.key AND d.costmap_stored_ms == null "
             "UPDATE d WITH {costmap_stored_ms: u.ms} IN @@col OPTIONS {mergeObjects: false} "
             "RETURN NEW._key")


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def candidates(node: Dict[str, Any], minio_ms: Optional[int], now_ms: int) -> Dict[str, int]:
    """Every usable time of one node, {source: epoch ms} (pure)."""
    out: Dict[str, int] = {}
    if minio_ms is not None and minio_ms > 0:
        out[SRC_MINIO] = int(minio_ms)
    receipt = scene_leaf._receipt(node)
    if receipt is not None:
        out[SRC_NODE] = scene_leaf._ms(receipt)
    now = datetime.datetime.fromtimestamp(now_ms / 1000.0, datetime.timezone.utc)
    moment = capture_time.parse(node.get(capture_time.FIELD), now)
    if moment is not None:
        out[SRC_CAPTURE] = scene_leaf._ms(moment)
    for rec in (node.get("costmap") or {}).values():
        raw = rec.get("costmap_stamp_ms") if isinstance(rec, dict) else None
        moment = capture_time.parse(raw, now)
        if moment is not None:
            out[SRC_RECORD] = max(out.get(SRC_RECORD, 0), scene_leaf._ms(moment))
    return out


def plan(cands: Dict[str, Dict[str, int]], todo: Iterable[str],
         run_ms: int) -> Tuple[Dict[str, Tuple[int, str]], List[str]]:
    """`({key: (ms, source)}, [keys without any time])` for the keys in `todo`, from the
    candidates of ALL the map's costmap nodes (the others only calibrate). Pure."""
    todo = list(todo)
    ref_src = next((src for src in PRIORITY
                    if any(src in cands.get(k, {}) for k in todo)), None)
    offsets: Dict[str, Optional[float]] = {}
    for src in PRIORITY:
        if src == ref_src or ref_src is None:
            continue
        diffs = [c[ref_src] - c[src] for c in cands.values() if src in c and ref_src in c]
        offsets[src] = statistics.median(diffs) if diffs else None
    out: Dict[str, Tuple[int, str]] = {}
    skipped: List[str] = []
    for key in todo:
        c = cands.get(key, {})
        if ref_src in c:
            ms, src = c[ref_src], ref_src
        else:
            src = next((s for s in PRIORITY if s in c), None)
            if src is None:
                skipped.append(key)
                continue
            off = offsets.get(src)
            ms = c[src] + int(off) if off is not None else c[src]
            src = f"{src}+offset" if off is not None else f"{src} (uncalibrated)"
        out[key] = (max(1, min(int(ms), run_ms)), src)
    return out, skipped


class ArangoStore:
    """The node collections of one ArangoDB database (`nodes_{map_id}`)."""

    def __init__(self, db: Any):
        self.db = db

    def maps(self) -> List[str]:
        return sorted(c["name"][len("nodes_"):] for c in self.db.collections()
                      if c["name"].startswith("nodes_") and not c.get("system"))

    def nodes(self, map_id: str, after: str, limit: int) -> List[Dict[str, Any]]:
        return list(self.db.aql.execute(SELECT_AQL, bind_vars={
            "@col": f"nodes_{map_id}", "after": after, "limit": limit}))

    def stamp(self, map_id: str, items: List[Dict[str, Any]]) -> int:
        return len(list(self.db.aql.execute(STAMP_AQL, bind_vars={
            "@col": f"nodes_{map_id}", "items": items})))


class MinioTimes:
    """Newest last-modified of a node's costmap objects, from the ImageDatabaseService."""

    def __init__(self, image_db: Any):
        self.image_db = image_db

    def __call__(self, map_id: str, key: str, layers: Iterable[str]) -> Optional[int]:
        best = None
        for layer in layers:
            try:
                st = self.image_db.client.stat_object(
                    self.image_db._bucket_name(map_id), self.image_db.costmap_key(key, layer))
            except Exception:  # noqa: BLE001 - missing object / bucket: no time from MinIO
                continue
            lm = getattr(st, "last_modified", None)
            if lm is not None:
                ms = int(lm.timestamp() * 1000) if lm.tzinfo else int(
                    lm.replace(tzinfo=datetime.timezone.utc).timestamp() * 1000)
                best = ms if best is None else max(best, ms)
        return best


def backfill_map(store: Any, map_id: str, minio: Optional[Callable] = None, apply: bool = False,
                 batch: int = 500, now_ms: Optional[int] = None) -> Dict[str, Any]:
    """Backfill one map; the report: nodes (with a costmap), already (stamped), to_stamp, stamped,
    skipped (no usable time; their keys in `skipped_keys`), sources {source: count}. Reads and
    writes in batches of `batch` (no collection lock; each write is a single guarded update)."""
    run_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    cands: Dict[str, Dict[str, int]] = {}
    todo: List[str] = []
    todo_set: set = set()
    nodes = already = 0
    after = ""
    while True:
        rows = store.nodes(map_id, after, batch)
        if not rows:
            break
        for n in rows:
            key = str(n["_key"])
            nodes += 1
            if _is_num(n.get("costmap_stored_ms")) and n["costmap_stored_ms"] > 0:
                already += 1
            else:
                todo.append(key)
                todo_set.add(key)
            minio_ms = None
            if minio is not None and key in todo_set:
                minio_ms = minio(map_id, key, list((n.get("costmap") or {}).keys()))
            cands[key] = candidates(n, minio_ms, run_ms)
        after = str(rows[-1]["_key"])
        if len(rows) < batch:
            break
    chosen, skipped = plan(cands, todo, run_ms)
    sources: Dict[str, int] = {}
    for _ms, src in chosen.values():
        sources[src] = sources.get(src, 0) + 1
    stamped = 0
    if apply:
        items = [{"key": k, "ms": ms} for k, (ms, _s) in sorted(chosen.items())]
        for i in range(0, len(items), batch):
            stamped += store.stamp(map_id, items[i:i + batch])
    return {"map": map_id, "nodes": nodes, "already": already, "to_stamp": len(chosen),
            "stamped": stamped, "skipped": len(skipped), "skipped_keys": skipped[:20],
            "sources": sources, "applied": apply}


def main(argv: Optional[List[str]] = None, store: Any = None, minio: Any = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--map", action="append", dest="maps", help="map id (repeatable); default all")
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--no-minio", action="store_true", help="do not stat MinIO objects")
    ap.add_argument("--batch", type=int, default=500)
    args = ap.parse_args(argv)
    if store is None:
        from arango import ArangoClient
        from packages import config
        client = ArangoClient(hosts=f"http://{config.ARANGO_HOST}:{config.ARANGO_PORT}")
        store = ArangoStore(client.db(config.DATA_BASE_NAME, username=config.ARANGO_USERNAME,
                                      password=config.ARANGO_PASSWORD or "openSesame"))
        if not args.no_minio:
            from packages.topomap_dbs.image_db.server import ImageDatabaseService
            minio = MinioTimes(ImageDatabaseService(
                minio_host=config.MINIO_HOST, minio_port=config.MINIO_PORT,
                minio_access_key=config.MINIO_ACCESS_KEY, minio_secret_key=config.MINIO_SECRET_KEY,
                minio_secure=config.MINIO_SECURE))
    if args.no_minio:
        minio = None
    print("APPLY" if args.apply else "DRY RUN (nothing written; --apply to write)")
    for map_id in (args.maps or store.maps()):
        r = backfill_map(store, map_id, minio, args.apply, args.batch)
        print(f"{map_id}: nodes={r['nodes']} already={r['already']} to_stamp={r['to_stamp']} "
              f"stamped={r['stamped']} skipped={r['skipped']} sources={r['sources']}")
        if r["skipped_keys"]:
            print(f"  skipped (no usable time), first keys: {r['skipped_keys']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
