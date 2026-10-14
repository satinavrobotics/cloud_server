# Per-map run statistics (2026-10-14)

`GET /api/v1/maps/{map_id}/run-stats` (shape: docs/API_REFERENCE.md). Code: `packages/api/run_stats.py`.

## Scope
Non-archived `mission_runs` with `map_id` = the map and `started_at` in `[from, to)` (default: last 30 days),
optionally one robot. `map_id` is the map of the robot's open session when the run started; runs without a
map (`runs_without_map`) belong to no map.

## Definitions
| Metric | Definition |
|---|---|
| finished | runs that are not RUNNING |
| success_rate | COMPLETED / (COMPLETED + FAILED + TIMEOUT + ABORTED). A cancel is not a failure. |
| abort_rate | (FAILED + TIMEOUT + ABORTED) / finished |
| interventions | cancels + teleop + takeovers |
| takeovers | CANCELED runs with a `ROBOT.ERROR_RAISED` `operatorTakeover` event |
| cancels | the other CANCELED runs (state-based, so a missing `MISSION.CANCEL_REQUESTED` event does not lose one) |
| teleop | `TELEOP.SESSION_STARTED` events with `payload.map_id` = the map in the window |
| free_success_rate | COMPLETED runs with no teleop session of that robot during the run / finished |
| hours | per robot, the union of `[started_at, ended_at]` of finished runs (overlaps once), summed. Runs longer than `RUN_STATS_MAX_RUN_S` (7200 s) are left out: a hung run (e.g. a cancel 14 h after the start) is not operating time. Their count: `data_quality.hours_excluded_runs`. |
| rate_per_hour | interventions / hours |
| duration_s | median and P95 of `ended_at - started_at` over COMPLETED runs; `timeout` separately (count, median) |
| recovery | over the legs of the scope's runs: median/P95 of `recovery_s` for legs with `recoveries > 0`; `share_of_time` = sum(recovery_s)/sum(duration_s); `share_of_legs` = legs with recovery / legs |
| safety | events counted by `SAFETY_CLASSES` in `run_stats.py`: `ROBOT.ERROR_RAISED` with `error_type` `safetyReflexError` (safety_reflex); `MISSION.EDGE_BLOCKED`, `NAV.GOAL_BLOCKED` (blocked); `MISSION.NODE_SKIPPED` (skipped); `ROBOT.HEARTBEAT_LOST` (link_loss). Attributed by `run_id`, or for events without one by robot and time inside a run of the scope. No collision or geofence data is invented. |
| by_cause | `abort_cause` of finished runs that did not complete (null = UNKNOWN) |

## Teleop signal
The operator's token carries no robot (the room is the operator's email), so the record is made when the
**robot** requests its room token (`POST /api/createToken`, role `robot`) on the self-hosted token service: that
service calls the API (`/internal/teleop/token-issued`, loopback, best effort, no token sent) and the API writes
`TELEOP.SESSION_STARTED` with the robot's RUNNING run and open-session map. The token service has no database
by design. A second token of the same robot and room within 30 min (`TELEOP_SESSION_GAP_S`) is the same session.
It counts room joins, an upper bound of actual driving. Before the release nothing was recorded:
`data_quality.teleop_recorded` is false when the window starts before the first stored event.

## Cause rules added (migration `20261014_01_run_cause_codes`)
`Safety reflex latched` -> `SAFETY.REFLEX_LATCHED`; `Robot base not responding` -> `HW.BASE_NOT_RESPONDING`;
`Robot did not accept the dispatched order` -> `DISPATCH.ORDER_NOT_ACCEPTED`; `not placed on map` ->
`MAP.NOT_PLACED`. UNKNOWN stays the fallback. Existing runs are not re-classified (their causes stay UNKNOWN).
Deploy the API (runs the migration) before or together with mission-dispatch.
