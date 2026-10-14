# Plan trajectory: the planner's full path for the client (2026-10-14)

Goal: the client draws the path the lattice planner computed, not straight lines between the simplified waypoints.

## Where the path exists

* Go-here (`scene_route._plan_on_graph`): `route.nodes` (priced by the clearance cost when `SCENE_GRAPH_SAFETY_KAPPA` > 0), with the snapped
  start and goal cells around it. `simplify`, the end-stub merge, `repair_leaf_ids` and `collapse_repeated_leaf_ids` only ever **drop**
  points of that list, so every output waypoint is a point of the path. `trajectory.split` cuts the path at the waypoints (a forward search
  by exact point; a waypoint not on the path means no trajectory, never a wrong one).
* Enrich (`node_policy_plan`): `_between` already routes **every** consecutive pair of operator waypoints (to decide whether planned
  waypoints are needed) and `_leg_width` routes the legs next to skippable candidates. A `LegRoutes` cache records the lattice path per
  (from xy, to xy): the whole route of a pair, or the pieces a planned waypoint cut it into. `_leg_width` reads the cache before it routes
  (the width of an uncut pair is then free), so the trajectory costs **no route call of its own**; one extra route per pair only for a pair
  inside one chart, which `_between` used to skip (it needs no planned waypoints) and is now routed once, cached, to give its leg. Pieces
  cut by a planned waypoint have no cached width: `annotate_corridors` routes them as before.
* Missions the planner does not enrich (no scene graph, planner down or slow, `NODE_POLICY_ENRICH_TIMEOUT_S`, mapless/mixed-map routes,
  topomap-paired routes keep their length but are still enriched for corridors and so get legs): no trajectory. The planner cannot give one
  without a graph; the client draws those straight. A pair whose route failed or whose waypoints do not snap has no leg either.

## Shape (planner responses, API navigate, stored route)

```
trajectory: [{from_index: int, to_index: int (= from_index + 1), points: [[x, y], ...]}]
```

Map frame, metres, cm-rounded, first/last point = the leg's waypoints (an operator's waypoint is not on a cell centre, so the leg is
forced to start/end exactly there), only direction changes kept (collinear points removed: the polyline through them is the lattice path).
Legs are in route order; a leg may be missing (enrich: pair not routed): draw it straight. Absent, never `[]`, when off / over the cap /
not derivable.

* `POST /api/v1/plan`, planner `POST /api/v1/navigate`, `POST /api/v1/route/enrich` responses: `trajectory`.
* Gateway `POST /api/v1/navigate`: `trajectory` (the gateway's response model now carries it).
* Stored: `mission_tree[i].route.trajectory` (`MissionRouteNodeV1.trajectory`, next to `waypoints`), written by the planner's `create_mission`
  (go-to), by `_replan_goto` (each start replaces it with the replan's) and by `_enrich_routes` (before the first order; a trajectory alone is
  stored even when the waypoints did not change). Order ids, tree indexes, `planned_path`, waypoint offsets are untouched.
* Not sent to the robot (the order builder reads `waypoints` only).

## Resume digest and rerouting

`_route_digest` excludes `trajectory` (`route.json(..., exclude={"trajectory"})`): a route stored before this change, or the same route
with a refreshed trajectory, has one digest. The dispatcher's reroute detection (route_rev handling of a changed spec) compares `route.waypoints`, not the whole route, so
a client that sends a route back without its trajectory (or with another) is not a reroute and cannot cancel a running node. Such a PUT
with changed waypoints stores the client's route as sent.

## Old and new clients

* Old client reading a mission: an extra key in `route`; ignored.
* Old client editing a route (`PUT /missions`) and sending it back without `trajectory`: the trajectory is lost for that route (display only;
  the client draws straight lines again). A client that keeps unknown fields keeps it.
* Staleness: `MissionRouteNodeV1` has a root validator that drops any leg whose indexes are out of range or whose first/last point is more
  than 2 cm from its waypoints. A client that moved a waypoint but sent the old trajectory gets the other legs, never a wrong line on the
  moved one; never an error. (A leg of an edited neighbouring waypoint is dropped the same way.)
* Planner older than the dispatcher (deploy order): no `trajectory` key in the answer, route stored as before.

## Size cap and switches

`PLAN_TRAJECTORY_ENABLED` (default true) and `PLAN_TRAJECTORY_MAX_POINTS` (default 3000 points per route after collinear removal; a 100 m
route with a few dozen turns is a few hundred). Over the cap: the whole trajectory is omitted, not truncated. Any exception while deriving it is
logged and the plan is served without it (it never fails a plan or an enrich). With the switch off the enrich path is the old one (no cache, no
extra routes).

## Risks

* Stored row grows by the points (a few KB for a long route); the cap bounds it.
* The cut-by-planned-waypoint pieces rely on the planned points being points of the route (they are, `simplify` picks from it).
* The lattice path is what the planner priced, not what the robot's local planner will drive; the client should present it as the plan.
* A same-chart pair now costs one route call in enrich (cached; the planner's pool budget `SCENE_GRAPH_PLAN_BUDGET_S` covers the whole
  enrich call as before).
