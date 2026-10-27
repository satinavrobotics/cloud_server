# Robot path efficiency: driven path vs the lattice plan path (2026-10-15)

Where: `packages/api/path_efficiency.py`. Served as `efficiency` in `GET /api/v1/maps/{id}/run-stats` and (opt-in) per leg in
`GET /api/v1/runs/{id}/legs?with_efficiency=true`. Read only; no schema change, no new writes.

## Question and answer

How much longer than the planner's path did the robot drive? Per leg: `ratio = planned / driven` (1.0 = exactly the plan, 0.8 = drove 25 %
more, > 1 = cut corners). `extra_pct = (driven / planned - 1) * 100`.

## What a leg is

One `run_legs` row: the move between two consecutive reached waypoints of a route node (leg_tracker). Its `from_vda_node` / `to_vda_node`
are `...-n{i}-s{seq}` with `seq = 2k + 2` for waypoint `k` of tree node `i`; `seq 0` is the padding node at the robot's position.

## Planned length

* **trajectory** (the headline): the polyline of `mission_runs.mission_tree[i].route.trajectory` leg `(k-1 -> k)` (docs/plan_trajectory_2026-10-14.md),
  the lattice path the planner priced. Used only when the leg is two adjacent waypoints of one route node.
* **straight** (fallback, reported apart): the distance between the two reached waypoints (`straight_m`). Until 2026-10-27 `run_legs.planned_m`
  was always this: it is NOT the lattice path, it is a lower bound of it, so a ratio against it reads as detour even for a perfect drive. Used
  for runs before 2026-10-14, for route nodes without a trajectory, for legs between route nodes.
* **Recorded plan (stage B, 2026-10-27)**: dispatch now compares the live route's trajectory with the leg when the leg ends and stores the answer in
  `run_legs.plan_source` (`trajectory` | `straight` | `stale`) with `planned_m` = the polyline length for `trajectory` (packages/utils/plan_length.py,
  the same rules as below, the ends compared with the poses the robot was sent to). Such legs are read as recorded. Legs with `plan_source` NULL
  (older, `planned_m` = `straight_m`) are classified at read time from the run's stored route snapshot as before.
