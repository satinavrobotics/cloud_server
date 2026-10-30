# Keep-out zones (2026-10-25, geometry 2026-10-27)

Operators switch off **regions** and **passages (edges)** of a map by hand; the mission planner then never routes through them.
Persistent per map (no expiry) until an operator disables or deletes them. Code: `packages/utils/keepouts.py` (model, validation,
SQL), `packages/api/keepouts.py` (routes), `packages/services/mission_planner/keepout_overlay.py` (zone -> cells, cache, legacy
gate). Table `map_keepouts` (migration `20261025_01_map_keepouts`, idempotent); the map delete removes the rows.

## Geometry zones (2026-10-27)

A zone is the SPACE the operator selected, not a reference into the scene graph: "if a region gets deleted or merged there it
doesn't affect keeping out the space there". Every zone is stored as geometry in the map frame (metres) in `map_keepouts.spec`
(no new column, no Alembic migration):

| kind | `spec` | resolves to |
|---|---|---|
| region | `{"polygon": [[x, y], ...], "source"?: {"chart_key"}}` | the lattice cells whose centre is inside the polygon (closed implicitly; 3..200 points, finite, fits 200 m) |
| edge | `{"line": [[x, y], [x, y]], "source"?: {"from", "to"}}` | the barrier across a passage: the cells whose centre is within one lattice step of the segment (2 cells thick, like the old chart seal) |

Both resolve from the geometry alone (`keepout_overlay.polygon_cells` / `line_cells`), so a graph rebuild, a chart merge or a retired
chart key cannot change or lose a zone, and a geometry zone never needs the scene-graph planner: it no longer forces it, no
longer triggers the legacy planner's refusal, and no longer triggers the stale-graph tolerance.

**Snapshot.** A create request may still name a chart (`region: {chart_key}`) or an edge (`edge: {from, to}`, ends are chart keys or
`[x, y]` points). The API (`KeepoutStore.snapshot`) reads the graph once and stores geometry:

* chart -> its footprint on the owner plane (largest blob, holes filled) traced along cell edges and simplified (Douglas-Peucker, tolerance
  from 0.5 cell up) to at most 48 vertices; the read window grows (4, 8, 16 m around the anchor) until the chart does not touch it;
* edge -> the seal between the two charts as in `keepout_overlay.edge_cells` (cells of each chart touching the other), reduced to
  one segment along the seal's principal axis, 0.05 m past each end (a single-cell seal: across the line between the two anchors);
* `source` keeps the chart key / chart pair for the UI ("made from chart X"); nothing resolves it again.

If the graph cannot be read the request FAILS and nothing is stored: 503 `scene_graph_unavailable` (store down, retry), 409
`no_scene_graph` (the map has none), 503 `chart_geometry_unavailable` (chart position / footprint not readable), 422 `chart_not_found` /
`no_link_between_charts` / `point_off_graph` (as before). A polygon or a `line` needs no graph.

**Existing rows** (chart key / chart pair only, made before this change) are **legacy**. `KeepoutStore.migrate`, run by every
`GET /keepouts` while the graph is readable, snapshots each once and writes the geometry into the row (`UPDATE ... WHERE spec = <old
spec>`: compare-and-set, so a concurrent edit or migration wins cleanly) which bumps `updated_at` and so the `revision`. Idempotent
(geometry rows are never touched); a row the graph cannot place (chart gone, graph unreadable) is left as it is with no data lost, and
keeps the old path: resolved against the graph at plan time (`chart_cells` / `edge_cells`), `applies: "unresolved"` with a reason when
it cannot be, the stale-graph tolerance and `graph_stale` notice (commit 62ffef2) and the legacy-planner refusal (`keepout_unverifiable`)
- all only while such a row exists (`keepouts.is_legacy`).

**Legacy planner** (graph-node path, used when the scene planner is off or unavailable): honours polygon AND line zones. Nodes inside
a polygon and legs that enter a polygon are excluded; a leg that crosses a line (or passes within 0.1 m of it) is excluded; the leg out of
the robot's own start node is exempt as before; no way around is an error.

