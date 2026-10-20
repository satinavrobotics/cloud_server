"""Apply a planned import (DATA_MIGRATION.md §3.4-3.6, §4, §6).

One map is one unit of work. For each planned map, `apply_import`:

1. takes the map's import lock (one runner per target name) and writes the target map row first, as
   `archived` with `status.imported_from = {source, map, import_id, phase: "importing"}`: hidden from lists and not
   operable, and a rerun finds it and resumes (the plan says `resume`);
2. copies the mapping sessions (ON CONFLICT DO NOTHING on the session id), the ArangoDB nodes and edges (insert,
   an existing key is left as it is) with the map name rewritten, the MinIO objects (an object that exists with the
   same size is skipped; the rest are streamed from the bundle) and the view settings (never replaces existing ones);
3. commits: the row becomes `ready` (`draft` when the map has no data; `archived` stays archived) with
   `imported_from.phase = "complete"`.

Everything is idempotent, so an interrupted import is finished by running it again; a failure leaves the map hidden
and says so in the report. Nothing already on the target is overwritten or removed. The bundle is verified once,
in full, before the first write.
"""
import json
import logging
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, Iterable, Iterator, List, Optional, Protocol

from packages.migration.bundle import BundleReader, map_dir, rosbag_dir, runs_dir

logger = logging.getLogger(__name__)

BATCH = 500
ARCHIVED, READY, DRAFT = "archived", "ready", "draft"


class ImportRefused(Exception):
    """The import cannot start or continue (damaged bundle, lock busy, name taken by another map)."""


class ImportStopped(Exception):
    """The user asked to stop (cooperatively: checked between batches and files). What was copied stays; the map stays hidden
    and a later apply of the same bundle carries on."""


class TargetStore(Protocol):
    async def lock(self, name: str) -> Any: ...                            # async context manager; ImportRefused if busy
    async def get_row(self, name: str) -> Optional[Dict[str, Any]]: ...     # {lifecycle, spec, status}
    async def begin_map(self, name: str, spec: Dict[str, Any], status: Dict[str, Any]) -> bool: ...  # insert if absent
    async def add_sessions(self, name: str, sessions: List[Dict[str, Any]]) -> int: ...
    async def put_nodes(self, name: str, docs: List[Dict[str, Any]]) -> int: ...
    async def put_edges(self, name: str, docs: List[Dict[str, Any]]) -> int: ...
    async def object_size(self, name: str, key: str) -> Optional[int]: ...
    async def put_object(self, name: str, key: str, stream: Any, size: int, content_type: Optional[str],
                         metadata: Dict[str, str]) -> None: ...
    async def put_view_settings(self, name: str, settings: Dict[str, Any]) -> bool: ...
    async def commit_map(self, name: str, state: str, imported_from: Dict[str, Any]) -> None: ...
    async def add_runs(self, import_id: str, runs: List[Dict[str, Any]], legs: List[Dict[str, Any]]) -> Dict[str, int]: ...   # {runs, legs} inserted, rest existed
    async def rosbag_size(self, robot: str, bag_id: str) -> Optional[int]: ...
    async def put_rosbag(self, import_id: str, robot: str, bag_id: str, stream: Any, size: int, sidecar: Dict[str, Any]) -> None: ...


ProgressFn = Callable[[Dict[str, Any]], Awaitable[None]]
StopFn = Callable[[], bool]


def rewrite_node(doc: Dict[str, Any], src: str, dst: str) -> Dict[str, Any]:
    """The node document for the target: ArangoDB's own `_id`/`_rev` dropped, `map_id` pointing at the target."""
    out = {k: v for k, v in doc.items() if k not in ("_id", "_rev")}
    if "map_id" in out:
        out["map_id"] = dst
    return out


