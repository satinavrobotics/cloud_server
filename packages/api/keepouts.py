"""Keep-out zones of a map (docs/keepout_zones.md): regions and edges of the scene graph an operator switched off by hand, so the
mission planner never routes through them.

    GET    /api/v1/maps/{map}/keepouts        -> {"map_id", "keepouts": [Keepout], "revision"}
    POST   /api/v1/maps/{map}/keepouts        {kind, label?, region | edge} -> Keepout (201)
    PATCH  /api/v1/maps/{map}/keepouts/{id}   {enabled?, label?} -> Keepout
    DELETE /api/v1/maps/{map}/keepouts/{id}   -> 204

Keepout = {id, kind, label, enabled, created_at, region | edge, applies: "applied" | "unresolved", reason?}. `applies` is "unresolved"
when the zone cannot be mapped onto the map's current scene graph (its chart no longer exists, no link joins the two charts, or
there is no graph): the planner then ignores it, and `reason` says why. Persistent until an operator deletes or disables it; the
robot-reported blocks (blocked_graph_nodes) and their expiry are a separate table (packages/utils/keepouts.py).

404 map_not_found / keepout_not_found, 409 map_deleting / keepout_limit / keepout_exists, 422 invalid_keepout / invalid_polygon /
chart_not_found / no_link_between_charts / point_off_graph (errors as {"detail": {"code", "message"}}, like the sibling routes).
"""
import json
import logging
import uuid
from typing import Any, Callable, Dict, List, Optional

from fastapi import HTTPException

from packages.api import map_view_settings
from packages.utils import keepouts as ko

logger = logging.getLogger("ApiDelegationService.keepouts")

NO_GRAPH = "no_scene_graph"
NO_CHART = "chart_not_found"
NO_LINK = "no_link_between_charts"
OFF_GRAPH = "point_off_graph"
POINT_SEARCH_M = 0.5            # a point of an edge is moved to the nearest chart cell within this distance


def http_error(err: ko.KeepoutError) -> HTTPException:
    return HTTPException(err.status, {"code": err.code, "message": err.message})


def _err(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, {"code": code, "message": message})


def applies_of(row: Dict[str, Any], topology: Optional[Dict[str, Any]]) -> tuple:
    """(applies, reason) of a stored row against the map's chart topology (None: no scene graph / not readable)."""
    spec = row["spec"]
    if "polygon" in spec:
        return ko.APPLIED, None                     # a shape needs no graph
    if topology is None:
        return ko.UNRESOLVED, NO_GRAPH
    keys, links = topology["keys"], topology["links"]
    if "chart_key" in spec:
        return (ko.APPLIED, None) if spec["chart_key"] in keys else (ko.UNRESOLVED, NO_CHART)
    ends = [spec["from"], spec["to"]]
    named = [e for e in ends if isinstance(e, str)]
    if any(e not in keys for e in named):
        return ko.UNRESOLVED, NO_CHART
    if len(named) == 2 and frozenset(named) not in links:
        return ko.UNRESOLVED, NO_LINK
    return ko.APPLIED, None                         # a point is placed on the graph by the planner


