"""Keep-out zones of a map (docs/keepout_zones.md): spaces an operator switched off by hand, so the mission planner never routes
through them. A zone is stored as GEOMETRY in the map frame (a polygon, or a line across a passage), never as a reference into the
scene graph: a chart or an edge in a create request is snapshotted to geometry at that moment (`source` keeps what it was made from).

    GET    /api/v1/maps/{map}/keepouts        -> {"map_id", "keepouts": [Keepout], "revision"}
    POST   /api/v1/maps/{map}/keepouts        {kind, label?, region | edge} -> Keepout (201)
    PATCH  /api/v1/maps/{map}/keepouts/{id}   {enabled?, label?} -> Keepout
    DELETE /api/v1/maps/{map}/keepouts/{id}   -> 204

Keepout = {id, kind, label, enabled, created_at, polygon (region) | line (edge), source?, region | edge (compat), applies, reason?}.
Create bodies: {kind: region, region: {polygon} | {chart_key}} (or `polygon`), {kind: edge, edge: {from, to}} | {line} (or `line`).
`applies` is "unresolved" only for a LEGACY row (made before the snapshot, still a chart key / chart pair) that the current graph
cannot place; such rows are rewritten as geometry on a list read as soon as the graph is readable (KeepoutStore.migrate). Persistent until an operator deletes or disables it; the
robot-reported blocks (blocked_graph_nodes) and their expiry are a separate table (packages/utils/keepouts.py).

404 map_not_found / keepout_not_found, 409 map_deleting / keepout_limit / keepout_exists / no_scene_graph (chart/edge create: the map has
no graph), 503 scene_graph_unavailable (chart/edge create: the graph cannot be read, retry), 422 invalid_keepout / invalid_polygon /
invalid_line / chart_not_found / no_link_between_charts / point_off_graph / chart_geometry_unavailable (errors as {"detail": {"code", "message"}}, like the sibling routes).
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
NO_GEOMETRY = "chart_geometry_unavailable"
GRAPH_DOWN = "scene_graph_unavailable"
CHART_WINDOWS_M = (4.0, 8.0, 16.0)   # half-sizes of the owner-plane window around a chart: grown while the chart touches its edge
POINT_SEARCH_M = 0.5            # a point of an edge is moved to the nearest chart cell within this distance


def http_error(err: ko.KeepoutError) -> HTTPException:
    return HTTPException(err.status, {"code": err.code, "message": err.message})


def _err(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, {"code": code, "message": message})


def applies_of(row: Dict[str, Any], topology: Optional[Dict[str, Any]]) -> tuple:
    """(applies, reason) of a stored row against the map's chart topology (None: no scene graph / not readable)."""
    spec = row["spec"]
    if ko.is_geometry(spec):
        return ko.APPLIED, None                     # geometry needs no graph
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
    def set_reads(self, reads: Callable[[], Any]) -> None:
        self._reads = reads

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
        if any(ko.is_legacy(r["spec"]) for r in rows):
            rows = await self.migrate(map_id, rows)
        legacy = any(ko.is_legacy(r["spec"]) for r in rows)
        topology = await self._topology(map_id) if legacy else None
        return {"map_id": map_id, "keepouts": [self._keepout(r, topology) for r in rows], "revision": ko.revision(rows)}

    async def create(self, map_id: str, body: Any) -> Dict[str, Any]:
        try:
            kind, label, spec = ko.parse_create(body)
            if not ko.is_geometry(spec):
                async with self._db.connection() as conn:
                    await self._map(conn, map_id)                 # 404 / 409 before any graph read
                spec = await self.snapshot(map_id, spec)          # the geometry of the chart / chart pair, as of now
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
            return self._keepout(ko.row_dict(row), None)

    # -- snapshot: chart / chart pair -> geometry
    async def _strict_topology(self, map_id: str) -> Dict[str, Any]:
        """The chart topology, or a 503 (graph not readable: retry) / 409 (the map has no graph). Never a silent None."""
        if self._reads is None:
            raise ko.KeepoutError(503, GRAPH_DOWN, "The scene graph is not available: a keep-out made from a chart or an edge cannot be placed")
        try:
            topology = await self._reads().topology(map_id)
        except Exception as exc:  # pylint: disable=broad-except
            logger.info("keep-outs of %s: scene graph topology not readable (%s)", map_id, exc)
            raise ko.KeepoutError(503, GRAPH_DOWN, "The scene graph cannot be read now: retry, or draw the zone as a polygon / line") from exc
        if topology is None:
            raise ko.KeepoutError(409, NO_GRAPH, "The map has no scene graph: draw the zone as a polygon / line")
        return topology

    async def _window(self, map_id: str, anchors: List[Any], pad: float) -> Dict[str, Any]:
        from packages.scene_graph.api import BBox
        xs, ys = [a[0] for a in anchors], [a[1] for a in anchors]
        try:
            plane = await self._reads().owner_plane(map_id, BBox(min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad), 0)
        except Exception as exc:  # pylint: disable=broad-except
            logger.info("keep-outs of %s: owner plane not readable (%s)", map_id, exc)
            raise ko.KeepoutError(503, GRAPH_DOWN, "The scene graph cannot be read now: retry, or draw the zone as a polygon / line") from exc
        if plane is None:
            raise ko.KeepoutError(409, NO_GRAPH, "The map has no scene graph: draw the zone as a polygon / line")
        return plane

    async def snapshot(self, map_id: str, spec: Dict[str, Any], topology: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """The geometry spec of a chart / chart-pair spec (a request or a legacy row) from the CURRENT scene graph:
        {"polygon", "source": {"chart_key"}} or {"line", "source": {"from", "to"}}. KeepoutError (422 unknown chart / not linked /
        point off graph, 409 no graph, 503 graph unreadable): a zone that cannot be placed is refused, never stored unresolved."""
        from packages.utils import keepout_geometry as kg
        topology = topology if topology is not None else await self._strict_topology(map_id)
        anchors = topology.get("anchors") or {}
        if "chart_key" in spec:
            key = spec["chart_key"]
            if key not in topology["keys"]:
                raise ko.KeepoutError(422, NO_CHART, f"Chart '{key}' is not in the map's scene graph")
            if key not in anchors:
                raise ko.KeepoutError(503, NO_GEOMETRY, f"The position of chart '{key}' is not known: retry")
            poly = None
            for pad in CHART_WINDOWS_M:
                plane = await self._window(map_id, [anchors[key]], pad)
                mask = kg.chart_mask(plane, key)
                if mask.any() and not kg.touches_border(mask):
                    break
            poly = kg.mask_polygon(mask, plane["origin"], float(plane["step"])) if mask.any() else None
            try:
                poly = ko.normalise_polygon(poly) if poly else None
            except ko.KeepoutError:
                poly = None
            if poly is None:
                raise ko.KeepoutError(503, NO_GEOMETRY, f"The footprint of chart '{key}' cannot be read now: retry")
            return {"polygon": poly, "source": {"chart_key": key}}
        ends = []
        for name in ("from", "to"):
            end = spec[name]
            if not isinstance(end, str):
                key = await self._chart_at(map_id, end)
                if key is None:
                    raise ko.KeepoutError(422, OFF_GRAPH, f"edge.{name} {end} is not on a chart of the scene graph")
                end = key
            elif end not in topology["keys"]:
                raise ko.KeepoutError(422, NO_CHART, f"Chart '{end}' is not in the map's scene graph")
            ends.append(end)
        if ends[0] == ends[1]:
            raise ko.KeepoutError(422, "invalid_keepout", "edge.from and edge.to are the same chart")
        if frozenset(ends) not in topology["links"]:
            raise ko.KeepoutError(422, NO_LINK, "The two charts are not linked in the scene graph")
        if not all(e in anchors for e in ends):
            raise ko.KeepoutError(503, NO_GEOMETRY, "The position of the charts is not known: retry")
        pts = [anchors[e] for e in ends]
        line = None
        for pad in CHART_WINDOWS_M:
            plane = await self._window(map_id, pts, pad)
            ma, mb = kg.chart_mask(plane, ends[0]), kg.chart_mask(plane, ends[1])
            if not (kg.touches_border(ma) or kg.touches_border(mb)):
                break
        line = kg.edge_line(plane, ends[0], ends[1], (pts[0], pts[1]))
        try:
            line = ko.normalise_line(line) if line else None
        except ko.KeepoutError:
            line = None
        if line is None:
            raise ko.KeepoutError(422, NO_LINK, "The two charts do not touch in the scene graph: no barrier can be placed")
        return {"line": line, "source": {"from": ends[0], "to": ends[1]}}

    async def migrate(self, map_id: str, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Rewrite the legacy rows (chart key / chart pair only) of `rows` as geometry, once, when the graph can be read now. Each
        row is updated compare-and-set on its old spec (so a concurrent migration or edit wins cleanly), which bumps its `updated_at`
        and with it the revision. A row the graph cannot place (chart gone, graph unreadable) stays as it is and keeps resolving
        against the graph at plan time. Idempotent: geometry rows are never touched. Returns the rows as they are now."""
        legacy = [r for r in rows if ko.is_legacy(r["spec"])]
        if not legacy:
            return rows
        topology = await self._topology(map_id)
        if topology is None:
            return rows
        out = list(rows)
        for i, row in enumerate(rows):
            if not ko.is_legacy(row["spec"]):
                continue
            try:
                geo = await self.snapshot(map_id, row["spec"], topology)
            except ko.KeepoutError as err:
                logger.info("keep-out %s of %s stays unmigrated: %s", row["id"], map_id, err.code)
                continue
            async with self._db.connection() as conn:
                cur = await conn.execute(ko.SPEC_SQL, (json.dumps(geo), map_id, row["id"], json.dumps(row["spec"])))
                new = await cur.fetchone()
                if new is None:                                   # changed meanwhile: take what is there
                    cur = await conn.execute(ko.GET_SQL, (map_id, row["id"]))
                    new = await cur.fetchone()
            if new is not None:
                out[i] = ko.row_dict(new)
                logger.info("keep-out %s of %s migrated to geometry (%s)", row["id"], map_id, "polygon" if "polygon" in geo else "line")
        return [r for r in out]

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
        topology = await self._topology(map_id) if ko.is_legacy(row["spec"]) else None
        return self._keepout(row, topology)

    async def delete(self, map_id: str, keepout_id: str) -> None:
        async with self._db.connection() as conn:
            await self._map(conn, map_id)
            cur = await conn.execute(ko.DELETE_SQL, (map_id, keepout_id))
            if await cur.fetchone() is None:
                raise _err(404, "keepout_not_found", f"Keep-out '{keepout_id}' not found on map '{map_id}'")
