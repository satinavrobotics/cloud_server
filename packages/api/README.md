# API Delegation Service

## Overview

The API Delegation Service is a central API gateway that provides a unified REST and WebSocket interface for clients to interact with the robot fleet management system. It acts as a single entry point that delegates requests to the appropriate microservices.

## Features

- **REST API** for synchronous operations:
  - Load maps from graph database
  - Retrieve images from image database
  - Submit navigation requests (proxy to mission planner)
  - Query robot and mission status (proxy to Mission Dispatcher)

- **WebSocket API** for real-time updates:
  - Map updates from graph builder
  - Mission status updates
  - Robot status updates

- **Service Delegation**:
  - Proxies requests to graph database, image database, mission planner, and Mission Dispatcher
  - Manages WebSocket connections for real-time notifications
  - Provides unified error handling and logging

## Architecture

### REST API Flow
```
┌─────────────────────────────────────────────────────────────┐
│                    API Delegation Service                    │
│                    (Single Entry Point)                      │
├─────────────────────────────────────────────────────────────┤
│  REST Endpoints          │  WebSocket Endpoints             │
│  - /api/v1/map/load      │  - /ws/map/{map_id}             │
│  - /api/v1/images/{id}   │  - /ws/mission/{mission_name}   │
│  - /api/v1/navigate      │  - /ws/robot/{robot_name}       │
│  - /api/v1/robots/{name} │                                  │
│  - /api/v1/missions/{id} │                                  │
└─────────────────────────────────────────────────────────────┘
           │              │              │              │
           ▼              ▼              ▼              ▼
    ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐
    │ Graph DB │  │ Image DB │  │ Mission  │  │ Mission  │
    │          │  │          │  │ Planner  │  │Dispatcher│
    └──────────┘  └──────────┘  └──────────┘  └──────────┘
```

### WebSocket Proxy Architecture

The API Delegation Service implements a **WebSocket proxy pattern** for real-time updates:

```
┌─────────┐
│ Client  │
└────┬────┘
     │ ws://api-delegation:8000/ws/map/{map_id}
     ▼
┌────────────────────────────────────┐
│  API Delegation Service            │
│  ┌──────────────────────────────┐ │
│  │  WebSocket Proxy Manager     │ │
│  │  - Accepts client connections│ │
│  │  - Connects to backend       │ │
│  │  - Forwards messages bi-dir  │ │
│  └──────────────────────────────┘ │
└────────────────────────────────────┘
     │
     │ ws://graph-builder:8004/ws/updates/{map_id}
     ▼
┌────────────────────────────────────┐
│  Graph Builder Service             │
│  - Processes MQTT node updates     │
│  - Publishes to WebSocket clients  │
│  - Sends real-time map changes     │
└────────────────────────────────────┘
```

**Key Features:**
- **Transparent Proxying**: Clients connect to API Delegation, which proxies to backend services
- **Connection Pooling**: Multiple clients can share a single backend connection
- **Automatic Cleanup**: Backend connections close when no clients remain
- **Service Ownership**: Each backend service owns its update logic

## API Endpoints

### Health & Stats

#### `GET /health`
Health check endpoint.

**Response:**
```json
{
  "status": "healthy",
  "service": "api_delegation",
  "dependencies": {
    "graph_db": true,
    "image_db": true,
    "database": true
  }
}
```

#### `GET /stats`
Get service statistics.

**Response:**
```json
{
  "service": "api_delegation",
  "graph_db_url": "http://localhost:6001",
  "image_db_url": "http://localhost:6002",
  "mission_planner_url": "http://localhost:8005",
  "database_url": "http://localhost:5000",
  "default_map_id": "default",
  "websocket_connections": {
    "map_updates": 2,
    "mission_status": 1,
    "robot_status": 3
  }
}
```

### Map Operations

#### `POST /api/v1/map/load`
Load a map from the graph database.

**Request:**
```json
{
  "map_id": "warehouse_floor_1"
}
```

**Response:**
```json
{
  "success": true,
  "map_id": "warehouse_floor_1",
  "stats": {
    "node_count": 150,
    "edge_count": 300
  },
  "message": "Map warehouse_floor_1 loaded successfully"
}
```

A map this route registers is typed like the M1 migration types old maps: `geo` if the datum is
real (not null, not (0, 0)), else `local`; state `ready`. It never retypes an existing map.
Since M2 `POST /map/load` with `GEO` / `LOCAL` (the old client's mapless views; reserved names,
never maps, since U6 no sentinels either) registers nothing: it returns no nodes and
`transform: null`.

#### Typed maps and mapping sessions (maps redesign M1)

See `docs/satinav-maps-redesign.md` §2-§4, §7, §13.1-§13.2. Code: `packages/api/maps.py`. The
routes above and `PUT /api/v1/maps/{id}/datum` are unchanged; `PUT /api/v1/robots/{r}/map` (a
deprecated shim over sessions since M2) was removed in U6 (below).

Every map has `type` (`local` | `geo`), `geo` (`{utm_zone, utm_north, origin_e, origin_n}` for a
geo map once its origin is known, else null) and `status.state` (`draft` | `mapping` | `paused`
| `ready` | `archived`). The object `lifecycle` (`ALIVE` / `DELETING`) is still the delete
bookkeeping. All additions are new keys; nothing was removed or renamed.

| Method | Path | Does |
|---|---|---|
| POST | `/api/v1/maps` | `{name, type, description?, slam_map?}` → 201, a `draft` map (`slam_map`, default false, only for `type: local`: 422 `["body","slam_map"]` otherwise; immutable; map views return it; converting to geo clears it). 409 if the name exists, collides with another map's image bucket (case/`_` vs `-`), or ArangoDB already has nodes under it. 422 on a bad name (1-59 of `A-Za-z0-9_-`, alphanumeric at both ends; not `GEO`/`LOCAL`) or type. |
| GET | `/api/v1/maps?type=&state=&include_archived=` | Same `{maps, count}` body as before. Archived maps only with `include_archived=true` or `state=archived`. |
| GET | `/api/v1/maps/{id}` | As before plus `type`, `geo`, `state`, `open_session_id`, `grid_version` and `sessions: {count, open, unaligned, items}` (newest first, at most 50). |
| PATCH | `/api/v1/maps/{id}` | `{description?, display_name?, slam_map?: bool}` → the map view. `display_name` (trimmed, at most 80 characters; "" or null clears) is the name shown for the map, stored in the map's spec JSON (no migration). `slam_map` only on a local map (409 geo), with no open/paused mapping session (409) and no SLAM save pending for the map's robots (409); `null` is 422; only the flag changes (an onboard SLAM map stays on the robot; turning it on again does not overwrite it); `MAP.SLAM_CHANGED` on a real change. Renaming is not supported (422): the name keys the map in Postgres, ArangoDB and MinIO; set `display_name` instead. |
| GET | `/api/v1/maps/{id}/graph` | `{map_id, type, geo, state, node_count, edge_count, nodes, edges, transform}`; nodes/edges as in `POST /map/load`, without its side effects. |
| POST | `/api/v1/maps/{id}/sessions` | `{robot}` → 201 `{map_id, map_state, changed, session}`. The robot must exist (404) and be online (409), and have no other open session (409). One open session per map for now (409). A geo map needs the robot's datum (409); its first session sets the map origin from it. The map goes to `mapping`. |
| POST | `/api/v1/maps/{id}/sessions/{sid}/pause` · `resume` · `finish` | Map → `paused` / `mapping` / `ready`, or `draft` after a finish when the map holds no data (no nodes in ArangoDB, read before the transaction, and no saved SLAM map: `status.slam_saved_at`; also for a `replace` and a robot delete). A SLAM map saved later (`MAP.SLAM_SAVE_DONE`) sets `status.slam_saved_at` and turns a `draft` map `ready`. Repeating an action already in effect returns `changed: false`; pause/resume of a finished session is 409. |
| POST | `/api/v1/maps/{id}/archive` · `restore` | `archived` (409 while a session is open) / back to `ready` when the map holds data (nodes or a saved SLAM map, the same rule as a finish), else `draft`. |
| DELETE | `/api/v1/maps/{id}` | As before (202, background delete); now 409 while a session is open. The map's sessions go with it. |

