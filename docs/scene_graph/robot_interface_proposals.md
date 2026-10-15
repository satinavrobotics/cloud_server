# Robot interface: waypoint authority, order trimming, replan trigger, factsheets (2026-10-15)

Status: proposals and decisions, nothing built. Written by the lattice_cimbi session. Section 1 is a decision (accepted by the user
2026-10-15). Sections 2 and 3 are proposals that need the robot team and a planner endpoint. Section 4 is an open, non-blocking item.
Section 5 is a note. "Verified" below means checked in the named source; "not verified" means it is a stated assumption or a design
point that no run or source read has confirmed.

---

## 1. Decision: the server decides the waypoints

**Verified** (robot repo `deployment_ws/src/sati_ros_navstack`, read by the lattice_cimbi session):

| Fact | Where |
|---|---|
| The robot's vda5050 client sends the order to Nav2 `NavigateThroughPoses`. | `sati_vda5050_client/src/mission_manager.cpp` |
| The behaviour tree runs `RemovePassedGoals` (radius 0.5 m), then `ComputePathThroughPoses` over the remaining goals. | `sati_nav_stack_bringup/config/nav2/bt_trees/navigate_through_poses_recovery_v2_budget.xml` |
| `general_goal_checker` xy_goal_tolerance is 0.5 m. | `sati_nav_stack_bringup/config/nav2/tier1/navigation_common.yaml` |
| The mission manager does not skip to a later waypoint after a failure (its comments say so on purpose). | `mission_manager.cpp` |
| mission-dispatch sends `allowedDeviationXY` 0.35 m for pass-through nodes. | `packages/controllers/mission/order_policy.py` |

**Consequence.** Nav2 plans again the global path between two consecutive waypoints on the robot's own costmap, with Smac, the
smoother and MPPI setting the curve. The waypoints are therefore the only interface between the server's plan and the robot. The
server owns:

* the homotopy class (which side of an obstacle, which door, which floor);
* the clearance placement of each waypoint;
* the number of waypoints.

The robot owns the curve between them. So the server formulates waypoints as **guidance with a cost budget**, not as a polyline the
robot must follow exactly. This is the waypoint budget of `close_evidence_and_turn_cost_design.md` section C.2, built as
`SCENE_GRAPH_WAYPOINT_BUDGET_M` (off by default).

Status: decided. Not verified: that the robot's curve between two waypoints stays within the budget on a real run (no run has measured
it yet).

---

## 2. Proposal B: order trimming by progress (not built)

### 2.1 Problem

The robot visits every waypoint in order. A waypoint is dropped only when the robot is within 0.5 m of it (`RemovePassedGoals`).
Nothing skips a waypoint when a later one is already visible and the shortcut is no dearer. The `skippable` node policy
(`order_policy.py`, `docs/node_policies_2026-10-13.md`) is for blocked nodes, not for shortcuts.

**Verified:** the 0.5 m radius rule and the absence of a shortcut rule (robot repo, section 1 table). **Not verified:** how often
this costs time or distance on real runs.

### 2.2 Statement

At each state report, with the robot position `p` and the last released node `i`, let `k` be the largest index `> i` such that:

