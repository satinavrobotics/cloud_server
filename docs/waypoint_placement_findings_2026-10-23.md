# Waypoint placement: findings and decisions (2026-10-23)

Phase 4 of `node_policies_v2_plan.md`. Investigation on the Pi 5 (`rasp`), read-only, against scene graph
`konferencia2` v69. The robot team reported waypoints on lethal cells (run 140048) and at spur tips
(run 123410); both ids look like robot-local time (UTC+2), so I checked every konferencia2 route
(iloop1, iloop3, hj, uloop, konferencia, two go-tos) instead of matching one run.

## What was found

1. **Stored routes go stale.**
   - A route is planned once, on the map of that moment. The map keeps learning while the robot drives.
   - Against the current map, 16 of 103 planner waypoints are off-chart or within 0.3 m of its edge
     (3 off-chart). They cluster at x 1.75–3.75, y 1.05–2.55.
   - The same operator waypoints of iloop1 and iloop3, re-planned today, give planner waypoints and a dense path of
     at least 0.4 m clearance. So the planner is fine on the current map; the stored plans are old.
   - Limits: old map versions are gone from the Pi, so the map the routes were planned on is not visible. Whether
     the chart edge equals the robot's lethal cell is not confirmed (needs a robot costmap dump).
2. **Narrow neck, no fix possible.** The konferencia route enters a pocket at about (3.4, 1.8) through
   a neck 0.14–0.30 m from the chart edge. The planner has no better path; moving waypoints would not help.
3. **Operator waypoints are fine.** 43 of 44 are clear of the edge. Exception: first uloop waypoint (2.56, 0.12), 0.10 m.
4. **The planner already prices clearance** and counts `waypoints_below_floor`. A plan-time "pull toward clearance"
   would duplicate that, so it is not proposed.

## Decisions (user, 2026-10-23)

- **Stale routes are handled on the robot side**, where its live costmap and the nodePolicy gate/skip already work.
  My reading, to be confirmed: the cloud does not move, reject or block anything on a stored route.
- **The operator keeps full access.** Operator waypoints are never moved, rejected or limited, wherever they are.
- **Spur tips:** probably a misunderstanding on the robot-team side. Kept open; not investigated further.

## Open items

- **Stale-route check on the cloud:** a send-time check against the current map (re-plan only planner waypoints; a
  notice only, recorded in the Phase 3 sent-order record) was proposed, not decided. Not implemented.
- **Spur tips:** needs topomap leaves; planner waypoints carry no `node_id` in stored missions. Ask the robot team
  what they meant before any work.
- **Chart edge vs lethal cell:** ask the robot team for a costmap dump of the band y 1.95–2.55 (optional now).
- **Narrow-neck warning in route preview** (the operator sees it, nothing is blocked): optional, not decided.