A session: `{session_id, map_name, robot_name, kind ('live' | 'legacy'), state ('mapping' |
'paused' | 'finished'), started_at, paused_at, ended_at, datum, map_T_session: {tx, ty, yaw},
aligned, node_count}`. `map_T_session` takes a robot-frame point into the map frame
(`packages/utils/map_geo.py`): a pure translation for a UTM datum in the map's zone, with the
grid-convergence yaw for an ENU datum; identity for a local map (aligned only for its first
session). Every pre-M1 map has one ended `legacy` session (robot `legacy`, identity).

Since M2 a session is what routes robot data: graph-builder stores a robot's nodes and images
in the map of its open, unpaused, placed **mapping** session (node `pose` in the map frame, plus
`robot_pose` and `session_id`) and drops everything else, reported as `MAP.INGEST_REJECTED` (at
most one per robot and reason a minute, with the drop counts). `reason`: `no_session`,
`map_deleting`, `map_missing`, `operate_session` (an operate session adds nothing, decision B
2026-10-09), `session_paused`, `session_unplaced`, `datum_changed`, `lookup_failed`. A pause or finish takes effect within ~1 s. Not yet:
alignment (M6), grid (M7). Events: `MAP.CREATED`, `MAP.ARCHIVED`,
`MAP.RESTORED`, `MAP.SESSION_STARTED/PAUSED/RESUMED/FINISHED`, `MAP.DELETED` (background delete
finished), `MAP.INGEST_REJECTED` (source `graph_builder`) in `fleet_events`. `POST /api/v1/maps`
and `POST .../sessions` accept `Idempotency-Key`.

#### `PUT /api/v1/robots/{robot}/map` — REMOVED (maps U6)

The old client's "assign map" (a shim over sessions since M2) and `robot.current_map` are gone:
a robot's map is its one open session (`GET /api/v1/robots[/{r}]` → `session`, below). The route
answers **410 Gone** for one release, with a message pointing to `POST /api/v1/maps/{id}/sessions`
(`purpose` `mapping` or `operate`, `replace: true` to switch maps) and
`POST /api/v1/maps/{id}/sessions/{sid}/finish` (mapless); it is removed in the release after.
Robot objects no longer have `current_map` (migration `20261002_01_drop_current_map` strips the
stored key); a `current_map` in a `POST` / `PUT /api/v1/robots[/{r}]` body is ignored. `GEO` /
`LOCAL` are no longer mapless sentinels: they stay reserved map names (old missions and runs carry
them as map ids), and a mission waypoint naming them is refused by the dispatcher like any map the
robot is not using. Mapless waypoints have an empty `map_id`.

#### Robot mapping switch through the orchestrator (docs/satinav-maps-redesign.md §14.15)