## Legacy model (rows not yet migrated)

Before the geometry snapshot a zone was stored by identifier (chart keys, rule A, `chart_keys.py`, and the links between charts) and
resolved at plan time. Rows of that form still work until the first list read migrates them:

| kind | `spec` | resolves to |
|---|---|---|
| region | `{"chart_key"}` | the lattice cells that chart owns on the graph version being planned on |
| region | `{"polygon": [[x, y], ...]}` | the lattice cells whose centre is inside the polygon (map frame; 3..200 points, finite, fits 200 m) |
| edge | `{"from", "to"}` (chart key or `[x, y]`) | a two-cell seal: the cells of the two charts that touch the other chart, along their whole shared boundary |

A point in an edge is a convenience: the API replaces it by the chart key at that point when the graph can be read (a key is
stable, a point is not). An edge seals only crossings between exactly those two charts; where the seal cells also touch a third
chart that chart is cut by a cell or two as well.

`applies` is **`unresolved`** when a zone cannot be mapped onto the current graph (chart no longer exists, no link joins the two
charts, a point is off the graph, no lattice cell inside a polygon, or no scene graph at all for chart/edge zones). `reason` says
which (`chart_not_found`, `no_link_between_charts`, `point_off_graph`, `no_cells`, `no_scene_graph`). The planner ignores an
unresolved zone and reports it; the UI should say so. Polygons never go unresolved for want of a graph.

## API

```
GET    /api/v1/maps/{map}/keepouts        -> {"map_id", "keepouts": [Keepout], "revision"}
POST   /api/v1/maps/{map}/keepouts        {kind, label?, region: {polygon} | {chart_key} | edge: {from, to} | {line}} -> 201 Keepout
                                           (shorthand: top-level `polygon` / `line`)
PATCH  /api/v1/maps/{map}/keepouts/{id}   {enabled?, label?} -> Keepout
DELETE /api/v1/maps/{map}/keepouts/{id}   -> 204
Keepout = {id, kind, label, enabled, created_at, polygon (region) | line (edge), source?, region | edge (compat), applies: "applied"|"unresolved", reason?}
```

Response items (`polygon` / `line` are always present on geometry rows; `region` / `edge` repeat them for older clients, with
the chart key / chart pair when known):

```json
{"id": "9f2c...", "kind": "region", "label": "wet floor", "enabled": true, "created_at": "2026-10-27T12:00:00Z",
 "polygon": [[1.2, 0.4], [3.2, 0.4], [3.2, 2.4], [1.2, 2.4]], "source": {"chart_key": "s008:1"},
 "region": {"polygon": [[1.2, 0.4], [3.2, 0.4], [3.2, 2.4], [1.2, 2.4]], "chart_key": "s008:1"}, "applies": "applied"}
{"id": "a31d...", "kind": "edge", "label": null, "enabled": true, "created_at": "2026-10-27T12:01:00Z",
 "line": [[3.15, 0.6], [3.15, 2.2]], "source": {"from": "s008:4", "to": "s008:1"},
 "edge": {"line": [[3.15, 0.6], [3.15, 2.2]], "from": "s008:4", "to": "s008:1"}, "applies": "applied"}
```

A legacy row not migrated yet has no `polygon` / `line` (only `region.chart_key` / `edge.{from,to}`, `source` mirroring them) and may be
`applies: "unresolved"` with a `reason`; geometry rows are always `applied` (`reason` kept for the unresolved legacy case).

`revision` changes whenever a zone is added, removed, enabled/disabled or relabelled. Errors are `{"detail": {"code", "message"}}`:
404 `map_not_found` / `keepout_not_found`; 409 `map_deleting` / `keepout_limit` (200 per map) / `keepout_exists` (same spec);
422 `invalid_keepout` / `invalid_polygon` / `invalid_line` / `chart_not_found` / `no_link_between_charts` / `point_off_graph`;
409 `no_scene_graph` and 503 `scene_graph_unavailable` / `chart_geometry_unavailable` when a chart / edge create cannot read the graph (nothing is stored).

