# Implementation plan: VDA5050 multi-waypoint and offline missions (server side)

Date: 2026-10-08. This answers the robot team's message "VDA5050 orders: what we need from the server…".
The reply sent to them is in `vda5050_robot_reply_2026-10-08.md`.
File:line references are against the tree on 2026-10-08. "Unverified" marks claims that depend on robot behaviour.

## 0. Findings that shape the plan

1. **mission-dispatch cannot import `packages/config.py`.**
   - Its image does not copy `config.py`, and `config.py` raises at import without the Arango/MinIO credentials (see the note at `config.py:177`).
   - Decision: put the new keys in `config.py` for the planner, API and graph_builder, and mirror them in a new, dependency-free `packages/controllers/mission/order_policy.py` that reads the same env names with the same defaults.
   - A parity unit test asserts the two agree. Add the env vars to the mission-dispatch service in `docker_compose/mission_dispatch_services.yaml`.
2. **`VDA5050ActionParameter.value` is `Optional[str]`** (`vda5050_types.py:51-54`). Pydantic v1 turns `True` into `"True"`, so nodePolicy cannot send typed bool or number values today.
3. **`handle_instant_action` stops at the first non-instant action state** (`server.py:1306-1310`, uses `break`).
   - If a robot lists nodePolicy states after the cancelOrder state, the cancel ack is never seen: the cancel is resent 20 times, then abandoned, and a reroute fails with "never confirmed the cancelOrder".
   - This must be fixed before nodePolicy ships.
4. **Adding fields to `Pose2D` changes `_route_digest`** (`server.py:259`), because the digest is `route.json(sort_keys=True)`.
   - Missions in flight at deploy would then be read as rerouted and get stuck.
   - Fix: `exclude_none=True`, plus a regression test that pins a digest computed under the old model.
5. **The client sends `allowedDeviationXY: 0` explicitly** (`sati-client/services/missionApi.ts:240-254`, with a stale comment). The server must treat 0 as unset.
6. **The API status PUT replaces the whole status** (`packages/api/main.py` ~2346) and keeps only `run_id`/`order_rev`. Every new dispatcher-owned status field must be preserved there.
7. **The mission timeout keeps running while the robot is offline** (`_arm_mission_timeout` `:2282`, `_wait_mission_timeout` `:2302`).
   - A long offline route is FAILED, and a cancelOrder is sent to an offline robot.
   - This conflicts with offline operation, so it is added as item C5.
8. **The "canceled" path runs before the edgeBlocked check** (`server.py:2725-2748`).
   - When the robot drops its order after an edgeBlocked, we immediately resend the remaining route, blocked node included.
   - This is the most likely mechanism behind "re-sending a route through a node just reported blocked" in the 854-version run.

---

## Phase 1: ships before the robot team builds nodePolicy

### 1. Correctness hardening (effort S, independent fixes)

| Fix | Where | Test |
|---|---|---|
| `_route_digest` uses `exclude_none=True` | `server.py:259` | the digest of an old route is unchanged after the model change |
| `handle_instant_action`: `break` → `continue` | `server.py:1306-1310`; update the docstring in `tests/dummy_robot/goal_follower.py` | cancelOrder FINISHED is seen when it comes after 20+ nodePolicy states |
| Action node completion matches the state by expected `actionId` (fall back to `[0]` for legacy robots) and guards an empty list | `update_mission_node_state` ACTION branch, `server.py:2461-2471` | a stale nodePolicy FINISHED does not complete an action node; empty actionStates raise no exception |
| Honour robot `missionStatus: "canceled"` only when nodeStates and edgeStates are empty | `server.py:2725-2733` | a stale "canceled" after adoption sends nothing |
| No `NEW_REVISION` while `status.blocked`, or while the message carries edgeBlocked; wait for the reroute | same block, reordered against `_handle_edge_blocked` | a robot that drops its order after edgeBlocked is not re-sent the same route |
| Preserve new status fields (`skipped_nodes`, `node_notes`, `offset_summary`, `sent_order.frame`) in the status PUT | `packages/api/main.py` ~2346 | API round-trip test |

### 2. Offline survivability (effort S-M)

- **C5, timeout paused offline.**
  - Cancel `_mission_timeout_task` in `_check_robot_online` (`:1285-1300`) when the robot goes offline.
  - Re-arm it with the remaining budget on the first state message after reconnect (`:1759-1761`).
  - Track elapsed online time ourselves, since `asyncio.sleep` cannot be paused.
  - Config: `MISSION_TIMEOUT_PAUSE_OFFLINE=true`.
  - Test: a robot offline longer than the timeout, then reconnecting, finishes the mission and no cancelOrder is sent.