1. the chord `p -> w_k` is in free space (the planner's line of sight on the stored map), and
2. its priced cost is at most the priced cost of the remaining polyline `p -> w_i ... w_k` plus a trim budget `delta_trim`
   (metres of priced cost; default the goal tolerance, 0.35 m).

Nodes `i .. k-1` are dropped and the shortened order is sent as a VDA5050 order update (same `orderId`, `updateId + 1`).

Guards:

* only forward trims (`k > i`);
* never while the robot is in recovery, or when the next node is blocked;
* at most one trim per `T` seconds (hysteresis);
* a kill switch, default off;
* an event for every trim.

Exactness: the test uses the same visibility and priced cost as the planner's pull, evaluated from the robot position. So a trim never
raises the planned cost above the plan's cost plus `delta_trim`.

### 2.3 Not verified

* Whether the robots accept an order update that drops released-horizon nodes mid-run. The VDA5050 update rules require an update to
  start from the last released node; whether this proposal fits those rules is **not verified**.
* How Nav2 reacts to a changed goal list mid-run. **Not verified**; needs the robot team.
* Alternative on the robot side (the robot team's decision): a BT condition that prunes a goal by cost-to-go progress instead of the
  0.5 m radius. Not verified as feasible.

---

## 3. Proposal: replan trigger (not built)

### 3.1 Statement

A plan has a priced cost `J*`, stored in the plan's pricing record. With the robot at `p`:

* `c(p)` is the priced cost-to-go to the goal on the stored map (the planner's distance field);
* `s` is the priced cost already driven, from the recorded track.

The plan is still within budget while `s + c(p) <= J* + delta_replan`.

Leaving the plan's cost ellipse (`close_evidence_and_turn_cost_design.md` C.2) is the principled trigger to ask the planner for a new
plan from `p` (a REPLACE). This applies whether the cause is a dynamic object or a change in the plan.

### 3.2 Current state

Stated in the session brief (not re-checked in code here): the server has no such rule. Re-routing relies on the robot's own costmap and on
blocked-node events.

### 3.3 Not verified and required

* Where `s` is measured (`run_legs` or the mission track). Not verified.
* The cost of computing `c(p)` per state report. Not measured.
* The rule needs a planner endpoint for cost-to-go. None exists yet.
* Hysteresis and a minimum interval between replans are required, so that noise does not cause replan loops. Not designed in detail.

---

## 4. Open future item (non-blocking): factsheets of two robots

**Verified** (read from the live API on 2026-10-14): `masked-frigatebird` and `subtle-pelican` have `speed_max` 1.0 and no `length`,
`width` or `angular_speed_max` (-1).

Consequences:

| Effect | Cause | Status |
|---|---|---|
| The planner uses the default footprint (margin `r * sqrt(2)`). | no length/width | Stated fact; not verified against the planner code in this session |
| The turn cost uses the operator fallback `SCENE_GRAPH_TURN_LAMBDA_M_PER_RAD` (0.5 m/rad) instead of `speed_max` / `angular_speed_max`. | no `angular_speed_max` | Stated fact; not verified in this session |
| Graph builds count their leaves in `default_r_d`. | no length/width | Stated fact; not verified in this session |

Action: the robot team registers `physicalParameters` (length, width, `speedMax`, `angularSpeedMax`; VDA5050 2.1) for these robots.

Reference robot: formidable-peacock has these parameters (0.5 x 0.5 m, 0.24 m/s, 0.36 rad/s, lambda about 0.67 m/rad).

Non-blocking. Not verified: the size of the effect on route quality for these two robots.

---

## 5. Note: kappa

`SCENE_GRAPH_SAFETY_KAPPA` = 1.0 behaved reasonably in the live run of 2026-10-13 (formidable-peacock, office7): 16 plans, all served
by the chart planner; 4 of 8 missions completed with 0 recoveries (`docs/scene_graph/pipeline_state_2026-10-14.md` section 4).

* It is a plateau choice, **not a calibrated value**. Range 0 to 10 (`docs/scene_graph/route_safety.md`).
* It can be tuned when more run data is available: join `run_legs` and NAV events with the clearance profile of the legs
  (`pipeline_state_2026-10-14.md` section 6, item 3). Not done.

Verified: the live-run numbers, as recorded in `pipeline_state_2026-10-14.md`. Not verified: that kappa 1.0 is the best value.

---

## 6. Related documents

| Document | Used for |
|---|---|
| `docs/scene_graph/close_evidence_and_turn_cost_design.md` | C.2 waypoint budget, cost ellipse, turn cost, lambda per robot |
| `docs/scene_graph/route_safety.md` | clearance pricing, kappa range |
| `docs/scene_graph/pipeline_state_2026-10-14.md` | live run 2026-10-13, kappa result, open items |
| `docs/node_policies_2026-10-13.md` | `skippable` node policy |
| `packages/controllers/mission/order_policy.py` | `allowedDeviationXY` 0.35 m |
| Robot repo `deployment_ws/src/sati_ros_navstack` | mission manager, BT XML, Nav2 configs (section 1 sources) |
