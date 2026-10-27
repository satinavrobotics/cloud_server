# Keep-out zones (2026-10-25)

Operators switch off **regions** and **edges** of a map's scene graph by hand; the mission planner then never routes through them.
Persistent per map (no expiry) until an operator disables or deletes them. Code: `packages/utils/keepouts.py` (model, validation,
SQL), `packages/api/keepouts.py` (routes), `packages/services/mission_planner/keepout_overlay.py` (zone -> cells, cache, legacy
gate). Table `map_keepouts` (migration `20261025_01_map_keepouts`, idempotent); the map delete removes the rows.

## Model

The engine routes on a lattice (0.1 m) and can only block by geometry (`Blocked`), but it has stable identifiers: **chart keys**
(rule A, `chart_keys.py`) and the **links** between charts. So a zone is stored by identifier where there is one:

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
POST   /api/v1/maps/{map}/keepouts        {kind, label?, region | edge} -> 201 Keepout
PATCH  /api/v1/maps/{map}/keepouts/{id}   {enabled?, label?} -> Keepout
DELETE /api/v1/maps/{map}/keepouts/{id}   -> 204
Keepout = {id, kind, label, enabled, created_at, region | edge, applies: "applied"|"unresolved", reason?}
```

`revision` changes whenever a zone is added, removed, enabled/disabled or relabelled. Errors are `{"detail": {"code", "message"}}`:
404 `map_not_found` / `keepout_not_found`; 409 `map_deleting` / `keepout_limit` (200 per map) / `keepout_exists` (same spec);
422 `invalid_keepout` / `invalid_polygon` / `chart_not_found` / `no_link_between_charts` / `point_off_graph` (the last three only
when the graph can be read; without it chart keys and points are accepted unchecked).

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
* **Other fallbacks** (scene-graph planner off or unavailable): the legacy planner applies polygon zones to its graph-node path
  (nodes inside, edges that enter a polygon are excluded; refused if no way around). With a chart or edge zone enabled it refuses
  (`reason: "keepout_unverifiable"`): it cannot place them, and silently ignoring them is not an option.
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

* **Missions already running keep their route**; a reroute (`PUT /missions/{name}` with `update_nodes`) and operator-drawn
  waypoints are not checked against zones (the preview lists crossed legs; the mission API does not refuse them). A replan by the
  dispatcher goes through the planner and is covered.
* A robot **inside** a zone, or a goal inside one, gets no route (the zone's cells are not drivable); `start_in_zone` /
  `goal_in_zone` in the error say so. Disable the zone to let the robot out.
* Zones are not shown to the robot; the robot's own safety layers know nothing of them.
* Chart keys are stable (rule A) but a chart can still be retired when the map changes a lot: the zone then turns `unresolved`.
  Nothing re-attaches it by overlap.
* Not run against a real map or real Postgres: tested on the synthetic chart worlds with in-memory fakes of the table.
