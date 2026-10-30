# Reroute and blocked-node follow-ups (2026-10-30)

Status after the reroute review (commits `fdf268c`, `5a7f624`, `d0659f7` on
`reroute/review-fixes`) and the robot team's report on run 20261030_152638
(robot formidable-peacock). The client side is in sati-client commit `1d6124a`.

## Done

- Reroute validation, pose-aware blocked-node check, atomic `route_rev` bump,
  `MISSION.REROUTED` (one per applied `route_rev`) and `MISSION.BLOCK_CLEARED`.
- `blocked_since` on the mission status and the websocket payload.
- Repeat missions: `MISSION_PASS_MIN_GAP_S` between passes; a pass that reached
  no waypoint stops the repeat (FAILED, `MISSION.PASS_NO_PROGRESS`).
- Client: blocked-mission alert card, keep-out warnings, 400/409 messages,
  event labels, route drawn for go-to missions after a reroute.

## Open: cloud server

1. **Hold a known-blocked node.** `edgeBlocked` carries nodeId, edgeId,
   nodeSequenceId, blockReason and heldS but no position, and every reroute
   renames the nodes, so the block cannot be matched across orders. The robot
   team will add the node's map `x`/`y` (optional fields, agreed). Then: store
   the position on the block, match later orders by distance (footprint
   radius), hold the order and raise an event instead of sending the node.
   Never mark the node `skippable` without a measured corridor (contract v2).
   Until the fields exist the matching is weaker.
2. **Per-node minimum clearance** in nodePolicy v2: one value in metres per
   node, from the planner's measured slack (the robot team does not want a
   flag). Write the contract addition, send it for review, then build. Worst
   node in the run was 7.5 mm at (3.33, -0.61).
3. **Confirm the repeat-lap cause.** The two orders 0.2 s apart (`rd10aa195`,
   `r46bc58f9`, same rerun stamp 1793371390588) are a hypothesis: a repeat
   mission re-sent by `_start_next_pass`. Needs the rerun mission's `repeat`
   and `passes_completed` from the database.
4. **Answer the robot team** on whether `force` was set on the reroutes
   (request log) and on the map history near (1.58, -0.96) (person, object or
   layout change). Both need database / log access that failed on the `.env`
   credentials.
5. **Dispatcher / PUT race.** A dispatcher write landing between
   `_plan_superseded` and its DB write can still replace an operator's
   `mission_tree`. Needs a compare-and-set on the route revision.
6. **Verification gaps.** No clean full-suite baseline (16 test files fail at
   collection in `/tmp/lkvenv`: yaml, jsonschema, alembic, scene_*). The new
   `update_spec_fields` SQL is not tested against a real Postgres.
7. **Old stored `MISSION.REROUTED` rows are block clears.** Optional: relabel
   them with a migration; the stats counts cannot tell them apart.
8. **Pass guard watch.** A completed pass whose robot never reports a waypoint
   now stops a repeat. Watch for false stops after deploy.

## Open: sati-client

- Look at the alert card in a browser, including a phone-width window.
- `kind` is per mission, not per run: after a kind change older runs follow the
  current kind. Putting `kind` on the run detail needs a server change.
- The 409 hint uses a fixed 0.5 m "near" radius; the server uses the robot
  footprint (`half_width_m`).
- `ApiMissionObject.update_nodes` and the `effectiveMissionTree` override are
  marked deprecated. Remove once old rows are confirmed migrated.
- Optional: sound or browser notification for a blocked mission; marks for
  `REROUTED` / `BLOCK_CLEARED` in the run timeline; an "insert waypoint" action
  in the 409 view. Not planned.
- CLAUDE.md says coverage thresholds 80/95/85; `package.json` enforces lower
  values (utils branches 90). Decide which is right.

## Open: robot team

- Add `x`/`y` (map frame, metres) to `edgeBlocked`.
- Log the order header id and timestamp.
- Operator-goal trigger gap (their fix).
- Treat the cell at (1.58, -0.96) as lethal until the map history is checked.
