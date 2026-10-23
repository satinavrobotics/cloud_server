# nodePolicy v2: plan (2026-10-23)

Plan for the cloud_server side of the nodePolicy / corridor contract agreed with the robot team on 2026-10-23,
after the 22 Oct konferencia runs in which no order carried a nodePolicy. Supersedes the open parts of
`node_policies_2026-10-13.md`. `satibot_orchestrator` is out of scope: it belongs to the robot side.

**Status (branch `feature/node-policies-v2`):** Phase 0 and steps 1.1–1.5 are implemented with unit tests; Phases 2–4 are open.
Call-outs from the implementation: D1–D3 were taken as recommended (the editor's explicit skippable on an operator waypoint is still
sent not skippable, see `annotate_corridors` `keep_intent`; the go-to `origin` work is Phase 2). Step 1.1 still needs the data action
`PUT /api/v1/robots/formidable-peacock {"node_policy": "on"}` after deploy. A registration with an empty `actions` list no longer
clears the stored list (only a factsheet message does).

## 1. The agreed contract (summary)

| Topic | Agreement |
|---|---|
| Pass-through node | Not the last node of the order and no HARD/SOFT action. Applies to guide nodes and operator waypoints alike. |
| Traversed | The robot is within `allowedDeviationXY` (0.35), OR it crossed the node's gate (a line across the route, on the bisector at corners, as wide as the corridor) inside the corridor. A gate counts only while its node is the next one. A large sideways offset is normal (the lenient reading). |
| Already past at order start | Counts as traversed, strictly in sequence. It stops at the first node the robot is not past. |
| `corridorWidth` | Per side, the room for the robot's centre line. Sent on every pass-through node where known, independent of `skippable`. Capped at 1.5 m. |
| `corridorWidth: 0` | Measured, no room: distance rule only, never skippable, no gate, and the robot default is never used. |
| `corridorWidth` missing | Unknown: the robot default of 0.75 m applies (gate only, never a skip). |
| Robot-side margin | The robot subtracts ~0.10 m for its pose error. A width ≤ 0.10 acts like 0. Nothing for us to do. |
| `skippable: true` | Only with `corridorWidth > 0`. Guide (planner) nodes only. Operator waypoints are non-skippable (see D1). |
| Last node | nodePolicy with `maxWaitS` only: `skippable: false`, no `corridorWidth`. The robot holds up to maxWaitS (capped at 120 s), then reports edgeBlocked. |
| Node without nodePolicy | No hold: blocked at once. |
| Start node (seq 0, the robot's pose) | Stays as it is and never carries a nodePolicy. |
| `allowedDeviationXY` | 0.35 m on pass-through nodes (unchanged). |
| Factsheet | The orchestrator is the only publisher (retained, at registration). The robot team adds nodePolicy (scope NODE) in top-level `actions` and in `protocolFeatures.agvActions`. We force nodePolicy on until that is live. |

## 2. What the code does today (verified)

Facts from reading the code on `main` @ 38aa447. Each one points at the step that changes it.

1. **The capability gate shut everything off.** `_sends_node_policy` (`packages/controllers/mission/server.py:1757`) defaults to `factsheet`. `.env.example:139` says `VDA5050_NODE_POLICY_MODE=on`, `profiles/server.env` sets it (the dev box is `on`), but the robot profiles (`pi4.env`, `pi5.env`) do not, and compose defaults to `factsheet` (`docker_compose/mission_dispatch_services.yaml:95`). The Pi is therefore in factsheet mode (corrected 2026-10-23: the dev box was already `on`). → Step 1.1
2. **The empty action list came from our side of the wire.** The orchestrator publishes a retained factsheet with `actions: []` (`satibot_orchestrator/app/mqtt_publisher.py:61`). Its HTTP registration also overwrites `custom_actions` with `[]`, even when the key is missing (`packages/api/main.py:2532`, `_custom_actions` defaults to `[]`). `_on_client_factsheet` (`server.py:3457`) reads only top-level `actions`. `protocolFeatures` is an unmodelled section (`vda5050_types.py:630`), and `headerId`/`timestamp`/topic are not stored. → Step 1.2
3. **The corridor is tied to `skippable` on the wire.** `waypoint_node_policy` passes the corridor only when skippable, and `VDA5050Action.node_policy` sends `corridorWidth` only when it is > 0 (`vda5050_types.py:143-177`). A width of 0 cannot be expressed, and there is no cap. → Step 1.3
4. **The last node gets no nodePolicy.** `from_route` uses `if node_policy and j != last` (`vda5050_types.py:445`). Adding one is safe: nodePolicy action states are already ignored as mission actions (`_NON_NODE_ACTION_TYPES`, `server.py:536`), and order completion is judged by `lastNodeId` alone (`_order_finished_on_robot`, `server.py:3406`). → Step 1.3
5. **"Narrow" and "unknown" look the same.** `corridor_per_side` returns None both for unknown widths and for no room (`node_policy_plan.py:38`). `annotate_corridors` gives a corridor only to `candidates`, so operator waypoints never get one (`node_policy_plan.py:102`). → Step 1.4
6. **Waypoint 0 never gets a corridor.** `annotate_corridors` requires both adjacent legs ("the first one: the edge before it starts at the robot"), and the chart planner passes `range(1, n-1)` (`scene_route.py:1089`). → Step 1.4
7. **This is the W4 cause: go-to routes begin with the robot's own position.** `plan_on_graph` builds `pts = [snapped start] + lattice + [goal]` (`scene_route.py:~1020`), so waypoint 0 is the robot's start snapped onto the graph (up to `SCENE_GRAPH_START_SNAP_M` = 0.5 m away). The legacy planner's waypoint 0 is the topomap node nearest the robot, which can be further away. The order then becomes [robot pose, a point next to or behind the robot, ...]. A go-to planned at submission is replanned before its first order (`_replan_goto`, `server.py:973`), but the snap still moves the point. The order build already supports starting a route later through `waypoint_offset` (`_build_order`, `server.py:1788`; progress indexing `_resolve_node_ref`, `server.py:3980`). → Step 1.5
8. **A planner fallback loses all skip data.** `plan_on_graph` raises `waypoints_without_distinct_leaf_ids` when `build_waypoints` has no complete leaf-id list (`scene_route.py:1079`). Interior waypoints without a leaf are already dropped by `repair_leaf_ids`, so the fallback means an END (the snapped start or the goal) has no topomap node within `SCENE_GRAPH_LEAF_SNAP_M` = 1.0 m. Everything downstream accepts a missing leaf id: `NavigationResponse.path`/`start_node_id`/`end_node_id` are optional (`mission_planner/main.py:115`), `run_legs.identity` falls back to VDA ids (`packages/utils/run_legs.py:58`), and `_resolve_node_ref` reads `waypoint.node_id` first. The legacy fallback sets no skip fields at all (`mission_planner/server.py:1259`), and the dispatcher never enriches go-tos (`_enrich_routes`, `server.py:1033`). → Steps 2.1–2.3
9. **The planner sets no `origin` on go-to waypoints.** Only gap fill sets `origin="planned"`. → Step 2.2
10. **Analytics are mostly unaffected by gates.** Progress uses `lastNodeId` only, and the leg tracker counts a waypoint whatever its offset (`server.py:3903`). Per-leg path efficiency (`packages/api/path_efficiency.py`) and expected leg times (`run_legs.expected_seconds`) measure between progress events. With gates, a leg boundary can move up to one corridor width (≤ 1.5 m) from the node, so per-leg values become less exact, while per-run totals stay correct. The `EFFICIENCY_MIN_COVERAGE` check may drop some legs that now end early. → Step 3.3 (documentation, maybe a tolerance)
11. **The editor already lets operators set skip fields.** The client edits `skippable`/`max_wait_s`/`corridor_width_m` per waypoint (`sati-client/utils/waypointPolicy.ts`), and the API describes them (`packages/api/main.py:2193`, `route_preview.py:65`). → D1

## 3. Steps

### Phase 0: config only (no code, can ship today)
- **0.1** Set `VDA5050_NODE_POLICY_MODE=on` in `profiles/pi4.env` and `pi5.env` (done on this branch; `server.env` already had it). Re-render the env files and recreate mission-dispatch. Today's robots all run the nodePolicy release (confirmed 2026-10-09). This is the "force on" until Step 1.1 lands.
  Verify: the dispatcher log shows nodePolicy on order nodes (`found>0` on the robot side).

### Phase 1: contract on the wire (needed before the robot's gate goes live)
- **1.1 Per-robot override.** Add `RobotSpecV1.node_policy: Optional[Literal["on", "off"]]` (None = follow the global mode), next to `needs_order_cancel` (`cloud_common/objects/robot.py:240`). `_sends_node_policy` checks it first. Expose it via PUT /robots like the other spec flags. Set `on` for formidable-peacock. When nodePolicy is withheld, the mission log says why, once per order (not just debug).
  Tests: the mode matrix (spec on/off/None × env off/factsheet/on × factsheet lists/doesn't).
- **1.2 Factsheet intake.**
  - Read actions from top-level `actions` AND `protocolFeatures.agvActions`, merged by `actionType`. Model `agvActions` in `VDA5050ProtocolFeatures`.
  - Store `received_at`, `topic`, `headerId`, `timestamp`, `manufacturer`, `serialNumber` in `status.factsheet` (a new `source` sub-object).
  - Registration (`api/main.py`): overwrite `custom_actions` only when the body has an `actions` key.
  - Tests: standard-only, legacy-only and both placements; registration without `actions` keeps the stored list.
- **1.3 Order wire.**
  - `VDA5050Action.node_policy(skippable, max_wait_s, corridor_width_m)`: `corridorWidth` is sent whenever the value is not None. It is clamped to [0, `NODE_POLICY_CORRIDOR_CAP_M` = 1.5] (new order-policy key), and a non-finite value counts as None.
  - `skippable` is true only if asked AND `corridorWidth > 0` AND the skip kill switch is on.
  - The kill switch off no longer strips the corridor; it only forces `skippable=false`.
  - Last node: nodePolicy with `maxWaitS` only.
  - Tests: wire snapshots for guide / guide-narrow / guide-unknown / operator / operator-unknown / last; the cap; NaN.
- **1.4 Annotation: tri-state and all interior waypoints** (`node_policy_plan.py`).
  - `corridor_per_side` returns `0.0` when it measured no room and `None` when the width is unknown.
  - `annotate_corridors` computes `corridor_width_m` for every waypoint except the last, operator waypoints included. `skippable` is set only on candidates (guide nodes), and only when the corridor is > 0.
  - Waypoint 0 uses its one known leg (0→1), since the leg before it starts at the robot. The robot's costmap remains the safety layer; the corridor is only permission.
  - An operator's own `corridor_width_m` still wins.
  - The report gains per-reason counts: `{total, skippable, corridor: {measured, narrow, unknown}, unavailable}`.
  - Tests: the narrow → 0 path, unknown → None, operator waypoints get a corridor, waypoint 0.
- **1.5 Start of an order: leave out waypoints the robot is past** (`_build_order`).
  - Before building, extend the offset over the leading waypoints the robot has already passed. A waypoint j (never the last) counts as passed when either:
    - the robot is within `deviation_xy_pass_m` of it, or
    - the robot projects forward onto the leg j→j+1 (dot > 0) with a lateral distance ≤ that waypoint's corridor (0.75 m when unknown, mirroring the robot's rule).

    Check strictly in sequence and stop at the first waypoint not passed.
  - Use the robot's freshest pose, and skip the check when the pose is older than a few seconds. Log "starting at waypoint k (k passed at order start)".
  - Progress: mark the passed waypoints as reached in `task_status`, the same way a resume does, so the client shows them done.
  - This one rule covers the go-to snapped start, user routes, resumes and re-sends. The planner keeps emitting waypoint 0, which the preview and `planned_path` need.
  - Tests: snapped start behind / beside / ahead; a loop route whose last waypoint is next to the robot is never dropped; a stale pose.

### Phase 2: the evidence survives every planner path
- **2.1 Chart planner fails soft.** When the leaf ids are incomplete, serve the plan with `planned_path=None` (a route with "VDA-id-only legs", already supported) instead of raising `waypoints_without_distinct_leaf_ids`. `start_node_id`/`end_node_id`/`path` become None or partial. Count it as `served_without_leaf_ids` in `_chart_outcomes`, so it stays visible.
  Tests: a goal with no leaf within 1 m returns a plan with corridors.
- **2.2 One finishing step for every go-to plan.** Add a shared `finalize_goto(poses, graph?)` in the planner, run after the chart plan AND after the legacy plan:
  - `origin="planned"` on waypoints 0..n-2 and `origin="user"` on the goal (see D2);
  - then `annotate_corridors` when a scene graph is available (the legacy fallback usually happened while it was), otherwise all None with `unavailable` set to the reason.

  The legacy path then stops reporting `legacy_planner` whenever a graph exists. Rename `node_policy.unavailable` to the actual reason.
- **2.3 The dispatcher checks instead of assuming.** In `_enrich_routes`, a go-to is no longer exempt from the check. Before the first order, when any pass-through waypoint of a go-to has neither a corridor nor a recorded reason, log a warning and set a mission event (`node_policy_unannotated`). Re-annotating is not needed once 2.2 is in place; the check is the safety net. Plus a unit test of the invariant: every stored go-to waypoint has an `origin`.

### Phase 3: what was sent is visible
_Status: 3.1 and 3.2 done on `feature/node-policies-v2` (reasons also include `gate_only`: guide node with room, not skippable). 3.3 open._
- **3.1 Decision record.**
  - `MissionSentOrderV1` gains `node_policy: {sent: bool, withheld_reason?, nodes: [{seq, waypoint_index, skippable, corridor, max_wait_s, reason}]}`.
  - Reasons: `structural_last`, `operator`, `narrow`, `unknown:<why>`, `skip_disabled`, `ok`.
  - The decision is computed in the same function that builds the actions, so the record cannot drift from the wire.
  - This answers "what did the robot see?" from the DB.
- **3.2 API summary.** The mission GET carries `node_policy_summary` (counts by reason for the current order). The client display follows in sati-client, as a separate task.
- **3.3 Analytics note.** Document in `path_efficiency_2026-10-15.md` that per-leg values have a boundary error of up to the corridor width under gates. Check the `EFFICIENCY_MIN_COVERAGE` rejection rate on the first gate run before changing anything.

### Phase 4: W9 waypoint placement (investigation first; needs the Pi online)
- **4.1** For each robot-team case (run 140048: 18 lethal goal cells and 3 spur tips; run 123410: 4 cases), find the waypoint in the stored mission. Then classify:
  - its origin: operator, gap fill, go-to chart, or legacy;
  - its static clearance in the map: does the occupancy say lethal, or was it a person?
  - whether it is a spur tip: a topomap leaf, or an end stub past a node.

  Most of the 140048 cases lie on y ≈ 1.95–2.55 on the iloop routes: one row of waypoints, probably along furniture.
- **4.2** Then fix what the classification shows. Possible directions:
  - push planned waypoints toward maximum clearance within their chart (the pricing already counts `waypoints_below_floor`);
  - reject or nudge operator waypoints on lethal cells in route preview/enrich, with a visible notice;
  - stop end stubs from passing beyond a node.

  The scope is decided after 4.1.

### Phase 5 (later, not part of this branch): data model cleanup
Store robot-independent passage widths per leg plus the graph version. Derive `corridor_width_m` at order build for the executing robot, and replace the stored `skippable` intent with `pass_through`. Do this only when a second robot type or a mission reassigned between robots makes the baked-in robot width a real problem.

## 4. Decisions needed

- **D1. Operator waypoints the user marked skippable in the editor.** Today the editor allows it, while the contract says operator waypoints are non-skippable.
  - Recommendation: keep honouring an explicit editor opt-in (the default stays non-skippable). It is deliberate user intent, and the robot does not care who placed a node.
  - Needs one line to the robot team.
- **D2. `origin` of the go-to goal.** Recommendation: `user` (the operator tapped it). Waypoints 0..n-2 are `planned`.
- **D3. Phase 0 global `on` AND Step 1.1 per-robot override, or only one?** Recommendation: both. Phase 0 unblocks today; 1.1 is the lasting control once the factsheet is live and the global mode can go back to `factsheet`.
- **D4. Phase 5 now or later?** Recommendation: later (reason above).

## 5. Order of work and verification
1. Phase 0 (deploy to the Pi before the next run).
2. 1.3 + 1.4 + the last-node part. These are pure functions with wire-snapshot tests, and give the most robot-visible effect.
3. 1.5 (W4), then 1.1 and 1.2.
4. Phase 2, then Phase 3.
5. Phase 4 when the Pi is reachable.

Each step comes with unit tests (`./scripts/run_unit_tests.sh`). End-to-end check after Phase 1, on the Pi:
- plan a go-to on konferencia2 and confirm in the dispatcher log / sent-order record that every pass-through node carries nodePolicy and `corridorWidth`, narrow ones carry 0, and the last node carries `maxWaitS` only;
- confirm the first order no longer contains a waypoint behind the robot.

Then compare with the robot team's per-node traversal offsets once their gate is live.