- **C3, progress jumps after reconnect.** Already correct (verified):
  - `task_status` is set straight from the reported sequence (`:2424-2450`);
  - completion compares for equality on the final sequence (`:2452`);
  - `_reached_waypoints` feeds the resume offset.

  Remaining work:
  - Add `tests/unit/test_mission_reconnect_jump.py` covering:
    - sequence 2 → 8 on 5 waypoints;
    - 0 → final completes;
    - a jump on an offset (resumed) order;
    - a jump together with nodeSkipped;
    - an older message after a newer one does not regress;
    - a jump during a block.
  - Optional: `max()` on `last_node_seq_id` within one order (`:2478`).

### 3. allowedDeviationXY on every node (effort S-M)

- **Where:** at order build (`VDA5050Order.from_route` / `from_move` / `from_action`), so every route source gets it: planner, client routes, reroutes, `_replan_goto`, and offset resume.
- **Model** (`cloud_common/objects/common.py:132-137`, `Pose2D`):
  - `allowedDeviationXY` / `allowedDeviationTheta` become `Optional[float] = None`, documented as "None/0 = server policy".
  - Add `node_id: Optional[str]` (the graph node the waypoint came from; needed by step 6).
- **Policy** (`order_policy.py`):
  - `resolve_deviation(value, is_final, has_actions, cfg)` returns `value` if it is > 0.
  - Otherwise it returns 0.1 when the node is final or carries actions, and 0.35 for a pass-through node.
  - The start node (robot pose, sequence 0) gets `ROUTE_START_DEVIATION_XY_M` (0.35), so the robot does not try to re-reach its own pose. If the start node carries actions, it gets 0.1.
  - Theta: pass-through gets π (heading free), final gets 0.785. Confirm with the robot team.
- **Planner** (`mission_planner/server.py:507-514`): drop the hardcoded 0.2 / 0.785 and set `node_id`.
- **Client** (`missionApi.ts:240-254`): stop sending the deviation fields; update `__tests__/services/missionApi.test.ts`.
- **Config:** `ROUTE_DEVIATION_XY_PASS_M=0.35`, `ROUTE_DEVIATION_XY_FINAL_M=0.1`, `ROUTE_START_DEVIATION_XY_M=0.35`, `ROUTE_DEVIATION_THETA_PASS_RAD=3.1416`, `ROUTE_DEVIATION_THETA_FINAL_RAD=0.785`, `ROUTE_DEVIATION_ZERO_IS_UNSET=true`.
- **Migration:** none (JSONB). Legacy 0.1/0.2 values on PENDING rows are respected; this is an accepted transitional cost.
- **Tests:** new `tests/unit/test_vda5050_node_deviation.py`:
  - pass-through 0.35, final 0.1, start per config;
  - explicit 0.5 respected; 0 and None take the policy value;
  - a one-waypoint route is final (0.1);
  - `from_move`;
  - the last node of an offset resume.

  Update `test_mission_lifecycle_fixes.py:469`, `test_mission_goto.py:137,188` and `mission_examples.py:27`.

### 4. Map frame verification (effort S; the fix is in Phase 2)

- **Trace (verified):**
  - Waypoints are stored in the map frame.
  - `_route_in_robot_frame` (`server.py:1635`) applies `inverse(map_T_session)`.
  - `from_pose2d` still labels the result `mapId=<map name>`.
  - The start, move and action nodes are built from `agvPosition` with `mapId=""`.
  - So one order mixes `mapId=""` and `mapId=<map>`, all in session coordinates.
- **Robot side (local checkout, deployed version unverified):** `sati_vda5050_client` ignores `nodePosition.mapId` (`vda5050_client_node.cpp:1893-1897`) and drives in its own `map_frame_`. So this version cannot double-transform. The wrong label will bite once they honour `mapId`.
- **Second candidate for the constant offset:** reloc placements assume `map_T_session = identity` (`packages/utils/map_sessions.py:129-151`, TODO D0). That is wrong when the robot's map came from a later mapping session, and a stale placement after an undetected run change has the same effect.
- **Work:**
  - Add `MissionSentOrderV1.frame` (`mission.py:308-319`) with `{map_name, session_id, map_t_session, applied: inverse|identity|none, mapId_sent}`.
  - Make `_route_in_robot_frame` return it.
  - Persist it with `sent_order` and log it at INFO for every new order.
  - Add `tests/unit/test_order_frame.py`: a characterization test that documents the mismatch and reproduces a double transform with a fake robot. Parametrize it over the fix modes.
