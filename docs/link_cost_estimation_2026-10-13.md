# Estimating link cost (time, speed) for the planner: exploration (2026-10-13)

Status: **exploration, read-only.** No code changed except the optional script
`scripts/analysis/linkcost_backtest.py`. All numbers come from SELECTs on the live Postgres (`mission`) on 2026-10-13; they are
a snapshot, re-run before relying on them. Companion to `docs/tracking_redesign_2026-10-13.md` (transits are the planned data source).

## 1. Summary

* The recorded behaviours are rich enough for a decomposed model (drive, stop, recovery by cause, block). What is missing is
  **volume at the chart-link level**: the live data has 343 leaf legs in 59 runs over 5 days, from two robots, one of which
  gave 9 legs. No chart-link transit exists yet, and only 50 leaf links have both ends as topomap nodes, 6 with three
  or more observations.
* Per-leg time is dominated by **per-hop overhead and events**, not distance (corr(straight_m, duration_s) = 0.21 on 1.4 m hops).
  A leg with a recovery takes a median 31.5 s against 11.0 s without. So the cost that matters for routing is the
  **probability and length of recoveries**, which a pure speed-from-distance model cannot see.
* Recommendation: compute-on-read, behaviour-decomposed estimate per chart link with hierarchical shrinkage and a
  recency half-life, a planner-facing quantile (p80-p90) beside the median, and the back-test in section 6 as the gate.
  Needs roughly **30+ completed mission runs that cross each link of interest, over at least 2-3 weeks and both
  directions**, before a link-specific estimate beats the prior (section 3). Today nothing can show that.

## 2. What the server records about behaviour

### 2.1 Per leg (`run_legs`, one row per waypoint-to-waypoint hop)

