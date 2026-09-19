# Deployment Ontology

This document maps the full set of entities, message topics, and state
machines that connect the **cloud server** (this repo, `Isaac Mission
Dispatch`) to the **robot fleet** (`deployment_ws/src/sati_ros_navstack`,
specifically `sati_vda5050_client` and `sati_pogany_nav2`). It is the
reference for "who talks to whom, over what topic, and what does each field
mean."

## 1. System-level picture

```
                         ┌─────────────────────────────────────────────────┐
                         │                 cloud_server                    │
                         │                                                 │
  Human / Fleet UI ──────┼──▶ api-delegation-service (8000, REST+WS)       │
                         │        │                                        │
                         │        ▼                                        │
                         │   mission-planner-service (8005)                │
                         │        │            │                           │
                         │        ▼            ▼                           │
                         │  graph-db-service  image-db-service             │
                         │   (ArangoDB,6001)   (MinIO,6002)                │
                         │        ▲                                        │
                         │        │ node/image updates                     │
                         │  graph-builder-service (8004) ◀───────┐         │
                         │        ▲                              │         │
                         │        │                              │         │
                         │   mission-dispatch (5000, VDA5050) ───┤         │
                         │        │            ▲                 │         │
                         │        ▼            │                 │         │
                         │   Postgres (5432)   │                 │         │
                         │                     │                 │         │
                         │                 mosquitto (1883/9001) ┼─────────┘
                         └─────────────────────┬┼────────────────┘
                                               ││ MQTT (VDA5050 + custom topics)
                                               ▼▼
                         ┌─────────────────────────────────────────────────┐
                         │                    robot                        │
                         │  sati_vda5050_client  ◀──▶  sati_pogany_nav2    │
                         │  (VDA5050 state/order/       (mission images /  │
                         │   connection/factsheet)       progress / nodes) │
                         └─────────────────────────────────────────────────┘
```

Two independent MQTT "vocabularies" coexist on the same broker:

1. **VDA5050 protocol topics** (`uagv/v2/<manufacturer>/<serial>/...`) —
   standardized fleet-management messages, owned by `sati_vda5050_client` on
   the robot and `mission-dispatch` on the cloud.
2. **Custom SATI topics** (`robot/...`) — mission-image delivery, waypoint
   progress, topological-map node updates, and GPS datum — owned by
   `sati_pogany_nav2` / `graph_builder` / `mission_planner`.

## 2. Cloud services and ports

| Service | Port | Role |
|---|---|---|
| api-delegation-service | 8000 | REST + WebSocket gateway, human-facing entry point |
| mission-dispatch | 5000 | VDA5050 mission controller (MQTT ↔ PostgreSQL) |
| graph-builder-service | 8004 | Subscribes to `robot/node_update`, `robot/image_upload`; builds topomap |
| mission-planner-service | 8005 | Path planning over the topological graph; mission image/progress MQTT |
| similarity-service | 8003 | Distance/traversability metric computation |
| graph-db-service | 6001 | ArangoDB wrapper — topological graph CRUD + spatial index |
| image-db-service | 6002 | MinIO wrapper — node image storage/retrieval |
| livekit-service | 8006 | Teleoperation video token service for LiveKit Cloud (pre-existing; caller-chosen grants) |
| mosquitto | 1883 (TCP) / 9001 (WS) | MQTT broker — the shared bus for everything below |
| postgres | 5432 | Mission/robot persistent state |
| arangodb | 8529 | Graph database backend |

Self-hosted LiveKit, in the same compose file (`docker_compose/mission_dispatch_services.yaml`, Tailscale-only; see `docs/livekit_sfu/README.md`):

| Service | Port | Role |
|---|---|---|
| livekit-sfu | 7880/7881 TCP, 50000-60000 UDP | Self-hosted LiveKit server (SFU) |
| livekit-sfu-tokens | 8008 | Role-scoped tokens for the self-hosted SFU (`robot` / `operator`); the self-hosted counterpart of `livekit-service` |
| (host: `tailscale serve`) | 443 (tailnet) | `wss://admin-satinav-pc.tail055f44.ts.net` → SFU `127.0.0.1:7880`, for the https dashboard; not a container, `tailscale serve status` |

## 3. VDA5050 topic ontology (`uagv/v2/<manufacturer>/<serial>/...`)

Prefix is built once per robot from `manufacturer` + `serial_number`
(robot side: `vda5050_client_node.cpp:259`; cloud side: `mission-dispatch`
uses the same prefix scheme with a `+` wildcard for the serial segment).