- **Config:** `ORDER_FRAME_MODE`. Default `legacy` (today's behaviour) until the robot team answers.

### 5. Robot reports: skipped nodes and node notes (effort M + M)

- **Already safe (verified):** an unknown WARNING is ignored by:
  - `get_mission_errors` (`:2622`);
  - `_track_stale_fatal` (`:2590-2614`);
  - `_handle_edge_blocked` (exact match on `"edgeBlocked"`);
  - `_dispatch_hold_reason`.
- **Hardening:** `ADVISORY_ERROR_TYPES = {"nodeSkipped"}` never fails a mission, even if a robot sends it as FATAL (we log the wrong level).
- **Models** (`cloud_common/objects/mission.py`):
  - `MissionSkippedNodeV1{node_id, order_id, mission_node, waypoint_index, graph_node_id, description, first_seen}`, deduped by `(order_id, node_id)`.
  - `MissionNodeNoteV1{node_id, order_id, info_type, description, waypoint_index, graph_node_id, offset{dx,dy,dtheta}, offset_map{dx,dy}, first_seen, last_seen}`, deduped by `(order_id, node_id, info_type)`, capped at `MISSION_NODE_NOTES_MAX=50`.
  - `MissionStatusV1.skipped_nodes`, `.node_notes`, and `.offset_summary{n, mean_dx, mean_dy, mean_norm, consistency}`.
- **Processing:** new `Robot._process_node_reports(message)`.
  - Call it in `update_mission_state` *before* the edge-blocked early return, so reports are kept while blocked.
  - Accept nodeIds from any revision of the current run (`_is_order_of_run`).
  - Compute `waypoint_index` only for the recorded `sent_order`, using the same formula as `:2530`.
  - Never change mission state and never send an order.
- **Offset detection:**
  - Rotate the robot's offsets into the map frame using the `frame` record of the order.
  - When n ≥ 5, consistency ≥ 0.8 and |mean| ≥ 0.2 m: log a WARNING once per run and emit `MISSION.FRAME_OFFSET_SUSPECTED`.
  - The event payload carries the mean vector and `map_t_session`, so a transform error stands out.
- **Events:** add `MISSION.NODE_SKIPPED`, `MISSION.NODE_NOTE` and `MISSION.FRAME_OFFSET_SUSPECTED` in `packages/events/codes.py` and `schemas.py`. Add `fleet_recorder` hooks modelled on `edge_blocked` (`fleet_recorder.py:936`).
- **Also update:**
  - reset the new fields in `_start_next_pass` (`:2169-2188`);
  - `agent_orchestrator/triggers.py`: report `nodeSkipped` as info, not as "new_error".
- **Wire names to agree with the robot team:** infoTypes `nodeOffset`, `nodeBlocked` and `areaNotObserved`, with infoReferences `nodeId`, `offsetX`, `offsetY` and `offsetTheta`.
- **UI:**
  - `missionApi.ts` `MissionStatus`;
  - a "Skipped N waypoints" line and a notes list in `MissionDetailOverlay.tsx`;
  - skipped indices in `utils/missionRouteProgress.ts`;
  - note markers through `utils/missionMapDisplay.ts`.
- **Tests:** `tests/unit/test_mission_node_reports.py`:
  - dedup and cap;
  - a reference to an older revision gives index None;
  - rotation;
  - the consistency maths;
  - the suspect event fires once;
  - no state change;
  - FATAL nodeSkipped does not fail the mission;
  - nodeSkipped is not taken for edgeBlocked.

  Client: `missionStatus.test.tsx`.

### 6. Blocked-node exclusion and reroute hygiene (effort L)

- **Store:** Postgres, per map, shared across robots. Dispatch and planner both already use Postgres.
  - Migration `packages/api/migrations/versions/20261008_01_blocked_graph_nodes.py` (down_revision `20261003_01_map_reconstructions`, idempotent):

    ```
    blocked_graph_nodes(map_name, graph_node_id, edge_from NULL, edge_to NULL,
      source CHECK IN ('edgeBlocked','nodeBlocked','operator'), robot_name, mission_name,
      vda_node_id, reason, x NULL, y NULL, created_at DEFAULT now(), expires_at NOT NULL,
      PRIMARY KEY (map_name, graph_node_id))
    ```

    Upsert sets `expires_at = GREATEST(old, now()+interval)`.
  - Do **not** add the table to `DISPATCH_REQUIRED_TABLES`: writes are best effort with a logged warning.
  - Shared SQL and helpers go in `packages/utils/blocked_nodes.py`, which is present in the dispatch, planner and API images.
- **Mapping a VDA nodeId to a graph node:**
  1. `order_ids.node_index` / `node_sequence`.
  2. `waypoint_index = offset + seq//2 - 1`.
  3. Take `Pose2D.node_id` from that waypoint (added in step 3).

  Fallbacks, in order:
  - `planned_path[waypoint_index]` when the mission has a single route node;
  - otherwise a synthetic `"@x,y"` id in the map frame, which the planner matches within `BLOCKED_NODE_MATCH_RADIUS_M=0.5`.
- **Writer (dispatch):** `_handle_edge_blocked` (only when the block is new, after the idempotency check) and `nodeBlocked` notes write via `asyncio.ensure_future`.
  - Also store the edge's endpoints (waypoint i-1 and i) for later edge-level exclusion.
  - Optional: clear a row when the robot later reaches that node (`BLOCKED_NODE_CLEAR_ON_PASS=true`).
- **Reader (planner):**
  - `plan_route` (`mission_planner/server.py:629-778`) loads the active rows.
  - With exclusions present, plan with a Python BFS over `edges_{map}` that skips excluded vertices. The current AQL `SHORTEST_PATH` is unweighted (hop count) and cannot filter vertices, so BFS gives the same results.
  - Without exclusions, the existing AQL path is used unchanged.
  - The start node is never excluded.
  - If no path avoids the excluded nodes, fail with `failed_at="blocked_nodes"`, a readable error (the node, its expiry, and which robot reported it) and the `blocked_nodes` list. There is no silent fallback.
  - `ignore_exclusions=true` overrides; a blocked goal gets its own error.
  - `_replan_goto` keeps its stored-route fallback but logs the reason.
- **API / UI:**
  - `GET /api/v1/maps/{map}/blocked-nodes` and `DELETE …/{id}` (operator clear).
  - The reroute PUT (`main.py:2298-2340`) answers 409 if the new route goes through an excluded node, unless `force: true`.
  - Client: draw blocked nodes in `RerouteMissionOverlay.tsx` and confirm "route anyway".
- **Agent orchestrator:** issues no reroutes today (`server.py:225-232`). Mention the exclusion in its prompt (`agent.py:33-36`).
- **Anti-pattern audit:**

| Anti-pattern | Today | Change |
|---|---|---|
| Cancelling an order version just sent | Partly prevented. Echoes cannot re-trigger it (`route_rev`/`applied_route_rev` `:1040-1062` plus the outstanding-cancel gate). A second operator reroute within ~1 s still cancels the order before the robot adopts it (`:1172-1190`). | Hold a REPLACE cancel until the robot reports the just-sent orderId, or for `ORDER_CANCEL_MIN_DWELL_S=2`. The latest route is still sent, because it is read at build time. |
| Cancelling a reroute when the robot reaches the first node | No cancel path reacts to progress. A related bug: a stale robot `missionStatus:"canceled"` right after adoption triggers a second order. | Step 1 fix (honour "canceled" only when not executing). Ask the robot team for a log. |
| Re-sending a route through a node just reported blocked | Not prevented (finding 8, and operator or planner routes). | Step 1 fix plus planner exclusion plus API 409. `_build_order` warns if a route contains an excluded node. |
| Reusing orderId+orderUpdateId with different content | Prevented: `orderUpdateId` is always 0; an identical resend reuses the same object (`:880-885`); new content always gets `_bump_order_rev`. | Add a property test: one content hash per published (orderId, orderUpdateId). |
| Re-sending the same order several times | Bounded: back-off 1→8 s until `MAX_ORDER_MISMATCHES=40` (about 7-8 resends). | Cap at `ORDER_MAX_RESENDS=3`. Fail fast on an order rejection error that references our orderId. |

- **Config:** `BLOCKED_NODE_EXCLUSION_MIN=10`, `BLOCKED_NODE_MATCH_RADIUS_M=0.5`, `BLOCKED_NODE_CLEAR_ON_PASS=true`, `ORDER_CANCEL_MIN_DWELL_S=2`, `ORDER_MAX_RESENDS=3`.
- **Tests:**
  - `tests/unit/test_blocked_node_exclusion.py`: the row is written via `node_id`, via `planned_path` and via position; the offset index; expiry extension; a DB failure is swallowed.
  - `test_planner_exclusions.py`: BFS avoids the node; only path blocked; goal blocked; `ignore_exclusions`; expired rows ignored; start never excluded.
  - API: 409 and `force`.
  - Anti-pattern tests in `test_mission_order_churn.py`.

### 7. nodePolicy, phase 1 (effort M, behind a flag)

- **Model:**
  - `VDA5050ActionParameter.value: Any = None`.
  - `from_mission_action` keeps stringifying operator mission action params, so their wire format does not change.
  - Add `VDA5050Action.node_policy(node_id, max_wait_s, skippable=False, corridor_width=None)`, with `blockingType=NONE`.
- **actionId:** `f"{nodeId}-policy"`. It is unique per run, revision and order, and byte-identical on resend.
- **Build:** `from_route` adds the action to pass-through nodes 1..n-1 only, never to the start or last node, and never to move or action orders. Phase 1 sends `skippable=false` and `maxWaitS=NODE_POLICY_MAX_WAIT_S` (10).
- **Gating:** `VDA5050_NODE_POLICY_MODE = off | factsheet | on`, default `factsheet` (send only if `factsheet.custom_actions` lists `nodePolicy`, `server.py:2104-2117`).
- **Elsewhere:**
  - fix the `int()` parse in `get_mission_errors` (`:2616-2655`) for `-policy` ids by using `order_ids.node_index`;
  - `triggers.py` `_failed_action_ids` ignores nodePolicy, because the robot FAILs waiting actions on every cancel;
  - the client hides nodePolicy in `CustomActionsModal`.
- **Tests:** `tests/unit/test_vda5050_node_policy.py`:
  - placement;
  - typed JSON (`false`, `10`);
  - unique and stable ids;
  - the three gating modes;
  - operator params still sent as strings;
  - the triggers filter.

---

## Phase 2: after the robot team builds nodePolicy and agrees the frame

8. **Graph metadata and skippable (effort L).**
   - graph_builder already stores node `metadata` (`graph_builder/server.py:529,555-579`). Add `place_kind ∈ {doorway, chokepoint, open}` and `corridor_width_m`.
   - Sources: the robot's `node_update` payload, and/or an operator `PATCH /api/v1/maps/{map}/graph/nodes/{node_id}` (`graph_db.update_node` exists, `topomap_dbs/graph_db/server.py:539`). Client annotation goes in ActiveGraph / NodePreviewCard.
   - The planner copies both fields onto `Pose2D` (new Optional fields, excluded from the digest when None).
   - `skippable=true` only if `place_kind == "open"`, the node is pass-through, has no actions, is not the last node, and `corridor_width_m` is known (always sent with skippable). Config `NODE_POLICY_SKIPPABLE_ENABLED=false`.
9. **Frame fix (effort M), per the robot team's answer.** Either way, every node of an order gets the same `mapId`.
   - `ORDER_FRAME_MODE=map`: send map-frame coordinates and convert the start pose session→map.
   - `ORDER_FRAME_MODE=session`: keep today's coordinates, with `mapId=<session id>` (or `""`) on every node.
10. **Optional (effort M):** edge-level exclusion, and automatic replan with exclusions for go-to missions on edgeBlocked.

---

## Joint test checklist (robot team's wire rig, `sati_vda5050_client/test/wire/run_wire_tests.py`)

1. Deviations 0.35 / 0.1 / start node are honoured (drive-by on pass-through, stop at final).
2. nodePolicy with typed values parses, NONE blocking does not split the route, and an identical resend is idempotent.
3. With nodePolicy states first and instant actions last, cancelOrder FINISHED is still seen.
4. nodeSkipped WARNING: the mission stays RUNNING and `skipped_nodes` is filled.
5. nodeOffset / nodeBlocked / areaNotObserved infos are stored with no reroute; nodeBlocked writes an exclusion.
6. Broker outage longer than the mission timeout: on reconnect progress jumps, the mission completes, and no cancel is sent.
7. edgeBlocked then operator reroute: exactly one cancelOrder, then one new orderId, and the route avoids the node.
8. A stale robot "canceled" after adoption does not cause a second order.
9. Frame: with a non-identity `map_T_session`, the nodes reached match the agreed frame mode.
10. Factsheet with or without `nodePolicy` switches sending on or off.

## Delivery order

Phase 1: steps 1 → 2 → 3 → 4 → 5 → 6 → 7 (steps 1-4 are each small and can go out as separate PRs). Phase 2: 8 → 9 → 10.

Caveat: the local test environment installs Pydantic 2. Run the new model tests under `pydantic==1.9.0` as well, because `Optional`/`Any` defaults and `exclude_none` behave differently.

## Could not verify

- How the deployed robot handles `mapId`, `allowedDeviationXY`, the order of actionStates entries, and stale `missionStatus` after adoption. Only the local checkout (commit 83c7446b) was read.
- The final infoType and reference key names.
- Whether production ArangoDB behaves the same for the AQL kept on the no-exclusion path.

---

## Implementation status (2026-10-08, branch `feat/offline-missions` on main b79ff08; uncommitted)

Phase 1 is implemented in cloud_server, with unit tests (`tests/unit/test_offline_missions.py`, 55 tests; full unit suite green under Pydantic 1.10).

| Step | State | Notes |
|---|---|---|
| 1 Hardening | done | `_route_digest` excludes None; instant-action scan `continue`; action node matched by actionId; stale "canceled" ignored while executing; no resend after edgeBlocked; API status PUT keeps all dispatcher-owned fields (`DISPATCHER_OWNED_STATUS_FIELDS`) |
| 2 Offline | done | Timeout paused offline and resumed with the remainder (`MISSION_TIMEOUT_PAUSE_OFFLINE`); reconnect jumps covered by tests |
| 3 Deviations | done | `order_policy.py`; planner no longer hardcodes, sets `Pose2D.node_id`; start node 0.35, action start node 0.1 |
| 4 Frame record | done | `MissionSentOrderV1.frame`, logged per order; characterization test. Fix (`ORDER_FRAME_MODE`) still waits for the robot team |
| 5 Reports | done | `skipped_nodes`, `node_notes`, `offset_summary`; events `MISSION.NODE_SKIPPED` / `NODE_NOTE` / `FRAME_OFFSET_SUSPECTED`; nodeSkipped never fatal; agent triggers updated |
| 6 Exclusion | done | Migration `20261008_01_blocked_graph_nodes`; dispatch writes on a new edgeBlocked (not on advisory nodeBlocked notes); planner BFS around active rows, `failed_at: "blocked_nodes"`, `ignore_exclusions`; API list/clear + reroute 409 unless `force`; reroute-cancel dwell (2 s / until adopted); identical resends capped at 3 |
| 7 nodePolicy | done | Typed parameters, `{nodeId}-policy` ids, pass-through nodes only, `VDA5050_NODE_POLICY_MODE` (default `factsheet`) |

Review fixes, dispatcher (2026-10-08, `tests/unit/test_offline_missions_dispatch.py`):
- A "canceled" without the edgeBlocked error keeps a block (it used to clear it, and the next "canceled" resent the route). Only a "canceled" of the reroute's own order ends it.
- A paused timeout resumes on any robot state, not only when the online flag flips. An operator cancel keeps the timeout running while the robot is offline, as the backstop that ends the mission.
- Node reports and the edgeBlocked exclusion are only taken for an order built from the node's current route. A reroute drops the reports on the node's old route.
- A "canceled" while nodes are listed is ignored only while the robot drives or within 10 s of the order's first send; otherwise it is a drop and the node is resent.
- The reroute cancel dwell counts from the order's first send, not the last resend. The held cancel is reset at a new pass and at mission end. The two cancel sends share `_cancel_current_order`.
- `skipped_nodes` capped at 100. Repeated reports are skipped before they are resolved, so a note the cap dropped is not taken again. Non-finite offsets are rejected.
- An allowedDeviationXY of 0.1 (the old Pose2D default, which stored routes carry) reads as unset (`ROUTE_DEVIATION_XY_LEGACY_DEFAULT_M`, `none` turns it off). An unknown `VDA5050_NODE_POLICY_MODE` warns and falls back to `factsheet`. The policy is logged at startup, and compose passes the theta, zero and legacy keys.

Not done / deviations from the plan:

- No parity test between `config.py` and `order_policy.py`: the dispatch keys live only in `order_policy.py` (config.py points to it), so nothing is duplicated.
- No fail-fast on an order-rejection error (the robot's error type for it is not agreed yet); the resend cap bounds the noise instead.
- No auto-clear of an exclusion when a robot passes the node (`BLOCKED_NODE_CLEAR_ON_PASS`); expiry and the operator DELETE clear it.
- The migration and the blocked-node SQL were checked once against a throwaway Postgres 14 (upgrade twice, upsert keeps the later expiry, expired rows ignored, delete, source check, downgrade). No integration run against ArangoDB, MQTT or the full stack.
