"""GET /api/v1/maps/{map_id}/sessions/{session_id}/trajectory: one mapping session packed as a tar.

Read-only. The same nodes in the same order as the session trace (packages/api/session_trace.py),
plus the bytes of every node's images, depth PNGs and costmap PNGs, streamed as an uncompressed
tar (the PNGs / JPEGs are compressed already):

    manifest.json                 first: the plan (nodes, poses, records, member paths)
    {index:06d}/image_{id}        the node's images (the original bytes, any format)
    {index:06d}/depth_{camera}.png
    {index:06d}/costmap_{layer}.png
    missing.json                  last: the members the manifest names but that could not be read

Everything that can fail with a status code (404 / 422, the Arango and MinIO listings) runs
in `trajectory_pack` before the first byte is sent. After that a member that cannot be read is
skipped and listed in `missing.json`; an archive without `missing.json` was cut short.
Memory: one prefetch window of nodes, never the session. Contract: packages/api/README.md.
"""
import asyncio
import collections
import json
import re
import tarfile
import time
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Tuple

from packages.api.session_trace import (
    _images_of, _iso, _query_error, _session_of, ordered_nodes)
from packages.utils import map_sessions as ms
from packages.utils import session_trace as trace

FORMAT = 1
PREFETCH_NODES = 4        # nodes whose blobs are being read while an earlier one is sent
BLOB_CONCURRENCY = 8      # MinIO reads in flight, over all prefetched nodes
_BLOCK = 512
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")


def window_args(from_index: Optional[int], to_index: Optional[int], total: int) -> Tuple[int, int]:
    """[from_index, to_index) clamped to the session (a window past the end is empty, as in the
    trace); 422 for a negative index or an explicit to_index below from_index."""
    start = 0 if from_index is None else int(from_index)
    end = total if to_index is None else int(to_index)
    if start < 0:
        raise _query_error("from_index", "must be >= 0")
    if end < 0:
        raise _query_error("to_index", "must be >= 0")
    if to_index is not None and start > end:
        raise _query_error("to_index", "must be >= from_index")
    return min(start, total), min(end, total)


def _member(index: int, kind: str, name: str, ext: str = "") -> str:
    """The tar member name: ASCII only, no separator or dot-dot from a camera / layer name."""
    return f"{index:06d}/{kind}_{_UNSAFE.sub('_', str(name)).lstrip('.') or '_'}{ext}"


def _without_session(record: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in record.items() if k != "session_id"}


def _item(index: int, node: Dict[str, Any], step: Optional[Dict[str, Any]],
          images: List[str]) -> Dict[str, Any]:
    row = node["row"]
    depth, costmap = row.get("depth") or {}, row.get("costmap") or {}
    return {
        "index": index,
        "node_id": str(row["id"]),
        "session_node_id": row.get("session_node_id"),
        "timestamp": _iso(node["capture"] or node["time"]),
        "timestamp_source": (trace.SOURCE_CAPTURE if node["capture"] is not None
                             else trace.SOURCE_INGEST),
        "pose": {"x": row.get("x"), "y": row.get("y"), "yaw": row.get("yaw")},
        "from_previous": step,
        "images": [{"image_id": i, "path": _member(index, "image", i)} for i in images],
        "depth": {cam: {"path": _member(index, "depth", cam, ".png"),
                        "record": _without_session(rec or {})}
                  for cam, rec in sorted(depth.items())},
        "costmap": {layer: {"path": _member(index, "costmap", layer, ".png"),
                            "record": _without_session(rec or {})}
                    for layer, rec in sorted(costmap.items())},
    }


def _blobs(item: Dict[str, Any]) -> List[Tuple[str, str, str]]:
    """(kind, name, path) of the item's members, in tar order."""
    return ([("image", i["image_id"], i["path"]) for i in item["images"]]
            + [("depth", c, d["path"]) for c, d in item["depth"].items()]
            + [("costmap", c, d["path"]) for c, d in item["costmap"].items()])