* **stale**: a trajectory exists but does not match the leg (see Replans). The straight line is used, and the leg is counted as `straight`.
* The first leg of an order (`from seq 0`: the robot's own pose, not a waypoint) has `straight_m` null: no plan, excluded (`no_plan`).

## Driven length

`robot_track_ts.x_map/y_map` (1 Hz, map frame) of the run, summed in SQL per `(run, leg_seq)`:

* consecutive rows of one run, session and run epoch form a pair; a pair belongs to the leg of its **end** row (its start row's where the end row
  is between legs). The error at each boundary is symmetric, so it is unbiased over many legs.
* same rules as drive-stats: a pair with dt > `DRIVE_STATS_MAX_GAP_S` (5 s) or speed > `DRIVE_STATS_MAX_SPEED_MS` (2 m/s, a pose jump)
  invalidates the leg (`gap`; it is not guessed over). dt <= 0 is ignored.
* a pair slower than `DRIVE_STATS_MIN_SPEED_MS` (0.05 m/s) adds nothing: localization jitter of a standing robot is not driving.
* a leg whose valid pairs sum to less than `EFFICIENCY_MIN_COVERAGE` (0.8) of `straight_m` has an incomplete track (`incomplete_track`):
  driving less than the straight line is impossible, so the track missed part of it.
* legs without any map-frame track row (before tracking stage 1, or recording level below track): `no_track`. `run_legs` has no driven distance,
  so such legs enter only the **time** block (and `source.leg_only`). A run is never counted from both sources.

## Decisions

* **Replans / reroutes mid-run.** `mission_runs.mission_tree` is the snapshot at the run's start; a replan (`_replan_goto`) or reroute (`order_rev` > 0)
  changes the waypoints afterwards. The leg's recorded `straight_m` is compared with the distance of the snapshot's two waypoints; a difference above
  `EFFICIENCY_STALE_TOL_M` (3 cm) marks the trajectory stale. On office7 the one rerouted run has 37 of 60 legs stale, caught this way.
  Stale legs are not in the headline (`source.stale_trajectory_legs`). Not detected: a replan that kept the same waypoints and only changed the
  path between them (cannot be seen from the stored data; the snapshot trajectory is then compared with the real drive).
* **Recovery.** The driven path includes recovery spins and back-ups. `excluding_recovery` removes, per track pair, the share of its distance that
  falls in the `NAV.RECOVERY_EXITED` interval (`ts - duration_s .. ts`) of the run (time-proportional). `time.ratio_excl_recovery` removes
  `run_legs.recovery_s` from the actual time. The headline keeps recovery in (the robot did drive it).
* **Legs without trajectory.** Not in the headline (`legs`, `driven_m`, `ratio`...). `by_plan.straight` and `source.straight_legs` show what
  they would say against the straight line; read it as an upper bound of the detour.
* **Short legs** (plan < `EFFICIENCY_MIN_LEG_M`, 0.2 m): excluded (`short`), the ratio of a few centimetres is noise.
* **Goals where the plan changed** (replan at a new goal): a new mission/run has its own stored trajectory; within a run see stale above.
* **Too little data.** Under `EFFICIENCY_MIN_LEGS` (3) legs the ratios, extra_pct, quantiles and `time.ratio` are `null` (counts and sums stay),
  with a note. `efficiency` itself is null only when the map has no legs in the window, or the computation failed (a note in `data_quality`).
* **Time block.** All legs with `expected_s` (VDA limits model `d/v + v/a + dpsi/omega`) and a duration: `ratio = expected / actual`
  (1.0 = as fast as expected, lower = slower). Independent of the track.

## Computation and cost

Three queries in one read-only transaction: legs of the scope, per-leg track sums (window function over the scope's track rows with the
recovery overlap in SQL; one row per leg returned) and the trajectory-bearing route nodes (waypoints x,y and trajectory only, not the whole
tree). numpy runs in a thread. Cached `EFFICIENCY_CACHE_TTL_S` (120 s) per (map, requested window, robot), single flight; the 5 s fleet-read
statement timeout applies (503 if exceeded, then `efficiency` is null with a note and run-stats still answers). Leg cap `EFFICIENCY_MAX_LEGS`
(200000; `data_quality.truncated`).

## Live cross-check (read-only, 2026-10-15)

office7, all runs: 691 legs read, 341 with a usable track, 239 on a trajectory in 28 runs: driven 391.5 m, planned 393.0 m, ratio 1.004,
extra_pct -0.4, per-leg p10 / median / p90 = 0.774 / 1.012 / 1.264; excluding recovery ratio 1.012 (2.9 m of recovery). The 102 straight-planned legs:
ratio 0.758. 291 legs have no track (time only). office8, Hospital, office6: legs but no map-frame track yet, so only the time block. Time ratio
(expected/actual) 0.56 on office7.

## Risks

* The headline is the ratio of a 1 Hz track length to a polyline: track noise lengthens the driven path slightly (jitter below 0.05 m/s is dropped),
  the local planner smoothing corners shortens it (the median above 1 shows both are small).
* Trajectories exist only from 2026-10-14; the sample is small until more runs accumulate.
* The stale test needs `straight_m` (null on the first leg of an order, which has no plan anyway).

## Note (2026-10-23): gates move the per-leg boundaries

With nodePolicy v2 the robot counts a pass-through node as traversed when it is within
`allowedDeviationXY` (0.35 m) **or** crosses the node's gate inside its corridor. A robot can
therefore pass a node with a sideways offset of up to the corridor width (per side, capped at
1.5 m) and still count it as reached. Per-leg times and distances, which cut the run at the
moment each node was reported reached, carry a boundary error of up to that width. Whole-route
numbers are not affected.

What to check on the first gate run: the `EFFICIENCY_MIN_COVERAGE` rejection rate. Change
nothing before that is seen. What each order sent per node is in the sent-order record
(`status.sent_order.node_policy`, summary as `node_policy_summary` on the mission GET).