A mapping session's `services` (today `topo`) are started and stopped on the robot's
`satibot_orchestrator` (`POST /services/{name}/start|stop`; the address is the robot's registered
`ip_address` / `entrypoint_port`, as for `/api/v1/orchestration/{robot}/*`), and their state is
read from it (`GET /services/{name}/status`, cached 5 s). Nothing is sent over MQTT any more
(the retained `mapping/set` and `mapping/.../state` topics are gone). Nodes are gated on
the server only: graph-builder puts them into the map of the robot's open, unpaused **mapping**
session while that session is placed (an operate or paused session's are dropped).
`topo` is `topomap` on the real robot and `sim_topomap` in the sim
(`packages/config.py::MAPPING_SERVICE_CANDIDATES`, env `MAPPING_SERVICE_TOPO`).

- `POST .../sessions` (mapping) **commits first, then starts** the services (outside any
  transaction, under the robot's lock). `.../resume` starts them again, `.../pause` and
  `.../finish` stop them (a service another open, unpaused mapping session of the robot runs is
  kept), `DELETE /api/v1/robots/{r}` stops those of the sessions it closes and saves a closed
  session's SLAM map in the background (as a finish; the whole delete runs under the robot's
  lock). **Switching never blocks or undoes the user's action** (2026-10-07): there is no 502 /
  504 / 409 from a failed start, the session is neither closed nor paused again, the change
  always commits; a failure (robot offline, no registered orchestrator, orchestrator unreachable
  / error / timeout, no such service) is only reported in `robot_actions`. Resume of an offline
  robot is allowed. 409 while a relocalization job runs for the robot, for a start or a resume of
  a session that records a SLAM map (a validation, not a switch; topomap-only sessions are not
  refused). A repeated pause/finish retries the stop, a repeated resume the start
  (`changed: false`).
- **Start during a pending SLAM save.** While the robot's previous SLAM map is still being saved
  (a finish's background save, minutes), a start / resume / replace sends nothing to the robot:
  its `robot_actions` are `ok: true` entries labelled `"<Topomap|SLAM recording|...> starts when
  the robot has saved its previous SLAM map"`, and when the save ended (saved, failed or nothing
  to save) the server starts the session's services (SLAM first, then the topomap). Before, the
  start was dropped (`BUSY`) and the switch back after the save killed the new topomap.
- **Restart after a robot run change.** When mission-dispatch sees a new robot run (a driver /
  orchestrator restart; it unplaces the session) it NOTIFYs `robot_run_changed` (payload: the
  robot name); the API then starts the services of the robot's open, **unpaused mapping**
  session again (SLAM first), retrying a refused start (`RUN_CHANGE_RESTART_TRIES`, default 3,
  `RUN_CHANGE_RESTART_RETRY_S`, 10 s apart). The same restart follows the end of a SLAM save (a
  deferred start, or a topomap the switch back stopped) and a discarded save. Each restart is the
  event `MAP.SESSION_SERVICES_RESTARTED` (or `MAP.SESSION_SERVICES_RESTART_FAILED` when an action
  failed; only the last failed attempt is reported) with payload `{map_name, session_id, reason:
  "run_changed" | "slam_save_done" | "slam_save_failed" | "slam_discarded", ok, robot_actions,
  slam_warning}`, and a `session_update` / `mapping_state_update` on the robot WebSocket. A NOTIFY
  sent while the API was not listening is not replayed.
- `.../place` does not touch the services; operate sessions never start or stop one.
- **Localization facade** (every robot's orchestrator: `GET/PUT /localization`, `/localization/save`; the deprecated `/maps/mapping`, `/maps/{n}/mapping/start`, `/maps/{n}/save`, `/maps/{n}/relocalize` and `/robot/config/map` are no longer called): SLAM
  recording is a mode switch, not a process. Start = `PUT /localization {"mode":"slam"}` (the stored intent before it is kept in memory and in the table `robot_slam_saves`); finish saves with
  `POST /localization/save?background=true {name: "cloud-<map>", cloud_map_id, cloud_session_id}`, polls `GET /localization/save`, emits
  `MAP.SLAM_SAVE_DONE`/`_FAILED`, then PUTs the previous intent back (`odometry` when unknown; with `topomap: false` on a mapping-API robot, whose topomap the open mapping session then restarts). A failed save leaves the robot in slam (leaving it would discard the unsaved
  map; the warning says so) and the robot's `slam_save` becomes `failed` until the operator retries or discards it (below); a refused switch-back (e.g. 409 order active) makes the save's robot action `ok: false` ("SLAM map saved, but ...") and is in the event label (and `slam_save` is `failed` too: discard then only switches back). The robot does not name the map it records, so a deleted map's recording is not stopped by the cloud (deleting a map never switches a robot out of slam). A save running when the API restarts comes back as `failed` (its outcome is unknown). Relocalization jobs
  `PUT /localization {"mode":"relocalization","map":"<onboard name>"}?wait=false&partial=ok`, then wait for the VDA5050 state (`position_initialized` and `pose.map_id` == the onboard map name); cancel/failure PUT the previous intent back. The robot's refusal text (409 "order active: cancel it first", 502, 503, 504), a partial answer's `problem` and `applied: false` (no driver runs) are the job's `error`. An assisted job on the map the robot already relocalizes on PUTs odometry first: the orchestrator takes a repeated mode and map as a no-op and would not load the new `init_pos`. All cloud PUTs send `partial=ok` (an error then means nothing changed on the robot).
- **SLAM map** (`"slam"` in a mapping session's `services`; the user chooses it per session): a *mapping* session that has `slam` in its `services` also records a
  SLAM map on the robot, named `onboard_map_name(map)` = `cloud-<map>`. After the topomap start
  (outside any transaction, under the robot's lock) the API switches the robot to slam (above; an
  existing stored map is not re-recorded); `.../finish` answers at once and saves in a background
  task (`POST /localization/save?background=true`, then `GET /localization/save` every
  `ORCHESTRATOR_SAVE_POLL_S` for up to `ORCHESTRATOR_SAVE_POLL_TOTAL_S` (660 s); a save that did
  not finish in time is retried once, `ORCHESTRATOR_SAVE_RETRY_S` later); `replace` saves the
  replaced session's map first, awaited when the new session is a mapping session that records
  SLAM (one driver per robot), otherwise in the background (the new mapping session's services
  then start when the save ended, above). Failures never fail a session:
  `slam_warning` says what (existing map file: "SLAM map already exists, not re-recorded"). Pause,
  and operate sessions never touch SLAM; resume starts the recording again when `slam` is in the session's `services` (a failure only warns). `services` omitted = `["topo"]` plus `"slam"` on a `slam_map` map; an explicit list is taken exactly, `[]` included (the session opens and nothing is started on the robot). `"slam"` on a local map that is not `slam_map` sets `slam_map: true` (`MAP.SLAM_CHANGED`); on a geo map it is 400. A failed start reads `slam_warning: "SLAM recording not started: <orchestrator reason>"`. Finish / replace save the SLAM map of a session with `slam` in its services, or of an older session whose map is `slam_map` while the robot is in slam mode. The background outcome (minutes after the finish response) is the event `MAP.SLAM_SAVE_DONE` / `MAP.SLAM_SAVE_FAILED` (robot set; payload `{map_name, session_id, status: "saved"\|"failed", label, detail}`), also logged; `nothing to save` emits none.

| Where | Field |
|---|---|
| `POST .../sessions`, `.../sessions/{sid}/pause\|resume\|finish` | `robot_actions` (below), `robot_notified` (bool: false when any robot action failed), `mapping_warning` (only then: the failed actions' `label`s joined with `; `), `mapping_state`; start only: `mapping_service` (`"running"` \| `"not_running"`: the topo service), `mapping_services`. `mapping_switch` is gone. |
| `DELETE /api/v1/robots/{r}` | `robot_actions` (the services stopped for the sessions it closed, and the background SLAM `save` of a closed session that recorded one; `[]` if none) next to `success`, `message`, `deleted` |
| `POST .../sessions` and `.../finish` | `slam_warning` (only when a SLAM map step went wrong; see "SLAM map" above; kept next to the matching `robot_actions` entry) |
| `POST .../sessions/{sid}/place` | `robot_notified`, `mapping_state` |
| `GET /api/v1/maps/{id}` | `sessions.mapping_state`, `sessions.mapping_service`, `sessions.mapping_services` (of the open session's robot; null without an open session) |
| `GET /api/v1/robots`, `GET /api/v1/robots/{r}` | `mapping_state`, `mapping_services`, `slam_save` per robot |
| `WS /ws/robot/{r}` `robot_update` | `slam_save` (as on the REST view) |
| `WS /ws/robot/{r}` | `{type: "mapping_state_update", robot_name, timestamp, mapping_state, service, service_state}` after a service was started / stopped **through the API** (not on every change on the robot: poll `GET /robots/{r}`) |

**`robot_actions`** (always present, possibly `[]`, on `POST .../sessions` (also `replace`), `.../pause`, `.../resume`, `.../finish` and `DELETE /api/v1/robots/{r}`; absent from other responses and when the server runs without a mapping switch). Every robot-side call the API made for the change, in the order it made them, each

```json
{"service": "topomap", "action": "start", "ok": true,
 "label": "Topomap service started", "detail": null}
```

| Field | Meaning |
|---|---|
| `service` | The orchestrator service name that was addressed (`topomap` on the real robot, `sim_topomap` in the sim; the name the orchestrator listed). For SLAM: the driver service when the orchestrator names it in its answer (e.g. `odin_driver_gpu`), else the fixed `"SLAM recording"`. If the orchestrator has no such service, the first candidate name. |
| `action` | `"start"` \| `"stop"` \| `"save"` (SLAM map save). A discarded SLAM save is a `"stop"` of `"SLAM recording"`. |
| `ok` | `true`: done (also "already running" on start and "was not running" on stop); `false`: failed. A failure never fails the response. |
| `label` | Short text for a notification (English). Success: `"Topomap service started"`, `"Topomap service stopped"`, `"Topomap service already running"`, `"Topomap service was not running"`, `"SLAM recording started"`, `"SLAM recording already running"`, `"SLAM map save started"` (finish: the save runs in the background), `"SLAM map saved"` / `"No SLAM map to save"` (replace: the old session's save is awaited), `"<Topomap|SLAM recording|Grid map> starts when the robot has saved its previous SLAM map"` (a start deferred during a pending save), `"Unsaved SLAM map discarded, robot back in <mode>"` (discard). Failure: `"SLAM recording not started: the SLAM map of '<map>' was not saved and robot '<r>' still records it: retry or discard that save first"`, `"SLAM recording not discarded: <detail>"`, `"Could not start topomap: <detail>"`, `"Could not stop topomap: <detail>"`, `"Could not start SLAM recording: <detail>"`, `"SLAM map not saved: <detail>"`. |
| `detail` | The orchestrator's (or transport's) error text; `null` when `ok`. |

What each endpoint lists: **start** (mapping): for `replace` first the `stop` of the replaced session's services, then its SLAM `save` (awaited when the new session records SLAM, else `"SLAM map save started"`), then, when `slam` is in the services, SLAM `start`, then `start` of each orchestrator service of the new session (all of them deferred entries while a save is pending); operate session: `[]` (or the `stop` of a replaced mapping session's services). **pause / finish**: `stop` per service (`[]` when another open mapping session of the robot still runs it, or for an operate session); finish of a session that recorded SLAM adds `save` (`ok: true` = the background save was started, `false` = the robot is gone/offline). **resume**: `start` per service, plus the SLAM `start` when `slam` is in the services. Pause never lists SLAM. The end of the background save is not in the response: it arrives as the event `MAP.SLAM_SAVE_DONE` / `MAP.SLAM_SAVE_FAILED`.

Example, failed start (the session is open and `mapping`; HTTP 201):

```json
{"session": {"state": "mapping", "...": "..."}, "robot_notified": false,
 "mapping_warning": "Could not start topomap: orchestrator at 10.0.0.5:8080 is not reachable (ConnectError)",
 "robot_actions": [{"service": "topomap", "action": "start", "ok": false,
   "label": "Could not start topomap: orchestrator at 10.0.0.5:8080 is not reachable (ConnectError)",
   "detail": "orchestrator at 10.0.0.5:8080 is not reachable (ConnectError)"}]}
```
| ~~`POST /api/v1/robots/{r}/mapping/off`~~ | removed (404); there is no retained state to force |

#### SLAM save state: retry / discard (2026-10-09)

A failed SLAM save leaves the robot in SLAM mode with the unsaved map (switching back would
discard it). The robot views show it and two calls end it.

**`slam_save`** on `GET /api/v1/robots`, `GET /api/v1/robots/{r}` and the `robot_update` of
`WS /ws/robot/{r}`:

```json
null
{"map": "yard", "state": "saving", "detail": null, "at": "2026-10-09T10:00:00.123456+00:00"}
{"map": "yard", "state": "failed",
 "detail": "SLAM map of 'yard' not saved on robot 'r1': driver refused (the robot was left in SLAM mode so the map is kept: retry or discard the save)",
 "at": "2026-10-09T10:02:31.000000+00:00"}
```

`map`: the cloud map whose SLAM map is (being) saved; `state`: `saving` while a background save
runs (finish, replace, retry), `failed` after a save failed, after a saved map whose switch back
failed (`detail` "the SLAM map was saved, but ..."), after a finish whose robot was not reachable,
and after an API restart during a save (`detail` says the outcome is unknown); `detail`: why
(null while saving); `at`: when the state was entered (ISO 8601, UTC). `null` otherwise. The state
and the robot's localization intent from before the recording are stored in `robot_slam_saves`
(migration `20261010_01_robot_slam_saves`) and survive an API restart. While `failed`, a new SLAM
recording on the robot is refused (the SLAM `start` action fails with "retry or discard that save
first"; the session still opens and its topomap starts) and relocalization is refused (409 /
`can_start_reason` "the robot 'r1' is still in SLAM mode after a failed save: retry or discard
it").

| Method | Path | Does |
|---|---|---|
| POST | `/api/v1/robots/{robot}/slam-save/retry` | Saves the SLAM map of `slam_save.map` again, in the background like a finish (`slam_save.state` `saving`, then `MAP.SLAM_SAVE_DONE` / `_FAILED`; on success the previous intent is put back, the map gets `status.slam_saved_at` (a `draft` map becomes `ready`) and the open mapping session's services are restarted). 200 `{"robot_actions": [{"service": "SLAM recording", "action": "save", "ok": true, "label": "SLAM map save started", "detail": null}]}`; an offline robot: the same entry with `ok: false`, `label` "SLAM map not saved: robot 'r1' is offline" (state stays `failed`). 404 unknown robot. 409 `{"detail": "Robot 'r1' has no failed SLAM save to retry"}` or `{"detail": "Robot 'r1' is already saving a SLAM map"}`. |
| POST | `/api/v1/robots/{robot}/slam-save/discard` | Leaves SLAM mode **without saving**: `PUT /localization` with the intent from before the recording (`odometry` when unknown) and `topomap: false` on a mapping-API robot; `slam_save` becomes `null`; then the services of the robot's open, unpaused mapping session are started again (`MAP.SESSION_SERVICES_RESTARTED`, reason `slam_discarded`). 200 `{"robot_actions": [{"service": "SLAM recording", "action": "stop", "ok": true, "label": "Unsaved SLAM map discarded, robot back in odometry", "detail": null}, ...the restart's actions]}`; a refused switch: the first entry `ok: false`, `label` "SLAM recording not discarded: <detail>" (state stays `failed`); an offline robot likewise. 404 unknown robot. 409 `{"detail": "Robot 'r1' has no failed SLAM save to discard"}` or `{"detail": "Robot 'r1' is saving a SLAM map; wait for it"}`. |

`mapping_state`: null (robot offline, no registered orchestrator, or its orchestrator has no
topo service) or `{status: "on"|"off"|"unreachable", online, enabled, service, session_id, map,
nodes_sent, since, stamp, received_at, source: "orchestrator", orchestrator_service}`.
`online` = the service runs; `on` = it runs and the robot's open mapping session is unpaused and
placed; `off` = it does not, or the session is paused / not placed; `unreachable` = the
orchestrator did not answer (`error` says why). `since` = the service's start time,
`nodes_sent` = the session's `node_count` (also on the robot views, from the open-session query they already make; null without an open mapping session). `mapping_services`:
`{topo, grid, slam: running | not_running | not_available}`, what the robot's orchestrator offers
(the client lists these as the mapping choices); `not_available` = robot offline / no
orchestrator / it has no such service / it did not answer. On a robot with the orchestrator's
mapping API (`GET /localization` reports `topomap`) `topo` is that flag and the topomap is
switched with `PUT /localization {topomap}` on the robot's current mode, whichever it is (a
refusal by the robot is a failed `topomap` robot action; the session still opens). `slam` = the localization facade's mode.

#### Operate sessions and placement (maps §14, U1)

A robot uses a map through its one open session. `purpose`: `mapping` (adds data, as above) or
`operate` (uses the map, adds nothing; the map state does not change; several robots may use a
map, also while another robot maps it). `aligned` = **placed**: `map_T_session` is valid for the
robot's current run. Geo sessions are placed by the robot's datum; a local map's first mapping
session on an empty map is identity; otherwise a local-map session is placed by the user. A
session that is not placed captures nothing, gets no route orders on that map and no plans.

| Method | Path | Does |
|---|---|---|
| POST | `/api/v1/maps/{id}/sessions` | `{robot, purpose?: "mapping"\|"operate" (default mapping), services?: ["topo"\|"grid"\|"slam"] (mapping only; default ["topo"] + "slam" on a slam_map map; [] = start nothing), placement?: {pose: {x, y, yaw}, robot_pose: {x, y, theta}}, replace?: bool}` → 201 `{map_id, map_state, changed, session, replaced_session, robot_notified, mapping_service, mapping_services: {topo: ..., grid: ..., slam: ...}, mapping_state}` (+ `warnings: [str]` for a placement while the robot drives or after it moved > 0.02 m / 0.5°: placed anyway). 409: robot offline; robot has an open session without `replace`; map archived / being deleted / `draft` for operate (also when the `replace` just made it `draft`); another open mapping session (mapping); geo map and no robot datum; a relocalization job runs for the robot and the session records SLAM. 400: `slam` on a geo map. 422: `placement` on a geo map, `services` on operate, unknown service. `replace: true` finishes the robot's open session in the same transaction (a refused start keeps it); on the same local map a placed session's transform carries over (`placement.source: "session"`). Without `placement` on a local map, the robot's last session on that map carries its transform the same way (`from_session_id`) when it ended placed and the robot's run has not changed since (§14.13). The "driving" warning counts a stored `ON_TASK` only while the robot has a PENDING/RUNNING mission or no fresh state message. |
| POST | `/api/v1/maps/{id}/sessions/{sid}/place` | `{pose: {x, y, yaw}, robot_pose: {x, y, theta}, source?: "last_position"}` → `{map_id, map_state, changed, session, robot_notified, mapping_state}`; `map_T_session = pose ⊕ robot_pose⁻¹`, `placement = {pose, robot_pose, source: "user" (or the given source), actor, at}`, `MAP.SESSION_PLACED` (graph-builder keeps the session's nodes from then on). 404 unknown map/session; 409 finished, geo map, robot offline. Placed anyway, with `warnings: [str]`: the robot drives or moved since its pose was shown, or a placed mapping session is re-placed (its nodes split). An operate session can be re-placed any time. |
| GET | `/api/v1/maps/{id}/sessions/{sid}/placement-suggestions` | `{map_id, session_id, suggestions: [{source: "last_position", basis: "unplace_snapshot"\|"state_history"\|"finished_session", map_T_session: {tx, ty, yaw}, pose: {x, y, yaw}, robot_pose: {x, y, theta}, at, from_session_id}]}`: where the robot last was on this map, for an unplaced session (run changed); at most one entry. `pose` is the last map pose; `robot_pose` is the robot's first pose in its current run, that `map_T_session` pairs it with. Empty for a placed session, a geo map or when nothing is known. 404 unknown map/session; 409 finished. Accept with `POST .../place` (`pose` + live `robot_pose`, optional `source: "last_position"`). |
| POST | `.../sessions/{sid}/finish` | both purposes (operate: "Stop using"; the map state stays). `pause` / `resume`: 409 on operate; `resume` of a session that records SLAM: 409 while a relocalization job runs for the robot; a resume during a pending SLAM save starts the services after the save (as a start). |
| GET | `/api/v1/maps/{id}/sessions?limit=&before=` | the whole history, newest first: `{map_id, count, items, next_before}`; `limit` 1-200 (default 50); `before` = the previous page's `next_before`. |
| GET | `/api/v1/maps/{id}` | `sessions.open` is the open **mapping** session; new `sessions.operating: [{robot, session_id, aligned}]`; §14.13 `sessions.placement_reusable: {robot: from_session_id}` (local maps; `{}` otherwise): robots not on this map whose start here without `placement` would be placed from their last session (a hint; the start decides again). |
| GET | `/api/v1/maps/{id}/graph`, `POST /map/load` | nodes gain `session_id` (null for untagged legacy nodes). |
| GET | `/api/v1/maps/{id}/graph`, `POST /map/load` | a node's `timestamp` is its `created_at` (graph-builder writes the server's local time, naive) returned **with the server's UTC offset** (e.g. `2026-10-09T13:09:50+02:00`), so a client in another time zone shows the real capture time. A stamp that already has an offset, or is not a string, is returned unchanged. |
| GET | `/api/v1/robots[/{r}]` | new `session`: `{session_id, map, purpose, state: "mapping"\|"paused"\|"operating", aligned, map_T_session (null while not placed), unplaced_reason, placement_source, node_count, services (the session's mapping services, [] if none)}` or null (mapless). Derived from `map_sessions`; it is the robot's map (`current_map` was removed in U6). |
| WS | `/ws/robot/{r}` | `robot_update` carries `session` (cached ≤ 1 s); `{type: "session_update", robot_name, timestamp, session}` right after a session change through the API. |
| DELETE / POST | `/api/v1/maps/{id}` / `.../archive` | 409 while **any** session is open; the message names the robots (`r1 (mapping), r2 (using)`). |

Session objects gain `purpose`, `services` (mapping) and `placement`; `state` is `mapping`,
`paused`, `operating` or `finished`. A mapping session runs its `services` on the robot's
orchestrator (above); an operate session runs none. `MAP.SESSION_STARTED/FINISHED` payloads
carry `purpose`.

#### Map location and relocalization (docs/satinav-maps-redesign.md §12, §16)

| Method | Path | Does |
|---|---|---|
| PUT | `/api/v1/maps/{id}/approx_location` | `{latitude, longitude, accuracy_m?, source?: "manual"\|"robot"}` sets a local map's approximate location (a hint for pins and sorting; never read by placement); the server stamps `set_at`. 404 unknown map, 409 geo map, 422 for (0, 0) or out of range. |
| GET | `/api/v1/maps/{id}/reloc?robot=` | `{available, known, source: "orchestrator", can_start, can_start_reason}` (`can_start` / `can_start_reason`: see the row below): does the robot's orchestrator hold a stored map for this map (cached `RELOC_MAP_HELD_TTL_S`, 15 s)? Geo map: `{false, true}`. Unknown or offline robot, or an orchestrator that cannot be asked: `known: false`. 404 unknown map. |
|  | *note* | The earlier `localization_api` flag was removed from this answer (every orchestrator has the localization facade). |
| - | **`reloc.can_start` / `warning`** | `can_start` is false when the robot is unknown, on a geo map, or while the robot's open MAPPING session records a SLAM map (its recording is the localization mode; reason in `warning`); a topomap-only mapping session does not block it (decision D, 2026-10-09: on a mapping-API robot the job's `PUT /localization` turns the topomap off and the job starts it again right after; a failed restart is a job `warnings` entry). `can_start` is also false when the robot answers that it holds no stored map for the cloud map. `warning` (same text as `can_start_reason`, kept for compatibility) is **non-blocking**: robot offline, no orchestrator address, its stored maps not readable, another job running, a SLAM save pending or failed (`POST .../place` answers 409 for the last three). The job relocalizes through `PUT /localization`; an untagged stored map is tried as `cloud-{map}` (`warnings` in the job), driving is irrelevant. Still failing the job: robot unknown/offline, orchestrator unreachable, the robot in slam (`mode: slam`, never left: "the robot is recording a SLAM map (409); finish its mapping session first", or after a failed save "the robot is still in SLAM mode after a failed save: retry or discard it"), a SLAM save, a mapping session that records SLAM opened meanwhile, a second job (409 at POST). **Manual placement** (`POST .../place` with poses, and `placement` on session start) never refuses a driving or moved robot or a re-place of a placed mapping session; the answer carries `warnings: [str]`; `POST .../unplace` of a mapping session likewise. |
| GET | `/api/v1/robots/{robot}/stored-maps` | `{known, maps: [{cloud_map_id, name, valid, saved_at, size_bytes}]}`: the cloud maps the robot's orchestrator holds as valid stored SLAM maps ("Find itself" works on them), from one `GET /maps/list` (cached `RELOC_MAP_HELD_TTL_S`; a map counts as in `held()`: tagged with `cloud_map_id`, else named `cloud-<id>`). `known: false` (`maps: []`): robot offline, no orchestrator address or not askable. 404 unknown robot. |
| GET | `.../sessions/{sid}/placement-suggestions` | gains `reloc`: the same object for an unplaced local-map session, else `null`. |
| POST | `.../sessions/{sid}/place` | `source` is `last_position` or `reloc`. `{"source": "reloc"}` (no poses): the robot relocalizes itself on its stored map; identity `map_T_session` (assumption D0, `map_sessions.reloc_map_t_session()`), the robot's current pose is recorded, the still check is skipped. 409 when already placed, the robot is offline or reports `position_initialized: false`, or the orchestrator does not hold the map (fresh check; unknown counts as not held). Not accepted on session start (422). **When `reloc.can_start` is true** it answers **202** `{map_id, map_state, changed: false, session: <still unplaced>, reloc_job: {id, state, step, mode: "odin"\|"assisted", started_at, deadline}}` (`deadline` is an estimate until the job starts waiting for the robot, then it is `RELOC_JOB_TIMEOUT_S` from that moment) and runs a job (`packages/api/reloc_job.py`): PATCH `init_pos` (null, or the body's `reloc: {init_pose: {x, y, yaw}}` in the cloud map frame), `PUT /localization` relocalization on the stored map, wait until the robot reports itself localized on it (`RELOC_JOB_TIMEOUT_S`, 90 s), propose, place in one transaction. 409 (the same reasons `can_start_reason` gives) when the robot's open mapping session records SLAM, another job / a pending or failed SLAM save exists, or `init_pose` is sent while `can_start` is false (a driving robot and a topomap-only mapping session are not refused); `POST .../sessions` and `.../resume` of a mapping session that records SLAM are 409 while a job runs; `position_initialized: false` does not refuse a job. With `can_start` false the call is the check-only one above. |
| GET | `.../sessions/{sid}/reloc-job` | `{id, state: preparing\|starting\|waiting\|confirming\|placed\|failed\|cancelled\|edit, step, mode: odin\|assisted, started_at, deadline, error?, warnings?: [str], position_initialized?, localization_score?, proposal?: {map_T_session: {tx, ty, yaw} (identity), pose: {x, y, yaw} (robot pose in the map frame, as stored by a reloc placement), robot_pose: {x, y, theta} (as reported), localization_score, confirm_deadline: ISO}, confirm_deadline? (= proposal.confirm_deadline), auto_confirmed?: true}`. When the robot reports itself localized the job enters `confirming` (non-terminal, counts as active: no second job, no mapping start) with a `proposal` and waits for `POST .../reloc-job/confirm` or `.../edit`; with no answer by `confirm_deadline` (`RELOC_CONFIRM_TIMEOUT_S`, default 30 s after entering `confirming`; a server task, independent of the client) the server confirms: `placed` with `auto_confirmed: true`. While `confirming` the old 90 s localization deadline does not apply, but robot offline / session finished fail the job. `placed`, `failed`, `cancelled`, `edit` are terminal. 404 none. The registry is **in memory** (lost on API restart) and so are the per-robot locks and the running SLAM save tasks (the SLAM save *state* is persisted, see "SLAM save state"): run the API with a **single worker** (multiple workers would not see each other's jobs, locks or saves). |
| POST | `.../sessions/{sid}/reloc-job/confirm` | **Reloc confirmation (2026-10-08).** Job must be `confirming`: places exactly as the old automatic placement did (identity `map_T_session`, `source: "reloc"`, `MAP.SESSION_PLACED`); the job becomes `placed`. Answers the job JSON. 409 when the job is not `confirming` (or was decided already), 404 no job. |
| POST | `.../sessions/{sid}/reloc-job/edit` | Job must be `confirming`: the job ends in the terminal state `edit` - nothing is placed and **nothing is rolled back** (the relocalization driver keeps running, `init_pos` stays). Answers the job JSON incl. `proposal`, so the client opens manual placement prefilled (`proposal.pose`, `proposal.robot_pose`). 409 / 404 as above. |
| DELETE | `.../sessions/{sid}/reloc-job` | cancel (also while `confirming`; rolls back like a failure): restores the previous `init_pos` and stored localization intent; every failure but a timeout restores them too; 409 when finished. |
| POST | `.../sessions/{sid}/unplace` | **Dev/test hook** (also "redo my placement"): marks a placed session on a LOCAL map as unplaced (`aligned: false`, `placement.unplaced_reason: "manual"`, `placement.actor`; the old `map_T_session` and placement are kept); a MAPPING session too, with a `warnings` entry (graph-builder drops its new nodes until it is placed again). Emits `MAP.SESSION_UNPLACED` (`reason: manual`). The system itself unplaces when the robot's run changes; this lets the place/relocalization flows be re-run without restarting robot services. 409: finished session, geo map, a relocalization job running for the robot. Already unplaced: `changed: false`. |

#### Map type conversion (docs/satinav-maps-redesign.md §17)

| Method | Path | Does |
|---|---|---|
| POST | `/api/v1/maps/{id}/type` | `{"type": "local"}` or `{"type": "geo", latitude, longitude, bearing_deg?: 0, frame?: "utm"\|"enu", anchor?: {x, y}, utm_zone?, utm_north?}` → `{map_id, changed, old_type, type, map, operating: [{robot, session_id, aligned}], warnings: [..]}`. Nothing moves: map-frame coordinates stay. To geo: the map point `anchor` (default the origin) is at (latitude, longitude), +X at `bearing_deg` from east, CCW (grid east for `utm`, true east at the anchor for `enu`); stored as `geo.bearing_deg` plus the matching `datum_*` (a `utm` datum at the origin with `datum_bearing_deg`). To local: `geo` and `datum_*` cleared, the old georeference kept as `former_datum {latitude, longitude, bearing_deg, utm_zone, utm_north, origin_e, origin_n, converted_at}` (the frame's origin), the old origin set as `approx_location`. 404 unknown map; 409 deleting, already that type, an open (or paused) mapping session, a running relocalization job of the map ("A relocalization is running on map ..."); 422 body (range, (0, 0), outside UTM 80 S..84 N, a zone more than 6 deg from the anchor). Open operate sessions keep their placement; to geo, a placed one gets the robot's current datum stamped as its `datum`. `MAP.TYPE_CHANGED`. |
| GET | `.../sessions/{sid}/placement-suggestions` | on a **geo** map, an unplaced session gets `{source: "datum", basis: "robot_datum", map_T_session, pose, robot_pose, datum, at (robot datum_changed_at), datum_after_unplace: true\|false\|null}` when the robot's datum is in the map's zone. |
| POST | `.../sessions/{sid}/place` | `{"source": "datum"}` (no poses, geo maps only): place an unplaced geo session from the robot's current datum (stores it as the session's `datum`). 409: a local map, a placed session, no usable datum, robot offline. Any other placement on a geo map stays 409. |

The robot view (`GET /robots[/{r}]`, WS `robot_update`) gains `status.position_initialized`,
`status.localization_score`, `status.approx_position` (from the robot's retained MQTT
`{prefix}/{robot}/approx_position`; display only, never a datum, never used for placement; writes are
suppressed under 5 m movement with unchanged fix quality, source and accuracy within 20 %, and
refreshed after about 300 s), `session.placement_source` and a top-level `localization_warning`
(null, or a reason for a placed `reloc` session whose robot reports `position_initialized: false` or
a `localization_score` below `RELOC_DEGRADED_SCORE`, default 0.3, or, with the robot's intent known,
whose robot is not in relocalization mode, is told another `cloud-` map, or is localized on another
map than it was told). The warning is informational and
the session stays placed; the score is provisional (a GNSS-sigma stopgap on the robot).

The robot view (REST and WS) also has `status.pose.map_id` (VDA5050 `agvPosition.mapId`: the
onboard map name once localized, `"map"` in odometry/slam) and a `localization` block
(`localization_view.py`), the one definition of "localized":
`{intent: {mode, map, cloud_map, set_at, topomap} | null, intent_read_at, intent_error,
switch: {status, mode, map, started_at, finished_at, error} | null, switching,
device: {state: LOCALIZED|RELOCALIZING|MAP_REJECTED|NOT_ON_MAP|UNKNOWN, map, cloud_map, detail},
usable, on_intended_map, reason, stale}`. `intent`/`switch` are the robot orchestrator's
`GET /localization` as the mapping-switch snapshot last read it (REST refreshes it every few
seconds; WS sends the last read and never calls the robot); `device` and `usable` come from the
VDA5050 state (`relocalizationMapRejectedError`, `relocalizationNotReadyError`, `mapId`,
`positionInitialized`). The
orchestrator proxy adds `cloud_map_id` and `cloud_session_id` to a proxied
`POST /orchestration/{robot}/localization/save` naming `cloud-<map>` while the robot has an open mapping session on that map (ids the
caller sent are kept). The proxy refuses with **409** `{"detail": "..."}` (the detail names the
server route to use) `PUT /localization`, `POST /localization/save` and `POST
/services/{name}/start|stop` of a mapping service (`MAPPING_SERVICE_CANDIDATES`: topomap, grid)
while the robot records SLAM for its mapping session, has a SLAM save pending, or a failed save
(`slam_save.state` `failed`): pause/finish the session, or `POST /api/v1/robots/{r}/slam-save/retry|discard`.
GETs and other services/calls (e.g. `localization/init_pos`) are still forwarded. Other
mutations of `localization*` and `services/*` hold the robot's lock (the one session operations
use) for the one forwarded call, and every mutation of `maps/`, `services/` or `localization*`
ends in `ApiDelegationService.robot_changed()` (forgets the mapping snapshot and held-map
answers; the reloc jobs call the same hook). Env: `RELOC_MAP_HELD_TTL_S`, `RELOC_DEGRADED_SCORE`, `RELOC_JOB_TIMEOUT_S`, `RELOC_JOB_POLL_S`, `RELOC_CONFIRM_TIMEOUT_S`.

#### 3D reconstruction (R3)

`docs/reconstruction/design.md` §6, §8, §9 (logic: `packages/api/reconstruction.py`). The work
runs in an **external service** (own repo; contract `docs/reconstruction/handover.md`); the API
is its gateway: it owns the job (`map_reconstructions`), sends a manifest of presigned MinIO
URLs, receives callbacks, verifies the service's `cloud.ply` + `meta.json`, derives the top view
(`ortho.png`, `height.png`) from the PLY itself in a child process (`reconstruction_topview.py`,
design.md §7.2; the job is `running`, stage `finalizing`, meanwhile) and stores the four files
under `map-{id}/reconstruction/{job_id}/`.

| Method | Path | |
|---|---|---|
| POST | `/api/v1/maps/{map}/reconstruction` | start / rebuild; body optional `{voxel_m, max_depth_m, clip_z}`; 202 + the job |
| GET | `/api/v1/maps/{map}/reconstruction` | `{map_name, configured, reconstruction, job}`: the current result (`stale`, `stale_reason`, file URLs) and the active job (or the newest failed/cancelled one); poll every 2 s while a job is active |
| POST | `/api/v1/maps/{map}/reconstruction/cancel` | cancel the active job |
| DELETE | `/api/v1/maps/{map}/reconstruction` | delete the result, cancel an active job; 204 |
| GET | `/api/v1/maps/{map}/reconstruction/files/{cloud.ply,ortho.png,height.png,relief_rgb.png,relief_height.png,meta.json}` | streamed; `ETag` = job id; `?v={job_id}` -> cached forever |

Errors: `detail = {code, message}`: 404 `map_not_found` / `no_active_job` / `no_reconstruction`
/ `file_not_found`; 409 `map_deleting` / `job_active` (with `job`) / `no_depth`; 422 bad
parameters / `too_many_nodes`; 503 `not_configured`. The service being down is not an error:
the job stays `queued` (`job.waiting_for_service: true`) for up to 30 min.

Callbacks from the service (not under `/api/`, never proxied by the client's nginx):
`POST /internal/reconstruction/jobs/{job_id}/{progress|finish|fail}` with
`Authorization: Bearer base64url(HMAC-SHA256(RECONSTRUCTION_CALLBACK_SECRET, job_id))`; answers
`200 {"action": "continue"|"cancel"}` or `410 {"action": "stop"}`.

Config (`packages/config.py`, passed by compose from `docker_compose/.env`):
`RECONSTRUCTION_SERVICE_URL`, `_SERVICE_KEY`, `_CALLBACK_SECRET` (all three set = feature on),
`RECONSTRUCTION_CALLBACK_BASE_URL` (default `http://localhost:8000`),
`RECONSTRUCTION_MINIO_ENDPOINT` (default `localhost:9000`: the host the SERVICE reaches MinIO
at; presigned URLs are signed for it), `RECONSTRUCTION_STAGING_BUCKET` (`recon-staging`, 1-day
expiry), `_URL_EXPIRY_S` (4 h), `_JOB_TIMEOUT_S` (1 h), `_QUEUE_TIMEOUT_S` (30 min),
`_MAX_INFLIGHT` (1), `_VOXEL_M` / `_MAX_DEPTH_M` / `_CLIP_Z`, `RECONSTRUCTION_TOPVIEW_MEM_MB`
(1024), `_TOPVIEW_TIMEOUT_S` (600), `RECONSTRUCTION_WORK_DIR` (system temp). Events: `MAP.RECONSTRUCTION_STARTED`
/ `_FINISHED` / `_FAILED` (source `reconstruction`). A map delete cancels the active job and
removes the rows; the bucket delete removes the files. Migration
`20261003_01_map_reconstructions`. Integration test with a stub service:
`tests/integration/reconstruction/run.sh`.

#### `WS /ws/map/{map_id}`
WebSocket endpoint for real-time map updates.

Connect to this endpoint after loading a map to receive updates from the graph builder service.

**Example (JavaScript):**
```javascript
const ws = new WebSocket('ws://localhost:8000/ws/map/warehouse_floor_1');

ws.onmessage = (event) => {
  const update = JSON.parse(event.data);
  console.log('Map update:', update);
};
```

### Image Operations

#### `GET /api/v1/images/{map_id}/{node_id}?image_id={image_id}&size={thumb|preview}`
Retrieve a node's image from the image database (its first image without `image_id`).
`size=thumb` (160 px) or `size=preview` (640 px, longest side) returns a downscaled JPEG, made
once and cached in MinIO at `{node}/thumbs/{size}/{image_id}.jpg` (outside `images/`, so it is
never listed or counted as a photo; deleted with the node's images, and dropped when the same `image_id` is stored again or deleted); omitted: the original (content type sniffed from its bytes).
Resizing runs off the event loop, at most 4 at a time. Caching: with `size` **and** `image_id` the response has `Cache-Control: private, max-age=86400` (a given id of a node changes only when the robot re-sends it); without `image_id` (first image) or without `size` it is `private, no-cache`.

**Parameters:**
- `image_id` (path): Image ID
- `node_id` (query, required): Node ID
- `map_id` (query, optional): Map ID (uses default if not provided)

**Response:**
Binary image data (JPEG)

### Navigation Operations

#### `POST /api/v1/navigate`
Request navigation for a robot (proxy to mission planner).

**Request:**
```json
{
  "robot_name": "carter01",
  "target_x": 10.5,
  "target_y": 20.3,
  "mission_name": "delivery_mission_001",
  "timeout_seconds": 300
}
```

`timeout_seconds` is optional. Omitted or `null` means no time limit (the default, and what "Go here" sends): the mission then ends only when the robot reports success or failure, or it is canceled.

**Response:**
```json
{
  "success": true,
  "mission_name": "delivery_mission_001",
  "state": "PENDING",
  "queued_behind": "patrol_loop"
}
```

`state` is the created mission's state when the request was answered (`PENDING` while it
waits). `queued_behind` is the name of the mission directly ahead of it in the robot's queue
(the go-to starts when that one has finished), or `null`; both are `null` on a failure or when
the queue could not be read. A request is never refused because the robot is busy. The
auto-generated name is `nav_<robot>_<YYYYmmdd_HHMMSS>_<4 hex>`, so two requests in one second
get two missions. The go-to mission has `kind: "goto"` and `goal: {x, y, map_id, node_id}`: the
dispatcher replans its route from the robot's pose when it starts, so a go-to that waited
behind another mission does not drive back to where the robot was when it was submitted (the
stored plan is used if the planner cannot be reached). Send an `Idempotency-Key` header and a
repeated submit returns the first answer (`Idempotent-Replayed: true`) without a second
mission; without the header every request makes its own.

#### Missions: reroute, queue views

`PUT /api/v1/missions/{name}` with `update_nodes` (a reroute) is folded into `mission_tree`
(validated; `planned_path` cleared; spec `route_rev` + 1) and the response is the updated mission;
the request is not stored. The dispatcher acts once per `route_rev` (`status.applied_route_rev`).
`task_status[node]` is the index of the last waypoint of a route node the robot reached,
for every waypoint (planner go-tos included). The robot view (`GET /robots[/{r}]`, WS
`robot_update`) carries `current_mission` (the RUNNING mission's name or `null`) and
`queued_missions` (PENDING names in dispatch order: started first, then by `created_at`).

#### Run legs

A leg is one robot move from one topomap node to the next. mission-dispatch records one row per
leg in `run_legs` from the robot's VDA5050 state (each change of `lastNodeId`), at every
recording level except `off` (legs are kept like events, indefinitely). The run's
`summary_metrics` is filled from them when the run ends.

**`GET /api/v1/runs/{run_id}/legs`** -- the run's legs in order: `{"run_id", "items": [leg...]}`
(empty when none were recorded). 404 for an unknown run. A leg:

| Field | Meaning |
|---|---|
| `seq` | 1-based leg order in the run (events carry it as `payload.leg_seq`) |
| `pass_index` | 0-based repeat pass; `order_rev` the order revision it ended in (a reroute resends the order, the leg is one leg across it) |
| `from_vda_node`, `to_vda_node` | VDA node ids (`<mission>-r<run>[v<rev>]-n<tree node>-s<seq>`); `from_vda_node` is the implied start node when the robot never reported it |
| `from_topomap_node`, `to_topomap_node` | Topomap node ids, or `null`: only a mission whose `planned_path` lines up one-to-one with its route waypoints (planner go-tos) has them; a rerouted or hand-made route has none. The first leg of a pass has no `from_topomap_node` |
| `map_id` | Map of the route |
| `started_at`, `ended_at` | Robot clock (VDA5050 header time). `received_started_at`, `received_ended_at`: dispatcher clock |
| `duration_s`, `stopped_s` | Leg time; part of it the robot reported `driving: false` |
| `straight_m`, `planned_m` | Map-frame distance between the two waypoints (`null` for a leg that starts at the robot's own position) |
| `expected_s` | `d / speed_max + speed_max / acceleration_max + abs(delta heading) / angular_speed_max` from the robot's factsheet; a term whose limit is unknown (-1) is dropped, `null` when nothing is known or the start has no pose |
| `recoveries`, `recovery_s`, `blocks` | Counts from `NAV.RECOVERY_ENTERED/EXITED`, `NAV.GOAL_BLOCKED`, `MISSION.EDGE_BLOCKED` events tagged with the leg; filled when the run ends (events arriving later are not counted) |

**`GET /api/v1/missions/{name}/legs`** -- legs of all non-archived runs of the mission and its
`-rerun-<n>` reruns grouped by leg identity (`from` -> `to`: the topomap node ids, else the
run-independent VDA tail `n<tree node>-s<seq>`; `topomap` says which). `{"mission", "runs",
"legs", "truncated", "items": [{from, to, topomap, count, runs, median_s, p90_s, expected_s,
ratio, recoveries, recovery_s, blocks}]}`, slowest median first. `ratio` = `median_s /
expected_s`; `p90_s` is linearly interpolated. 404 when the mission has no run.

`GET /api/v1/runs/{run_id}` also returns `run.planned_path` (the mission's planned path when the
run started) and, once the run has ended, `run.summary_metrics`: `leg_count`, `duration_s`,
`distance_m`, `time_moving_s`, `time_stopped_s` (rest of the run: non-driving time and time
outside any leg), `time_recovery_s` (overlaps stopped time), `recovery_count`, `block_count`,
`expected_s`, `actual_vs_expected` (time of the legs that have an expected time over their
expected sum), `pass_durations_s` (`[{pass, legs, duration_s}]`).

#### Run track (recording level `track`)

The recording ladder is `off` < `events_only` (default) < `track` < `full` (`telemetry_recording`
on a robot, site or the global settings; `track` is accepted wherever the other levels are). Each
level records what the one below does, plus: `events_only` events, runs, legs and run metrics;
`track` a 1 Hz pose and speed row per robot while a run is open (`robot_track_ts`, from the VDA5050
`agvPosition` and `velocity`; one per second of robot time, tagged with the run and the leg in
progress); `full` the robot state and diagnostics time series as before. Track rows are throttled on
the recorder's receive clock (a robot clock that jumps does not flood or silence the track) and are
not recorded while the robot reports `positionInitialized: false`. Retention: track rows are kept
1 year (compressed after 3 days), `robot_state_ts` only 30 days, so a `full` run's state series is
gone long before its track. Track rows are deleted with their run (bounded by robot and run time
span). The track endpoint reads `robot_track_ts` live: whatever rows exist for the run are served,
whatever level the run recorded at. `not_recorded` in the run timeline lists
`track` as missing at `events_only` and `off`.

**`GET /api/v1/runs/{run_id}/track`** -- `{"run_id", "robot_name", "map_id", "frame", "downsampled",
"points": [{ts, x, y, theta, speed, omega, leg_seq}]}`, oldest first. `speed` is `|(vx, vy)|` in
m/s, `omega` rad/s, `leg_seq` the `run_legs.seq` of the leg in progress (`null` between legs).
`frame` is `"map"` when exactly one placed session of the robot on the run's map (`map_id`) spans
the whole run and its placement (`placement.at` / `unplaced_at`) did not change after the run
started: x, y and theta are then converted with that session's current `map_T_session`, the frame
of the legs and `planned_path` (a null theta stays null). Otherwise (no or unplaced session,
several sessions, placement changed or session opened/closed inside the run) `frame` is `"run"`:
the robot's own frame, not drawable on the map, never a partly shifted track.
`points` is empty when nothing was recorded; more than `FLEET_TRACK_MAX_POINTS` (20000) points are
thinned by the query to every k-th row plus the last (`downsampled`). 404 for an unknown run. `debug` is not a level yet.

### Robot and mission writes

- `PUT /api/v1/robots/{robot}` takes only `labels`, `battery`, `heartbeat_timeout`, `switch_teleop`,
  `current_model`, `position_mode`, `ip_address`, `entrypoint_port`, `telemetry_recording` (written as
  just those spec keys); `name`, `lifecycle`, `current_map` are ignored, any other key is a 400 naming it.
  `status` still replaces the status wholesale (deprecated: use clear-fault).
- `PUT /api/v1/missions/{mission}` takes `robot`, `mission_tree`, `timeout`, `deadline`, `repeat`,
  `then_run`, `register_map`, `mode`, `planned_path` (PENDING missions only, else 409), `update_nodes` +
  `force` (reroute) and `status`; `route_rev`, `kind`, `goal`, `created_at`, `name`, `lifecycle` are
  ignored, any other key (e.g. `needs_canceled`: use `POST .../cancel`) is a 400.
- `POST /api/v1/robots` ignores a caller's `status`, `lifecycle` and the dispatcher-owned spec fields
  (`needs_order_cancel`, `datum*`); re-registering an existing robot writes only the changed spec keys
  and `status.factsheet`.
- `POST /api/createToken` always issues the operator grants (publish, subscribe, publish data);
  `canPublish` / `canSubscribe` / `canPublishData` in the body are ignored.

#### `POST /api/v1/robots/{robot_name}/clear-fault`
Operator override for a stuck fault: sets `status.state` to `IDLE` and `status.errors` to `{}`, leaving
the rest of the status untouched (field-level write, no body). Returns the robot (as `GET
/api/v1/robots/{robot_name}`); 404 for an unknown robot.

### Status Operations

#### `GET /api/v1/robots/{robot_name}/status`
Get robot status (proxy to Mission Dispatcher database).

**Response:**
```json
{
  "name": "carter01",
  "status": {
    "position": {
      "x": 5.2,
      "y": 10.1,
      "theta": 1.57
    },
    "battery": 85.5,
    "state": "IDLE"
  }
}
```

#### `GET /api/v1/missions/{mission_name}/status`
Get mission status (proxy to Mission Dispatcher database).

**Response:**
```json
{
  "name": "delivery_mission_001",
  "status": {
    "state": "RUNNING",
    "progress": 0.45,
    "current_node": 5
  }
}
```

#### `WS /ws/mission/{mission_name}`
WebSocket endpoint for real-time mission status updates.

#### `WS /ws/robot/{robot_name}`
WebSocket endpoint for real-time robot status updates.

## Usage

### Starting the Service

```bash
cd packages/api
python main.py --host 0.0.0.0 --port 8000 --log-level info
```

**Command-line arguments:**
- `--host`: Host to bind to (default: 0.0.0.0)
- `--port`: Port to bind to (default: 8000)
- `--log-level`: Logging level (default: info)

**Environment variables:**
- `GRAPH_DB_URL`: Graph database service URL (default: http://localhost:6001)
- `IMAGE_DB_URL`: Image database service URL (default: http://localhost:6002)
- `MISSION_PLANNER_URL`: Mission planner service URL (default: http://localhost:8005)
- `DATABASE_URL`: Mission database service URL (default: http://localhost:5000)
- `DEFAULT_MAP_ID`: Default map ID (default: default)

### Example Client Usage

#### Python Client

```python
import requests
import websocket
import json

# Base URL
BASE_URL = "http://localhost:8000"

# Load a map
response = requests.post(f"{BASE_URL}/api/v1/map/load", json={
    "map_id": "warehouse_floor_1"
})
print(response.json())

# Connect to map updates WebSocket
ws = websocket.WebSocket()
ws.connect("ws://localhost:8000/ws/map/warehouse_floor_1")

# Request navigation
response = requests.post(f"{BASE_URL}/api/v1/navigate", json={
    "robot_name": "carter01",
    "target_x": 10.5,
    "target_y": 20.3
})
print(response.json())

# Get robot status
response = requests.get(f"{BASE_URL}/api/v1/robots/carter01/status")
print(response.json())

# Get image
response = requests.get(f"{BASE_URL}/api/v1/images/node_001_front", params={
    "node_id": "1",
    "map_id": "warehouse_floor_1"
})
with open("image.jpg", "wb") as f:
    f.write(response.content)
```

#### JavaScript Client

```javascript
// Load a map
fetch('http://localhost:8000/api/v1/map/load', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ map_id: 'warehouse_floor_1' })
})
.then(response => response.json())
.then(data => console.log(data));

// Connect to map updates
const mapWs = new WebSocket('ws://localhost:8000/ws/map/warehouse_floor_1');
mapWs.onmessage = (event) => {
  const update = JSON.parse(event.data);
  console.log('Map update:', update);
};

// Request navigation
fetch('http://localhost:8000/api/v1/navigate', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    robot_name: 'carter01',
    target_x: 10.5,
    target_y: 20.3
  })
})
.then(response => response.json())
.then(data => console.log(data));

// Connect to mission status updates
const missionWs = new WebSocket('ws://localhost:8000/ws/mission/delivery_mission_001');
missionWs.onmessage = (event) => {
  const status = JSON.parse(event.data);
  console.log('Mission status:', status);
};
```

## Integration with Other Services

The API Delegation Service integrates with:

1. **Graph Database Service** (`packages/topomap_dbs/graph_db`)
   - Queries map data
   - Retrieves node and edge information

2. **Image Database Service** (`packages/topomap_dbs/image_db`)
   - Retrieves images associated with nodes

3. **Mission Planner Service** (`packages/services/mission_planner`)
   - Proxies navigation requests
   - Handles path planning

4. **Mission Dispatcher** (`packages/database`)
   - Queries robot status
   - Queries mission status
   - Monitors mission progress

5. **Graph Builder Service** (`packages/services/graph_builder`)
   - Receives map updates via WebSocket
   - Broadcasts to connected clients

## Development

### Running Tests

```bash
# TODO: Add tests
```

### Docker Deployment

```bash
# TODO: Add Dockerfile and docker-compose configuration
```

## License

SPDX-FileCopyrightText: NVIDIA CORPORATION & AFFILIATES
Copyright (c) 2021-2024 NVIDIA CORPORATION & AFFILIATES. All rights reserved.

Licensed under the Apache License, Version 2.0.

