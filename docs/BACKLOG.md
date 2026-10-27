# Backlog

Known issues and follow-ups that are understood but not (fully) resolved, or
resolved with a narrow fix that leaves a related risk in place. Newest first.

## Keep-out reroute: a detour the lazy reader could not read, root cause of the user's report not reproduced (2026-10-27)

Branch `fix/keepout-reroute` (`docs/keepout_zones.md` "Reroute"). Fixed: the lazy reader's overlay search jumped from 3 rings of charts to every
chart (`CorridorTooLarge` on a big map), and `plan_on_graph` measured corridor widths without the zones. Every other path was tested
and routes around a zone when a way exists; the report itself is not reproduced on synthetic worlds.
- Needs a look on the real map: the planner log line "Chart planner not used (reason)", `scene_graph.outcomes`, and the `issues[].reason` of the preview.
- The client prints "No route between" for any unroutable leg; it does not show `blocked_by_keepout` (sati-client `utils/routePreview.ts` `describeIssues`).
- A start/goal within the 0.2 m edge seal or inside a region has no route (by design); the editor does not say it is the zone.

## Bounded reconstruction cloud: open (2026-10-25)

`cloud.ply?max_points=N` is built and unit-tested (`docs/reconstruction/point_budget.md`); not run on a Pi, against a real MinIO or behind the new nginx location.
- The first request per (cloud, bucket) sends no byte until the decimated file is built (a full read of the source); no pre-building when a job finishes, and no tee-while-streaming.
- Uniform stride keeps the writer's order: if the service ever writes clouds sorted by one axis, the thinning is still uniform, but not a spatial (voxel) thinning.
- Cache lives in the API container's temp dir by default: lost on a container recreate (rebuilt on demand); set `RECONSTRUCTION_LOD_CACHE_DIR` to a volume to keep it.

## Keep-out zones: open (2026-10-25)

Built and unit-tested on synthetic maps (`docs/keepout_zones.md`); not planned against a real map or a real Postgres.
- A running mission's reroute and operator-drawn waypoints are not checked against zones (preview lists crossed legs only).
- A zone whose chart was retired turns `unresolved` and is ignored; no re-attachment by cell overlap (`lineage`).
- A robot standing inside a zone has no route out until the zone is disabled.

## Done after the 2026-10-22 demo, with what is still open (2026-10-23)

Pi 5 (`rasp`) runs tag `7dd955e869b5`; desktop 0.3.15 is packaged (not published).

