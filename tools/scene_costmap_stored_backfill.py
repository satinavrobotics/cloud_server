"""Backfill `costmap_stored_ms` on topomap nodes stored before the field existed.

The scene-graph worker reads leaves by the server-side cursor `(costmap_stored_ms, _key)`
(GraphDatabaseService.costmap_nodes_since): a node whose costmap record has no such field is
invisible to it. Both write paths of costmap records (`set_node_costmap`, `set_node_costmaps`)
stamp it with DATE_NOW() already; this tool sets it, once, on the older nodes. One-off, explicit,
never run automatically, dry run by default.

    python -m tools.scene_costmap_stored_backfill [--map ID ...] [--no-minio]            # dry run
    python -m tools.scene_costmap_stored_backfill [--map ID ...] --apply [--batch 500]

Run where ARANGO_* and MINIO_* are set and packages.services.graph_builder.scene_leaf exists:
the graph-builder container (the API image does not carry scene_leaf), e.g.
    docker compose -f docker_compose/mission_dispatch_services.yaml exec graph-builder-service \\
        python -m tools.scene_costmap_stored_backfill --map <ID>

Which nodes: those with a non-empty `costmap` object and no usable `costmap_stored_ms` (absent,
null, 0, negative or not a number: a stamp is valid only when it is a positive number). Nothing else is
written, and only when the field is still unstamped in that sense at write time (a live writer's
DB-clock stamp, a positive number, is never overwritten), so a second run changes nothing and an
interrupted run is resumed by running it again.

Which time, per node, best first (the `source` of the report):
  node_timestamp       the node's `timestamp` (graph-builder receipt), else `created_at`; naive
                       values are read as UTC, like scene_leaf does. Millisecond resolution and
                       the order the nodes were received in: the primary source.
  minio_last_modified  newest last-modified of the node's costmap PNGs in MinIO (1 s resolution,
                       the MinIO host's clock; a fallback for nodes without a timestamp, and the
                       calibration of that fallback; skipped with --no-minio)
  capture_time         the robot's `capture_time`
  costmap_stamp_ms     the robot's stamp inside the costmap record
All sources of one map are put on ONE axis so the relative order is the real one: the reference
source is the best one any node to stamp has (normally node_timestamp, which needs no MinIO call);
a node that lacks it gets its best other source shifted by the median (reference - other) over the nodes that
have both (source `<name>+offset`; `<name>` unshifted when no node has both: `uncalibrated`).
MinIO is stat-ed only for those nodes plus a sample of 64 used to measure the offset.
Nodes with the reference source are stamped first, so an interrupted run keeps them.
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
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from packages.services.graph_builder import scene_leaf
from packages.utils import capture_time

SRC_MINIO = "minio_last_modified"
SRC_NODE = "node_timestamp"
SRC_CAPTURE = "capture_time"
SRC_RECORD = "costmap_stamp_ms"
PRIORITY = (SRC_NODE, SRC_MINIO, SRC_CAPTURE, SRC_RECORD)

PROJECTION = ("{_key: n._key, timestamp: n.timestamp, created_at: n.created_at, "
              "capture_time: n.capture_time, costmap_stored_ms: n.costmap_stored_ms, "
              "costmap: n.costmap}")
SELECT_AQL = ("FOR n IN @@col FILTER n._key > @after AND IS_OBJECT(n.costmap) "
              "AND LENGTH(ATTRIBUTES(n.costmap)) > 0 SORT n._key LIMIT @limit RETURN " + PROJECTION)
# Only while the field is still unstamped (absent, null, 0, negative, not a number; the same rule
# as the Python selection): never overwrites a valid stamp a live write set meanwhile.
STAMP_AQL = ("FOR u IN @items FOR d IN @@col FILTER d._key == u.key "
             "AND (!IS_NUMBER(d.costmap_stored_ms) OR d.costmap_stored_ms <= 0) "
             "UPDATE d WITH {costmap_stored_ms: u.ms} IN @@col OPTIONS {mergeObjects: false} "
             "RETURN NEW._key")


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def is_stamped(node: Dict[str, Any]) -> bool:
    """A usable `costmap_stored_ms`: a positive number (0 / non-numeric count as unstamped)."""
    v = node.get("costmap_stored_ms")
    return _is_num(v) and v > 0


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


def plan(cands: Dict[str, Dict[str, int]], todo: Iterable[str], run_ms: int,
         ref: Optional[str] = None) -> Tuple[Dict[str, Tuple[int, str]], List[str]]:
    """`({key: (ms, source)}, [keys without any time])` for the keys in `todo`, from the
    candidates of ALL the map's costmap nodes (the others only calibrate). `ref` forces the
    reference source (the axis nodes stamped earlier are on). Pure."""
    todo = list(todo)
    ref_src = ref or next((src for src in PRIORITY
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
                 batch: int = 500, now_ms: Optional[int] = None, workers: int = 8,
                 progress: Optional[Callable[[str], None]] = None, progress_every: int = 20,
                 calib_sample: int = 64) -> Dict[str, Any]:
    """Backfill one map; the report: nodes (with a costmap), already (stamped), to_stamp, stamped,
    skipped (no usable time; their keys in `skipped_keys`), sources {source: count}. Reads and
    writes in batches of `batch` (no collection lock; each write is a single guarded update).

    Phases (MinIO is the slow one, so it is touched as little as possible):
      1. read every node with a costmap (database only; no MinIO);
      2. when some node to stamp has a node timestamp, that is the reference: those nodes are
         stamped NOW (incrementally; an interrupted run keeps them and is resumed by running
         again), then MinIO is asked only for the nodes without a timestamp plus an evenly spread
         sample of `calib_sample` nodes that have both, to learn the median offset;
      3. otherwise (no node has a timestamp) MinIO is asked for every node to stamp.
    `progress(line)` is called every `progress_every` batches of each phase."""
    if batch < 1 or workers < 1:
        raise ValueError("batch and workers must be >= 1")
    say = progress or (lambda _line: None)
    run_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    cands: Dict[str, Dict[str, int]] = {}
    rows_by_key: Dict[str, Dict[str, Any]] = {}
    todo: List[str] = []
    nodes = already = batches = 0
    after = ""
    while True:
        rows = store.nodes(map_id, after, batch)
        if not rows:
            break
        for n in rows:
            key = str(n["_key"])
            nodes += 1
            if is_stamped(n):
                already += 1
            else:
                todo.append(key)
            cands[key] = candidates(n, None, run_ms)
            rows_by_key[key] = n
        after = str(rows[-1]["_key"])
        batches += 1
        if batches % progress_every == 0:
            say(f"{map_id}: read {nodes} nodes ({len(todo)} to stamp)")
        if len(rows) < batch:
            break
    say(f"{map_id}: read {nodes} nodes ({len(todo)} to stamp)")

    stamped = 0
    chosen: Dict[str, Tuple[int, str]] = {}
    skipped: List[str] = []

    def write(items_map: Dict[str, Tuple[int, str]], label: str) -> int:
        done = 0
        items = [{"key": k, "ms": ms} for k, (ms, _s) in sorted(items_map.items())]
        for i in range(0, len(items), batch):
            done += store.stamp(map_id, items[i:i + batch])
            if (i // batch + 1) % progress_every == 0:
                say(f"{map_id}: {label} {min(i + batch, len(items))}/{len(items)}")
        return done

    def stats(keys: List[str]) -> None:
        """Add the MinIO time to `cands` for `keys` (threaded)."""
        if minio is None or not keys:
            return

        def stat(k: str) -> Optional[int]:
            return minio(map_id, k, list((rows_by_key[k].get("costmap") or {}).keys()))
        if workers > 1:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                times = list(pool.map(stat, keys))
        else:
            times = [stat(k) for k in keys]
        for k, ms in zip(keys, times):
            if ms is not None and ms > 0:
                cands[k][SRC_MINIO] = int(ms)
        say(f"{map_id}: MinIO times for {len(keys)} nodes")

    if any(SRC_NODE in cands[k] for k in todo):
        # the reference is the node timestamp: those nodes need nothing from MinIO
        direct, rest = [k for k in todo if SRC_NODE in cands[k]], [k for k in todo if SRC_NODE not in cands[k]]
        first, _ = plan({k: cands[k] for k in direct}, direct, run_ms)
        chosen.update(first)
        if apply:
            stamped += write(first, "stamped")
        if rest:
            have = sorted(k for k, c in cands.items() if SRC_NODE in c)
            step = max(1, len(have) // max(1, calib_sample))
            sample = have[::step][:max(1, calib_sample)] if calib_sample > 0 else []
            stats(sorted(set(rest) | set(sample)))
            second, skipped = plan(cands, rest, run_ms, ref=SRC_NODE)
            chosen.update(second)
            if apply:
                stamped += write(second, "stamped (calibrated)")
    else:
        stats(todo)
        chosen, skipped = plan(cands, todo, run_ms)
        if apply:
            stamped += write(chosen, "stamped")
    sources: Dict[str, int] = {}
    for _ms, src in chosen.values():
        sources[src] = sources.get(src, 0) + 1
    return {"map": map_id, "nodes": nodes, "already": already, "to_stamp": len(chosen),
            "stamped": stamped, "skipped": len(skipped), "skipped_keys": skipped[:20],
            "sources": sources, "applied": apply}


def main(argv: Optional[List[str]] = None, store: Any = None, minio: Any = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--map", action="append", dest="maps", help="map id (repeatable); default all")
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--no-minio", action="store_true", help="do not stat MinIO objects")
    ap.add_argument("--batch", type=int, default=500, help="nodes per read/write batch (>= 1)")
    ap.add_argument("--workers", type=int, default=8,
                    help="threads for the MinIO stats (>= 1, default 8)")
    args = ap.parse_args(argv)
    if args.batch < 1 or args.workers < 1:
        ap.error("--batch and --workers must be >= 1")
    if store is None:
        from arango import ArangoClient
        from packages import config
        if not config.ARANGO_PASSWORD:
            print("ARANGO_PASSWORD is not set: export it (the API's environment) and run again",
                  file=sys.stderr)
            return 2
        client = ArangoClient(hosts=f"http://{config.ARANGO_HOST}:{config.ARANGO_PORT}")
        store = ArangoStore(client.db(config.DATA_BASE_NAME, username=config.ARANGO_USERNAME,
                                      password=config.ARANGO_PASSWORD))
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
        r = backfill_map(store, map_id, minio, args.apply, args.batch, workers=args.workers,
                         progress=lambda line: print("  " + line, file=sys.stderr, flush=True))
        print(f"{map_id}: nodes={r['nodes']} already={r['already']} to_stamp={r['to_stamp']} "
              f"stamped={r['stamped']} skipped={r['skipped']} sources={r['sources']}")
        if r["skipped_keys"]:
            print(f"  skipped (no usable time), first keys: {r['skipped_keys']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
