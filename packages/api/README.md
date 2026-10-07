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
| PATCH | `/api/v1/maps/{id}` | `{description?, slam_map?: bool}` → the map view. `slam_map` only on a local map (409 geo), with no open/paused mapping session (409) and no SLAM save pending for the map's robots (409); `null` is 422; only the flag changes (an onboard SLAM map stays on the robot; turning it on again does not overwrite it); `MAP.SLAM_CHANGED` on a real change. Renaming is not supported (422): the name keys the map in Postgres, ArangoDB and MinIO. |
| GET | `/api/v1/maps/{id}/graph` | `{map_id, type, geo, state, node_count, edge_count, nodes, edges, transform}`; nodes/edges as in `POST /map/load`, without its side effects. |
| POST | `/api/v1/maps/{id}/sessions` | `{robot}` → 201 `{map_id, map_state, changed, session}`. The robot must exist (404) and be online (409), and have no other open session (409). One open session per map for now (409). A geo map needs the robot's datum (409); its first session sets the map origin from it. The map goes to `mapping`. |
| POST | `/api/v1/maps/{id}/sessions/{sid}/pause` · `resume` · `finish` | Map → `paused` / `mapping` / `ready`. Repeating an action already in effect returns `changed: false`; pause/resume of a finished session is 409. |
| POST | `/api/v1/maps/{id}/archive` · `restore` | `archived` (409 while a session is open) / back to `ready` (`draft` if it never had a session). |
| DELETE | `/api/v1/maps/{id}` | As before (202, background delete); now 409 while a session is open. The map's sessions go with it. |

A session: `{session_id, map_name, robot_name, kind ('live' | 'legacy'), state ('mapping' |
'paused' | 'finished'), started_at, paused_at, ended_at, datum, map_T_session: {tx, ty, yaw},
aligned, node_count}`. `map_T_session` takes a robot-frame point into the map frame
(`packages/utils/map_geo.py`): a pure translation for a UTM datum in the map's zone, with the
grid-convergence yaw for an ENU datum; identity for a local map (aligned only for its first
session). Every pre-M1 map has one ended `legacy` session (robot `legacy`, identity).

Since M2 a session is what routes robot data: graph-builder stores a robot's nodes and images
in the map of its open, unpaused session (node `pose` in the map frame, plus `robot_pose` and
`session_id`) and drops everything else, reported as `MAP.INGEST_REJECTED` (at most one per robot
and reason a minute, with the drop counts). A pause or finish takes effect within ~1 s. Not yet:
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
(the retained `mapping/set` and `mapping/.../state` topics are gone). Nodes are still gated on
the server: graph-builder drops a node with no open, unpaused, placed mapping session.
`topo` is `topomap` on the real robot and `sim_topomap` in the sim
(`packages/config.py::MAPPING_SERVICE_CANDIDATES`, env `MAPPING_SERVICE_TOPO`).

