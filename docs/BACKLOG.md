# Backlog

Known issues and follow-ups that are understood but not (fully) resolved, or
resolved with a narrow fix that leaves a related risk in place. Newest first.

## Mission dispatch: a fresh mission can complete instantly off the previous
mission's terminal waypoint sequence id (2026-09-14)

**Symptom:** After one mission on a robot completed, every subsequent mission
started from the client was reported `COMPLETED` (or in the client's display,
"done") within ~100ms of being sent, before the robot had moved at all.

**Root cause:** `Robot.update_mission_node_state()` in
`packages/controllers/mission/server.py` decides a route mission node is
complete by combining two fields from the robot's VDA5050 `/state` message:
`lastNodeId` (checked only for the *prefix* — does it belong to the current
mission?) and `lastNodeSequenceId` (compared numerically against the route's
terminal sequence id). The robot's VDA5050 client
(`sati_vda5050_client/src/vda5050_client_node.cpp`, `InitAGVState()`) reset
`last_node_id` to the new order's node 0 id as soon as a new order was
accepted, but did **not** reset `last_node_sequence_id` at the same time —
that field was only ever written later, by the waypoint-reached feedback
handler. So the first `/state` message of a new order could pair the new
order's node-0 id (passing the prefix check) with the *previous* order's
final sequence id (failing to be reset), e.g. observed live:
`lastNodeId=Test2-n1-s0` with `lastNodeSequenceId=6` left over from a
previous 3-waypoint route. Sequence id 6 satisfied the route-complete
arithmetic (`current_order_node_id == route.size * 2 + 2`) for a mission with
enough waypoints, completing it on the spot.

This is the same *class* of bug as the one already documented inline at
`server.py:922-932` (a mission completing 55ms after dispatch off a stale
`lastNodeId`) — that fix keyed the sequence id on the `lastNodeId` prefix so
a *foreign* last-node id couldn't leak in. It did not cover the case where
the id and the sequence id both look like they belong to the current mission
but do not describe the same node, because the robot updated one and not the
other.

**Fix applied (both sides, defense in depth):**
- Robot: `InitAGVState()` now resets `last_node_sequence_id` to the new
  order's node-0 `sequence_id` in the same place it resets `last_node_id`
  (`sati_vda5050_client/src/vda5050_client_node.cpp`).
- Cloud: `update_mission_node_state()` cross-checks `lastNodeSequenceId`
  against the sequence id encoded in `lastNodeId`'s own `-s{seq}` suffix
  (every node id the cloud generates carries one) and prefers the id's own
  suffix when they disagree, so a robot that only updates one of the two
  fields can no longer produce an inconsistent pair. Covered by
  `tests/unit/test_mission_stuck_order.py::test_stale_sequence_id_under_own_node_id_does_not_complete_mission`
  and `::test_sequence_id_from_node_id`.

**Verified live (2026-09-14):** two single-waypoint missions with distinct,
non-overlapping waypoints dispatched back to back on the same robot; both
ran their full, realistic durations (22s and 24s) with real navigation
in between — no instant completion. (An earlier verification attempt using
identical waypoints for both missions produced a *different*, superficially
similar-looking instant completion — see the "Known risk" note below; that
was a Nav2 waypoint-pruning artifact of the identical-waypoints test setup,
not this bug, and does not indicate the fix is incomplete for the case it
targets.)

**Known risk not covered by this fix:** `get_mission_errors()` in the same
file treats *any* FATAL-level entry in the robot's `errors[]` array as
belonging to the currently-tracked mission node, regardless of which order
the error actually references. Since VDA5050 `errors[]` is a live snapshot
re-sent on every `/state` message (not a one-shot event), a lingering error
from a rejected order-replace attempt (e.g. `orderUpdateError` /
"An order is running", currently `WARNING` level so not yet hit by this
path) could instantly fail a brand-new mission if it or a future error of
that kind is ever raised at `FATAL` level while unrelated to the order in
flight. Not fixed here — flagging for a future pass: `get_mission_errors()`
should filter by the same node/order-reference check it already does for
`node_id`/`action_id` references before letting an error fail the *current*
mission node, rather than trusting `errorLevel == FATAL` alone.

**Related, separately fixed:** the cloud never sends `cancelOrder` before
dispatching a new mission's order while the robot may still be finishing the
previous one (no cancel-before-replace handshake in `_send_order()` /
`_try_start_mission()`), which is what produces the `orderUpdateError` /
"An order is running" rejection and the resend-until-`MAX_ORDER_MISMATCHES`
storm in the first place (see `git log` on this file for the "Robot never
adopted our order" guard, and [[livekit-bridge-udp-only]] /
[[dds-shm-container-fragility]] for a different, transport-level cause of
the same-looking "robot didn't adopt the order" symptom). Adding an explicit
cancel-and-wait-for-idle step before dispatching a mission's first order
would remove the rejection entirely rather than only bounding its blast
radius; not done here because it changes the dispatch protocol's timing
rather than fixing a specific incorrect read of robot state.
