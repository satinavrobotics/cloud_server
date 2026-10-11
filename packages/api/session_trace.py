"""GET /api/v1/maps/{map_id}/sessions/{session_id}/trace: what one mapping session laid down.

Read-only. The session comes from Postgres (`map_sessions`), the nodes from ArangoDB through
`GraphDatabaseService.session_trace_nodes` (filtered by session id and sorted there), the image
names from MinIO through the image service (one listing per node of the returned page). The
derived values (distance, dt, speed, summary) are computed here from the stored data by
packages/utils/session_trace.py. Contract: packages/api/README.md.
"""
import asyncio
import urllib.parse
import uuid
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from packages.api import maps
from packages.utils import capture_time
from packages.utils import map_sessions as ms
from packages.utils import session_trace as trace

DEFAULT_LIMIT = 100
MAX_LIMIT = 200
_LIST_CONCURRENCY = 8


def _query_error(name: str, msg: str) -> HTTPException:
    return HTTPException(422, [{"loc": ["query", name], "type": "value_error", "msg": msg}])


def page_args(limit: Optional[int], offset: Optional[int]) -> tuple:
    limit = DEFAULT_LIMIT if limit is None else int(limit)
    offset = 0 if offset is None else int(offset)
    if not 1 <= limit <= MAX_LIMIT:
        raise _query_error("limit", f"must be between 1 and {MAX_LIMIT}")
    if offset < 0:
        raise _query_error("offset", "must be >= 0")
    return limit, offset


async def _session_of(db: Any, map_id: str, session_id: str) -> Dict[str, Any]:
    """The session row; 404 for an unknown map, an unknown / malformed session id, or a session
    of another map."""
    try:
        sid = str(uuid.UUID(str(session_id)))
    except ValueError:
        raise HTTPException(404, f"Did not find session \"{session_id}\"") from None
    try:
        async with maps.open_store(db, uuid.uuid4()) as store:
            if await store.get_map(map_id) is None:
                raise HTTPException(404, f"Did not find \"map\" with name \"{map_id}\"")
            session = await store.session(sid)
    except maps._SCHEMA_ERRORS as exc:
        raise maps._undefined_table(exc) from exc
    if session is None or session["map_name"] != map_id:
        raise HTTPException(404, f"Did not find session \"{session_id}\" on map \"{map_id}\"")
    return session


def _image_entry(map_id: str, node_id: str, image_id: str) -> Dict[str, Any]:
    base = f"/api/v1/images/{urllib.parse.quote(map_id, safe='')}/" \
           f"{urllib.parse.quote(node_id, safe='')}?image_id={urllib.parse.quote(image_id, safe='')}"
    return {"image_id": image_id, "url": base, "thumb_url": base + "&size=thumb"}


async def _images_of(image_db: Any, map_id: str, node_ids: List[str]) -> Dict[str, List[str]]:
    gate = asyncio.Semaphore(_LIST_CONCURRENCY)

    async def one(node_id: str) -> List[str]:
        async with gate:
            return sorted(await asyncio.to_thread(image_db.list_node_images, node_id, map_id))

    found = await asyncio.gather(*(one(n) for n in node_ids))
    return dict(zip(node_ids, found))


def _capture(row: Dict[str, Any]) -> Optional[Any]:
    """The robot's capture time of a stored node: its `capture_time`, else (nodes stored before
    that field existed) derived from the stored camera_metadata[].timestamp (epoch ms) with the
    same sanity rules; None when neither is usable."""
    got = capture_time.parse(row.get("capture_time")) if row.get("capture_time") else None
    if got is None:
        stamps = [{"timestamp": v} for v in (row.get("camera_stamps_ms") or [])]
        got = capture_time.from_camera_metadata(stamps)
    return got


def _iso(when: Any) -> Optional[str]:
    return when.isoformat() if when is not None else None


def ordered_nodes(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The `session_trace_nodes` rows as trace nodes (x, y, time, capture, session_node_id, id,
    row) in laying-down order."""
    return trace.order([{"x": r.get("x"), "y": r.get("y"),
                         "time": trace.parse_time(r.get("timestamp")), "capture": _capture(r),
                         "session_node_id": r.get("session_node_id"), "id": str(r["id"]),
                         "row": r} for r in rows])


async def session_trace(db: Any, graph: Any, image_db: Any, map_id: str, session_id: str,
                        limit: Optional[int] = None, offset: Optional[int] = None
                        ) -> Dict[str, Any]:
    """The trace response (see packages/api/README.md). An operate session adds no data: an
    empty trace, not an error. 404 unknown map / session / session of another map; 422 bad
    limit / offset."""
    limit, offset = page_args(limit, offset)
    session = await _session_of(db, map_id, session_id)
    sid = str(session["session_id"])
    rows: List[Dict[str, Any]] = []
    if ms.purpose_of(session) == ms.MAPPING:
        rows = await asyncio.to_thread(graph.session_trace_nodes, map_id, sid)
    nodes = ordered_nodes(rows)
    rows = [n["row"] for n in nodes]
    source = trace.timestamp_source(nodes)
    all_steps = trace.steps(nodes)
    summary = trace.summarize(nodes, all_steps)
    page = range(offset, min(offset + limit, len(rows)))
    node_ids = [str(rows[i]["id"]) for i in page]
    images = await _images_of(image_db, map_id, node_ids) if node_ids else {}
    items = []
    for i in page:
        r, node_id = rows[i], str(rows[i]["id"])
        items.append({
            "index": i,
            "node_id": node_id,
            "session_node_id": r.get("session_node_id"),
            "timestamp": _iso(nodes[i]["capture"] or nodes[i]["time"]),
            "timestamp_source": (trace.SOURCE_CAPTURE if nodes[i]["capture"] is not None
                                 else trace.SOURCE_INGEST),
            "pose": {"x": r.get("x"), "y": r.get("y"), "yaw": r.get("yaw")},
            "images": [_image_entry(map_id, node_id, name) for name in images.get(node_id, [])],
            "depth": sorted(r.get("depth") or []),
            "costmap": sorted(r.get("costmap") or []),
            "from_previous": all_steps[i],
        })
    end = offset + len(items)
    return {
        "map_id": map_id,
        "session_id": sid,
        "purpose": ms.purpose_of(session),
        "state": ms.session_state(session),
        "timestamp_source": source,
        "total": len(rows),
        "limit": limit,
        "offset": offset,
        "next_offset": end if end < len(rows) else None,
        "summary": summary,
        "items": items,
    }