def rewrite_edge(doc: Dict[str, Any], src: str, dst: str) -> Dict[str, Any]:
    """The edge document for the target: `_from`/`_to` name the target's node collection."""
    out = {k: v for k, v in doc.items() if k not in ("_id", "_rev")}
    for end in ("_from", "_to"):
        value = out.get(end)
        if isinstance(value, str) and value.startswith(f"nodes_{src}/"):
            out[end] = f"nodes_{dst}/" + value.split("/", 1)[1]
    if "map_id" in out:
        out["map_id"] = dst
    return out


def _batches(items: Iterable[Any], size: int) -> Iterator[List[Any]]:
    batch: List[Any] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def final_state(source_state: Optional[str], node_count: int) -> str:
    if source_state == ARCHIVED:
        return ARCHIVED
    return READY if node_count > 0 else DRAFT


async def apply_import(reader: BundleReader, plan: Dict[str, Any], store: TargetStore, import_id: str,
                       progress: Optional[ProgressFn] = None, should_stop: Optional[StopFn] = None) -> Dict[str, Any]:
    """Run the plan; the report `{ok, stopped, maps: [{source, target, ...counts, error?}]}`. Never raises for a failed
    map (the others still run); raises ImportRefused when nothing can start. `should_stop()` is asked between batches and
    files: once it says yes the current map is left as it is (hidden, resumable), the later maps are not started
    (`not_started`), and the report says `stopped`."""
    if not plan.get("ok"):
        raise ImportRefused("the plan has errors: " + "; ".join(plan.get("errors") or ["unknown"]))
    problems = reader.verify()
    if problems:
        raise ImportRefused("bundle damaged: " + "; ".join(problems[:5]))

    async def tell(**kw: Any) -> None:
        if progress is not None:
            await progress(kw)

    report: Dict[str, Any] = {"import_id": import_id, "ok": True, "stopped": False, "maps": []}
    for entry in plan["maps"]:
        src, dst = entry["source_name"], entry["target_name"]
        out: Dict[str, Any] = {"source": src, "target": dst, "action": entry["action"]}
        report["maps"].append(out)
        if not report["stopped"] and should_stop is not None and should_stop():
            report["ok"], report["stopped"] = False, True               # asked to stop between two maps
        if report["stopped"]:
            out["not_started"] = True
            continue
        try:
            await _apply_map(reader, plan, store, import_id, src, dst, out, tell, should_stop)
            out["ok"] = True
        except ImportStopped:
            out["ok"], out["stopped"] = False, True
            report["ok"], report["stopped"] = False, True
        except Exception as exc:  # noqa: BLE001 - one map failing must not stop the others
            logger.exception("Import %s: map %s -> %s failed", import_id, src, dst)
            out["ok"], out["error"] = False, f"{type(exc).__name__}: {exc}"
            report["ok"] = False


    renames = plan.get("map_renames") or {}
    report["runs"] = []
    for entry in plan.get("runs") or []:
        out = {"mission": entry["mission"]}
        report["runs"].append(out)
        if report["stopped"] or (should_stop is not None and should_stop()):
            report["ok"], report["stopped"] = False, True
            out["not_started"] = True
            continue
        try:
            await _apply_runs(reader, store, import_id, entry["mission"], renames, out, tell, should_stop)
            out["ok"] = True
        except ImportStopped:
            out["ok"], out["stopped"] = False, True
            report["ok"], report["stopped"] = False, True
        except Exception as exc:  # noqa: BLE001
            logger.exception("Import %s: runs of %s failed", import_id, entry["mission"])
            out["ok"], out["error"] = False, f"{type(exc).__name__}: {exc}"
            report["ok"] = False
    report["rosbags"] = []
    for entry in plan.get("rosbags") or []:
        out = {"robot": entry["robot"], "bag_id": entry["bag_id"]}
        report["rosbags"].append(out)
        if report["stopped"] or (should_stop is not None and should_stop()):
            report["ok"], report["stopped"] = False, True
            out["not_started"] = True
            continue
        try:
            await _apply_rosbag(reader, store, import_id, entry, renames, out, tell, should_stop)
            out["ok"] = True
        except ImportStopped:
            out["ok"], out["stopped"] = False, True
            report["ok"], report["stopped"] = False, True
        except Exception as exc:  # noqa: BLE001
            logger.exception("Import %s: recording %s/%s failed", import_id, entry["robot"], entry["bag_id"])
            out["ok"], out["error"] = False, f"{type(exc).__name__}: {exc}"
            report["ok"] = False
    return report