def _json(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _header(name: str, size: int, mtime: int) -> bytes:
    info = tarfile.TarInfo(name)
    info.size, info.mtime, info.mode = size, mtime, 0o644
    return info.tobuf(tarfile.GNU_FORMAT, "utf-8", "strict")


def _member_bytes(name: str, data: bytes, mtime: int) -> bytes:
    return _header(name, len(data), mtime) + data + b"\0" * (-len(data) % _BLOCK)


class Pack:
    """A validated, planned trajectory: `manifest` is final, `stream()` sends the archive."""

    def __init__(self, manifest: Dict[str, Any], map_id: str, image_db: Any) -> None:
        self.manifest = manifest
        self.map_id = map_id
        self.image_db = image_db
        self.missing: List[Dict[str, str]] = []

    @property
    def filename(self) -> str:
        """`{map}_{session id prefix}_{from_index}.tar`, ASCII with no quote or separator."""
        m = self.manifest
        return f"{_UNSAFE.sub('_', m['map_id'])}_{m['session_id'][:8]}_{m['from_index']}.tar"

    def _reader(self, kind: str, node_id: str, name: str) -> Callable[[], Optional[bytes]]:
        db, map_id = self.image_db, self.map_id
        return {"image": lambda: db.get_image(name, node_id, map_id),
                "depth": lambda: db.get_depth(node_id, name, map_id),
                "costmap": lambda: db.get_costmap(node_id, name, map_id)}[kind]

    async def _read(self, gate: asyncio.Semaphore, item: Dict[str, Any]
                    ) -> List[Tuple[str, Optional[bytes], str]]:
        """(path, bytes or None, reason) of the item's members; a read that raises is missing too."""
        async def one(kind: str, name: str, path: str) -> Tuple[str, Optional[bytes], str]:
            async with gate:
                try:
                    data = await asyncio.to_thread(self._reader(kind, item["node_id"], name))
                except Exception as exc:  # noqa: BLE001 - one bad object must not cut the stream
                    return path, None, f"error: {type(exc).__name__}"
            return path, data, "not_found"

        return list(await asyncio.gather(*(one(*b) for b in _blobs(item))))

    async def stream(self) -> AsyncIterator[bytes]:
        self.missing = []
        mtime = int(time.time())
        yield _member_bytes("manifest.json", _json(self.manifest), mtime)
        gate = asyncio.Semaphore(BLOB_CONCURRENCY)
        pending: collections.deque = collections.deque()
        items = iter(self.manifest["items"])
        try:
            while True:
                for item in items:
                    pending.append(asyncio.ensure_future(self._read(gate, item)))
                    if len(pending) >= PREFETCH_NODES:
                        break
                if not pending:
                    break
                for path, data, reason in await pending.popleft():
                    if data is None:
                        self.missing.append({"path": path, "reason": reason})
                    else:
                        yield _member_bytes(path, data, mtime)
            yield _member_bytes("missing.json", _json({"missing": self.missing}), mtime)
            yield b"\0" * (2 * _BLOCK)
        finally:
            for task in pending:   # the client went away: stop reading for it
                task.cancel()


async def trajectory_pack(db: Any, graph: Any, image_db: Any, map_id: str, session_id: str,
                          from_index: Optional[int] = None,
                          to_index: Optional[int] = None) -> Pack:
    """Validate and plan the pack of nodes [from_index, to_index) of the session (404 unknown
    map / session / session of another map; 422 bad window). An operate session adds no data:
    an empty pack, not an error."""
    session = await _session_of(db, map_id, session_id)
    sid = str(session["session_id"])
    rows: List[Dict[str, Any]] = []
    if ms.purpose_of(session) == ms.MAPPING:
        rows = await asyncio.to_thread(graph.session_trajectory_nodes, map_id, sid)
    nodes = ordered_nodes(rows)
    start, end = window_args(from_index, to_index, len(nodes))
    all_steps = trace.steps(nodes)
    node_ids = [str(nodes[i]["row"]["id"]) for i in range(start, end)]
    images = await _images_of(image_db, map_id, node_ids) if node_ids else {}
    items = [_item(i, nodes[i], all_steps[i], images.get(node_ids[i - start], []))
             for i in range(start, end)]
    manifest = {
        "format": FORMAT,
        "map_id": map_id,
        "session_id": sid,
        "purpose": ms.purpose_of(session),
        "state": ms.session_state(session),
        "timestamp_source": trace.timestamp_source(nodes),
        "total": len(nodes),
        "from_index": start,
        "to_index": end,
        "next_index": end if end < len(nodes) else None,
        "summary": trace.summarize(nodes, all_steps),
        "items": items,
    }
    return Pack(manifest, map_id, image_db)
