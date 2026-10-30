# Map events and safety events (2026-10-14)

`GET /api/v1/maps/{map_id}/events-summary` and `/events` (shapes: docs/API_REFERENCE.md). Code:
`packages/api/map_events.py` (ONE helper, `_attributed_cte`, used by both endpoints and by run-stats' `safety`).

## Attribution (which map an event belongs to)
First match wins:
1. **run** - `run_id` names a non-archived `mission_runs` row with a `map_id`: that map.
2. **session** - the robot's `map_sessions` row (operate or mapping, paused or not) open at the event time
   (`started_at <= ts` and `ended_at` null or `ts < ended_at`): its `map_name`.
3. **none** - neither; the robot was on no map. Not counted anywhere (`attribution.unattributed` reports the
   number for the robots seen on the map).

Why: only about 5% of `ROBOT.ERROR_RAISED` and 35% of `NAV.RECOVERY_ENTERED` carry a `run_id`, and the old
run-stats rule (run_id, else robot+time inside a run of the scope) missed everything the robot did outside a mission.
Run and session never disagreed in the audit.

### Audit (live data, last 30 days, 2831 safety-relevant events)
| | events |
|---|---|
| with `run_id` whose run has a map | 631 |
| run-less, attributable by robot + time overlap with a run (old rule) | ~33 |
| attributable by the robot's map session (new rule, no run map) | 1247 (1218 operate, 29 mapping) |
| attributable by neither | 953 (940 of them robot formidable-peacock, outside any session) |

Per event code (total / with run_id / via session / none): RECOVERY_ENTERED 1462/528/343/605;
ERROR_RAISED safetyReflexError 193/1/186/6; robotBaseNotReady 546/3/425/118; GOAL_BLOCKED 77/20/15/42;
HEARTBEAT_LOST 69/2/32/35; MISSION.EDGE_BLOCKED 19/19/0/0.

Safety counts per map, old rule -> new rule: reflex office6 5 -> 78, Officemap 0 -> 53, office04 0 -> 33,
office7 2 -> 15, office01 0 -> 6, office03 0 -> 2; blocked office7 26 -> 33, office01 1 -> 7; link_loss
office6 1 -> 9, office7 0 -> 9, Officemap 0 -> 4, office04 0 -> 3. 193 reflex events globally: 187 now land on a
map, 6 on none.

## Episodes (duplicates)
`ROBOT.ERROR_RAISED` is already edge-triggered by the robot-side bridge: 1191 raised, 1190 episodes
(one repeat in 30 days), so the earlier worry (a fault raised every heartbeat) does not occur in the data. The
rule is still enforced: a RAISED counts only if the previous event of the same robot + `error_type` (RAISED or
CLEARED, looked up `MAP_EVENTS_EPISODE_LOOKBACK_DAYS` = 7 before the window) is not a RAISED. `NAV.RECOVERY_ENTERED`
is counted once per recovery: the previous NAV.RECOVERY_* event of the robot must not be another ENTERED
(1462 of 1462 qualify; ENTERED/EXITED are balanced). Every other code counts one per event.
`ROBOT.ERROR_RAISED` of `edgeBlocked` / `nodeSkipped` mirror `MISSION.EDGE_BLOCKED` / `NODE_SKIPPED` and are left out.

## Event universe and classes
`NAV.RECOVERY_ENTERED` (recovery); `ROBOT.ERROR_RAISED` (safety_reflex for `safetyReflexError`, else error);
`MISSION.NODE_FAILED` (error); `MISSION.EDGE_BLOCKED`, `NAV.GOAL_BLOCKED` (blocked); `MISSION.NODE_SKIPPED`
(skipped); `ROBOT.HEARTBEAT_LOST` (link_loss); `ROBOT.OFFLINE`, `MISSION.REROUTED` (a reroute the dispatcher applied, one per applied `route_rev` that replaced at least one route; before 2026-10-30 it was also recorded when an edge block cleared, now `MISSION.BLOCK_CLEARED`, not counted),
`MISSION.CANCEL_REQUESTED`, `TELEOP.SESSION_STARTED` (other). Not counted: battery, node up/down, state
changes, run start/finish, map administration, the cleared/exited/restored halves.
**Safety** = the first five classes of `SAFETY_CLASSES` (safety_reflex, blocked, skipped, link_loss); recovery is
reported but is not a safety event. Rules: `CLASS_RULES` (one table; the `class` filter SQL is derived from it).

## SQL and cost
Reads `fleet_events` through `fleet_events_code_ts_idx`, restricted to the robots with a session or run on the map
in the window; the attribution joins use `mission_runs_pkey` and `map_sessions` (robot, started_at). Live: a
30-day summary on office7 in about 60 ms. No migration or index is needed. Answers are cached 30 s
(`MAP_EVENTS_CACHE_TTL_S`). run-stats' `safety` now counts by event time in `[from, to)` rather than by run start.