async def _apply_map(reader: BundleReader, plan: Dict[str, Any], store: TargetStore, import_id: str,
                     src: str, dst: str, out: Dict[str, Any], tell: ProgressFn,
                     should_stop: Optional[StopFn] = None) -> None:
    def check() -> None:
        if should_stop is not None and should_stop():
            raise ImportStopped()
    base = map_dir(src)
    meta = reader.read_json(f"{base}/map.json")
    marker = {"source": plan["source_key"], "source_name": (plan.get("source") or {}).get("name"),
              "map": src, "import_id": import_id, "phase": "importing", "created_at": plan.get("created_at")}
    async with await store.lock(dst):
        row = await store.get_row(dst)
        if row is not None:
            theirs = (row.get("status") or {}).get("imported_from") or {}
            if theirs.get("source") != marker["source"] or theirs.get("map") != src:
                raise ImportRefused(f"{dst!r} now exists and was not imported from this source; run the plan again")
            if theirs.get("phase") == "complete":
                marker["import_id"] = theirs.get("import_id", import_id)   # keep who created it first
        else:
            spec = dict(meta.get("spec") or {})
            status = dict(meta.get("status") or {})
            for k in ("open_session_id", "delete_requested_at", "delete_attempts", "delete_error"):
                status.pop(k, None)
            source_state = status.get("state")
            marker["source_state"] = source_state
            status.update({"state": ARCHIVED, "imported_from": marker})
            if not await store.begin_map(dst, spec, status):
                raise ImportRefused(f"{dst!r} was created by someone else while the import started")
        row = await store.get_row(dst)
        marker = dict((row.get("status") or {}).get("imported_from") or marker)
        ids = list(marker.get("import_ids") or [marker.get("import_id")])
        if import_id not in ids:
            ids.append(import_id)       # a rollback of any run that touched the map finds it
        marker["import_ids"] = ids
        await tell(map=dst, phase="sessions")
        listed_sessions = meta.get("sessions") or []
        out["sessions"] = await store.add_sessions(dst, listed_sessions)
        if out["sessions"] < len(listed_sessions):
            # Either a resume (already there) or a session id this server has under another map.
            out["sessions_not_inserted"] = len(listed_sessions) - out["sessions"]

        await tell(map=dst, phase="nodes")
        nodes = 0
        for batch in _batches((rewrite_node(d, src, dst) for d in reader.iter_ndjson(f"{base}/nodes.ndjson")), BATCH):
            check()
            nodes += await store.put_nodes(dst, batch)
            out["nodes"] = nodes
            await tell(map=dst, phase="nodes", done=nodes)
        out["nodes"] = nodes
        await tell(map=dst, phase="edges")
        edges = 0
        for batch in _batches((rewrite_edge(d, src, dst) for d in reader.iter_ndjson(f"{base}/edges.ndjson")), BATCH):
            check()
            edges += await store.put_edges(dst, batch)
            out["edges"] = edges
        out["edges"] = edges

        copied = skipped = 0
        listed = list(reader.iter_ndjson(f"{base}/objects.ndjson")) if reader.has(f"{base}/objects.ndjson") else []
        out["objects_total"] = len(listed)
        for i, obj in enumerate(listed, 1):
            out["objects_copied"], out["objects_skipped"] = copied, skipped      # what a stop here leaves behind
            check()
            have = await store.object_size(dst, obj["key"])
            if have == obj["size"]:
                skipped += 1
                continue
            if obj.get("present"):
                raise ImportRefused(f"{obj['key']} was left out of the bundle because this server had it, but it is gone now; "
                                    "start the transfer again")
            with reader.open(f"{base}/objects/{obj['key']}") as stream:
                await store.put_object(dst, obj["key"], stream, obj["size"], obj.get("content_type"),
                                       {str(k): str(v) for k, v in (obj.get("metadata") or {}).items()})
            copied += 1
            if i % 50 == 0:
                await tell(map=dst, phase="objects", done=i, total=len(listed))
        out["objects_copied"], out["objects_skipped"] = copied, skipped
        check()

        if meta.get("view_settings"):
            out["view_settings"] = await store.put_view_settings(dst, meta["view_settings"])

        await tell(map=dst, phase="commit")
        marker = dict(marker, phase="complete")
        await store.commit_map(dst, final_state(marker.get("source_state"), out["nodes"]), marker)


