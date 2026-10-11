"""Per-edge traversal statistics of the topomap: GET /api/v1/maps/{map_id}/edge-stats and the
writer for finished mapping sessions.

Observations live in `edge_observations` (migration 20261011_01_edge_observations, newest 20 per
map / edge / robot, packages/utils/edge_stats_db.py). Mission legs are written by mission-dispatch
when a run ends (packages/controllers/mission/fleet_recorder.py); mapping sessions are written
here, when the session is finished (`record_session`, called after the finishing transaction
committed and the robot-side actions are done, as a background task: it never delays them, holds
no robot lock, and a failure is logged and never fails or undoes the finish). A session whose
ArangoDB read failed stores nothing; a repeat finish changes nothing and so does not call the
writer again, so `scripts/backfill_edge_observations.py --apply` (idempotent) records it later.
The statistics are computed
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

# The running record_session tasks: a reference keeps each from being garbage collected.
_tasks: Set["asyncio.Task[Any]"] = set()


def _min_count(value: Optional[int]) -> int:
    count = 1 if value is None else int(value)
    if count < 1:
        raise session_trace._query_error("min_count", "must be >= 1")
    return count


def _edges_of(graph: Any, map_id: str) -> List[Dict[str, Any]]:
    """The map's edges; RAISES on an ArangoDB error (get_edges would answer [] and make a
    database outage look like a map without edges)."""
    return graph.get_edges_strict(map_id)


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
    try:
        edges = es.edge_set(await asyncio.to_thread(_edges_of, graph, map_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Edge statistics of map %s: could not read its edges: %s", map_id, exc)
        raise HTTPException(503, f"The edges of map \"{map_id}\" could not be read from "
                                 "ArangoDB; try again shortly") from exc
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
        edges = es.edge_set(await asyncio.to_thread(_edges_of, graph, map_id))
    return es.on_edges(observations, edges)


async def record_session(db: Any, graph: Any, session: Mapping[str, Any]) -> int:
    """Store the observations of a finished mapping session (a repeat changes nothing). Never
    raises: a failure is logged. Returns the number of rows inserted. When ArangoDB cannot be
    read nothing is stored (a warning); no marker says the session is done, so the backfill
    script records it later."""
    sid = session.get("session_id")
    try:
        observations = await session_observations(graph, session)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read the nodes / edges of session %s from ArangoDB, its edge "
                       "observations are not recorded (run scripts/backfill_edge_observations.py "
                       "--apply later): %s", sid, exc)
        return 0
    if not observations:
        return 0
    try:
        async with db.connection() as conn:
            async with conn.cursor() as cursor:
                return await edge_stats_db.store(cursor, observations)
    except Exception:  # noqa: BLE001
        logger.exception("Could not record the edge observations of session %s", sid)
        return 0


def _task_done(task: "asyncio.Task[Any]") -> None:
    _tasks.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.error("Recording the edge observations of a session failed", exc_info=task.exception())


def schedule_record_session(db: Any, graph: Any, session: Mapping[str, Any]
                            ) -> Optional["asyncio.Task[Any]"]:
    """Run record_session as a tracked background task and return at once (None when it could
    not be started); never raises. `session` is copied, the caller may change its own."""
    try:
        task = asyncio.get_running_loop().create_task(record_session(db, graph, dict(session)))
    except Exception:  # noqa: BLE001
        logger.exception("Could not start recording the edge observations of session %s",
                         session.get("session_id"))
        return None
    _tasks.add(task)
    task.add_done_callback(_task_done)
    return task