## Planner behaviour

* The enabled rows are read per map and cached for 2 s (an edit is live within seconds); resolution to cells is cached per (map,
  graph version, revision), so the hot path is a dict lookup. Resolved cells go into `Blocked.cells` (new, additive field) next
  to the robot-reported discs/segments, and into the line of sight (`blocked_free_los`).
* Keep-outs are **not** `ignore_exclusions`: that flag skips robot-reported blocks only.
* **Detour**: the plan response carries `keepout: {applied, unresolved, detour, forced_by, extra_length_m}`. `detour` is true when
  the route without the zones would have crossed one (a second search runs only when a zone can lie on a route no longer than ours).
* **Unreachable**: when only the zones cut the goal off, the plan fails with `failed_at: "keepout"`, an error text and
  `keepout: {unreachable: true, reason: "blocked_by_keepout", forced_by, start_in_zone, goal_in_zone}`. It does **not** fall back to
  the legacy planner (which knows no zones). Same for an unreadable table (`keepout_unreadable`) or zones over the cell budget
  (`keepout_too_large`, 2M cells).
* **Other fallbacks** (scene-graph planner off or unavailable): the legacy planner applies polygon and line zones to its graph-node path
  (nodes inside, edges that enter a polygon or cross a line are excluded; refused if no way around). Only an unmigrated legacy row
  (chart key / chart pair) makes it refuse (`reason: "keepout_unverifiable"`): it cannot place those.