| Suffix | Publisher → Subscriber | QoS / retain | Purpose |
|---|---|---|---|
| `/connection` | Robot → *(not consumed by mission-dispatch)* | QoS 1, retained | Liveness/LWT — see §4 |
| `/state` | Robot → mission-dispatch | QoS 0, not retained | Full VDA5050 telemetry — see §5 |
| `/order` | mission-dispatch → Robot | — | Assigns a VDA5050 order (nodes/edges/actions) |
| `/instantActions` | mission-dispatch → Robot | — | Out-of-band commands (e.g. `factsheetRequest`) |
| `/factsheet` | Robot → mission-dispatch | retained | Static capability description (published on connect + on `factsheetRequest`) |
| `/datum` | Robot → mission-dispatch | — | `RobotDatum {latitude, longitude, bearing_deg}` — GPS origin, used to auto-seed a map's datum |

Cloud subscriptions are registered in
`packages/controllers/mission/server.py:1220-1222` (`_mqtt_on_connect`):
`+/state`, `+/factsheet`, `+/datum` — **note `/connection` is not among
them**, so mission-dispatch currently has no explicit ONLINE/OFFLINE/
CONNECTIONBROKEN awareness; robot liveness on the cloud side would have to
be inferred from `/state` message recency (no such watchdog exists yet as
of this writing — see §4 caveat).

## 4. Connection ontology — ONLINE / OFFLINE / CONNECTIONBROKEN

Defined and owned entirely by the robot (`sati_vda5050_client`):

| State | Set by | Trigger | Mechanism |
|---|---|---|---|
| `ONLINE` | App code, explicitly | Successful MQTT connect + subscribed to `/order` and `/instantActions` | Regular retained publish (QoS 1) |
| `OFFLINE` | App code, explicitly | Node destructor, only if still connected | Regular retained publish (QoS 1), then clean disconnect |
| `CONNECTIONBROKEN` | **MQTT broker**, not the app | Abnormal disconnect (crash, network loss, missed keepalive) | Registered as the MQTT Last Will and Testament (LWT) at connect time; broker auto-publishes it, retained |

Practical meaning: `ONLINE` is an *application-readiness* signal (not just
"TCP connected") — it fires only after the robot has finished subscribing to
command topics, so a fleet manager seeing `ONLINE` knows the robot is ready
to receive `/order` and `/instantActions`. `CONNECTIONBROKEN` is the only
state the robot process never sets itself; it exists specifically to cover
the case where the robot can't say goodbye (crash/power loss/link drop).

**Gap to be aware of:** since `mission-dispatch` doesn't currently subscribe
to `/connection`, this LWT mechanism is *available on the bus* (any MQTT
client, e.g. a fleet dashboard, can subscribe to it directly) but is not
yet wired into the cloud's own robot-availability logic. If robot
availability needs to gate mission dispatch, this topic is the correct
place to add that subscription.

## 5. `/state` message (VDA5050State) — key fields

Published every `update_feedback_period` (default 1s) plus opportunistic
pushes on state changes. Parsed on the cloud side into `types.VDA5050State`
(`packages/controllers/mission/vda5050_types/vda5050_types.py`) and drives
`Robot`'s in-memory mission tracking in `mission-dispatch`.

