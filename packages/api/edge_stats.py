"""Per-edge traversal statistics of the topomap: GET /api/v1/maps/{map_id}/edge-stats and the
writer for finished mapping sessions.

Observations live in `edge_observations` (migration 20261011_01_edge_observations, newest 20 per
map / edge / robot, packages/utils/edge_stats_db.py). Mission legs are written by mission-dispatch
when a run ends (packages/controllers/mission/fleet_recorder.py); mapping sessions are written
here, when the session is finished (`record_session`, called after the finishing transaction
committed: a failure is logged and never fails or undoes the finish). The statistics are computed
on read by packages/utils/edge_stats.py and only for pairs that are an edge of the map NOW (an
observation on a pair without an edge, or on an edge since deleted, is ignored, not an error).
Contract: packages/api/README.md. Groundwork for edge weights: nothing here feeds the planner.
"""
import asyncio
import logging
import uuid
from typing import Any, Dict, List, Mapping, Optional, Set

from fastapi import HTTPException

from packages import config
from packages.api import maps, session_trace
from packages.api.session_trace import page_args
from packages.utils import edge_stats as es
from packages.utils import edge_stats_db
from packages.utils import map_sessions as ms

logger = logging.getLogger("ApiDelegationService.edge_stats")


def _min_count(value: Optional[int]) -> int:
    count = 1 if value is None else int(value)
    if count < 1:
        raise session_trace._query_error("min_count", "must be >= 1")
    return count


async def edge_stats(db: Any, graph: Any, map_id: str, robot: Optional[str] = None,
                     min_count: Optional[int] = None, limit: Optional[int] = None,
                     offset: Optional[int] = None) -> Dict[str, Any]:
    """The response of GET /api/v1/maps/{map_id}/edge-stats. 404 unknown map; 422 bad
    limit / offset / min_count."""
    limit, offset = page_args(limit, offset)
    min_count = _min_count(min_count)
    try:
        async with maps.open_store(db, uuid.uuid4()) as store:
            if await store.get_map(map_id) is None:
                raise HTTPException(404, f"Did not find \"map\" with name \"{map_id}\"")
            observations = await edge_stats_db.fetch(store.cursor, map_id, robot or None)
    except maps._SCHEMA_ERRORS as exc:
        raise maps._undefined_table(exc) from exc
    edges = es.edge_set(await asyncio.to_thread(graph.get_edges, map_id))
    items = [i for i in es.group_stats(es.on_edges(observations, edges)) if i["count"] >= min_count]
    page = items[offset:offset + limit]
    end = offset + len(page)
    return {
        "map_id": map_id,
        "robot": robot or None,
        "min_count": min_count,
        "window": config.EDGE_STATS_WINDOW,
        "total": len(items),
        "limit": limit,
        "offset": offset,
        "next_offset": end if end < len(items) else None,
        "items": [{k: v for k, v in i.items() if k != "map_id"} for i in page],
    }


async def session_observations(graph: Any, session: Mapping[str, Any],
                               edges: Optional[Set[es.EdgeKey]] = None) -> List[Dict[str, Any]]:
    """The observations of a mapping session that lie on an existing edge of its map (`edges`:
    the map's edge set, read when not given). An operate session adds nothing: []. RAISES on an
    ArangoDB error."""
    map_id, sid = session["map_name"], str(session["session_id"])
    if ms.purpose_of(session) != ms.MAPPING:
        return []
    rows = await asyncio.to_thread(graph.session_trace_nodes, map_id, sid)
    nodes = session_trace.ordered_nodes(rows)
    observations = es.session_observations(map_id, sid, session["robot_name"], nodes)
    if not observations:
        return []
    if edges is None:
        edges = es.edge_set(await asyncio.to_thread(graph.get_edges, map_id))
    return es.on_edges(observations, edges)


async def record_session(db: Any, graph: Any, session: Mapping[str, Any]) -> int:
    """Store the observations of a finished mapping session (a repeat changes nothing). Never
    raises: a failure is logged. Returns the number of rows inserted."""
    try:
        observations = await session_observations(graph, session)
        if not observations:
            return 0
        async with db.connection() as conn:
            async with conn.cursor() as cursor:
                return await edge_stats_db.store(cursor, observations)
    except Exception:  # noqa: BLE001
        logger.exception("Could not record the edge observations of session %s",
                         session.get("session_id"))
        return 0