`duration_s`, `stopped_s` (time the VDA5050 `driving` flag was false, per state message, capped at 30 s per interval),
`straight_m` (chord between the legs' waypoints; null on 69 of 343 legs), `planned_m`, `expected_s` (d/v + v/a + |dpsi|/w from the
robot's factsheet limits; null on the same 69), `recoveries`, `recovery_s`, `blocks`, from/to VDA node and (on 75 legs) topomap node,
`map_id`, `pass_index`, `order_rev`, start/end with robot and receive time. `recoveries`, `recovery_s` and `blocks` are filled
after the run from `fleet_events` rows whose payload carries `leg_seq`.

### 2.2 Events (`fleet_events`; `packages/events/schemas.py`)

| Code | Payload | Rows live | Notes |
|---|---|---|---|
| `NAV.RECOVERY_ENTERED` | `cause`, `leg_seq` | 1254 (46 runs) | from the robot's `nav_supervisor` state, DRIVE to RECOVER |
| `NAV.RECOVERY_EXITED` | `cause`, `duration_s`, `leg_seq` | 1254 | RECOVER to anything else |
| `NAV.GOAL_BLOCKED` | `cause`, `detail`, `leg_seq` | 60 (5 runs) | rising edge of `blocked_pending` |
| `MISSION.EDGE_BLOCKED` | `edge_id`, `block_reason`, `held_s`, `skip_refused`, `leg_seq` | 12 (11 runs) | the robot's error reference, VDA5050 |

Caveats measured:

* Only 337 of 1254 recovery entries have a `run_id` and 243 a `leg_seq`: most recoveries happen outside missions (teleop, mapping,
  idle) or were not tagged, so they never reach `run_legs`.
* `duration_s` has absurd tails on a few causes (`FROZEN` max 51 345 s, `NO_VALID_CONTROL` 94 870 s, `UNATTRIBUTED` 69 296 s;
  medians are 1.6-7.8 s). These are a robot left in RECOVER while idle or offline. `run_legs.recovery_s` sums them as is: it
  must be **capped or censored** before use (on the leg table the max is 47 s, so these are not in the legs now, but nothing
  prevents it).
* Recovery `cause` (the supervisor's `last_drive_cause`) is recorded per event, e.g. `RESOLVE_LOOK` (203), `MANEUVER_FAILED` (162),
  `FROZEN` (151), `NO_VALID_CONTROL` (137), `UNATTRIBUTED` (109), `PLANNER_NO_PATH` (97), `FRONTIER_FOOTPRINT_BLOCKED` (86),
  `NEAR_BLIND_MEMORY` (83), `NEAR_FIELD_BLOCKED` (76), `UNOBSERVABLE` (60). Median recovery lengths per cause are 1-8 s.

### 2.3 What the server does not record

* **There is no "resolver" or "maneuver" state on the server.** The words appear only as recovery `cause` strings
  (`RESOLVE_LOOK`, `MANEUVER_FAILED`) copied from the robot's `nav_supervisor` messages. No code under `packages/` models them.
* **The robot's behaviour-tree state does not reach the server.** The server gets (a) VDA5050 state (`driving`, `lastNodeId`,
  velocity, errors, node/edge states), (b) the `nav_supervisor` status, of which the detectors keep only DRIVE vs RECOVER, `last_drive_cause` and
  `blocked_pending` (the latest raw snapshot sits in `robot_latest.nav_supervisor`; there is no history of it), and (c) diagnostics.
  Time spent *inside* a recovery (looking, manoeuvring, waiting) is one number per episode, its cause being the one that entered it.
* The server's own mission behaviour tree (`packages/controllers/mission/behavior_tree.py`) is the dispatcher's mission logic, not
  the robot's.

## 3. What the track shows (`robot_track_ts`)

Rows: 6563, all at `recording_level = track`, 61 runs, 2026-10-09 to 2026-10-13. Sampling is **1.5 s** (median and p99), not 1 Hz;
7 gaps over 2.5 s. `x_map/y_map` are still 0 rows (stage 1 not deployed), so poses are run-frame. `speed` is |(vx, vy)| of the
reported velocity, `omega` the reported yaw rate. 2.35 h in total.

Time shares (each row weighted by the time to the next row, capped at 3 s), three states: **driving** `speed >= v0`; **turning in
place** `speed < v0` and `|omega| >= w0`; **still** otherwise.

| v0 (m/s) | w0 (rad/s) | driving | turning in place | still |
|---|---|---|---|---|
| 0.02 | 0.05 / 0.10 / 0.20 | 0.731 | 0.026 / 0.011 / 0.002 | 0.243 / 0.258 / 0.267 |
| 0.05 | 0.05 / 0.10 / 0.20 | 0.569 | 0.161 / 0.135 / 0.111 | 0.269 / 0.296 / 0.320 |
| 0.10 | 0.05 / 0.10 / 0.20 | 0.407 | 0.272 / 0.226 / 0.186 | 0.320 / 0.367 / 0.406 |

Chosen thresholds: v0 = 0.05 m/s, w0 = 0.10 rad/s, giving driving 57%, turning in place 13.5%, still 30%. Per run (53 runs of at least 30 s):
driving median 0.67 (p10 0.45, p90 0.82), turning median 0.15 (p90 0.22), still median 0.19 (p90 0.51).

Sensitivity: the **driving share moves from 0.73 to 0.41 with v0 alone** because this robot drives slowly (moving rows: speed
quartiles 0.09 / 0.15 / 0.22 m/s, p90 0.25, max 0.46) and a large part of the rows sits just above zero (ramp up and down). The
turning share is almost entirely a function of v0. The split between "slow driving" and "still" is therefore **not identifiable
with a fixed threshold**; the comparison with `run_legs.stopped_s` (driving flag false: 7.5% of leg time) says the robot's own
`driving` flag is far more permissive than v0 = 0.05. Prefer the robot's flag for "stopped", and use speed only to measure the
moving speed (median of moving rows is about 0.15 m/s, and chord/duration of legs 0.15 m/s too).

What the track can add over the legs: stop time *inside* a leg located in time, turn-in-place time (a cost at sharp joints that the
legs fold into `duration_s`), and an independent check of `straight_m`. What it cannot: the cause of a stop (needs the events).

## 4. Data volume and what it means for power

* `run_legs`: 343 legs, 59 runs (of 106 `mission_runs`; the others have no legs, 34 aborted: OPERATOR.CANCELED 17, UNKNOWN 14, one each
  NAV.PATH_BLOCKED, NAV.LOCALIZATION_LOST, COMMS.HEARTBEAT_LOST). 2026-10-08 to 2026-10-13; 1.36 h of leg time; robots:
  formidable-peacock 334 legs, masked-frigatebird 9.
* Leg duration: median 11 s, 37 legs under 1 s (padding or zero-length hops). Hop length 0.8-2.0 m. Planned path 3.6 nodes on
  average (52 runs with `planned_path`; the mission tree carries a `wait` step in only 5 of 106 runs).
* **Link identity**: only 75 legs have both ends as topomap nodes (50 distinct links); the other 268 have VDA-tail ids
  (`nK-sM`: position in the mission, comparable only inside one mission definition, **not** geometric).
  Observation counts per topomap link: 39 links x1, 5 x2, 4 x3, 1 x6, 1 x8. Six links have a reverse direction recorded.
* 12.7% of legs (39 of 306 over 1 s) have a recovery, 2 have a block.
* `edge_observations`: 558 rows (leaf pairs, mostly mapping drives); not useful for this (decision D4 drops it).
* `recording_level`: 45 of 106 runs are `events_only` or `full` (no track), 61 are `track`, all from 2026-10-09 on.

Power. For one link with per-observation CV of about 0.15 (the 8- and 6-observation links; 0.48 on a link with a recovery) a median
good to +-10% needs roughly (1.96 x CV / 0.10)^2 = 9 clean observations; with recoveries in the mix (CV about 0.5, heavy right tail) a
**p90 needs 30-50 observations per link and direction** to be stable. Probability of recovery at 12.7% per leg and an error of
+-5 points needs about 170 traversals; +-10 points about 40. A chart link is crossed once per route through it, so reaching these
counts per link takes tens of runs through the same region, not hundreds of runs over the whole map. Today the best link has 8.

## 5. Variance and dwell

* Where several clean observations exist, leg time is tight: CV 0.06-0.14 (n = 3-8, mean 6-13 s); one link with a recovery in the
  sample shows CV 0.48 (10.0 to 29.5 s). The variance is mostly a **mixture**: clean traversals cluster; recoveries add a
  heavy tail. Legs without recovery or block: median 11.0 s, p90 22.0 s. With a recovery: median 31.5 s, p90 70.4 s, mean
  recovery 9.1 s per leg (median per episode 3.3 s) while the leg grows by about 20 s, so the recovery itself explains only part
  (it also contains the stop and re-approach). Treat the delta, not `recovery_s`, as the cost of a recovery.
* `stopped_s` and `recovery_s` correlate 0.75 across legs; the median leg has no stopped time, 53 legs have more than 0.5 s.
* **Dwell at waypoints.** The `nodePolicy` action is non-blocking and says how long to *hold when blocked* (`maxWaitS`); the robot
  drives through intermediate nodes, so it is not a planned dwell. There is no planned stop at a node in a go-to. The
  only deliberate dwell is the mission tree's `wait` action, executed by the dispatcher as a timer (no robot order).
  `leg_tracker` restarts the leg's clock when a new route node's base node is reported, so a `wait` between route nodes is
  outside the legs by construction (by reading the code, **not verified against a live run with a wait**: only 5 runs
  contain one). Rules for exclusion: drop a leg whose interval overlaps a `wait` of the mission tree; never count a time with
  `blocked_pending` as drive; time held by the robot at a node because of `nodePolicy` (`held_s` on `EDGE_BLOCKED`) is a block cost, not
  a link time. Also ignore the dispatcher's `cancel_min_dwell_s` (2 s, a reroute detail).

## 6. Estimator options

Unit: a chart-link traversal (a **transit**), with the component data from the legs and the events inside its interval.

1. **Behaviour-decomposed (recommended).**
   `E[T] = L / v + P(recovery) x E[recovery cost] + P(block) x E[block cost] + t_turn + t_stop`, with each term estimated on its
   own: `v` is the moving speed (median of moving rows or `straight_m/(duration - stopped - recovery)`), the recovery
   probability a rate per traversal (or per metre, since a longer link crosses more risk), cost as the *delta* of leg time with
   versus without recovery (section 5), and cause-specific terms where counts allow (`RESOLVE_LOOK`, `MANEUVER_FAILED`
   are frequent enough; the rest lump into other). Why: recoveries are rare but dominate the variance; a single mean blends them
   and is wrong for both the typical and the bad case. A quantile of the full distribution comes from a small mixture or an
   empirical resample of clean times plus recovery episodes.
2. **Direct empirical** (median/p90 of transit times, what `edge_stats` does): simplest, no model, but needs the section 4 counts
   per link and direction and says nothing about a link that has not been driven.
3. **Recency.** Exponential decay with a half-life (try 7 / 14 / 30 days) against last-N (N = 3 / 5 / 10). The decay copes with
   a map that changes (obstacles, floor, a new robot software); last-N is simpler to explain and bounds storage, but is noisy at
   small N. Pick by the back-test; add a **change reset** on `sw_version`, a new graph version or a robot change.
4. **Shrinkage for sparse links.** Hierarchical prior: link estimate = (n x link mean + k x prior)/(n + k), prior from (a) length
   / nominal speed (the `expected_s` model, with the observed ratio actual/expected, median 1.14, p90 2.9, so the prior needs its
   own calibration), (b) the robot-wide mean pace (s/m), (c) the chart-class mean (corridor vs open space, from the scene graph).
   k about 2-5 observations; choose by back-test. Unseen link: the prior and a **wide** interval.
5. **Direction and robot.** Keep direction as part of the key (a link a to b is not b to a: turns, slopes, approach); pool with
   a penalty only when a direction is empty. Robot as a random effect or as a pace factor relative to the fleet mean; with two
   robots and 9 legs on one, nothing can be estimated now, so key on robot only when it has data and fall back to the fleet.
6. **Censored and aborted traversals.** A run cancelled or failed mid-link gives a lower bound (the link took at least this
   long), not a sample. Do not drop them silently: that biases the estimate low exactly where the robot struggles. Options:
   store them as censored and use a survival fit (Kaplan-Meier on right-censored times), or count the abort as a block event
   and use it for the probability term. Aborts by operator cancel say nothing about the link and should be excluded.
7. **Risk-aware cost to the planner.** Planner edge weight = median + lambda x (p90 - median), or a pure quantile (p80-p90) for time-critical
   missions, plus P(block) as an exclusion hint. Expose the number, the count and the age so the planner can treat a stale or thin
   estimate as uncertain (inflate towards the prior).
8. **Calibration.** Predicted p90 should hold 90% of later actuals; check coverage per count bucket and per link class (PIT
   histogram or reliability curve), and report the Brier score for P(recovery). A calibration alarm per map is a cheap health
   check on the whole pipeline.

## 7. Back-test design

Replay in time order. For each transit i (sorted by `t_in`), build each estimator using **only transits ended before `t_in`**, predict
mean/median and p90, then compare with the actual. Rules: split by run so the legs of a run in progress are not used; no
leakage from post-run fields (`recoveries` is known only after); evaluate the first prediction per link separately (cold start)
from warm ones; keep time-ordered folds (rolling origin), never random folds.

Metrics: MAE and median absolute error of the median; relative error stratified by count bucket (0, 1-2, 3-9, 10+); p90
coverage (target 0.90) and pinball loss at 0.5/0.9; P(recovery) Brier; **planning value**: how often a route choice changes
between two candidate paths and which was faster in hindsight (the number that matters). Compare: global median, length-based
prior, direct empirical, shrunk mean, last-N, decayed, decomposed.

### First sketch on the live data (`scripts/analysis/linkcost_backtest.py`)

The sketch runs on **leaf legs, not chart links** (no transits yet) and is only a smoke test of the method. Five estimators (global median,
pace x length, shrunk link mean, shrunk last-3, shrunk decayed mean with a 7-day half-life), p90 by running ratio quantile.
Results on 306 legs of 1 s or longer:

* All legs, MAE (s): global median 7.90, pace x length 5.82 (n = 270), link mean shrunk 7.81, last-3 7.55, decayed 7.81.
  Median abs. error 2.5-3.5 s. Every estimator is biased low by 3-4 s because the recoveries are not predicted (clean legs: bias about 0).
* On clean legs of a link seen before: MAE 4.1-4.6 s link-based vs **3.3 s for pace x length** and 4.3 for the global median. The
  VDA-tail link keys are mission-structural, not geometric, so this proves nothing about link learning; it says that
  **length alone already beats the naive link history here**.
* `--topo-only` (75 legs, 25 predicted on a seen link): link estimators MAE 4.9-5.0 against 5.2 for the global median: a difference
  well inside the noise.
* p90 calibration: coverage 0.87-0.91 (n = 285) for all estimators, i.e. the ratio-quantile construction is calibrated by design;
  it has no information on which links are risky.

**Conclusion of the sketch**: with the present data the estimators cannot be told apart; the only visible signal is that leg time
depends on the recovery events and weakly on length. The sketch does show the pipeline works and which fields it needs.

### What would be needed for a real back-test

* Chart-link transits (stage 2 of the tracking redesign), with `t_in/t_out`, entry/exit pose, `recoveries`, `blocks`, `graph_version`.
* Counts: at least **10 links with 30+ traversals each in one direction** (about 30-40 mission runs that cover the same area), over
  **at least 3 weeks** with several operating days, so that time-ordered folds have 10+ warm predictions per link. With the
  current mission rate (59 runs with legs in 5 days, but concentrated on a few legs) this is probably 1-2 months of normal operation,
  or a **deliberate repeat-run campaign**: loop one route 30 times in both directions at a few times of day. That is cheaper than
  waiting and gives a clean ground truth for the clean/recovery mixture.
* A second robot with 30+ legs per link to say anything on robot dependence.
* Recovery events tagged with `run_id` and `leg_seq` (only 243 of 1254 entries today), with a cap on `duration_s`.
* Persisted `chart_keys` and `graph_version` per run (stage 1.5), so a run without a track can still be attributed.

## 8. Recommendation

1. **First version (compute-on-read)**: per (map, chart a, chart b, direction), over a half-life-weighted window (start 14 days, last
   N = 20 cap): count, median, p90, moving speed, recovery rate and cost delta, block rate; planner weight = median + risk term, with
   the count and the age shipped alongside; shrink to length/nominal-speed while n < 10 and mark the estimate "prior" below n = 3.
2. Record what is missing for it: cap and tag recoveries, keep the cause (already there), mark censored runs, persist `chart_keys`
   and `graph_version`, and store the leg's `driving`-false time in the transit.
3. **Later (accepted, planner team)**: a version-stamped aggregate per chart link in ArangoDB (count, median, p90, speed, recoveries,
   blocks, window, graph_version), rebuilt from the transit table per graph version. Transits stay the source of truth; the
   aggregate is a cache that can be rebuilt.
4. Gate the planner's use of link costs on the back-test: use them only where the warm error beats the prior's by a margin and the p90
   coverage is in 0.85-0.95.

## 9. Honest limits of today's data

* Two robots, one with 97% of the legs; five days; one site/map family; mostly short supervised test missions. Conclusions on
  fleet-wide behaviour are not supported.
* Legs are leaf hops of 1-2 m; chart links span many of them. Per-hop acceleration overhead is mixed into leg times, so leg speed
  (0.15 m/s) understates cruising speed over a long link; do not extrapolate it.
* The track is run-frame (no map-frame yet) and sampled at 1.5 s; the driving/still split depends on the threshold (section 3).
* No direction effect, robot effect, time-of-day effect or drift can be measured: the data has no repeated traversals of one link
  across days.
* Recovery causes, rates and costs come from a robot whose software changed during these days (`sw_version` not checked here).
* Wait-step exclusion rests on reading `leg_tracker`, not on a live run.