RUN_BATCH = 500


def remap_run(row: Dict[str, Any], renames: Dict[str, str]) -> Dict[str, Any]:
    """A run or leg row for the target: its map id follows the rename of that map (when it came with the same transfer)."""
    out = dict(row)
    mid = out.get("map_id")
    if mid and mid in renames:
        out["map_id"] = renames[mid]
    return out


async def _apply_runs(reader: BundleReader, store: TargetStore, import_id: str, mission: str, renames: Dict[str, str], out: Dict[str, Any],
                      tell: ProgressFn, should_stop: Optional[StopFn]) -> None:
    base = runs_dir(mission)
    runs = [remap_run(r, renames) for r in reader.iter_ndjson(f"{base}/runs.ndjson")]
    legs_by_run: Dict[str, List[Dict[str, Any]]] = {}
    for leg in reader.iter_ndjson(f"{base}/legs.ndjson"):
        legs_by_run.setdefault(str(leg["run_id"]), []).append(remap_run(leg, renames))
    out.update({"runs": len(runs), "runs_inserted": 0, "legs_inserted": 0})
    for batch in _batches(runs, RUN_BATCH):
        if should_stop is not None and should_stop():
            raise ImportStopped()
        legs = [leg for r in batch for leg in legs_by_run.get(str(r["run_id"]), [])]
        res = await store.add_runs(import_id, batch, legs)
        out["runs_inserted"] += res.get("runs", 0)
        out["legs_inserted"] += res.get("legs", 0)
        await tell(phase="runs", mission=mission, done=out["runs_inserted"], total=len(runs))
    out["runs_existing"] = len(runs) - out["runs_inserted"]


async def _apply_rosbag(reader: BundleReader, store: TargetStore, import_id: str, entry: Dict[str, Any], renames: Dict[str, str],
                        out: Dict[str, Any], tell: ProgressFn, should_stop: Optional[StopFn]) -> None:
    robot, bag_id = entry["robot"], entry["bag_id"]
    base = rosbag_dir(robot, bag_id)
    meta = reader.read_json(f"{base}/meta.json")
    size = int(meta["size"])
    out["bytes"] = size
    have = await store.rosbag_size(robot, bag_id)
    if have == size:
        out["skipped"] = True                                              # copied before (a repeat or a continue)
        return
    if entry.get("action") == "have":
        raise ImportRefused(f"{robot}/{bag_id} was left out of the bundle because this server had it, but it is gone now; "
                            "start the transfer again")
    if have is not None:
        raise ImportRefused(f"{robot}/{bag_id} already exists here with a different size ({have} bytes, not {size}); not overwritten")
    if should_stop is not None and should_stop():
        raise ImportStopped()
    await tell(phase="rosbag", robot=robot, bag_id=bag_id, bytes=size)
    sidecar = {k: v for k, v in meta.items() if k not in ("size", "last_modified", "download_url")}
    if sidecar.get("map_id") in renames:
        sidecar["map_id"] = renames[sidecar["map_id"]]
    with reader.open(f"{base}/data") as stream:
        await store.put_rosbag(import_id, robot, bag_id, stream, size, sidecar)
    out["copied"] = True