class KeepoutStore:
    """map_keepouts on the API's PostgresDatabase. `reads` is a callable returning the scene-graph read side
    (`_scene_graph_reads`), used only to tell which zones still apply and to place the points of an edge."""

    def __init__(self, db: Any, reads: Optional[Callable[[], Any]] = None):
        self._db = db
        self._reads = reads

    # -- scene graph (best effort: a graph that cannot be read leaves zones "unresolved", never fails a read)
    async def _topology(self, map_id: str) -> Optional[Dict[str, Any]]:
        if self._reads is None:
            return None
        try:
            return await self._reads().topology(map_id)
        except Exception as exc:  # pylint: disable=broad-except
            logger.info("keep-outs of %s: scene graph topology not readable (%s)", map_id, exc)
            return None

    async def _chart_at(self, map_id: str, point: List[float]) -> Optional[str]:
        """The chart key at (or within POINT_SEARCH_M of) the point; None when nothing is found or the graph cannot be read."""
        if self._reads is None:
            return None
        from packages.scene_graph.api import BBox
        from packages.utils import blocked_chart_tags
        x, y = point
        try:
            plane = await self._reads().owner_plane(map_id, BBox(x - 1.0, y - 1.0, x + 1.0, y + 1.0), 0)
        except Exception as exc:  # pylint: disable=broad-except
            logger.info("keep-outs of %s: owner plane not readable (%s)", map_id, exc)
            return None
        if plane is None:
            return None
        import numpy as np
        rows, cols = plane["shape"]
        locate = blocked_chart_tags.plane_locator(plane["origin"], plane["step"], (rows, cols),
                                                  np.asarray(plane["index"]).reshape(rows, cols), plane["keys"])
        step = float(plane["step"])
        n = int(POINT_SEARCH_M / step)
        best = None
        for dy in range(-n, n + 1):
            for dx in range(-n, n + 1):
                key = locate(x + dx * step, y + dy * step)
                d = dx * dx + dy * dy
                if key is not None and (best is None or d < best[0]):
                    best = (d, key)
        return best[1] if best else None

    # -- map
    async def _map(self, conn: Any, map_id: str) -> None:
        cur = await conn.execute(map_view_settings.MAP_SQL, (map_id,))
        row = await cur.fetchone()
        if row is None:
            raise _err(404, "map_not_found", f"Map '{map_id}' not found")
        if row[0] == "DELETING":
            raise _err(409, "map_deleting", f"Map '{map_id}' is being deleted")

    async def _rows(self, conn: Any, map_id: str) -> List[Dict[str, Any]]:
        cur = await conn.execute(ko.LIST_SQL, (map_id,))
        return [ko.row_dict(r) for r in await cur.fetchall()]

    @staticmethod
    def _keepout(row: Dict[str, Any], topology: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        applies, reason = applies_of(row, topology)
        return ko.to_keepout(row, applies, reason)

    # -- the four routes
    async def list_all(self, map_id: str) -> Dict[str, Any]:
        async with self._db.connection() as conn:
            await self._map(conn, map_id)
            rows = await self._rows(conn, map_id)
        topology = await self._topology(map_id) if any("polygon" not in r["spec"] for r in rows) else None
        return {"map_id": map_id, "keepouts": [self._keepout(r, topology) for r in rows], "revision": ko.revision(rows)}

    async def create(self, map_id: str, body: Any) -> Dict[str, Any]:
        try:
            kind, label, spec = ko.parse_create(body)
        except ko.KeepoutError as err:
            raise http_error(err) from err
        topology = await self._topology(map_id) if "polygon" not in spec else None
        try:
            spec = await self._place(map_id, spec, topology)
        except ko.KeepoutError as err:
            raise http_error(err) from err
        async with self._db.connection() as conn:
            await self._map(conn, map_id)
            for row in await self._rows(conn, map_id):
                if row["kind"] == kind and row["spec"] == spec:
                    raise _err(409, "keepout_exists", f"The same keep-out exists already ({row['id']})")
            cur = await conn.execute(ko.INSERT_SQL, (uuid.uuid4().hex, map_id, kind, label, json.dumps(spec),
                                                     map_id, ko.MAX_PER_MAP))
            row = await cur.fetchone()
            if row is None:
                raise _err(409, "keepout_limit", f"A map holds at most {ko.MAX_PER_MAP} keep-outs")
            return self._keepout(ko.row_dict(row), topology)

    async def _place(self, map_id: str, spec: Dict[str, Any], topology: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Check a chart key / the two ends of an edge against the graph when it can be read, and replace the points of an edge
        by the chart keys at them (a key is stable, a point is not)."""
        if "polygon" in spec:
            return spec
        if "chart_key" in spec:
            if topology is not None and spec["chart_key"] not in topology["keys"]:
                raise ko.KeepoutError(422, NO_CHART, f"Chart '{spec['chart_key']}' is not in the map's scene graph")
            return spec
        ends = []
        for name in ("from", "to"):
            end = spec[name]
            if not isinstance(end, str):
                key = await self._chart_at(map_id, end)
                if key is None and topology is not None:
                    raise ko.KeepoutError(422, OFF_GRAPH, f"edge.{name} {end} is not on a chart of the scene graph")
                end = key if key is not None else end
            elif topology is not None and end not in topology["keys"]:
                raise ko.KeepoutError(422, NO_CHART, f"Chart '{end}' is not in the map's scene graph")
            ends.append(end)
        if ends[0] == ends[1]:
            raise ko.KeepoutError(422, "invalid_keepout", "edge.from and edge.to are the same chart")
        if topology is not None and all(isinstance(e, str) for e in ends) and frozenset(ends) not in topology["links"]:
            raise ko.KeepoutError(422, NO_LINK, "The two charts are not linked in the scene graph")
        return {"from": ends[0], "to": ends[1]}

    async def patch(self, map_id: str, keepout_id: str, body: Any) -> Dict[str, Any]:
        try:
            changes = ko.parse_patch(body)
        except ko.KeepoutError as err:
            raise http_error(err) from err
        async with self._db.connection() as conn:
            await self._map(conn, map_id)
            cur = await conn.execute(ko.GET_SQL, (map_id, keepout_id))
            current = await cur.fetchone()
            if current is None:
                raise _err(404, "keepout_not_found", f"Keep-out '{keepout_id}' not found on map '{map_id}'")
            now = ko.row_dict(current)
            enabled = changes.get("enabled", now["enabled"])
            label = changes["label"] if "label" in changes else now["label"]
            cur = await conn.execute(ko.UPDATE_SQL, (enabled, label, map_id, keepout_id))
            row = await cur.fetchone()
            if row is None:
                raise _err(404, "keepout_not_found", f"Keep-out '{keepout_id}' not found on map '{map_id}'")
            row = ko.row_dict(row)
        topology = await self._topology(map_id) if "polygon" not in row["spec"] else None
        return self._keepout(row, topology)

    async def delete(self, map_id: str, keepout_id: str) -> None:
        async with self._db.connection() as conn:
            await self._map(conn, map_id)
            cur = await conn.execute(ko.DELETE_SQL, (map_id, keepout_id))
            if await cur.fetchone() is None:
                raise _err(404, "keepout_not_found", f"Keep-out '{keepout_id}' not found on map '{map_id}'")