**Done**
- **LiveKit on the LAN and the tailnet with one config** (`ec87188`): the Pi's hand-edits are in the repo (`rtc.ips`, signalling also on `ROBOT_LAN_IP`, `LIVEKIT_SFU_LAN_URL`, real image tags instead of the hand-built `lanvideo`). Checked from the Pi itself on 192.168.1.70, not from a real LAN-only client or with the Jetson on the LAN.
- **Restart and settings from the robot's LAN without the token** (`b44e76f`, `OPERATIONS_TRUST_LAN`, pi4 and pi5 profiles). Anyone on that network can restart services and change settings (`docs/security/open-items-2026-10-21.md` 5a).
- **Mosquitto keeps its state and has room** (`7dd955e`): 256m and a `mosquitto_data` volume; a retained message survived a restart in a local test.
- **Memory limits fit the Pi 5** (`7dd955e`): 7296 MB (91.7% of 7955 MB), planner 512m, Postgres 1g, MinIO 512m (local override). The app's 95% guard counts `RECON_MEM_LIMIT` where the profile sets it. Not measured under a real reconstruction job.
- **Diagnostics list judges the container, not its logs** (`38d6984`).
- **Scene graph and occupancy grid from depth only** (`0054236`, `POST /api/v1/maps/{map}/scene-graph/rebuild-from-depth`, client row "Build from depth"). Never run on real Arango/MinIO data; nodes without `pose3d_map` cannot be built; needs `SCENE_GRAPH_WORKER_ENABLED` and an ended or paused session.
- **Finish gate says why the upload does not drain** (`fix/look-for`): graph-builder keeps the robot's spool_state fields (`robot_report` on `GET /ingest/backlog`), the 409 and the client text name the cause and point to ORCHESTRATION > Spool. The robot-side latch (`leftovers_held`, `sent=0`) is not in these repos: root cause open.
- **Comments and one default**: robot compose/profile comments match the config; the MinIO console default is 9002 (9001 is mosquitto's websocket port).
- **Client 0.3.15** (`sati-client` branch `release/0.3.15`, local): frozen-video recovery and Reload button, diagnostics tabs, logs follow the newest line, teleop velocity client. Not run in a real app; the 8 s freeze threshold and the stats fields are guesses.
- **Teleop velocity v2, client side** follows the robot team's plan (`cmd_vel2/<identity>`, int16 mm/s and mrad/s, advertised by `teleop.velocity_v2` in participant metadata). Open on their side: the bridge, per-robot limits, the firmware maximum, and that the legacy topic uses the participant *name* while v2 uses the *identity*.
- **Robot picks its server from a candidate list** (`satibot_orchestrator` branch `feat/server-candidates`, `ea2447f`, not merged, not on the Jetson): LAN, then Pi tailnet, then dev box; `GET /server/active`. Rollout: merge, pull on the Jetson, compare its live `config.yaml`, restart the orchestrator, check `/server/active`, restart `base_services` and `livekit`.

**Open (found by the look-for pass, ranked by offline-run impact)**
1. Video token service and MinIO links are tied to the Pi's tailnet address (`robot.yaml` token `--host ${ROBOT_TAILNET_IP}`, `MINIO_HOST`). A LAN-only Jetson cannot fetch a video token unless the cached Tailscale netmap works. Needs a security decision (bind on the LAN too, or a gateway route).
2. `100.85.3.47` is a default in `electron/main.js` (`DEFAULT_SERVER_URL`, update feed), `IMAGE_REGISTRY` in the robot compose files, `scripts/setup_robot.sh`, `scripts/livekit/robot_client.py`.
3. Pi 4 memory: defaults sum to 4096 MiB on a 4 GB machine, plus 768m reconstruction, plus `MEM_GRAPH_BUILDER=1g`. Drop the 1g or turn something off.
4. The robot SFU relies only on `rtc.ips.includes`, which the cloud config calls unreliable (`livekit.yaml:7-9`). Check with `verify_candidates.sh` on the Pi.
5. Settings duplicated or contradictory between code, compose and profiles: `SCENE_GRAPH_WORKER_THREADS`, `SCENE_GRAPH_CACHE_VERSIONS`, `UPLOAD_BUFFER_MAX_BYTES` (256 MiB vs 100 MB), `VDA5050_NODE_POLICY_MODE`, missing path-cost settings on the Pi profiles, the LiveKit UDP range in docs (50000-60000) vs the robot (50000-50100), `SERVER_DIAGNOSTICS_AVAILABLE`.
6. Cheap Pi savings, not measured: graph-builder logs about 10 INFO lines per node, uvicorn access logs on everywhere, Postgres healthcheck every 5 s. Measure CPU first.
7. No cloud-side control to resume or discard a robot's spool latch (only via ORCHESTRATION > Spool on the robot).
8. Client connection-loss and offline UX was not reviewed.

## LiveKit: the client was on LiveKit Cloud, the robots on the self-hosted SFU (2026-10-03)

**Symptom:** The LiveKit room showed only client laptops; formidable-peacock (and
masked-frigatebird) never appeared, so no robot video or teleop.

**Cause:** sati-client picked its LiveKit backend from `EXPO_PUBLIC_LIVEKIT_BACKEND` and
defaulted to `cloud` when it was unset (also through `scripts/start-dev.sh`, which printed
"LiveKit backend: cloud"). Every robot's `sati_livekit_bridge` gets its token from the
self-hosted token service (`http://100.85.3.47:8008/api/createToken`, room
`admin@satinavrobotics.com`) and joins `livekit-sfu` (`ws://100.85.3.47:7880`). Different
servers, so never the same room. Separately, formidable-peacock's orchestrator `livekit` service
was stopped at 14:52 UTC (and stopped/started every few minutes before that; cause unknown), and
masked-frigatebird's Go bridge had been dead since 2026-10-01 ~18:02 UTC with nothing restarting it.

**Fix:** sati-client `0a8b988`: selfhost is the default (cloud only with
`EXPO_PUBLIC_LIVEKIT_BACKEND=cloud`), start-dev.sh exports selfhost, a test guards the default.
formidable-peacock's `livekit` service was started again (joined 14:54:44 UTC).

**Still open:** why formidable-peacock's `livekit` service keeps being stopped; its video needs
`odin_driver` or `navstack_sati_odin` for images; masked-frigatebird needs a clean `sim_livekit`
start and the bridge launch should respawn (or exit with) its Go process. Check who is in the room
with `docker logs sati_livekit_sfu | grep -E "participant (active|closing)"`.

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

**Known risk — fixed since (2026-10-09, `2be7831`):** `get_mission_errors()` now ignores a FATAL error whose references all belong to another run, revision or mission (`_is_foreign_error`, `packages/controllers/mission/server.py`), and unreferenced FATALs that predate the order (`_track_stale_fatal`). The text below is the original risk note.

**Known risk not covered by this fix (original note):** `get_mission_errors()` in the same
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