- `POST .../sessions` (mapping) **commits first, then starts** the services (outside any
  transaction, under the robot's lock): if the start fails the new session is closed again
  (compensating close, best effort) and the error is returned, so nothing stays open. With
  `replace` it is the other way round: the services start *before* the commit (after a dry run),
  so a failed start changes nothing and the replaced session stays open. Errors: 502 the robot has
  no registered orchestrator / it does not answer / it reports an error, 504 timeout, 409 the
  orchestrator has no such service, 409 a relocalization job runs for the robot. The `detail` says
  which ("Could not start mapping service 'topo' on robot 'r1': ...").
- `.../resume` commits, then starts them (same errors; the session is paused again), `.../pause` and `.../finish`
  stop them after the commit, best effort: on an offline robot the session is paused / closed
  anyway and the response has `robot_notified: false` and `mapping_warning`.
- `.../place` does not touch the services.
- **SLAM map** (a `local` map created with `slam_map: true`): a *mapping* session also records a
  SLAM map on the robot, named `onboard_map_name(map)` = `cloud-<map>`. After the topomap started
  (outside any transaction, under the robot's lock) the API calls `POST /maps/{onboard}/mapping/start`
  `{"overwrite": false}`; `.../finish` answers at once and saves in a background task
  (`POST /maps/{onboard}/save?background=true` `{cloud_map_id, cloud_session_id, stop_after: true}`,
  then `GET /maps/mapping/save` every `ORCHESTRATOR_SAVE_POLL_S` for up to
  `ORCHESTRATOR_SAVE_POLL_TOTAL_S` (660 s); a save that did not finish in time is retried every
  `ORCHESTRATOR_SAVE_RETRY_S` while `late_save_sec` > 0; a failed save never stops the driver); `replace` saves the
  replaced session's map first (awaited), then starts the new one. Failures never fail a session:
  `slam_warning` says what (existing map file: "SLAM map already exists, not re-recorded"). Pause,
  resume and operate sessions never touch SLAM. The background outcome is only logged. A driver still recording `cloud-<X>` after map X was deleted (nothing saved, `saving` false, no open session) is stopped by the API at the end of the map delete and by a reconcile pass every `SLAM_RECONCILE_INTERVAL_S` (300 s); the delete response is unchanged.

| Where | Field |
|---|---|
| `POST .../sessions` | `robot_notified` (bool: false only when a service of the *replaced* session could not be stopped), `mapping_switch` (`{service: started \| already_running \| stopped \| already_stopped \| failed}`), `mapping_service` (`"running"` \| `"not_running"`: the topo service), `mapping_services`, `mapping_state`, `mapping_warning` (only with `robot_notified: false`) |
| `POST .../sessions/{sid}/pause\|resume\|finish` | `robot_notified`, `mapping_switch`, `mapping_state`, `mapping_warning` |
| `POST .../sessions` and `.../finish` | `slam_warning` (only when a SLAM map step went wrong; see "SLAM map" above) |
| `POST .../sessions/{sid}/place` | `robot_notified`, `mapping_state` |
| `GET /api/v1/maps/{id}` | `sessions.mapping_state`, `sessions.mapping_service`, `sessions.mapping_services` (of the open session's robot; null without an open session) |
| `GET /api/v1/robots`, `GET /api/v1/robots/{r}` | `mapping_state`, `mapping_services` per robot |
| `WS /ws/robot/{r}` | `{type: "mapping_state_update", robot_name, timestamp, mapping_state, service, service_state}` after a service was started / stopped **through the API** (not on every change on the robot: poll `GET /robots/{r}`) |
| ~~`POST /api/v1/robots/{r}/mapping/off`~~ | removed (404); there is no retained state to force |

`mapping_state`: null (robot offline, no registered orchestrator, or its orchestrator has no
topo service) or `{status: "on"|"off"|"unreachable", online, enabled, service, session_id, map,
nodes_sent, since, stamp, received_at, source: "orchestrator", orchestrator_service}`.
`online` = the service runs; `on` = it runs and the robot's open mapping session is unpaused and
placed; `off` = it does not, or the session is paused / not placed; `unreachable` = the
orchestrator did not answer (`error` says why). `since` = the service's start time,
`nodes_sent` = the session's `node_count` (also on the robot views, from the open-session query they already make; null without an open mapping session). `mapping_services`:
`{service: running | not_running | not_available}`; `not_available` = robot offline / no
orchestrator / it has no such service / it did not answer.

#### Operate sessions and placement (maps §14, U1)

A robot uses a map through its one open session. `purpose`: `mapping` (adds data, as above) or
`operate` (uses the map, adds nothing; the map state does not change; several robots may use a
map, also while another robot maps it). `aligned` = **placed**: `map_T_session` is valid for the
robot's current run. Geo sessions are placed by the robot's datum; a local map's first mapping
session on an empty map is identity; otherwise a local-map session is placed by the user. A
session that is not placed captures nothing, gets no route orders on that map and no plans.

| Method | Path | Does |
|---|---|---|
| POST | `/api/v1/maps/{id}/sessions` | `{robot, purpose?: "mapping"\|"operate" (default mapping), services?: ["topo"\|"grid"] (mapping only, default ["topo"]), placement?: {pose: {x, y, yaw}, robot_pose: {x, y, theta}}, replace?: bool}` → 201 `{map_id, map_state, changed, session, replaced_session, robot_notified, mapping_service, mapping_services: {topo: ..., grid: ...}, mapping_state}`. 409: robot offline; robot has an open session without `replace`; map archived / being deleted / `draft` for operate; another open mapping session (mapping); geo map and no robot datum; placing while the robot drives or after it moved (> 0.02 m / 0.5°). 422: `placement` on a geo map, `services` on operate, unknown service. `replace: true` finishes the robot's open session in the same transaction (a refused start keeps it); on the same local map a placed session's transform carries over (`placement.source: "session"`). Without `placement` on a local map, the robot's last session on that map carries its transform the same way (`from_session_id`) when it ended placed and the robot's run has not changed since (§14.13). Placing is refused as "driving" for a stored `ON_TASK` only while the robot has a PENDING/RUNNING mission or no fresh state message. |
| POST | `/api/v1/maps/{id}/sessions/{sid}/place` | `{pose: {x, y, yaw}, robot_pose: {x, y, theta}, source?: "last_position"}` → `{map_id, map_state, changed, session, robot_notified, mapping_state}`; `map_T_session = pose ⊕ robot_pose⁻¹`, `placement = {pose, robot_pose, source: "user" (or the given source), actor, at}`, `MAP.SESSION_PLACED` (graph-builder keeps the session's nodes from then on). 404 unknown map/session; 409 finished, geo map, an already placed mapping session, robot offline / driving / moved. An operate session can be re-placed any time. |
| GET | `/api/v1/maps/{id}/sessions/{sid}/placement-suggestions` | `{map_id, session_id, suggestions: [{source: "last_position", basis: "unplace_snapshot"\|"state_history"\|"finished_session", map_T_session: {tx, ty, yaw}, pose: {x, y, yaw}, robot_pose: {x, y, theta}, at, from_session_id}]}`: where the robot last was on this map, for an unplaced session (run changed); at most one entry. `pose` is the last map pose; `robot_pose` is the robot's first pose in its current run, that `map_T_session` pairs it with. Empty for a placed session, a geo map or when nothing is known. 404 unknown map/session; 409 finished. Accept with `POST .../place` (`pose` + live `robot_pose`, optional `source: "last_position"`). |
| POST | `.../sessions/{sid}/finish` | both purposes (operate: "Stop using"; the map state stays). `pause` / `resume`: 409 on operate. |
| GET | `/api/v1/maps/{id}/sessions?limit=&before=` | the whole history, newest first: `{map_id, count, items, next_before}`; `limit` 1-200 (default 50); `before` = the previous page's `next_before`. |
| GET | `/api/v1/maps/{id}` | `sessions.open` is the open **mapping** session; new `sessions.operating: [{robot, session_id, aligned}]`; §14.13 `sessions.placement_reusable: {robot: from_session_id}` (local maps; `{}` otherwise): robots not on this map whose start here without `placement` would be placed from their last session (a hint; the start decides again). |
| GET | `/api/v1/maps/{id}/graph`, `POST /map/load` | nodes gain `session_id` (null for untagged legacy nodes). |
| GET | `/api/v1/robots[/{r}]` | new `session`: `{session_id, map, purpose, state: "mapping"\|"paused"\|"operating", aligned, map_T_session (null while not placed), unplaced_reason}` or null (mapless). Derived from `map_sessions`; it is the robot's map (`current_map` was removed in U6). |
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
| GET | `/api/v1/maps/{id}/reloc?robot=` | `{available, known, source: "orchestrator", can_start, can_start_reason}` (`can_start`: the API can start relocalization: robot online, orchestrator holds the map and can relocalize: it offers `POST /maps/{name}/relocalize` (its `GET /maps/mapping` reports `mode`/`relocalizing`; preferred) or, as a fallback, lists a `RELOC_SERVICE_CANDIDATES` service, and the server would not refuse the start: the robot is not driving, has no open mapping session, no job runs, no SLAM save is pending; else false + a reason in `can_start_reason`): does the robot's orchestrator hold a stored map for this map (cached `RELOC_MAP_HELD_TTL_S`, 15 s)? Geo map: `{false, true}`. Unknown or offline robot, or an orchestrator that cannot be asked: `known: false`. 404 unknown map. |
| GET | `.../sessions/{sid}/placement-suggestions` | gains `reloc`: the same object for an unplaced local-map session, else `null`. |
| POST | `.../sessions/{sid}/place` | `source` is `last_position` or `reloc`. `{"source": "reloc"}` (no poses): the robot relocalizes itself on its stored map; identity `map_T_session` (assumption D0, `map_sessions.reloc_map_t_session()`), the robot's current pose is recorded, the still check is skipped. 409 when already placed, the robot is offline or reports `position_initialized: false`, or the orchestrator does not hold the map (fresh check; unknown counts as not held). Not accepted on session start (422). **When `reloc.can_start` is true** it answers **202** `{map_id, map_state, changed: false, session: <still unplaced>, reloc_job: {id, state, step, mode: "odin"\|"assisted", started_at, deadline}}` (`deadline` is an estimate until the job starts waiting for the robot, then it is `RELOC_JOB_TIMEOUT_S` from that moment) and runs a job (`packages/api/reloc_job.py`): PATCH `init_pos` (null, or the body's `reloc: {init_pose: {x, y, yaw}}` in the cloud map frame), PUT the current map, restart the reloc service, wait for `position_initialized` (`RELOC_JOB_TIMEOUT_S`, 90 s), place in one transaction. 409 (the same reasons `can_start_reason` gives, so a bare reloc on an unplaced MAPPING session is refused) when the robot drives, has an open mapping session, another job / a pending SLAM save exists, or `init_pose` is sent while `can_start` is false; `POST .../sessions` for a mapping session is 409 while a job runs; `position_initialized: false` does not refuse a job. With `can_start` false the call is the check-only one above. |
| GET | `.../sessions/{sid}/reloc-job` | `{id, state: preparing\|starting\|waiting\|placed\|failed\|cancelled, step, mode, started_at, deadline, error?, position_initialized?, localization_score?}`; 404 none. The registry is **in memory** (lost on API restart) and so are the per-robot locks and pending SLAM saves: run the API with a **single worker** (multiple workers would not see each other's jobs, locks or saves). |
| DELETE | `.../sessions/{sid}/reloc-job` | cancel: restores the previous `init_pos` and current map (the service is not stopped, but restarted if the job had stopped it); every failure but a timeout restores them too; 409 when finished. |
| POST | `.../sessions/{sid}/unplace` | **Dev/test hook** (also "redo my placement"): marks a placed OPERATE session on a LOCAL map as unplaced (`aligned: false`, `placement.unplaced_reason: "manual"`, `placement.actor`; the old `map_T_session` and placement are kept). Emits `MAP.SESSION_UNPLACED` (`reason: manual`). The system itself unplaces when the robot's run changes; this lets the place/relocalization flows be re-run without restarting robot services. 409: finished or mapping session, geo map, a relocalization job running for the robot. Already unplaced: `changed: false`. |

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
a `localization_score` below `RELOC_DEGRADED_SCORE`, default 0.3). The warning is informational and
the session stays placed; the score is provisional (a GNSS-sigma stopgap on the robot). The
orchestrator proxy adds `cloud_map_id` and `cloud_session_id` to a proxied
`POST /orchestration/{robot}/maps/{name}/save` while the robot has an open mapping session (ids the
caller sent are kept). Env: `RELOC_MAP_HELD_TTL_S`, `RELOC_DEGRADED_SCORE`, `RELOC_SERVICE_CANDIDATES`, `RELOC_JOB_TIMEOUT_S`, `RELOC_JOB_POLL_S`, `RELOC_JOB_SETTLE_S`, `RELOC_FORCE_SERVICE` (true: always use the reloc service, never the relocalize endpoint).

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

#### `GET /api/v1/images/{image_id}?node_id={node_id}&map_id={map_id}`
Retrieve an image from the image database.

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

**Response:**
```json
{
  "success": true,
  "mission_name": "delivery_mission_001"
}
```

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