Key fields (robot → cloud):
- Identity/order bookkeeping: `headerId`, `timestamp`, `orderId`,
  `orderUpdateId`, `lastNodeId`, `lastNodeSequenceId`. **`lastNodeId`/
  `lastNodeSequenceId` lag `orderId`**: they describe the last node the robot
  physically *reached*, so right after a new order is dispatched the robot echoes
  the new `orderId` while still reporting the previous order's final node. Node
  ids are named for their mission *run* (`{prefix}-n{node}-s{seq}`; the order id
  is `{prefix}-n{node}`), so `update_mission_node_state()` reads them as progress
  only when `lastNodeId` carries the current prefix, and treats
  a foreign one as "this mission has reached nothing yet" (the same as the empty
  `lastNodeId` before the robot's first order). Reading them unconditionally
  completes a brand-new mission on its very first `/state` message, because the
  previous route's terminal sequence id already satisfies the route-complete test
  (`current_order_node_id == route.size * 2 + 2`).
- Motion: `driving`, `paused`, `newBaseRequest`, `distanceSinceLastNode`,
  `velocity` (vx, vy, omega), `agvPosition` (x, y, theta, mapId,
  positionInitialized, deviationRange/localizationScore)
- Health: `operatingMode`, `safetyState` (eStop, fieldViolation),
  `batteryState` (batteryCharge, batteryVoltage, batteryHealth, reach, charging)
- Order progress: `nodeStates[]`, `edgeStates[]`, `actionStates[]`
- Faults: `errors[]` (with `errorReferences`), `information[]`. `error.errorLevel`
  is `WARNING` or `FATAL` (`VDA5050ErrorLevel`); only a `FATAL` error fails the
  mission (`get_mission_errors`) and only a `WARNING`-level `edgeBlocked` forces
  the robot back to `IDLE` (`_handle_edge_blocked`) — every other reported error
  is mirrored into `robot.status.errors` (`vda5050_errors_to_status_dict`, sent
  on every `/state` message) without touching mission/robot state at all. Which
  `errorType`s a *client* should badge as a robot fault (as opposed to a benign
  warning) is now an operator-editable fleet policy, not implied by this
  protocol layer — see `SettingsObjectV1` in §7 and `GET/PUT /api/v1/settings`
  in `docs/API_REFERENCE.md`.

  `errors[]` is a **snapshot of the robot's current error state, not a log of
  events**: `vda5050_errors_to_status_dict()` overwrites `robot.status.errors`
  wholesale on every message, so whatever the robot keeps republishing is what
  operators keep seeing. The robot side owns both halves of that contract —
  deduplicating a repeated condition instead of appending it, and dropping an
  error once it no longer holds (`NavigationHandler::AddError` /
  `ClearErrorsOfType`). A robot that appends instead will pin a stale fault on
  itself indefinitely: a rejected order (`orderUpdateError`, "An order is
  running") was observed republished 40 times per `/state` message, one per
  server resend, long after the orders involved had finished.

## 6. Custom SATI topics (`robot/...`)

These are outside the VDA5050 spec, defined by this project for
map-building and visual-navigation mission delivery.

| Topic | Direction | Owner (cloud) | Owner (robot) | Payload |
|---|---|---|---|---|
| `robot/node_update` | Robot → Cloud | `graph-builder-service` | `sati_pogany_nav2` (topomap side) | New topological node: `map_id`, `node_id`, `x`, `y`, `yaw`, `edges` |
| `robot/image_upload` | Robot → Cloud | `graph-builder-service` | `sati_pogany_nav2` | Node reference image, stored via `image-db-service` |
| `robot/{robot_name}/mission/images` | Cloud → Robot | `mission-planner-service` | `visnav_nav2.py` | Per-waypoint: `mission_id`, base64 `image`, `x`, `y`, `order`, `metadata {node_id, map_id, theta}` |
| `robot/{robot_name}/mission/progress` | Robot → Cloud | `mission-planner-service` (QoS 1) | `visnav_nav2.py` | `mission_id`, `robot_name`, `current_waypoint_index`, `next_waypoint_index`, `timestamp`, `status` |

`status` ontology for `mission/progress`: `navigating` (en route to a
waypoint) → `completed` (mission done) or `failed` (error/obstacle).
Mission-planner tracks mission progress purely from these MQTT messages
(not from Postgres) and serves it back out over
`GET /api/v1/missions/{mission_id}/progress`.

## 7. Core data objects (cloud side)

- **`RobotStateV1`** (`cloud_common/objects/robot.py`) — cloud-level robot
  lifecycle, distinct from VDA5050's `operatingMode`:
  `IDLE`, `ON_TASK`, `CHARGING`, `MAP_DEPLOYMENT`, `TELEOP`. Gates what
  transitions are legal (`can_switch_teleop`, `can_deploy_map`, `running`).
- **`MissionStateV1`** (`cloud_common/objects/mission.py`) —
  `PENDING → RUNNING → {COMPLETED | CANCELED | FAILED}` (terminal set via
  `.done`). Driven by VDA5050 order/state exchange, not by the
  `mission/progress` custom topic (that one is VisNav-specific waypoint
  tracking layered on top of a single VDA5050 order).
- **`MissionFailureCategoryV1`** — why a mission ended in `FAILED`:
  `ROBOT_APP` (robot reported failure), `TIMEOUT` (stuck in RUNNING too
  long), `DEADLINE` (missed deadline), `CANCELED`.
- **`SettingsObjectV1`** (`cloud_common/objects/settings.py`) — a single,
  fleet-wide, operator-editable config object, always stored under the
  fixed name `"global"` (a singleton simulated by convention on top of the
  normal name-keyed Postgres storage — there's no separate keyless/singleton
  mode). Currently carries just `fault_error_types: List[str]` (see §5).
  Served via `GET`/`PUT /api/v1/settings`.

## 8. End-to-end flows

**Mission dispatch (VDA5050 path):**
`api-delegation-service` (REST) → `mission-planner-service` (path plan) →
`mission-dispatch` publishes `/order` → robot's `sati_vda5050_client`
executes it, reports progress via `/state` → `mission-dispatch` updates
Postgres → `api-delegation-service` reflects it back over REST/WebSocket.

One robot runs one order at a time: `Robot._missions` is a per-robot FIFO and
`_try_start_mission()` dispatches only while `_current_mission is None`, so a
second queued mission stays `PENDING` until the first reaches a terminal state.

That serialization depends on a finished mission actually leaving the queue, and
the last leg of the loop above makes it circular — `mission-dispatch` writes the
terminal status to Postgres, and its own write comes back through the database
watcher as a mission change. `_on_mission_change()` therefore has to recognise
its own echo. Checking the echoed object's `status.state.done` is *not* enough:
the watcher can deliver a snapshot taken before the terminal write landed, still
reading `RUNNING`. `Robot._finished_missions` (bounded by
`MAX_FINISHED_MISSIONS_TRACKED`, forgotten when the object is deleted so a name
can be reused) records what this controller actually ran, and is the check that
does not depend on winning that race.

Losing it re-queues a completed mission, which then dispatches on top of the one
that legitimately followed it. The robot refuses the duplicate — correctly, with
`orderUpdateError` "An order is running" — and the duplicate fails after
`MAX_ORDER_MISMATCHES` state messages with "Robot did not accept the dispatched
order", overwriting the `COMPLETED` status the mission had already earned.

**Order / node id scheme** (`packages/controllers/mission/order_ids.py`): a
VDA5050 `orderId` names one order, and a robot may treat a repeated id as a replay
or a continuation, so the dispatcher never sends two different orders under one id.
`prefix` is `{mission}-r{run_id}` (`{mission}-r{run_id}v{order_rev}` after a
revision), where `run_id` (`MissionStatusV1.run_id`) is assigned once per run just
before the first order and persisted first, and `order_rev` is bumped, and persisted
first, when a cancelled node is resent with new content (operator route update,
edge-blocked reroute). Both are dispatcher-owned: the API ignores them on create and
preserves them on a status write. A mission that was already running before run ids
existed keeps the legacy `{mission}-n{node}` ids. `orderUpdateId` is always `0`: a
new run or revision is a new order, never an update. Plain resends (a robot that
hasn't adopted the order yet, a dispatcher restart) keep the same id. A robot that
ignores repeated ids should record only orders it *completed* -- not ones it merely
received or rejected -- or those legitimate retries would be dropped.

**Mission cancel (VDA5050 `cancelOrder` instant action):**
`POST /api/v1/missions/{name}/cancel` sets `needs_canceled` on the mission →
`mission-dispatch` publishes a `cancelOrder` on `/instantActions` (action id
`{prefix}-instantaction-n{headerId}`) → the robot cancels its Nav2 goal,
reports the action `FINISHED` in `actionStates[]` and stops republishing the
order's progress → `mission-dispatch` marks the mission `CANCELED`.

Two rules keep this to *one* cancel per user click. First, the same database
echo problem as above applies: the cancel write comes back as a mission change
with `needs_canceled` still set, so `_on_mission_change()` only mints a new
`cancelOrder` when `_has_outstanding_cancel()` is false — otherwise every echo
created a fresh action id (observed: 31 cancels for one click, each of which the
robot then had to reject). Second, the robot fails a `cancelOrder` that arrives
while another is in progress ("A cancelOrder is already in progress"), and
`handle_instant_action()` treats a `FAILED` `cancelOrder` as terminal and
cancelled — the earlier one is what actually stopped the robot, so the mission
is cancelled either way. Any other instant action reported `FAILED` is dropped
from the outstanding set without being counted as finished.

On the robot side, `cancelOrder` with no running order answers with a single
`noOrderToCancel` WARNING that *replaces* the previous one (snapshot semantics
per §5) and is cleared once a later cancel completes; a `cancelOrder` that finds
the client `IDLE` but a navigation goal still active stops that goal rather than
reporting `noOrderToCancel`. `driving` is true only while Nav2 is being driven
toward a node of the current order — it is forced false on cancel, on every
terminal navigation result, and while stopped at a node, and re-asserted for
each subsequent leg of the route.

**Visual-navigation mission images (custom path, parallel to the above):**
`mission-planner-service` fetches waypoint images from `image-db-service`
→ publishes them on `robot/{robot_name}/mission/images` → `visnav_nav2.py`
matches Nav2 goal poses to the closest image → publishes
`robot/{robot_name}/mission/progress` → `mission-planner-service` serves
progress over REST.

**Topological mapping (robot → cloud only):**
Robot publishes `robot/node_update` + `robot/image_upload` →
`graph-builder-service` calls `graph-db-service`, `image-db-service`, and
`similarity-service` to grow the topological graph used by
`mission-planner-service` for path planning.