* **Route preview** (`POST /api/v1/route/preview`, `enrich`): planned waypoints inserted between the operator's waypoints go
  around zones; the response gets `keepout: {applied, unresolved, crossed: [{from_index, to_index, zones}]}` listing the final
  legs that still enter a zone (the operator's own waypoints are never moved). With trajectory on, a gap cut off only by a zone is
  an `issues` entry with reason `blocked_by_keepout`.
* Robot-reported blocks live in `blocked_graph_nodes` and expire/purge there; keep-outs are another table, so no cleanup of the
  former can touch them (pinned by a test).

## Reroute (2026-10-27)

Reported: adding a node to a mission whose route would use a kept-out edge showed "No route between ..." instead of a way around.
What the code does, per path (all pinned by `tests/unit/test_keepout_reroute.py`):

* **The editor's call** is `POST /api/v1/route/preview` -> `enrich_route` (never `plan_route`): each leg is routed over the zone cells
  (`node_policy_plan._route_leg`); a leg that fails is an `issues` entry, which the client prints as "No route between a and b"
  whatever the reason (`reason` is `blocked_by_keepout` when the zones alone cut it, `unreachable`, `outside_graph`, `error`).
* With a way around, both planners (engine and lazy reader) return it, for region and edge zones, with a robot footprint and the
  trajectory on, for a third waypoint added to the route, and for the go-to plan (`keepout.detour`). Not reproduced on the synthetic
  worlds (ring, room loop, pillar room, 12-room grid: every link as an edge zone, point to point and anchor to anchor).
* **Fixed, a way around the lazy reader could not find**: under an overlay (zones, robot-reported blocks) it searched 3 rings of charts
  around the chart path and then every chart, so a detour of more than 3 charts read every tile of the map; over
  `SCENE_GRAPH_LAZY_MAX_TILES` that is `CorridorTooLarge` -> an `error` leg in the editor ("No route"), and in a go-to the fallback
  `corridor_too_large` -> the legacy planner -> `failed_at: "keepout"`, `reason: "keepout_unverifiable"` (it cannot place chart/edge
  zones). The rings now widen 1, 2, 3, 4, 6, 8, 12 ... hops until a route is found (`lazy_plan.RING_HOPS`) and then refine inside the
  length ellipse as before, so only a detour that really needs more than the cap still raises. The result is the engine's.
* **Fixed**: `plan_on_graph` measured the corridor widths of its legs without the zones (`annotate_corridors` got no `blocked`), so a
  detour's widths came from a route through the zone. The route preview already passed them.
* **Unchanged, by design**: a start or goal inside a zone (a chart zone's cells, or the 2-cell edge seal along the chart boundary)
  has no route; the preview says `blocked_by_keepout`, a go-to `failed_at: "keepout"` with `start_in_zone` / `goal_in_zone`.
* **Not verified**: the user's own map. A real map with a kept-out edge whose detour is long, a node placed within 0.2 m of the
  chart boundary, or a planner that timed out (`SCENE_GRAPH_PLAN_BUDGET_S`, answers "none", no issues) can look the same in the
  editor; look at the planner log ("Chart planner not used (...)"), `GET /stats` `scene_graph.outcomes` and the `issues[].reason`.

## Live findings on the dev box, map office9 (2026-10-27)

* **Zones shown `unresolved: no_scene_graph` although the graph is there.** `python -m packages.api.main` runs main.py as `__main__`;
  `ApiDelegationService` built the keep-out store's reader with `from packages.api import main`, a SECOND copy of the module whose
  `service` is None, so `_scene_graph_reads()` raised its 503 `scene_graph_unavailable` for ever (logged only at INFO as "topology not
  readable"). main.py now hands its own reader over (`keepouts.set_reads`). Effect before: the list/create routes could not place
  points or tell which zones apply (the planner itself, a separate service, was not affected).
* **`Chart planner not used (stale_graph)`** (15:23:49): the planner's cached graph was from before a live mapping run (versions 158 ->
  216 between 15:13 and 15:26); the first request after the idle hours found its ingest cursor older than
  `SCENE_GRAPH_MAX_VERSION_LAG_S` (120 s) and fell back; ten seconds later the new version was loaded (plans at graph_version 209/210
  served). Transient, lag 0 since. The fallback then hit the legacy planner, which refuses chart/edge zones, so the go-to failed.
* **Behaviour now**: on a map with a chart or edge zone a stale graph (lag over the limit, or an older cached version kept past
  `max_stale_s`) is planned on anyway; `plan_route` answers `keepout.graph_stale: {lag_s, limit_s, graph_version, reason}`. Such a
  route only uses cells the older graph knows are free (newly mapped areas are unknown to it), and zones resolve on that version. Maps
  without chart/edge zones keep the old behaviour (`stale_graph` -> legacy planner). The route preview (`enrich_route`) tolerates an
  older cached graph for such maps too, without a notice yet. When the legacy planner still refuses, `keepout.planner_fallback` and
  the error text name why the scene planner was skipped.

## Not covered

* **Missions already running keep their route**; a reroute (`PUT /missions/{name}` with `update_nodes`) is not refused for
  crossing a zone: the response carries a non-blocking `warnings` entry `ROUTE_CROSSES_KEEPOUT` (`node`, `crossed`, `message`) from the
  planner's `keepout.crossed`. Operator-drawn waypoints are not checked (the preview lists crossed legs; the mission API does not refuse them). A replan by the
  dispatcher goes through the planner and is covered.
* A robot **inside** a zone, or a goal inside one, gets no route (the zone's cells are not drivable); `start_in_zone` /
  `goal_in_zone` in the error say so. Disable the zone to let the robot out.
* An edge zone is one straight segment: a strongly curved boundary leaves the curve's ends open (see BACKLOG).
* Zones are not shown to the robot; the robot's own safety layers know nothing of them.
* A geometry zone never turns unresolved for want of a chart; only an unmigrated legacy row can (chart retired before it was
  migrated). The geometry is the footprint at creation: later mapping of that space does not grow it.
* Not run against a real map or real Postgres: tested on the synthetic chart worlds with in-memory fakes of the table.
