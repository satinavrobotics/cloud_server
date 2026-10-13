# nodePolicy skip data and planned waypoints (2026-10-13)

Design note. Robot-side contract: node action `nodePolicy` (blocking NONE) with `skippable`,
`maxWaitS`, `corridorWidth`. A skip needs a positive corridorWidth, is never on the first,
last or a stop-action node, and needs the robot's own `node_policy=hold`, `skip_by_node_policy=on`.

## Where each rule lives

| Rule | Where |
|---|---|
| Per-waypoint policy fields | `cloud_common/objects/common.py` `Pose2D`: `skippable`, `max_wait_s`, `corridor_width_m`, `origin` (all optional, default None = unchanged behaviour) |
| Order build | `VDA5050Order.from_route` reads the waypoint fields; `order_policy` decides whether anything is sent (`node_policy_mode`, new `NODE_POLICY_SKIP_ENABLED` kill switch) |
| Go-here: all skippable but the last | planner (`scene_route._plan_on_graph` via `node_policy_plan.mark_goto`): marks every waypoint but the last and fills `corridor_width_m` |
| User missions: default not skippable | no field set = `skippable=false`; the mission editor sets `skippable` / `max_wait_s` per waypoint |
| Corridor width from the scene graph | `node_policy_plan.leg_widths` + `corridor_per_side` (below) |
| Gap filling for user routes | planner `POST /api/v1/route/enrich`, called by the dispatcher once before the first order (next to `_replan_goto`) |

## corridorWidth

Unit: **per side** (robot team reply, `vda5050_open_items_2026-10-09.md`: `corridorWidth: 1.0` = a 2 m band).
The task text said "metres of ground around the edges"; per side is what is already agreed in writing, so it is used (flagged in the report).

Source: the engine gives `Route.min_width` (narrowest chart-pair passage, widest arc per pair, "twice the largest physical clearance")
for any route between two snapped points. There is no per-edge width in the public API, and the chart/owner raster would need
a full `query` (not available on the lazy graph). The cheapest faithful value: for each leg (waypoint i -> i+1) run
`graph.route(snap(i), snap(i+1))` and take its `min_width`; the width of node N is `min(width(N-1 -> N), width(N -> N+1))`.
That is the passage the robot can actually use around those two edges, from the same engine and rules the planner already trusts
for `too_narrow`. Each leg is routed once (cached per leg), under a time budget (`NODE_POLICY_WIDTH_BUDGET_S`); a leg that cannot be
routed (missing tiles, no route, budget) has width None and its two nodes are not skippable.

`min_width` is a full physical passage width, not accounting for the body. Per-side room for the robot centre line
is `(w - robot_width - margin) / 2` (margin = `SCENE_GRAPH_WIDTH_MARGIN_M`, the lattice error), floored to 1 cm.
Unknown robot width, or a result <= 0: no corridor, `skippable=false`. Conservative on purpose: if the robot's own
planner also inflates by its body, skips are rarer, never unsafe.

## Not skippable is visible, never fatal

Planner responses (`/plan`, `/route/enrich`) carry `node_policy: {skippable, total, unavailable}`; `unavailable` is a reason
(`no_scene_graph`, `legacy_planner`, `robot_width_unknown`, `width_unknown`, `disabled`). The dispatcher logs when a waypoint
asked for skippable but has no corridor and sends `skippable=false` with no corridor. The order is never refused.

## Auto-added waypoints (rule 4)

Only for a route whose map has a scene graph, planner enabled, mission `planned_path` unset (a topomap-paired route is left alone:
the leg tracker pairs `waypoints[i]` with `planned_path[i]`). For each consecutive pair of the user's waypoints:
snap both; same chart -> nothing. Else route between them; the lattice path crosses charts in a contiguous sequence, so a stretch of
the path touching at most two distinct charts is "same or neighbouring". The path is simplified with line of sight **and** that
two-chart window, so each consecutive pair of the result is in one or two adjacent charts. Interior points become waypoints
`origin="planned"`, `skippable=true`, `corridor_width_m` from the leg widths of the final list. Pairs that cannot be snapped or routed are left as they are.
The user's waypoints keep their own policy; planned points take no user fields.

### Id handling

Inserted waypoints are written into the **stored** `mission_tree` route node (like `_replan_goto`, via `update_spec_fields`)
before the first order, so everything downstream (order ids `{prefix}-n{tree idx}`, node ids `-s{2*j+2}`, `waypoint_offset`,
resume digest, `skipped_nodes[].waypoint_index`, leg tracker) sees one consistent route. `order_ids` does not change: tree indexes
are untouched, only the waypoint count in a route grows. Enrichment runs only on the first start (`start_timestamp is None`), never on resume,
so indexes never shift under a running mission. The enrich step first drops any `origin="planned"` waypoint already in the route
(a client round-trip of an enriched tree), then re-inserts: idempotent. Reroutes (`PUT ... reroute`) store the new waypoints as given
(no insertion; their policy fields still apply) - open item.

`origin` is also serialised to the client: it draws `planned` waypoints differently.
