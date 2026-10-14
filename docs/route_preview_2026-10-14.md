# Route preview (2026-10-14)

Goal: the mission editor shows the lattice trajectory, planned waypoints and unroutable legs before the first start, without creating a mission.

* `POST /api/v1/route/preview` on the gateway (`packages/api/main.py`, logic in `packages/api/route_preview.py`; shapes in `docs/API_REFERENCE.md`).
* It calls the planner's `POST /api/v1/route/enrich` through the existing `MissionPlannerClient.enrich_route` with the dispatcher's timeout
  (`NODE_POLICY_ENRICH_TIMEOUT_S`) and `insert_planned=true`. No planner logic is duplicated: the preview is what `_enrich_routes` would store
  (without `robot_name` there is no footprint width: no corridor widths, `node_policy.unavailable = robot_width_unknown`).
* No database access, no events, nothing stored. Cap `ROUTE_PREVIEW_MAX_WAYPOINTS` (100, 413 above; with `return` the planner sees up to 199).
* Planner failure is an answer, not an error: `planner: "none"`, `reason`, waypoints as sent.

## Leg issues (planner)

`LegRoutes.failed` (node_policy_plan) records why a leg could not be routed: `outside_graph` (a point does not snap), `unreachable` (no route),
`error` (the engine raised). `enrich_route` maps them onto consecutive pairs of the returned waypoints and returns `issues:
[{from_index, to_index, reason}]` (key present, maybe empty, whenever the leg cache is on, i.e. `PLAN_TRAJECTORY_ENABLED`; absent without a
scene graph). `EnrichResponse.issues` is optional: the dispatcher ignores it. "Blocked" is not a reason: enrich plans without the blocked-node overlay.

## Return

The mission model has no return route: `return: true` is outbound plus its mirror (reverse order, theta + pi, the turning point once), sent as one
route. If the editor later stores a return differently, change `route_preview.build_route` only.

## Risks

* A preview costs a full enrich (up to the planner pool budget); there is no rate limit, as for other gateway routes.
* `issues` is absent from the planner when trajectory is switched off, so the preview then reports none.
* An older planner (before this change) answers without `issues`: the preview returns `[]`.
