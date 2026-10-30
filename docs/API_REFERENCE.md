# Robot Fleet Management System - API Reference for Frontend Developers

**Version:** 1.0.0  
**Base URL:** `http://localhost:8000` (API Delegation Service)

## Table of Contents

1. [Overview](#overview)
2. [Authentication](#authentication)
3. [REST API Endpoints](#rest-api-endpoints)
   - [Map Operations](#map-operations)
   - [Image Operations](#image-operations)
   - [Navigation Operations](#navigation-operations)
   - [Robot Management](#robot-management)
   - [Fleet Settings](#fleet-settings)
   - [Mission Management](#mission-management)
   - [Detection Results](#detection-results)
4. [WebSocket Endpoints](#websocket-endpoints)
5. [Data Schemas](#data-schemas)
6. [Error Handling](#error-handling)
7. [Integration Patterns](#integration-patterns)

---

## Overview

The Robot Fleet Management System provides a unified API through the **API Delegation Service**, which acts as a central gateway for all client interactions. This service proxies requests to specialized backend microservices while providing a consistent interface.

### Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              API Delegation Service (Port 8000)              │
│                    (Single Entry Point)                      │
├─────────────────────────────────────────────────────────────┤
│  REST Endpoints          │  WebSocket Endpoints             │
│  - Map loading           │  - Real-time map updates         │
│  - Image retrieval       │  - Mission status updates        │
│  - Navigation requests   │  - Robot status updates          │
│  - Robot management      │                                  │
│  - Mission management    │                                  │
└─────────────────────────────────────────────────────────────┘
         │                │                │
         ▼                ▼                ▼
┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│  Graph DB    │  │  Mission     │  │  Graph       │
│  Service     │  │  Dispatcher  │  │  Builder     │
│  (ArangoDB + │  │  (VDA5050)   │  │  Service     │
│   R-tree)    │  │              │  │              │
└──────────────┘  └──────────────┘  └──────────────┘
```

### Backend Services

The API Delegation Service integrates with the following backend services:

- **Graph Database Service**: Manages topological maps with ArangoDB (persistent storage) and R-tree (in-memory spatial index)
- **Image Database Service**: Stores robot camera images in MinIO object storage
- **Mission Planner Service**: Plans navigation paths and creates missions
- **Mission Dispatcher Service**: Executes missions using VDA5050 protocol via MQTT
- **Graph Builder Service**: Builds topological maps from robot sensor data
- **Similarity Service**: Validates edge traversability between nodes

---

## Authentication

**Current Status:** No authentication required (development mode)

**Production Recommendations:**
- Implement API key authentication
- Use OAuth 2.0 / OIDC for user authentication
- Enable TLS/SSL for all connections
- Implement rate limiting

---

## REST API Endpoints

### Service Information

#### `GET /`

Get service information and available endpoints.

**Response:**
```json
{
  "service": "API Delegation Service",
  "version": "1.0.0",
  "description": "Central API gateway for robot fleet management",
  "endpoints": {
    "health": "GET /health",
    "stats": "GET /stats",
    "load_map": "POST /api/v1/map/load",
    "get_image": "GET /api/v1/images/{image_id}",
    "navigate": "POST /api/v1/navigate",
    "robots": { ... },
    "missions": { ... },
    "websockets": { ... }
  }
}
```

#### `GET /health`

Health check endpoint.

**Response:**
```json
{
  "status": "healthy",
  "service": "api_delegation",
  "dependencies": {},
  "details": {}
}
```

#### `GET /api/v1/server-info`

What this server is and which optional services it has. The same API runs in the cloud and on a robot's own computer;
a client (the desktop app) asks this first and hides what the server lacks. Read-only, no authentication, no database
access. Computed from configuration (`packages/api/server_info.py`, settings in `packages/config.py`).

**Response:**
```json
{
  "schema_version": 1,
  "name": "Satina cloud",
  "kind": "cloud",
  "version": "3f2a9c1",
  "server_id": null,
  "capabilities": {
    "robots": true, "missions": true, "maps": true, "orchestration": true,
    "scene_graph": true, "reconstruction": true, "semantic": true, "mesh": true, "agent": true, "fleet": true
  },
  "livekit": {
    "available": true, "backend": "selfhost",
    "token_path": "/api/livekit-selfhost/createToken", "room_policy": "client"
  },
  "min_client_version": null,
  "recommended_client_version": null
}
```

- `kind` is `"cloud"` or `"robot"` (`SERVER_KIND`; anything but `robot` means cloud). `version` is the build commit.
- A capability is true when its service is **configured**, not when it is up: `scene_graph` = `SCENE_GRAPH_WORKER_ENABLED`,
  `reconstruction` = `RECONSTRUCTION_SERVICE_URL`, `semantic` = `SEMANTIC_SERVICE_URL`, `mesh` = `MESH_SERVICE_URL` (each of these three also needs its `_SERVICE_KEY` and `_CALLBACK_SECRET`), `agent` = `SERVER_AGENT_AVAILABLE`
  (default on for a cloud, off for a robot), `livekit.available` = `SERVER_LIVEKIT_AVAILABLE` when set, else whether the SFU key
  pair is set (the key pair alone does not prove a token service is deployed: set the variable to `false` on such a server).
- `room_policy` says who names the LiveKit room: `"client"` (today) or `"server"` (the token decides): `LIVEKIT_ROOM_POLICY`.
- `min_client_version` / `recommended_client_version` (`x.y.z`) tell the desktop app to refuse or to suggest an update.
- Additive contract: keys are only added. A client that gets 404 is talking to a server older than this endpoint and
  should assume everything is available. Design: `sati-client/docs/ROBOT_SERVER_PROFILE.md`.

#### `GET /stats`

Get service statistics including WebSocket connection counts.

**Response:**
```json
{
  "service": "API Delegation Service",
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

---

### Map Operations

#### `POST /api/v1/map/load`

Load a topological map from the graph database.

**Request Body:**
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
    "edge_count": 420
  },
  "message": "Map loaded successfully"
}
```

**Error Response:**
```json
{
  "success": false,
  "map_id": "warehouse_floor_1",
  "error": "Map not found"
}
```

**Backend Service:** Graph Database Service

---

### Map Mesh

A photo-textured GLB mesh of a map, built on demand by the external mesh service (`docs/mesh/design.md`). Advertised as capability `mesh`; every route answers `503 not_configured` (POST) while `MESH_SERVICE_URL`, `MESH_SERVICE_KEY` and `MESH_CALLBACK_SECRET` are not all set. The same shape as the semantic layer.

- `POST /api/v1/maps/{map_id}/mesh` starts a job. Body optional: `{session?, quality?, max_frames?, triangles?, atlas?, splat_steps?, texture_opt?}` (`session` is `"auto"` or a session id; `quality` is `fast` or `standard`; `atlas` is 1024, 2048, 4096 or 8192). `202` with the job (`state: queued`). `404 map_not_found`, `409 map_deleting | job_active | no_depth`, `422` for a bad body, `too_many_nodes` or `unknown_session` (with `sessions`: node counts per session), `503 not_configured`.
- `GET /api/v1/maps/{map_id}/mesh` returns `{map_name, configured, mesh, job}`: `mesh` is the current result (`job_id`, `finished_at`, `params`, `session`, `triangles`, `bounds`, `poses_source`, `warnings`, `frames_used`, `duration_s`, `stale`, `stale_reason`, `files: {mesh, meta}` each `{name, bytes, url}`) or `null`; `job` is the active job, or the newest one when it failed or was cancelled after the result.
- `POST /api/v1/maps/{map_id}/mesh/cancel` cancels the active job (`404 no_active_job`). `DELETE /api/v1/maps/{map_id}/mesh` deletes the current result and cancels an active job (`204`).
- `GET /api/v1/maps/{map_id}/mesh/files/model.glb` (`model/gltf-binary`) and `.../meta.json`: the current result's files, streamed. `ETag` is the job id (`304` on `If-None-Match`); with `?v=<job_id>` the response is `immutable` for a year (the URLs in `files` carry it). `404 no_mesh | file_not_found`.

### Map Drive Statistics

#### `GET /api/v1/maps/{map_id}/drive-stats`

Per-tile and per-region driving statistics (speed, time lost, coverage) of a map, from the robots' recorded
map-frame trajectories. Read only. Full rules, limits and the live cross-check: `docs/drive_stats_2026-10-14.md`.

Query (all optional): `from`, `to` (ISO-8601 with time zone; rows with `from <= ts < to`), `robot` (exact name),
`min_speed` (m/s, 0..1, default 0.05: a movement slower than this counts as stopped time),
`series=day` (opt-in; adds a per-UTC-day `series` block over `from`/`to`, default the last 30 days, at most 120 days:
`docs/map_series_2026-10-15.md`).

Response:

```json
{
  "map_id": "office7", "graph_version": 5, "cell_m": 0.1, "step": 0.1, "origin": [-6.4, -6.4],
  "tile_fields": ["ix", "iy", "n", "len_m", "time_s", "recovery_s", "stopped_s"],
  "tiles": [[64, 64, 3, 0.412, 2.35, 0.0, 1.5]],
  "regions": [{"key": "b63f0f5e-...:0", "n": 57, "len_m": 178.04, "time_s": 1455.6, "recovery_s": 119.8,
               "stopped_s": 400.1, "dwell_s": 60.0, "transit_stopped_s": 340.1, "cells": 366, "visited": 233}],
  "source": {"track_runs": 48, "track_rows": 7516, "track_segments": 7464, "track_pairs_rejected": 4,
             "leg_runs": 31, "leg_rows": 61, "legs_skipped": 0, "legs_dropped_run_has_track": 94,
             "recovery_unattributed_s": 3.1, "partial": false, "tiles_track": 939, "tiles_leg_only": 94,
             "scene_graph": "ok"},
  "window": {"from": null, "to": null, "robot": null, "min_speed": 0.05, "max_gap_s": 5.0, "max_speed": 2.0}
}
```

* **Tile index:** a tile is one 0.1 m cell of the scene-graph owner raster. `ix = floor(x / 0.1) - round(origin_x / 0.1)`,
  `iy` likewise (can be negative only without a scene graph). `origin` is the lower-left of the data on the
  scene-graph tile grid (a multiple of 6.4 m), so `owner[iy, ix]` of a raster whose origin equals `origin` is the tile's
  chart; for a raster with another origin `O` (also a multiple of 6.4 m) use `ix + round((origin_x - O_x) / 0.1)`.
  Without a scene graph (`source.scene_graph` is `none` or `error`) `origin` is `[0, 0]`, `ix, iy` are absolute
  `floor(x / 0.1)` and `regions` is `[]`. A 5 cm occupancy cell `(i, j)` lies in tile `(i // 2, j // 2)` of the same lattice.
* **Tile fields:** `n` = distinct runs that crossed the tile (coverage); `len_m` driven length, `time_s` time spent
  (including stopped and recovery time), `recovery_s` time inside NAV recoveries, `stopped_s` time in movements slower than
  `min_speed`. Mean speed of a tile = `len_m / time_s`; time lost = `recovery_s` + `stopped_s` (they can overlap).
* **Regions:** per chart key of the current graph version: `n` distinct runs, the summed fields, `cells` the chart's size
  in 0.1 m cells, `visited` how many of them have drive data. `stopped_s` is every stop; `dwell_s` is the part of it spent
  waiting near the run's start, goal and action nodes and `transit_stopped_s` = `stopped_s - dwell_s` the rest. The latter
  is the region-stats `stopped_s`. Runs read raw (no stored summary) carry no dwell split: `dwell_s` 0 for them.
* **Archived runs** (`mission_runs.archived_at`) are left out of the raw part (tracks and legs), as they are of run-stats.
* `source.partial` true: more than `DRIVE_STATS_MAX_ROWS` (2,000,000) raw track rows matched, the newest were used (the
  stored summaries are never cut).
* **Summaries:** by default (`DRIVE_STATS_SOURCE=summaries`, the default `min_speed`) a run that started in the window and
  has a stored cell summary (`run_cells`) is summed from it; all other runs (still running, not summarised yet, started
  before the window) come from the raw rows, so none is missing or counted twice. `source.basis` is `summaries` or `raw`
  (`basis_reason` `min_speed`, `config` or `params` says why raw; `params`: a stored summary was written with other rules than
  today's `DRIVE_STATS_*` / `RUN_CELLS_DWELL_RADIUS_M`, so the whole read is raw until the runs are recomputed), `source.summary_runs` / `raw_runs` count the runs of each part
  (the other `source` keys keep their meaning and cover both). `series=day` puts a summarised run on the UTC day it started
  (a raw movement keeps the day it started).
* Headers: `ETag`, `Cache-Control: private, max-age=60`; 304 on a matching `If-None-Match`. Results are cached
  server-side for 120 s per (map, window, robot, min_speed).
* Errors: 404 unknown map, 422 bad `from`/`to`/`min_speed`, 503 when the database cannot be read in time.

---

### Map Region Statistics

#### `GET /api/v1/maps/{map_id}/region-stats`

Per-chart driving statistics of a map from the stored per-run cell summaries: what a route through a chart costs, per
robot class. Read only; the planner will load the same table (`packages/utils/region_stats_load.py`). Rules, tables and
definitions: `docs/region_stats_2026-10-27.md`.

Query (all optional): `robot_class` (default `*` = all classes pooled; a class is
`<agv_class>|<length>x<width>` of the factsheet, without speed_max; `packages/utils/robot_class.py`), `version` (a stored scene-graph
version; default the newest built), `robot` (one robot, display only: computed on read from its runs, cached).

Response:

```json
{
  "map_id": "office9", "graph_version": 5, "stamp": "3f2a9c0d41b7e8aa", "built_at": "2026-10-28T09:12:00+00:00",
  "window_days": 90, "min_len_m": 1.0, "max_runs": 50, "current_graph_version": 5, "stale": false,
  "status": "ok", "robot_class": "*", "robot": null, "source": "stored", "runs": 135,
  "regions": [{"map_id": "office9", "graph_version": 5, "chart_key": "b63f0f5e-...:0", "robot_class": "*", "runs": 31,
               "len_m": 212.4, "time_s": 640.2, "stopped_s": 51.0, "recovery_s": 12.5, "blocks": 3,
               "pace_median_s_per_m": 2.9, "pace_p90_s_per_m": 4.1, "moving_pace_median_s_per_m": 2.6,
               "moving_speed_mps": 0.36, "stopped_share": 0.08, "recovery_s_per_100m": 5.9, "blocks_per_run": 0.1,
               "speed_max": null, "cells": 366, "visited": 233, "first_at": "...", "last_at": "...", "stamp": "...",
               "dwell_s": 22.0, "norm_runs": 31, "norm_len_m": 212.4, "norm_moving_m": 215.0, "norm_stopped_m": 12.2}]
}
```

* Field names of `regions` are the columns of `region_stats`. A **sample** is one run's drive in one chart of at least
  `min_len_m`; `runs` is the number of samples kept (the newest `max_runs` per chart and class); sums are over them;
  `pace_*` are `time_s / len_m` per sample (median, p90), `moving_*` leave out `stopped_s` and `dwell_s` (waiting within 0.5 m of
  the run's start, goal and action nodes, which `stopped_s` does not hold); `norm_*` are sums over the samples whose run had a
  factsheet speed_max, each time multiplied by that run's own speed_max (`norm_moving_m / norm_len_m` = 1 at speed_max); ratios are null on a zero
  denominator; `visited` counts the chart's 0.1 m cells with movement; `speed_max` is the class's factsheet speed (null for `*`).
* `stale` is true while the stored build is of an older graph version than `current_graph_version`, or a run was deleted
  since; the next build (the API worker, a debounce after a change) replaces it. `status` `no_scene_graph`: the map has no graph,
  `regions` is `[]`. `source` is `stored` or `computed`.
* Only the current graph version can be computed on read. Headers `ETag`, `Cache-Control: private, max-age=30`; 304 on a
  matching `If-None-Match`. Errors: 404 unknown map or a version that is neither stored nor current.

---

### Map Mapping Statistics

#### `GET /api/v1/maps/{map_id}/mapping-stats`

How long the mapping of a map took: totals, per session, per m^2 and per 0.1 m cell. Read only. Rules, definitions and the
live cross-check: `docs/mapping_stats_2026-10-14.md`. Query (optional): `robot` (exact name; filters the sessions, not the area), `series=session` (opt-in `series` block, one
point per mapping session, newest 30). Each session now also has `new_cells`, `area_added_m2` and `minutes_per_100m2`
(estimates, see `docs/map_series_2026-10-15.md`).

```json
{
  "map_id": "office7", "graph_version": 5, "cell_m": 0.1, "step": 0.1, "origin": [-6.4, -6.4],
  "tile_fields": ["ix", "iy", "seconds", "nodes"],
  "headline": {"sessions": 3, "sessions_open": 0, "wall_s": 576.9, "paused_s": 4.9, "active_s": 572.0, "active_h": 0.159,
               "mapped_area_m2": 67.99, "free_area_m2": 53.53, "blocked_area_m2": 14.46, "scene_graph_area_m2": 23.41,
               "seconds_per_m2": 8.41, "minutes_per_100m2": 14.0,
               "seconds_per_m2_scene_graph": 24.43, "minutes_per_100m2_scene_graph": 40.7,
               "nodes": 105, "images": 105, "attributed_s": 438.8},
  "sessions": [{"session_id": "e9727ca2-...", "robot": "formidable-peacock", "started_at": "...", "ended_at": "...",
                "open": false, "paused_now": false, "wall_s": 487.6, "paused_s": 4.9, "active_s": 482.7,
                "attributed_s": 405.3, "nodes": 95, "images": 95, "node_count_recorded": 95, "area_added_m2": null,
                "time_source": "nodes", "timestamp_source": "robot_capture", "nodes_without_time": 0,
                "nodes_outside_session": 0, "gap_s": 0.0,
                "pairs": {"pairs": 94, "pairs_attributed": 94, "pairs_missing": 0, "pairs_gap": 0, "pairs_jump": 0,
                          "pairs_long": 0, "pairs_reversed": 0}}],
  "tiles": [[64, 64, 1.52, 1]],
  "regions": [{"key": "b63f0f5e-...:10", "seconds": 115.5, "cells": 162, "area_m2": 1.62, "seconds_per_m2": 71.29,
               "nodes": 25, "visited": 51}],
  "source": {"sessions_nodes": 2, "sessions_track": 0, "sessions_without_data": 1, "nodes_without_time": 0,
             "nodes_outside_session": 0, "occupancy": "ok", "occupancy_version": 5, "occupancy_tiles": 6,
             "charts": 16, "charts_status": "ok", "scene_graph": "ok"},
  "window": {"robot": null, "max_gap_s": 60.0, "max_speed": 2.0, "max_segment_m": 10.0, "now": null}
}
```

* **Time:** `active_s = wall_s - paused_s` over the map's mapping sessions (open ones counted up to `window.now`); pauses
  from the `MAP.SESSION_PAUSED/_RESUMED` events. `sessions[].time_source` is `nodes`, `track` or `none`.
* **Area:** `mapped_area_m2` = known (free + blocked) fused-occupancy cells x 0.05^2; `scene_graph_area_m2` = summed chart
  area. `seconds_per_m2 = active_s / mapped_area_m2`; null (with `source.occupancy` `unavailable`/`none`) when unknown.
  `sessions[].area_added_m2` is always null.
* **Tile index:** identical to drive-stats (0.1 m cells, `origin` a multiple of 6.4 m; `[0, 0]` and absolute without a scene
  graph). `seconds` = mapping time attributed to the cell (consecutive node pairs spread along the straight segment, paused
  seconds excluded, pairs over `max_gap_s`, jumps and segments over `max_segment_m` dropped); `nodes` = nodes in the cell.
* **Regions:** per chart of the current graph version: `seconds`, `nodes`, `cells` (0.1 m cells), `area_m2`,
  `seconds_per_m2`, `visited` (cells with time).
* Headers: `ETag`, `Cache-Control: private, max-age=60`; 304 on a matching `If-None-Match`; server cache 120 s per (map, robot).
* Errors: 404 unknown map, 503 when the database cannot be read in time. Missing occupancy / scene graph only null the
  affected figures.

---

### Map Session Trajectory

#### `GET /api/v1/maps/{map_id}/sessions/{session_id}/trajectory`

One mapping session as a stream of topology nodes with their data: the nodes of `.../trace` (same order and `index`)
and the bytes of each node's images, depth PNGs and costmap PNGs, as an uncompressed tar (`application/x-tar`, chunked).
Query (optional): `from_index` (default 0) and `to_index` (exclusive, default the end), a window by trace index; the
manifest's `next_index` is where the next window starts (null on the last). Full contract: `packages/api/README.md`.

```
manifest.json                      plan: nodes, map-frame poses, depth/costmap records, member paths
000000/image_<image_id>            original image bytes (any format)
000000/depth_<camera>.png          16-bit mm depth (x record.depth_scale)
000000/costmap_<layer>.png         8-bit: 0-100 occupancy, 255 unknown (253 = person, read as unknown)
000001/...
missing.json                       {"missing": [{"path", "reason"}]}: always last
```

```python
import json, tarfile, requests
r = requests.get(f"{API}/api/v1/maps/{map_id}/sessions/{sid}/trajectory", stream=True)
with tarfile.open(fileobj=r.raw, mode="r|") as tar:          # stream mode: one pass, no seeking
    for member in tar:
        data = tar.extractfile(member).read()
        ...  # the first member is manifest.json; map the others through its `path` fields
```

An archive without `missing.json` as its last member was cut short. 404 unknown map / session (before the first byte);
422 bad window.

### Map Location and Relocalization

Details: `docs/satinav-maps-redesign.md` §12 and §16.

#### `PUT /api/v1/maps/{map_id}/approx_location`

Set the approximate location of a `local` map (map pins, distance sorting). A hint only: placement, alignment and sessions never read it.

**Request Body:**
```json
{
  "latitude": 47.4979,
  "longitude": 19.0402,
  "accuracy_m": 25,
  "source": "manual"
}
```

`accuracy_m` is optional; `source` is `manual` (default) or `robot`. The server sets `set_at`.

**Response:**
```json
{
  "success": true,
  "map_id": "warehouse_floor_1",
  "approx_location": {"latitude": 47.4979, "longitude": 19.0402, "accuracy_m": 25.0, "source": "manual", "set_at": "2026-10-01T12:00:00+00:00"}
}
```

**Errors:** 404 unknown map; 409 geo map (its location is its datum, see `transform` in the map summary); 422 for (0, 0) or out-of-range values. `GET /api/v1/maps` and `GET /api/v1/maps/{map_id}` return the stored `approx_location` unchanged.

#### `GET /api/v1/maps/{map_id}/reloc?robot={robot_name}`

Does the robot's orchestrator hold a stored map for this map, so it can relocalize itself without a manual initial position? Works before any session exists. The answer is cached for `RELOC_MAP_HELD_TTL_S` (15 s).

**Response:**
```json
{"available": true, "known": true, "source": "orchestrator", "can_start": true, "can_start_reason": null}
```

- `can_start` / `can_start_reason`: `true` whenever the robot exists, unless its orchestrator answers that it holds no stored map for this map or its open session is a mapping session; `can_start_reason` is then the reason, otherwise a non-blocking warning (robot offline, no orchestrator address, stored maps not readable, a job or SLAM save running) or `null`. See "Starting relocalization" below.
- `available: true`: no manual initial position needed; place with `POST /api/v1/maps/{map_id}/sessions/{session_id}/place` and `{"source": "reloc"}`.
- `known: false`: the orchestrator could not be asked (unknown or offline robot, no orchestrator address, unreachable or error); `available` is then `false`.
- A geo map is placed by its datum: `{"available": false, "known": true, "source": "orchestrator", "can_start": false, "can_start_reason": "..."}`.
- 404 unknown map.

`GET /api/v1/maps/{map_id}/sessions/{session_id}/placement-suggestions` carries the same object as `reloc` (`null` for a geo map or a placed or finished session).

#### `POST /api/v1/maps/{map_id}/sessions/{session_id}/place` with `source: "reloc"`

`source` is `last_position` (accepting a suggestion, with `pose` and `robot_pose`) or `reloc`. With `reloc` the body is just:
```json
{"source": "reloc"}
```
The robot relocalizes itself on the stored map its orchestrator holds. The server places the session with the identity `map_T_session` (assumption D0: the session frame equals the robot's map frame for a map built in that session) and records the robot's current pose. The robot-still check is skipped.

**Errors:** 404 unknown map, session or robot; 409 session finished or already placed, geo map, robot offline, robot reports `position_initialized: false`, or the orchestrator does not hold the map (a fresh check is made, an unknown answer counts as not held); 422 on a bad body. `reloc` is not accepted when starting a session (422): start the session, then place it. A low localization score never refuses a placement; it shows as `localization_warning` (below).

This is the **check-only** behaviour, and it is exactly what happens while `reloc.can_start` is `false` and no `init_pose` is sent (200, identity placement, no call to the robot).

#### Starting relocalization (three modes)

1. **Manual placement**: unchanged (`pose` + `robot_pose`, or `source: "last_position"`).
2. **Odin alone**: `{"source": "reloc"}` while `reloc.can_start` is `true`.
3. **Odin assisted by a pose**: `{"source": "reloc", "reloc": {"init_pose": {"x": 2.0, "y": -1.0, "yaw": 0.5}}}` (cloud map frame, metres / radians CCW). Needs `can_start`: **409** otherwise (no robot call).

With `can_start` the answer is **202** and a background job:
```json
{"map_id": "shed", "map_state": "ready", "changed": false, "session": {"...": "unplaced"},
 "reloc_job": {"id": "…", "state": "preparing", "step": "checking", "mode": "odin",
               "started_at": "2026-10-04T10:00:00+00:00", "deadline": "2026-10-04T10:01:30+00:00"}}
```
`mode` is `odin` or `assisted`. The job: re-checks, finds the stored map on the robot, `PATCH /maps/{name}` `init_pos` (`null` for `odin`: this also clears a stale seed and drops a hand-set value; `[x, y, 0, qx, qy, qz, qw]` from the yaw for `assisted`), reads `GET /localization` (a robot in `slam` fails the job: "finish it first"), then `PUT /localization {"mode":"relocalization","map":"<onboard name>"}?wait=false&partial=ok` (the robot switches in place; an assisted job on the map it already relocalizes on goes through `odometry` first so the new `init_pos` is loaded). It then waits until the robot's VDA5050 state has `position_initialized: true` and `agvPosition.mapId` equal to the onboard map name (`RELOC_JOB_TIMEOUT_S`, default 90 s, from the start of the waiting), proposes the placement (`confirming`, Accept / Edit, auto-accepted after `RELOC_CONFIRM_TIMEOUT_S`) and finally places the session (identity `map_T_session`, `placement.source: "reloc"`, plus `placement.init_pose` for `assisted`; `MAP.SESSION_PLACED`).

- `GET /api/v1/maps/{map_id}/sessions/{session_id}/reloc-job` returns `{id, state, step, mode, started_at, deadline, error?, position_initialized?, localization_score?}`; `state` is `preparing`, `starting`, `waiting`, `placed`, `failed` or `cancelled`. 404 when there is no job (jobs are kept **in memory** and are lost when the API restarts; a lost job's robot keeps relocalizing and nothing is placed).
- `POST /api/v1/maps/{map_id}/sessions/{session_id}/unplace` is a **developer/test hook** (also usable as "redo my placement"): it marks a placed session on a local map as unplaced (`placement.unplaced_reason: "manual"`, the old `map_T_session` is kept; a mapping session with a `warnings` entry) and emits `MAP.SESSION_UNPLACED`. The system unplaces by itself when the robot's run changes. 409 on a finished session, a geo map, or while a relocalization job runs; an already unplaced session returns `changed: false`.
- `DELETE` on the same URL cancels a running job: the previous `init_pos` and stored localization intent (`odometry` when none) are restored on the robot, unless someone changed the intent meanwhile. 409 when it has finished.
- **409 (at the POST)**: robot offline, **driving**, has an open **mapping session**, another reloc job or a pending SLAM save for the robot, the map not held, or `init_pose` without `can_start`. A robot that reports `position_initialized: false` is *not* refused (the job is about to fix that).
- **`failed`** (`error` says why): the robot's refusal is spelled out (409 e.g. "order active: cancel it first", 502 map refused, 503 not ready yet), and so are a partial answer's `problem` and `applied: false` (no driver runs); a failed PATCH / PUT restores the previous `init_pos` and intent (best effort); the robot going offline, or the session finished, replaced or placed meanwhile, fails the job with no placement; on **timeout** the robot is left relocalizing and nothing is restored.
- The frame is the D0 assumption (identity), kept in `map_sessions.reloc_map_t_session()` / `reloc_bin_pose()`; see docs/satinav-maps-redesign.md section 16 for the open questions (a map saved by a later session, the robot's frame after a reloc-mode driver start).

#### Robot view additions

`GET /api/v1/robots[/{robot_name}]` and the WebSocket `robot_update`:

- `current_mission` (the running mission's name or `null`) and `queued_missions` (names of the PENDING missions, in dispatch order), derived from the mission rows.
- `status.position_initialized` (boolean or `null`) and `status.localization_score` (0..1 or `null`): VDA5050 `agvPosition` as last reported.
- `status.approx_position`: `{latitude, longitude, accuracy_m?, fix_quality?, source, stamp?, stored_at}` or `null`, from the robot's MQTT `approx_position` topic. Display only, never used for placement.
- `session.placement_source`: how a placed session was placed (`user`, `last_position`, `reloc`, `session` or `datum`), else `null`.
- `localization_warning`: a reason string, or `null`. Set only for a placed `reloc` session when the robot reports `position_initialized: false` or a `localization_score` below `RELOC_DEGRADED_SCORE` (default 0.3). Informational: the session stays placed. The score is provisional: the robot's current value is a GNSS-sigma stopgap, not a map-matching score.

The robot spec also has `datum_changed_at` and `datum_stamp` (see `docs/MQTT_MISSION_INTEGRATION.md`).

#### Orchestrator proxy and map links

`POST /api/v1/orchestration/{robot}/localization/save` with `name` = `cloud-<map>` adds `cloud_map_id` (the open mapping session's map) and `cloud_session_id` to the body when the robot has an open mapping session on that map and the caller did not send them (an explicit `null` counts as not sent). This links the saved map to the cloud map for `reloc`. After any proxied non-GET `maps/*` call the cached held-map answer for that robot is dropped.

**Environment variables:** `RELOC_MAP_HELD_TTL_S` (default 15), `RELOC_DEGRADED_SCORE` (default 0.3), `RELOC_JOB_TIMEOUT_S` (default 90), `RELOC_JOB_POLL_S` (default 1), `RELOC_CONFIRM_TIMEOUT_S` (default 30).

---

### Image Operations

#### `GET /api/v1/images/{map_id}/{node_id}`

Retrieve an image associated with a specific node.

**Path Parameters:**
- `map_id` (string): Map identifier
- `node_id` (string): Node identifier

**Query Parameters:**
- `image_id` (string, optional): Specific image ID. If not provided, returns the first available image for the node.

**Response:**
- Content-Type: `image/jpeg`
- Binary image data

**Example:**
```javascript
// Fetch image for node 1001 in warehouse map
fetch('http://localhost:8000/api/v1/images/warehouse_floor_1/1001')
  .then(response => response.blob())
  .then(blob => {
    const imageUrl = URL.createObjectURL(blob);
    document.getElementById('robot-view').src = imageUrl;
  });
```

**Error Responses:**
- `404 Not Found`: Image not found
- `503 Service Unavailable`: Service not initialized

**Backend Service:** Image Database Service (MinIO)

---

### Navigation Operations

#### `POST /api/v1/navigate`

Request navigation for a robot to a target location.

**Request Body:**
```json
{
  "robot_name": "carter01",
  "target_x": 10.5,
  "target_y": 20.3,
  "mission_name": "delivery_mission_001",
  "timeout_seconds": 300
}
```

**Request Fields:**
- `robot_name` (string, required): Name of the robot
- `target_x` (float, required): Target X coordinate in meters
- `target_y` (float, required): Target Y coordinate in meters
- `mission_name` (string, optional): Custom mission name (auto-generated if not provided)
- `timeout_seconds` (integer, optional): Optional mission time limit in seconds (default: none, no limit; the mission then ends only when the robot reports success or failure, or it is canceled)

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
mission; without the header every request makes its own. With the scene-graph planner the
response also has `trajectory` (the planner's dense path per leg, see "Route trajectory" below).

Scene-graph plans (`POST /api/v1/navigate`, planner `POST /api/v1/plan`) also carry `footprint`:
`{robot_half_side_m, map_half_side_m, chart_radius_m, status, message}`, the robot's half smaller factsheet
side against the half side the map was built for. `status` is `ok`, `larger_than_map` or `unknown_footprint`
(`robot_half_side_m` null; both warnings, the plan proceeds, with the map's value when unknown), or
`exceeds_chart` (robot half side above the map's chart radius): then the plan is refused as any planning
failure, `success: false`, `failed_at: "footprint"`, `error` = `message`, with no legacy fallback. Absent for
the legacy planner and for maps without scene-graph parameters.

They also carry `goal_far` when the route's goal is far from verified space (the goal lies beyond the map's
close-evidence range from free space; it is planned to the nearest free cell, not refused):
`{goal_xy, free_xy, distance_from_free_m, limit_m, unverified_length_m}`, `goal_xy` and `free_xy` as `[x, y]`.
`unverified_length_m` is the metres of the route not validated by close evidence, or `null` when the store has
no validity plane (unknown; it is never reported as 0). Absent (`null`) when the goal is within range or for the
legacy planner.

**Error Response:**
```json
{
  "success": false,
  "error": "Robot 'carter01' not found"
}
```

**Workflow:**
1. API validates robot exists
2. Request forwarded to Mission Planner Service
3. Mission Planner finds path using Graph Database
4. Mission submitted to Mission Dispatcher
5. Mission Dispatcher sends VDA5050 commands to robot via MQTT
6. Client monitors progress via WebSocket (`/ws/mission/{mission_name}`)

**Backend Services:** Mission Planner → Mission Dispatcher → Robot (MQTT)

---

#### POST /api/v1/route/preview

Dry-run preview of a route for the mission editor (docs/route_preview_2026-10-14.md). Runs the planner's route enrichment (the same
call the dispatcher makes before a mission's first start) and returns the result; nothing is stored and no event is written.

**Request:**
```json
{
  "map_id": "office",
  "waypoints": [{"x": 1.0, "y": 2.0, "theta": 0.0, "skippable": true, "max_wait_s": 10, "corridor_width_m": 0.3, "origin": "user"}],
  "return": false,
  "robot_name": "carter01"
}
```
`map_id` and `waypoints` are required (map frame; every waypoint gets `map_id`). `return: true` appends the way back (the
waypoints in reverse order, heading + pi, the last waypoint not repeated). `robot_name` (optional) gives the robot footprint width for
corridor widths. Waypoints with `origin: "planned"` are dropped and re-planned. `node_policy.total` is the number of waypoints but the last that are planned or ask to be skippable (the same count whether or not the planner answered); where nothing could be measured, positive measured corridors are cleared. An operator corridor above 1.5 m is capped silently.

**Response (200):**
```json
{
  "waypoints": [{"x": 1.0, "y": 2.0, "origin": null}, {"x": 4.0, "y": 2.0, "origin": "planned", "skippable": true, "corridor_width_m": 0.2}],
  "trajectory": [{"from_index": 0, "to_index": 1, "points": [[1.0, 2.0], [4.0, 2.0]]}],
  "node_policy": {"total": 1, "skippable": 1, "unavailable": null},
  "issues": [{"from_index": 1, "to_index": 2, "reason": "unreachable"}],
  "planner": "scene_graph",
  "reason": null,
  "graph_version": 12
}
```
`trajectory` has the shape of the stored one (`[]` when none; a leg may be missing: draw it straight). `issues` lists the legs of
`waypoints` the planner could not route (`outside_graph`, `unreachable`, `error`). `planner` is `scene_graph` or `none`; with `none` the
waypoints come back as sent (return mirrored), `trajectory` and `issues` are empty and `reason` says why (`planner_unavailable`,
`no_scene_graph`, `plan_timeout`, `disabled`, `error`). **Status:** 200 (also when the planner is down), 400 invalid body (empty
waypoints, bad pose, non-finite value, unknown `origin`), 413 more than 100 waypoints (`ROUTE_PREVIEW_MAX_WAYPOINTS`), 422 missing
`map_id`/`waypoints`. Time budget: `NODE_POLICY_ENRICH_TIMEOUT_S` (5 s).

---

### Robot Management

#### `GET /api/v1/robots`

List all robots with optional filtering.

**Query Parameters:**
- `min_battery` (float, optional): Minimum battery level (0-100)
- `max_battery` (float, optional): Maximum battery level (0-100)
- `state` (string, optional): Robot state filter (`IDLE`, `ON_TASK`, `CHARGING`, `MAP_DEPLOYMENT`, `TELEOP`)
- `online` (boolean, optional): Online status filter
- `robot_type` (string, optional): Robot type filter (`FORKLIFT`, `CARRIER`)

**Response:**
```json
[
  {
    "name": "carter01",
    "labels": ["warehouse", "floor1"],
    "battery": {
      "critical_level": 10.0,
      "recommended_minimum": 20.0,
      "recommended_maximum": 80.0
    },
    "heartbeat_timeout": 30.0,
    "switch_teleop": false,
    "status": {
      "pose": {
        "x": 10.5,
        "y": 20.3,
        "theta": 1.57
      },
      "online": true,
      "battery_level": 85.5,
      "state": "IDLE",
      "software_version": {
        "os": "Ubuntu 22.04",
        "app": "1.2.3"
      },
      "hardware_version": {
        "manufacturer": "NVIDIA",
        "serial_number": "SN12345"
      },
      "errors": {}
    },
    "lifecycle": "ALIVE"
  }
]
```

**Example:**
```javascript
// Get all robots with battery > 50% that are online
fetch('http://localhost:8000/api/v1/robots?min_battery=50&online=true')
  .then(response => response.json())
  .then(robots => console.log(robots));
```

**Backend Service:** Mission Dispatcher Database

---

#### `GET /api/v1/robots/{robot_name}`

Get detailed information about a specific robot.

**Path Parameters:**
- `robot_name` (string): Robot identifier

**Response:**
```json
{
  "name": "carter01",
  "labels": ["warehouse", "floor1"],
  "battery": {
    "critical_level": 10.0
  },
  "status": {
    "pose": {
      "x": 10.5,
      "y": 20.3,
      "theta": 1.57
    },
    "online": true,
    "battery_level": 85.5,
    "state": "IDLE"
  }
}
```

**Error Response:**
- `404 Not Found`: Robot not found

---

#### `GET /api/v1/robots/{robot_name}/status`

Get the current status of a robot.

**Response:** the robot's `status` object only (the `status` field of
`GET /api/v1/robots/{robot_name}`), mirroring `GET /api/v1/missions/{name}/status`.
It can be PUT back as `{"status": ...}` without clobbering anything.

---

#### `POST /api/v1/robots`

Create a new robot.

**Request Body:**
```json
{
  "name": "carter02",
  "labels": ["warehouse", "floor2"],
  "battery": {
    "critical_level": 10.0,
    "recommended_minimum": 20.0,
    "recommended_maximum": 80.0
  },
  "heartbeat_timeout": 30.0
}
```

**Required Fields:**
- `name` (string): Unique robot identifier

**Optional Fields:**
- `labels` (array of strings): Robot labels for grouping
- `battery` (object): Battery specifications
- `heartbeat_timeout` (number): Timeout in seconds for robot heartbeat

**Response:**
```json
{
  "name": "carter02",
  "labels": ["warehouse", "floor2"],
  "status": {
    "pose": { "x": 0, "y": 0, "theta": 0 },
    "online": false,
    "battery_level": 0,
    "state": "IDLE"
  },
  "lifecycle": "ALIVE"
}
```

**Error Response:**
- `400 Bad Request`: Missing required field or validation error

---

#### `PUT /api/v1/robots/{robot_name}`

Update robot specification.

**Request Body:**
```json
{
  "labels": ["warehouse", "floor2", "updated"]
}
```

**Response:**
```json
{
  "name": "carter02",
  "labels": ["warehouse", "floor2", "updated"],
  "status": { ... }
}
```

**Note:** This endpoint updates the robot's **specification** (labels, battery config, etc.), not the status. Robot status is updated by the Mission Dispatcher based on MQTT messages from the robot.

---

#### `DELETE /api/v1/robots/{robot_name}`

Delete a robot. Always closes the robot's open map session and removes its site assignments,
run epoch, latest-state row and the robot itself; the name can then be registered again
(`POST /api/v1/robots`) like a new robot. History is kept unless asked for.

**Query parameters** (both boolean, default `false`):
- `delete_telemetry`: also delete the robot's rows in `robot_state_ts`, `diagnostics_ts`,
  `fleet_events`, `mission_runs` and `mission_trajectory`.
- `delete_rosbags`: also delete the robot's rosbags from MinIO.

**LiveKit:** after the robot is deleted, the API makes a best-effort call to the self-hosted
LiveKit SFU (server API, `LIVEKIT_SFU_API_KEY`/`LIVEKIT_SFU_API_SECRET`, optionally
`LIVEKIT_SFU_ADMIN_URL`, default `http://localhost:7880`) to remove the robot's participant
(identity or name equal to the robot name, or `dev_<name>`) from every room, so dashboards stop
showing a ghost card. Skipped (logged) when the key pair is not configured; any LiveKit error or
timeout (`LIVEKIT_ADMIN_TIMEOUT`, 3 s) is logged and ignored and never changes the response.
This only disconnects the participant: a robot that is still running reconnects with its token
unless it is stopped.

**Response:**
```json
{
  "success": true,
  "message": "Robot carter02 deleted",
  "deleted": {"telemetry": false, "rosbags": false, "sessions_closed": 1}
}
```

**Error Responses:**
- `404 Not Found`: Robot not found (or already deleted)
- `409 Conflict`: the robot has an active mission (state `ON_TASK`, or a pending/running
  mission); nothing is changed. `detail` is
  `{"code": "ROBOT_HAS_ACTIVE_MISSION", "message": "...", "robot": "carter02", "mission": "<name or null>"}`
- `500 Internal Server Error`: any other failure

---

### Fleet Settings

A single, fleet-wide, operator-editable settings object — not tied to any
one robot/mission/map. Always stored under the fixed name `"global"`
(`SettingsObjectV1`, `cloud_common/objects/settings.py`): a singleton
simulated by convention on top of the normal name-keyed Postgres storage,
since there's no separate keyless/singleton storage mode. Auto-created
with defaults on first `GET` or `PUT` if it doesn't exist yet.

#### `GET /api/v1/settings`

Fetch the fleet-wide settings.

**Response:**
```json
{
  "name": "global",
  "lifecycle": "ALIVE",
  "fault_error_types": [],
  "status": {}
}
```

**Backend Service:** Mission Dispatcher Database (Postgres)

---

#### `PUT /api/v1/settings`

Update the fleet-wide settings. Unknown fields are ignored; `name`,
`status`, and `lifecycle` in the request body are ignored too (this is a
spec-only update, same convention as `PUT /api/v1/robots/{robot_name}`).

**Request Body:**
```json
{
  "fault_error_types": ["motorStalledError"]
}
```

**`fault_error_types`:** VDA5050 `errorType` strings severe enough that a
robot reporting one should be badged FAULT by clients (see
`sati-client`'s `utils/robotStatus.ts`). Any other `errorType` a robot
reports is treated as a non-fault warning by clients. Empty by default —
nothing is FAULT until an operator opts specific error types in here.

Separate from FAULT classification, mission-dispatch holds a PENDING mission
(`held`/`held_reason`) while the robot reports one of these standing readiness
`errorType`s: `robotBaseNotReadyError` ("Robot base is not responding");
`navigationNotReadyError`, `poseHealthNotReadyError`, `tfChainNotReadyError`
("Robot navigation is not ready"). The reason is a fixed string, and the hold
releases when the error disappears from the robot's state.

**Response:** Same shape as `GET /api/v1/settings`, reflecting the update.

**Backend Service:** Mission Dispatcher Database (Postgres)

---

### Mission Management

#### `GET /api/v1/missions`

List all missions.

**Response:**
```json
[
  {
    "name": "delivery_mission_001",
    "robot": "carter01",
    "mission_tree": [
      {
        "name": "navigate_to_target",
        "parent": "root",
        "route": {
          "waypoints": [
            { "x": 5.0, "y": 10.0, "theta": 0.0 },
            { "x": 10.5, "y": 20.3, "theta": 1.57 }
          ]
        }
      }
    ],
    "timeout": 300.0,
    "status": {
      "state": "RUNNING",
      "progress": 0.45,
      "node_status": {
        "root": {
          "state": "RUNNING",
          "progress": 0.45
        },
        "navigate_to_target": {
          "state": "RUNNING",
          "progress": 0.45
        }
      }
    },
    "lifecycle": "ALIVE"
  }
]
```

---

#### `GET /api/v1/missions/{mission_name}`

Get detailed information about a specific mission.

**Path Parameters:**
- `mission_name` (string): Mission identifier

**Response:**
```json
{
  "name": "delivery_mission_001",
  "robot": "carter01",
  "mission_tree": [ ... ],
  "timeout": 300.0,
  "status": {
    "state": "RUNNING",
    "progress": 0.45,
    "start_time": "2024-01-15T10:30:00Z",
    "node_status": { ... }
  }
}
```

**Error Response:**
- `404 Not Found`: Mission not found

---

#### `GET /api/v1/missions/{mission_name}/status`

Get current status of a mission (alias for GET /api/v1/missions/{mission_name}).

---

#### `POST /api/v1/missions`

Create a new mission.

**Request Body:**
```json
{
  "name": "custom_mission_001",
  "robot": "carter01",
  "mission_tree": [
    {
      "name": "move_forward",
      "parent": "root",
      "move": {
        "distance": 5.0,
        "direction": "forward"
      }
    },
    {
      "name": "capture_image",
      "parent": "root",
      "action": {
        "action_type": "capture_image",
        "action_parameters": {
          "camera": "front"
        }
      }
    }
  ],
  "timeout": 300
}
```

**Required Fields:**
- `name` (string): Unique mission identifier
- `robot` (string): Robot name
- `mission_tree` (array): List of mission nodes (tasks)

**Mission Node Types:**
- `route`: Navigate through waypoints
- `move`: Relative movement (distance/rotation)
- `action`: Execute robot action
- `notify`: API callback
- `selector`: Choose first successful child
- `sequence`: Execute children in order

**Response:**
```json
{
  "name": "custom_mission_001",
  "robot": "carter01",
  "mission_tree": [ ... ],
  "status": {
    "state": "PENDING",
    "progress": 0.0
  },
  "lifecycle": "ALIVE"
}
```

**Error Response:**
- `400 Bad Request`: Missing required fields or validation error

---

#### `PUT /api/v1/missions/{mission_name}`

Update a mission.

Editing the spec (`robot`, `mission_tree`, `timeout`, `deadline`, `repeat`, `then_run`,
`register_map`, `mode`, `planned_path`) is only allowed while the mission is `PENDING`;
any other state answers `409`, and an invalid spec answers `400` with the validation
message. `name` cannot be changed (it is the mission's identity), so a rename or an edit
of a mission that has already run is a new mission. A new `mission_tree` gets its
`status.node_status` entries created and the dropped ones removed. The dispatcher applies
the edit to a queued (or held, not yet dispatched) mission; once the mission has been
dispatched a late edit is ignored. `update_nodes` (a reroute of a running mission) is not
an edit and is not restricted this way: `{"update_nodes": {"<route node>": {"waypoints": [...]}}}`
is validated (the node must exist, be a route node, and not be finished), folded into the
stored `mission_tree`, clears `planned_path` and bumps the spec's `route_rev`; the request itself
is not stored, and the response is the updated mission. The dispatcher acts on a reroute once per
`route_rev` (`status.applied_route_rev` is the revision it has acted on), cancelling the running
order and resending the node with the new route. Other spec fields: `kind` (`"goto"` for a
planner go-to, else `null`), `goal` (`{x, y, map_id, node_id?}` of a go-to), `created_at`
(set when the row is created); all server-owned.

A reroute whose waypoints go through a graph node a robot reported blocked recently (see
[Blocked graph nodes](#blocked-graph-nodes)) is refused with `409`
`{"error": "HTTP Error", "detail": {"code": "ROUTE_THROUGH_BLOCKED_NODES", "message": ...,
"blocked_nodes": [...]}, "status_code": 409, "path": ...}`; add `"force": true` to the body to
reroute anyway. The check uses the planner's rule, so a route the planner returned passes it:
each route's first waypoint (where the robot starts) is never blocked; a waypoint with a
`node_id` matches a row of that graph node, or a position row (`"@x,y"`) within
`BLOCKED_NODE_MATCH_RADIUS_M` (0.5 m); a waypoint without `node_id` matches by lying within
that radius of where the row's node was reported.

Waypoint tolerances: `allowedDeviationXY` / `allowedDeviationTheta` left out (or 0) mean the
dispatcher's policy: 0.35 m / heading free on nodes the robot drives through, 0.35 m / 0.785 rad
on the last node. A positive value is sent as given. A waypoint may
carry `node_id`, the graph node it was taken from (the planner sets it).

Waypoint skip policy (all optional; left out = the old behaviour, a waypoint is not skippable):
`skippable` (bool: the robot may skip this waypoint when it is blocked), `max_wait_s` (seconds
the robot holds at the waypoint before it counts as blocked; default `NODE_POLICY_MAX_WAIT_S`,
capped at 120), `corridor_width_m` (metres of free ground on each side of the edges before and
after the waypoint that a skip may use), `origin` (`"user"` or `"planned"`). `max_wait_s` and
`corridor_width_m` must be finite and >= 0, else 422/400. A skip is sent to the robot only when
`skippable` is true AND the corridor width is above `NODE_POLICY_SKIP_MIN_CORRIDOR_M` (0.10 m, the robot's own margin; agreed with the robot team 2026-10-27): a corridor of 0.10 m or less is still sent as `corridorWidth` (the gate) but with `skippable=false`, and 0 means measured, no room. The planner fills `corridor_width_m` from the
map's scene graph when the mission starts (the operator may also give one). Without a scene
graph, or where the width is unknown, the waypoint is sent not skippable (logged, never an
error); the last waypoint of a route is never skippable. Go-to missions (`POST /navigate`) get
every waypoint but the last skippable by the planner.

Planned waypoints: when a mission starts and the map has a scene graph, the planner inserts
waypoints with `origin: "planned"` (and `skippable: true`, a `corridor_width_m`) between two of
the operator's consecutive waypoints that are not in the same or a neighbouring chart, so every
consecutive pair is close enough for the robot to route. They are stored in the mission's
`mission_tree` before the first order (the route gets longer; `skipped_nodes[].waypoint_index`
and the order's nodes refer to the stored route). The operator's own waypoints keep their
fields. A client that edits the route should keep or drop planned waypoints freely: the planner
drops them and plans them again at the next first start. Missions with `planned_path` get no
inserted waypoints, and neither does a reroute (`update_nodes`). Switches: `NODE_POLICY_SKIP_ENABLED`,
`NODE_POLICY_GAP_FILL_ENABLED`.

Status fields the dispatcher owns (an API status update keeps them): `skipped_nodes` (nodes the
robot skipped as their nodePolicy allowed: `node_id`, `mission_node`, `waypoint_index`,
`graph_node_id`, `description`), `node_notes` (the robot's advisory per-node notes, newest
first, at most 50: `info_type` such as `nodeOffset`, `offset`, `offset_map`), and
`offset_summary` (`n`, `mean_dx`, `mean_dy`, `consistency`, `suspected_frame_error`: node
offsets that all point the same way suggest a frame error between graph and robot map).

**Request Body:**
```json
{
  "timeout": 600,
  "repeat": 3,
  "then_run": "dock_check"
}
```

**Response:**
```json
{
  "name": "custom_mission_001",
  "timeout": 600,
  "status": { ... }
}
```

---

#### `DELETE /api/v1/missions/{mission_name}`

Delete a mission.

**Response:**
```json
{
  "success": true,
  "message": "Mission custom_mission_001 deleted"
}
```

---

#### `POST /api/v1/missions/{mission_name}/cancel`

Cancel an active mission.

**Response:**
```json
{
  "success": true,
  "message": "Mission custom_mission_001 cancelled"
}
```

**Error Response:**
- `400 Bad Request`: Mission cannot be cancelled (already completed/failed)

#### Route trajectory (the planner's full path)

The scene-graph planner finds its route on a 0.1 m lattice and keeps only the simplified waypoints. The dense path
is returned as display data so the client can draw it instead of straight lines between waypoints:

```json
"trajectory": [
  {"from_index": 0, "to_index": 1, "points": [[0.55, 0.55], [9.45, 0.55], [9.55, 0.65], [9.55, 0.95]]},
  {"from_index": 1, "to_index": 2, "points": [[9.55, 0.95], [9.55, 9.45]]}
]
```

One entry per leg (`from_index` / `to_index` index the route's `waypoints`, `to_index = from_index + 1`), in route
order; `points` are `[x, y]` in the map frame, rounded to the cm, only the direction changes of the lattice path (the
polyline through them is the lattice path exactly), the first and last point are the leg's waypoints. A leg may be
missing (the planner did not route that pair): draw it straight. The key is absent when the legacy planner served the
plan, the planner has no scene graph, `PLAN_TRAJECTORY_ENABLED` is off, or the whole route has more than
`PLAN_TRAJECTORY_MAX_POINTS` (3000) points (omitted whole, never truncated). It is never sent to the robot and not part
of the route's resume digest. Where: `POST /api/v1/plan`, `POST /api/v1/route/enrich`, the planner's navigate response, and
the gateway's `POST /api/v1/navigate` carry `trajectory`; stored missions carry it as
`mission_tree[].route.trajectory` (a go-to from creation and again after the dispatcher replans it at start; an operator's
route once the dispatcher enriched it before the first order). A client that edits a route (`PUT /missions`) and sends
the route back without `trajectory`, or with legs whose ends no longer sit at their waypoints, loses it (stale legs are
dropped on load, never an error). See `docs/plan_trajectory_2026-10-14.md`.

#### Blocked graph nodes

When a robot reports `edgeBlocked`, the dispatcher keeps the blocked waypoint's graph node out of
new routes for `BLOCKED_NODE_EXCLUSION_MIN` (10) minutes, for every robot on the map. The
planner keeps its usual shortest path unless that path goes through an active row, and then
plans around it; when the goal is blocked, or no route avoids the rows, it fails with
`failed_at: "blocked_nodes"` and, in `blocked_nodes`, the rows that made it fail (the planner's
`POST /api/v1/plan` takes `ignore_exclusions: true` to plan through them). A goal with no path
at all is still `failed_at: "find_path"`.

`GET /api/v1/maps/{map_id}/blocked-nodes` returns `{"map_id", "blocked_nodes": [{"map_name",
"graph_node_id", "edge_from", "edge_to", "source", "robot_name", "mission_name", "vda_node_id",
"reason", "x", "y", "created_at", "expires_at"}]}`. `graph_node_id` `"@x,y"` is a position
(map frame) whose graph node was not known; `map_name` is the waypoints' `map_id`;
`created_at` is when the current block began (a report on an expired row starts a new one). If
the table is not readable the list is empty and the body carries `error` (the planner and the
reroute check also go on without it).

`DELETE /api/v1/maps/{map_id}/blocked-nodes/{graph_node_id}` clears one (`404` if it is not
blocked: no row, or its exclusion had already expired). URL-encode the id (`%40x%2Cy`).

#### Run legs

A leg is one robot move from one topomap node to the next. mission-dispatch records one row per
leg in `run_legs` from the robot's VDA5050 state (each change of `lastNodeId`), at every
recording level except `off` (legs are kept like events, indefinitely). The run's
`summary_metrics` is filled from them when the run ends.

**`GET /api/v1/runs/{run_id}/legs`** -- the run's legs in order: `{"run_id", "items": [leg...]}`
(empty when none were recorded). 404 for an unknown run. With `?with_efficiency=true` every leg also has
`plan_m`, `plan_source` (`trajectory`|`straight`|`stale`|null), `driven_m`, `driven_excl_recovery_m`,
`path_ratio` (`plan_m / driven_m`) and `efficiency_excluded` (`no_track`|`gap`|`incomplete_track`|`no_plan`|`short`|null),
and the top level has `efficiency` (the run's block, same shape as run-stats' below); see
`docs/path_efficiency_2026-10-15.md`. A leg:

| Field | Meaning |
|---|---|
| `seq` | 1-based leg order in the run (events carry it as `payload.leg_seq`) |
| `pass_index` | 0-based repeat pass; `order_rev` the order revision it ended in (a reroute resends the order, the leg is one leg across it) |
| `from_vda_node`, `to_vda_node` | VDA node ids (`<mission>-r<run>[v<rev>]-n<tree node>-s<seq>`); `from_vda_node` is the implied start node when the robot never reported it |
| `from_topomap_node`, `to_topomap_node` | Topomap node ids, or `null`: only a mission whose `planned_path` lines up one-to-one with its route waypoints (planner go-tos) has them; a rerouted or hand-made route has none. The first leg of a pass has no `from_topomap_node` |
| `map_id` | Map of the route |
| `started_at`, `ended_at` | Robot clock (VDA5050 header time). `received_started_at`, `received_ended_at`: dispatcher clock |
| `duration_s`, `stopped_s`, `silent_s`, `moving_s` | Leg time, split by the robot's state messages (additive, stage C of the edge statistics). `stopped_s`: time after a message with `driving: false` (a missing `driving` counts as driving), at most 30 s per interval between two messages. `silent_s`: the part of every interval above 30 s (no messages: a link or dispatcher gap), whatever `driving` said. `moving_s` = max(0, `duration_s` - `stopped_s` - `silent_s`). Recovery time is not subtracted (it overlaps `stopped_s`, and a recovery maneuver may report driving). `silent_s` and `moving_s` are `null` on a leg recorded before 2026-10-27 (the silent time is unknown) |
| `straight_m`, `planned_m` | `straight_m`: map-frame straight-line distance between the two waypoints (`null` for a leg that starts at the robot's own position). `planned_m`: the length of the plan between them: the polyline of the route node's plan trajectory when the leg is two adjacent waypoints of one route node whose trajectory entry ends where the leg ends (`plan_source` `trajectory`), else `straight_m` (`straight`: no trajectory entry; `stale`: an entry whose ends are not the leg's, after a reroute). Legs recorded before 2026-10-27 have `planned_m` = `straight_m` and `plan_source` `null` |
| `plan_source`, `from_chart`, `to_chart`, `graph_version` | `plan_source`: `trajectory` \| `straight` \| `stale` \| `null` (recorded before 2026-10-27, or no plan: `straight_m` is `null`). `from_chart`, `to_chart`: the scene-graph chart key of the leg's start and end waypoint as the planner stored them with the route (`null` for the robot's own position, a hand-made route or a route without them); `graph_version`: the scene-graph version those keys were made on. The edge statistics use `planned_m` as the leg's distance |
| `expected_s` | `d / speed_max + speed_max / acceleration_max + abs(delta heading) / angular_speed_max` from the robot's factsheet; a term whose limit is unknown (-1) is dropped, `null` when nothing is known or the start has no pose |
| `recoveries`, `recovery_s`, `blocks` | `recoveries` counts recovery EPISODES (as `map_events`: a `NAV.RECOVERY_ENTERED` right after another ENTERED of the run, without an EXITED in between, is the same recovery); `recovery_s` sums the `NAV.RECOVERY_EXITED` durations; `blocks` counts `NAV.GOAL_BLOCKED` + `MISSION.EDGE_BLOCKED`; events tagged with the leg, filled when the run ends (events arriving later are not counted; runs ended before this rule counted every ENTERED) |

**`GET /api/v1/missions/{name}/legs`** -- legs of all non-archived runs of the mission and its
`-rerun-<n>` reruns grouped by leg identity (`from` -> `to`: the topomap node ids, else the
run-independent VDA tail `n<tree node>-s<seq>`; `topomap` says which). `{"mission", "runs",
"legs", "truncated", "items": [{from, to, topomap, count, runs, median_s, p90_s, expected_s,
ratio, recoveries, recovery_s, blocks}]}`, slowest median first. `ratio` = `median_s /
expected_s`; `p90_s` is linearly interpolated. 404 when the mission has no run.

`GET /api/v1/runs/{run_id}` also returns `run.planned_path` (the mission's planned path when the
run started) and, once the run has ended, `run.summary_metrics`: `leg_count`, `duration_s`,
`distance_m`, `time_moving_s` (the legs' `moving_s`; `duration_s - stopped_s` for a leg without it), `time_stopped_s` (rest of the run: everything that is not moving, incl. time
outside any leg), `time_silent_s` (the legs' `silent_s`, part of `time_stopped_s`), `time_recovery_s` (overlaps stopped time), `recovery_count`, `block_count`,
`expected_s`, `actual_vs_expected` (time of the legs that have an expected time over their
expected sum), `pass_durations_s` (`[{pass, legs, duration_s}]`).

#### Per-map operating statistics

`GET /api/v1/runs` also takes `map=<name>`: runs whose `map_id` (the map of the robot's open
session at the run's start) is that map.

**`GET /api/v1/maps/{map_id}/run-stats?from=&to=&robot=`** -- operating metrics of the map's
non-archived runs that started in `[from, to)` (ISO-8601 with time zone; default: the last 30
days), optionally of one robot. 422 for `from >= to`; an unknown map answers zeros/nulls. The headline, `efficiency` and
`series` are all cached 120 s (`EFFICIENCY_CACHE_TTL_S`; the headline used to be uncached).

```
{"map_id", "window": {"from", "to"}, "robot": null | str,
 "runs": {"finished", "completed", "failed", "timeout", "aborted", "canceled", "running"},
 "success_rate": float|null,        // completed / (completed+failed+timeout+aborted)
 "intervention": {"count", "cancels", "teleop", "takeovers", "rate_per_hour": float|null,
                  "free_success_rate": float|null, "free_runs"},
 "hours": float,                    // operating time, see docs/map_run_stats_2026-10-14.md
 "duration_s": {"median", "p95", "n"},   // COMPLETED runs
 "timeout": {"count", "median_s"},
 "abort_rate": float|null,          // (failed+timeout+aborted) / finished
 "recovery": {"legs", "legs_with_recovery", "median_s", "p95_s", "total_s",
              "share_of_time", "share_of_legs"},
 "safety": {"by_class": {"safety_reflex", "blocked", "skipped", "link_loss"},
            "by_code": {"<code>[:<error_type>]": n}, "total"},
 "by_cause": {"<abort_cause>": n},  // finished runs that did not complete
 "efficiency": null | {              // path driven vs the lattice plan path (cached 120 s)
   "basis": "trajectory", "runs", "legs",   // legs planned by a stored plan trajectory, with a track
   "driven_m", "planned_m",
   "ratio": float|null,            // planned/driven: 1.0 = drove the plan, lower = detours (null under 3 legs)
   "extra_pct": float|null,        // (driven/planned - 1) * 100
   "p10", "median", "p90",         // of the per-leg ratio (p10 = the worst-tail legs)
   "excluding_recovery": {"driven_m", "recovery_m", "ratio", "extra_pct"},
   "time": {"legs", "actual_s", "expected_s", "ratio", "ratio_excl_recovery"},   // expected/actual, all legs
   "by_plan": {"trajectory": {"legs", "driven_m", "planned_m", "ratio", "extra_pct"},
               "straight":   {...}},      // legs with a track but no usable trajectory (lower-bound plan)
   "source": {"track_legs", "leg_only", "trajectory_legs", "straight_legs", "stale_trajectory_legs"},
   "excluded": {"no_track", "gap", "incomplete_track", "no_plan", "short"},
   "data_quality": {"legs_read", "truncated", "notes": [str]}},
 "data_quality": {"runs_without_map", "teleop_recorded": bool, "hours_excluded_runs",
                  "max_run_s", "notes": [str]}}
```

Opt-in `series=day|run` adds a `series` block of arrays aligned with `t` (one point per UTC day of the window, at most
120, or the newest 60 finished runs, oldest first); without the parameter nothing changes. Shapes:
`docs/map_series_2026-10-15.md`. 422 for another value or a window over 120 days with `series=day`.

Definitions, the hung-run rule and the teleop signal: `docs/map_run_stats_2026-10-14.md`. `efficiency`
(null when the map has no legs or the computation failed; a note says which): `docs/path_efficiency_2026-10-15.md`.

**`GET /api/v1/maps/{map_id}/events-summary?from=&to=&robot=&limit=10`** -- the most usual events
of the map and its safety events, as episodes (a repeated error counts once). Window `[from, to)`,
default the last 30 days; `limit` 1-50 per `top` list. 422 for `from >= to` or a bad `limit`;
an unknown map answers zeros. Cached 30 s, `series` included (it was 120 s). Opt-in `series=day` adds `series: {bucket, t, total, safety,
by_code}` per UTC day (`docs/map_series_2026-10-15.md`).

```
{"map_id", "window": {"from", "to"}, "robot": null | str,
 "total_episodes": int,
 "by_severity": {"info"|"warning"|"error": n},
 "top": [{"code": "NAV.RECOVERY_ENTERED" | "ROBOT.ERROR_RAISED:<error_type>" | ...,
          "severity", "class": "safety_reflex|blocked|skipped|link_loss|recovery|error|other",
          "count", "share": float|null, "last_ts": ISO-8601 UTC, "runs": n, "robots": [str]}],
 "safety": {"episodes", "by_class": {"safety_reflex", "blocked", "skipped", "link_loss"},
            "top": [same shape]},
 "attribution": {"by_run", "by_session", "unattributed"},
 "data_quality": {"notes": [str]}}
```

**`GET /api/v1/maps/{map_id}/events?from=&to=&code=&class=&scope=&robot=&limit=50&cursor=`** -- the
same events one by one, newest first. `code` is one event code (`ROBOT.ERROR_RAISED[:error_type]`),
`class` one of the classes above, `scope=safety` restricts to the four safety classes (`class` then
filters further; a non-safety class gives an empty list), `limit` 1-200. Pass `next` back as `cursor`
(keyset on `(ts, event_id)`); 422 for unknown code/class/scope or a bad cursor.

```
{"items": [{"ts", "event_id", "code", "error_type": str|null, "severity", "class", "robot",
            "run_id": uuid|null, "session_id": uuid|null, "mission_name": str|null,
            "detail": str|null, "attribution": "run"|"session"}],
 "next": str|null}
```

Attribution, episode rule and the event universe: `docs/map_events_2026-10-14.md`. run-stats'
`safety` uses the same helper.

**`TELEOP.SESSION_STARTED`** (event, info, source `api`): a robot obtained its LiveKit room
token from the self-hosted token service. Payload `{role, room_hash, map_id, session_id,
mission_name}` (no token, room name or identity); `run_id` is the robot's RUNNING mission.
The token service tells the API over the loopback-only `POST /internal/teleop/token-issued`
(not under `/api/`, not proxied).

---

### Detection Results

#### `GET /api/v1/detection_results`

List all detection results from robot object detectors.

**Response:**
```json
[
  {
    "name": "detection_carter01_20240115",
    "status": {
      "detected_objects": [
        {
          "object_id": 1,
          "class_id": "person",
          "bbox2d": {
            "center": { "x": 320, "y": 240, "theta": 0 },
            "size_x": 100,
            "size_y": 200
          }
        }
      ]
    }
  }
]
```

---

#### `GET /api/v1/detection_results/{name}`

Get specific detection results.

**Response:**
```json
{
  "name": "detection_carter01_20240115",
  "status": {
    "detected_objects": [ ... ]
  }
}
```

---

#### `DELETE /api/v1/detection_results/{name}`

Delete detection results.

**Response:**
```json
{
  "success": true,
  "message": "Detection results deleted"
}
```

---

## WebSocket Endpoints

WebSocket connections provide real-time updates for maps, missions, and robots. All WebSocket endpoints are proxied through the API Delegation Service.

### Connection Pattern

```javascript
const ws = new WebSocket('ws://localhost:8000/ws/{endpoint}');

ws.onopen = () => {
  console.log('Connected');
};

ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  console.log('Update received:', data);
};

ws.onerror = (error) => {
  console.error('WebSocket error:', error);
};

ws.onclose = () => {
  console.log('Connection closed');
};
```

---

### `WS /ws/map/{map_id}`

Subscribe to real-time map updates for a specific map.

**Path Parameters:**
- `map_id` (string): Map identifier

**Message Types:**

#### Node Added
```json
{
  "type": "node_added",
  "map_id": "warehouse_floor_1",
  "node": {
    "node_id": 1001,
    "x": 10.5,
    "y": 20.3,
    "yaw": 1.57,
    "metadata": {
      "robot_id": "carter01",
      "timestamp": "2024-01-15T10:30:00Z"
    }
  },
  "edges": [
    {
      "from_node_id": 1001,
      "to_node_id": 1000,
      "metadata": {
        "distance": 5.2,
        "created_at": "2024-01-15T10:30:00Z"
      }
    }
  ],
  "timestamp": "2024-01-15T10:30:00Z"
}
```

**Use Cases:**
- Display real-time map building progress
- Update map visualization as robots explore
- Show new nodes and edges as they are created

**Example:**
```javascript
const mapWs = new WebSocket('ws://localhost:8000/ws/map/warehouse_floor_1');

mapWs.onmessage = (event) => {
  const update = JSON.parse(event.data);

  if (update.type === 'node_added') {
    // Add node to map visualization
    addNodeToMap(update.node);

    // Add edges to map visualization
    update.edges.forEach(edge => addEdgeToMap(edge));
  }
};
```

**Backend Service:** Graph Builder Service (proxied through API Delegation)

---

### `WS /ws/mission/{mission_name}`

Subscribe to real-time mission status updates.

**Path Parameters:**
- `mission_name` (string): Mission identifier

**Message Format:**
```json
{
  "type": "mission_update",
  "mission_name": "delivery_mission_001",
  "status": {
    "state": "RUNNING",
    "progress": 0.65,
    "current_node": "navigate_to_target",
    "node_status": {
      "root": {
        "state": "RUNNING",
        "progress": 0.65
      },
      "navigate_to_target": {
        "state": "RUNNING",
        "progress": 0.65
      }
    }
  },
  "timestamp": "2024-01-15T10:35:00Z"
}
```

**Mission States:**
- `PENDING`: Mission created but not started
- `RUNNING`: Mission in progress
- `COMPLETED`: Mission completed successfully
- `FAILED`: Mission failed
- `CANCELED`: Mission was cancelled

**Example:**
```javascript
const missionWs = new WebSocket('ws://localhost:8000/ws/mission/delivery_mission_001');

missionWs.onmessage = (event) => {
  const update = JSON.parse(event.data);

  // Update progress bar
  document.getElementById('progress').value = update.status.progress;

  // Update status text
  document.getElementById('status').textContent = update.status.state;

  // Check if mission completed
  if (update.status.state === 'COMPLETED') {
    console.log('Mission completed successfully!');
    missionWs.close();
  }
};
```

**Backend Service:** Mission Dispatcher (proxied through API Delegation)

---

### `WS /ws/robot/{robot_name}`

Subscribe to real-time robot status updates.

**Path Parameters:**
- `robot_name` (string): Robot identifier

**Message Format:**
```json
{
  "type": "robot_update",
  "robot_name": "carter01",
  "status": {
    "pose": {
      "x": 12.3,
      "y": 21.5,
      "theta": 1.60
    },
    "online": true,
    "battery_level": 82.3,
    "state": "ON_TASK",
    "errors": {}
  },
  "timestamp": "2024-01-15T10:35:00Z"
}
```

**Robot States:**
- `IDLE`: Robot is idle and available
- `ON_TASK`: Robot is executing a mission
- `CHARGING`: Robot is charging
- `MAP_DEPLOYMENT`: Robot is deploying a map
- `TELEOP`: Robot is in teleoperation mode

**Example:**
```javascript
const robotWs = new WebSocket('ws://localhost:8000/ws/robot/carter01');

robotWs.onmessage = (event) => {
  const update = JSON.parse(event.data);

  // Update robot position on map
  updateRobotPosition(update.status.pose);

  // Update battery indicator
  document.getElementById('battery').textContent =
    `${update.status.battery_level.toFixed(1)}%`;

  // Check for errors
  if (Object.keys(update.status.errors).length > 0) {
    console.error('Robot errors:', update.status.errors);
  }
};
```

**Backend Service:** Mission Dispatcher (proxied through API Delegation)

---

## Data Schemas

### Robot Object

```typescript
interface RobotObject {
  name: string;
  labels: string[];
  battery: {
    critical_level: number;
    recommended_minimum?: number;
    recommended_maximum?: number;
  };
  heartbeat_timeout: number;  // seconds
  switch_teleop: boolean;
  status: {
    pose: {
      x: number;
      y: number;
      theta: number;  // radians
    };
    software_version: {
      os: string;
      app: string;
    };
    hardware_version: {
      manufacturer: string;
      serial_number: string;
    };
    factsheet: {
      agv_class: string;
      speed_max: number;
      length: number;   // footprint, metres (physicalParameters.length); -1 until reported
      width: number;    // footprint, metres (physicalParameters.width); -1 until reported
      height: number;   // metres (physicalParameters.heightMax); -1 until reported
    };
    online: boolean;
    battery_level: number;  // 0-100
    position_initialized?: boolean | null;  // VDA5050 agvPosition.positionInitialized
    localization_score?: number | null;     // 0..1, informational (provisional)
    approx_position?: {                      // from MQTT approx_position, display only
      latitude: number;
      longitude: number;
      accuracy_m?: number;
      fix_quality?: string;
      source: string;       // 'gnss'
      stamp?: string;
      stored_at?: string;   // server time of the last stored write
    } | null;
    state: 'IDLE' | 'ON_TASK' | 'CHARGING' | 'MAP_DEPLOYMENT' | 'TELEOP';
    info_messages?: object;
    errors: object;
  };
  lifecycle: 'ALIVE' | 'DELETED' | 'PENDING_DELETE';
}
```

---

### Mission Object

```typescript
interface MissionObject {
  name: string;
  robot: string;
  mission_tree: MissionNode[];
  timeout: number;  // seconds
  deadline?: string;  // ISO 8601 timestamp
  repeat: number;  // total runs of the whole mission_tree; 1 = once (default), 0 = until cancelled
  then_run?: string | null;  // mission to start (as a copy) after this one and all repeats complete
  needs_canceled: boolean;
  status: {
    passes_completed: number;  // finished passes so far (dispatcher-owned): "lap 2 of 3"
    state: 'PENDING' | 'RUNNING' | 'COMPLETED' | 'FAILED' | 'CANCELED';
    progress: number;  // 0.0 - 1.0
    start_time?: string;  // ISO 8601 timestamp
    end_time?: string;  // ISO 8601 timestamp
    failure_category?: 'ROBOT_APP' | 'TIMEOUT' | 'DEADLINE' | 'CANCELED';
    node_status: {
      [nodeName: string]: {
        state: string;
        progress: number;
        error_msg?: string;
      };
    };
  };
  lifecycle: 'ALIVE' | 'DELETED' | 'PENDING_DELETE';
}
```

---

### Repeating, chaining and waiting

- **`repeat`**: `N` runs the mission N times in total, `0` until it is cancelled. When a
  pass completes and more remain, the dispatcher runs the *same* mission object again
  in place: node statuses reset to `PENDING`, `status.run_id` renewed (so the VDA5050
  order/node ids of each pass are unique), `status.passes_completed` incremented, and
  the timeout re-armed per pass. The robot is not idled between passes. A cancel, a
  failure or a timeout in any pass ends the whole repeat.
- **`then_run`**: when the mission has completed (last pass included) the dispatcher
  creates a new mission named `{then_run}-run-{unix_ms}` as a copy of the named mission
  (same `robot`, `mission_tree`, `timeout`, `mode`, `register_map`, `repeat` and
  `then_run`) and it is queued behind whatever is already waiting. A missing mission or
  one for another robot is logged and skipped; the finished mission is never failed by
  it. Cycles (A then B then A) are allowed; cancelling the running mission breaks them.
- **`wait` action**: `{"action": {"action_type": "wait", "action_parameters":
  {"seconds": 5}}}` (`0 < seconds <= 3600`). The dispatcher runs it as a timer between
  the surrounding nodes; nothing is sent to the robot. Cancelling during a wait cancels
  the mission at once (there is no robot order to cancel). The mission timeout keeps
  running through it.
- **Several route nodes in one mission** (needed to put a wait mid-route: route, wait,
  route). The robot's `missionStatus: "completed"` now completes only the node of the
  order it came with, and the mission completes when the whole tree does.

### Mission Node Types

```typescript
interface MissionNode {
  name?: string;
  parent: string;  // Parent node name (use "root" for top-level)

  // Node type (only one should be specified)
  route?: {
    waypoints: Array<{
      x: number;
      y: number;
      theta: number;
      map_id?: string;
      allowedDeviationXY?: number;
      allowedDeviationTheta?: number;
      node_id?: string;
      skippable?: boolean;          // robot may skip it when blocked (needs a corridor width)
      max_wait_s?: number;          // seconds held before it counts as blocked
      corridor_width_m?: number;    // per side; the planner fills it from the scene graph
      origin?: 'user' | 'planned';  // 'planned': inserted by the planner
    }>;
  };

  move?: {
    distance?: number;
    rotation?: number;
    direction?: 'forward' | 'backward';
  };

  action?: {
    action_type: string;
    action_parameters: object;
  };

  notify?: {
    url: string;
    method: 'GET' | 'POST';
    payload?: object;
  };

  selector?: object;  // Chooses first successful child
  sequence?: object;  // Executes children in order
  constant?: {
    success: boolean;
  };
}
```

**Common Action Types:**
- `capture_image`: Capture image from camera
- `wait`: Wait for specified duration
- `explore`: Explore environment
- `dock`: Dock at charging station
- `undock`: Undock from charging station

---

### Detection Results Object

```typescript
interface DetectionResultsObject {
  name: string;
  status: {
    detected_objects: Array<{
      object_id: number;
      class_id: string;
      bbox2d?: {
        center: {
          x: number;
          y: number;
          theta: number;
        };
        size_x: number;
        size_y: number;
      };
      bbox3d?: {
        center: {
          position: { x: number; y: number; z: number };
          orientation: { w: number; x: number; y: number; z: number };
        };
        size_x: number;
        size_y: number;
        size_z: number;
      };
    }>;
  };
}
```

---

### Map Node

```typescript
interface MapNode {
  node_id: number | string;
  x: number;  // meters
  y: number;  // meters
  yaw: number;  // radians (also called theta)
  metadata?: {
    map_id?: string;
    timestamp?: string;
    robot_id?: string;
    [key: string]: any;
  };
}
```

---

### Map Edge

```typescript
interface MapEdge {
  from_node_id: number | string;
  to_node_id: number | string;
  metadata?: {
    distance?: number;  // meters
    weight?: number;
    created_at?: string;
    [key: string]: any;
  };
}
```

---

## Error Handling

### HTTP Error Codes

The API uses standard HTTP status codes:

| Code | Meaning | Description |
|------|---------|-------------|
| 200 | OK | Request succeeded |
| 201 | Created | Resource created successfully |
| 204 | No Content | Request succeeded with no response body |
| 400 | Bad Request | Invalid request (missing fields, validation error) |
| 404 | Not Found | Resource not found |
| 500 | Internal Server Error | Server error |
| 503 | Service Unavailable | Service not initialized or backend unavailable |

---

### Error Response Format

All error responses follow this format:

```json
{
  "detail": "Error message describing what went wrong"
}
```

**Examples:**

```json
// 404 Not Found
{
  "detail": "Robot 'carter99' not found"
}

// 400 Bad Request
{
  "detail": "Missing required field: name"
}

// 503 Service Unavailable
{
  "detail": "Service not initialized"
}
```

---

### Error Handling Best Practices

#### 1. Always Check Response Status

```javascript
async function getRobot(robotName) {
  const response = await fetch(`http://localhost:8000/api/v1/robots/${robotName}`);

  if (!response.ok) {
    const error = await response.json();
    throw new Error(`Failed to get robot: ${error.detail}`);
  }

  return await response.json();
}
```

#### 2. Handle Network Errors

```javascript
async function navigateRobot(robotName, targetX, targetY) {
  try {
    const response = await fetch('http://localhost:8000/api/v1/navigate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        robot_name: robotName,
        target_x: targetX,
        target_y: targetY
      })
    });

    if (!response.ok) {
      const error = await response.json();
      console.error('Navigation failed:', error.detail);
      return null;
    }

    return await response.json();
  } catch (error) {
    console.error('Network error:', error);
    return null;
  }
}
```

#### 3. WebSocket Error Handling

```javascript
function connectToMissionUpdates(missionName) {
  const ws = new WebSocket(`ws://localhost:8000/ws/mission/${missionName}`);

  ws.onerror = (error) => {
    console.error('WebSocket error:', error);
  };

  ws.onclose = (event) => {
    if (event.code !== 1000) {  // 1000 = normal closure
      console.error('WebSocket closed unexpectedly:', event.code, event.reason);

      // Implement reconnection logic
      setTimeout(() => {
        console.log('Attempting to reconnect...');
        connectToMissionUpdates(missionName);
      }, 5000);
    }
  };

  return ws;
}
```

#### 4. Retry Logic for Transient Failures

```javascript
async function fetchWithRetry(url, options = {}, maxRetries = 3) {
  for (let i = 0; i < maxRetries; i++) {
    try {
      const response = await fetch(url, options);

      if (response.ok) {
        return response;
      }

      // Don't retry client errors (4xx)
      if (response.status >= 400 && response.status < 500) {
        return response;
      }

      // Retry server errors (5xx)
      console.log(`Attempt ${i + 1} failed, retrying...`);
      await new Promise(resolve => setTimeout(resolve, 1000 * (i + 1)));

    } catch (error) {
      if (i === maxRetries - 1) throw error;
      console.log(`Attempt ${i + 1} failed, retrying...`);
      await new Promise(resolve => setTimeout(resolve, 1000 * (i + 1)));
    }
  }
}
```

---

## Integration Patterns

### Pattern 1: Complete Navigation Workflow

This pattern demonstrates a complete navigation workflow from start to finish.

```javascript
class RobotNavigationClient {
  constructor(baseUrl = 'http://localhost:8000') {
    this.baseUrl = baseUrl;
    this.missionWs = null;
  }

  async navigateToTarget(robotName, targetX, targetY) {
    // Step 1: Verify robot exists and is available
    const robot = await this.getRobot(robotName);
    if (!robot) {
      throw new Error(`Robot ${robotName} not found`);
    }

    if (robot.status.state !== 'IDLE') {
      throw new Error(`Robot ${robotName} is not idle (current state: ${robot.status.state})`);
    }

    // Step 2: Request navigation
    const response = await fetch(`${this.baseUrl}/api/v1/navigate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        robot_name: robotName,
        target_x: targetX,
        target_y: targetY,
        timeout_seconds: 300
      })
    });

    if (!response.ok) {
      const error = await response.json();
      throw new Error(`Navigation failed: ${error.detail}`);
    }

    const result = await response.json();
    const missionName = result.mission_name;

    // Step 3: Monitor mission progress via WebSocket
    return new Promise((resolve, reject) => {
      this.missionWs = new WebSocket(`ws://localhost:8000/ws/mission/${missionName}`);

      this.missionWs.onmessage = (event) => {
        const update = JSON.parse(event.data);

        console.log(`Mission progress: ${(update.status.progress * 100).toFixed(1)}%`);

        if (update.status.state === 'COMPLETED') {
          console.log('Navigation completed successfully!');
          this.missionWs.close();
          resolve(update);
        } else if (update.status.state === 'FAILED') {
          console.error('Navigation failed:', update.status.failure_category);
          this.missionWs.close();
          reject(new Error(`Mission failed: ${update.status.failure_category}`));
        } else if (update.status.state === 'CANCELED') {
          console.log('Navigation was canceled');
          this.missionWs.close();
          reject(new Error('Mission canceled'));
        }
      };

      this.missionWs.onerror = (error) => {
        console.error('WebSocket error:', error);
        reject(error);
      };
    });
  }

  async getRobot(robotName) {
    const response = await fetch(`${this.baseUrl}/api/v1/robots/${robotName}`);
    if (!response.ok) return null;
    return await response.json();
  }

  async cancelNavigation(missionName) {
    const response = await fetch(`${this.baseUrl}/api/v1/missions/${missionName}/cancel`, {
      method: 'POST'
    });
    return response.ok;
  }
}

// Usage
const client = new RobotNavigationClient();

client.navigateToTarget('carter01', 10.5, 20.3)
  .then(result => console.log('Navigation completed:', result))
  .catch(error => console.error('Navigation error:', error));
```

---

### Pattern 2: Real-Time Map Visualization

This pattern shows how to build a real-time map visualization that updates as robots explore.

```javascript
class MapVisualization {
  constructor(mapId, baseUrl = 'http://localhost:8000') {
    this.mapId = mapId;
    this.baseUrl = baseUrl;
    this.nodes = new Map();
    this.edges = new Set();
    this.mapWs = null;
  }

  async initialize() {
    // Step 1: Load existing map data
    const response = await fetch(`${this.baseUrl}/api/v1/map/load`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ map_id: this.mapId })
    });

    if (!response.ok) {
      throw new Error('Failed to load map');
    }

    const mapData = await response.json();
    console.log(`Loaded map with ${mapData.stats.node_count} nodes`);

    // Step 2: Connect to real-time updates
    this.connectToUpdates();
  }

  connectToUpdates() {
    this.mapWs = new WebSocket(`ws://localhost:8000/ws/map/${this.mapId}`);

    this.mapWs.onmessage = (event) => {
      const update = JSON.parse(event.data);

      if (update.type === 'node_added') {
        this.addNode(update.node);

        // Add edges
        update.edges.forEach(edge => {
          this.addEdge(edge);
        });

        // Trigger visualization update
        this.render();
      }
    };

    this.mapWs.onerror = (error) => {
      console.error('Map WebSocket error:', error);
    };

    this.mapWs.onclose = () => {
      console.log('Map WebSocket closed, reconnecting...');
      setTimeout(() => this.connectToUpdates(), 5000);
    };
  }

  addNode(node) {
    this.nodes.set(node.node_id, node);
    console.log(`Added node ${node.node_id} at (${node.x}, ${node.y})`);
  }

  addEdge(edge) {
    const edgeKey = `${edge.from_node_id}-${edge.to_node_id}`;
    this.edges.add(edgeKey);
    console.log(`Added edge: ${edge.from_node_id} -> ${edge.to_node_id}`);
  }

  render() {
    // Implement your visualization logic here
    // This could use Canvas, SVG, WebGL, or a library like D3.js
    console.log(`Rendering map with ${this.nodes.size} nodes and ${this.edges.size} edges`);
  }

  destroy() {
    if (this.mapWs) {
      this.mapWs.close();
    }
  }
}

// Usage
const mapViz = new MapVisualization('warehouse_floor_1');
mapViz.initialize()
  .then(() => console.log('Map visualization initialized'))
  .catch(error => console.error('Initialization error:', error));
```

---

### Pattern 3: Fleet Management Dashboard

This pattern demonstrates monitoring multiple robots simultaneously.

```javascript
class FleetDashboard {
  constructor(baseUrl = 'http://localhost:8000') {
    this.baseUrl = baseUrl;
    this.robots = new Map();
    this.robotConnections = new Map();
  }

  async initialize() {
    // Load all robots
    const response = await fetch(`${this.baseUrl}/api/v1/robots`);
    const robots = await response.json();

    // Subscribe to each robot's updates
    robots.forEach(robot => {
      this.addRobot(robot);
      this.subscribeToRobot(robot.name);
    });
  }

  addRobot(robot) {
    this.robots.set(robot.name, robot);
    this.updateDashboard();
  }

  subscribeToRobot(robotName) {
    const ws = new WebSocket(`ws://localhost:8000/ws/robot/${robotName}`);

    ws.onmessage = (event) => {
      const update = JSON.parse(event.data);

      // Update robot data
      const robot = this.robots.get(robotName);
      if (robot) {
        robot.status = update.status;
        this.updateDashboard();
      }
    };

    ws.onerror = (error) => {
      console.error(`WebSocket error for ${robotName}:`, error);
    };

    this.robotConnections.set(robotName, ws);
  }

  updateDashboard() {
    const stats = {
      total: this.robots.size,
      online: 0,
      idle: 0,
      on_task: 0,
      charging: 0,
      low_battery: 0
    };

    this.robots.forEach(robot => {
      if (robot.status.online) stats.online++;
      if (robot.status.state === 'IDLE') stats.idle++;
      if (robot.status.state === 'ON_TASK') stats.on_task++;
      if (robot.status.state === 'CHARGING') stats.charging++;
      if (robot.status.battery_level < 20) stats.low_battery++;
    });

    console.log('Fleet Status:', stats);

    // Update UI here
    this.renderDashboard(stats);
  }

  renderDashboard(stats) {
    // Implement your dashboard UI update logic
    console.log(`Dashboard: ${stats.online}/${stats.total} robots online`);
  }

  async sendRobotToLocation(robotName, x, y) {
    const response = await fetch(`${this.baseUrl}/api/v1/navigate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        robot_name: robotName,
        target_x: x,
        target_y: y
      })
    });

    if (!response.ok) {
      const error = await response.json();
      console.error('Navigation failed:', error.detail);
      return null;
    }

    return await response.json();
  }

  destroy() {
    // Close all WebSocket connections
    this.robotConnections.forEach(ws => ws.close());
    this.robotConnections.clear();
  }
}

// Usage
const dashboard = new FleetDashboard();
dashboard.initialize()
  .then(() => console.log('Fleet dashboard initialized'))
  .catch(error => console.error('Initialization error:', error));
```

---

### Pattern 4: Image Retrieval and Display

This pattern shows how to retrieve and display images from nodes.

```javascript
async function displayNodeImage(mapId, nodeId, imageId = null) {
  const baseUrl = 'http://localhost:8000';

  // Build URL
  let url = `${baseUrl}/api/v1/images/${mapId}/${nodeId}`;
  if (imageId) {
    url += `?image_id=${imageId}`;
  }

  try {
    const response = await fetch(url);

    if (!response.ok) {
      console.error('Image not found');
      return null;
    }

    // Get image as blob
    const blob = await response.blob();

    // Create object URL
    const imageUrl = URL.createObjectURL(blob);

    // Display in img element
    const imgElement = document.getElementById('node-image');
    imgElement.src = imageUrl;

    // Clean up object URL when done
    imgElement.onload = () => {
      URL.revokeObjectURL(imageUrl);
    };

    return imageUrl;

  } catch (error) {
    console.error('Failed to load image:', error);
    return null;
  }
}

// Usage
displayNodeImage('warehouse_floor_1', '1001', 'front_camera');
```

---

## Backend Service Details

### Graph Database Service

**Purpose:** Manages topological maps with persistent storage (ArangoDB) and fast spatial queries (R-tree)

**Key Features:**
- **Dual Storage:** ArangoDB for persistence + R-tree for ultra-fast spatial queries (10-100 μs)
- **Spatial Queries:** k-NN search, radius search
- **Multi-Map Support:** Manage multiple maps simultaneously
- **Automatic Synchronization:** R-tree automatically rebuilds when nodes are added

**Performance:**
- k-NN query: ~2-3ms total (including HTTP overhead)
- Radius search: ~2-3ms total
- Node insertion: ~10-20ms (includes ArangoDB write + R-tree update)

---

### Image Database Service

**Purpose:** Stores robot camera images using MinIO object storage

**Key Features:**
- **Object Storage:** MinIO-backed storage for scalability
- **Multi-Map Support:** Organize images by map and node
- **Metadata:** Associate images with nodes and timestamps

**Storage Structure:**
```
bucket: {map_id}
  └── {node_id}/
      ├── front_camera.jpg
      ├── back_camera.jpg
      └── ...
```

---

### Mission Planner Service

**Purpose:** Plans navigation paths and creates missions

**Key Features:**
- **Path Planning:** Uses graph database to find optimal paths
- **Mission Creation:** Generates VDA5050-compatible mission trees
- **Automatic Submission:** Submits missions to Mission Dispatcher

**Workflow:**
1. Receive navigation request (target coordinates)
2. Find nearest node to robot's current position
3. Find nearest node to target position
4. Compute path using graph database
5. Create mission with waypoints
6. Submit to Mission Dispatcher

---

### Mission Dispatcher Service

**Purpose:** Executes missions using VDA5050 protocol via MQTT

**Key Features:**
- **VDA5050 Protocol:** Industry-standard AGV communication protocol
- **MQTT Communication:** Real-time bidirectional communication with robots
- **Mission Lifecycle:** Manages mission states (PENDING → RUNNING → COMPLETED/FAILED)
- **Robot Status Tracking:** Monitors robot position, battery, errors

**MQTT Topics:**
- `uagv/v2/RobotCompany/{robot_name}/order`: Send missions to robot
- `uagv/v2/RobotCompany/{robot_name}/state`: Receive robot status updates
- `uagv/v2/RobotCompany/{robot_name}/factsheet`: Receive robot capabilities

**Note:** MQTT is not directly exposed to client applications. Clients interact via REST API and WebSockets.

---

### Graph Builder Service

**Purpose:** Builds topological maps from robot sensor data

**Key Features:**
- **MQTT Integration:** Subscribes to robot node updates
- **Image Storage:** Saves robot camera images
- **Spatial Indexing:** Uses R-tree for fast nearby node queries (5m radius default)
- **Traversability Checking:** Validates edges using Similarity Service
- **Bidirectional Edges:** Automatically creates edges in both directions

**Workflow:**
1. Receive MQTT node update from robot
2. Save images to Image Database
3. Find nearby nodes (radius search, default 5m)
4. Check traversability with Similarity Service
5. Create bidirectional edges to traversable nodes
6. Save node to Graph Database
7. Broadcast update via WebSocket

---

### Similarity Service

**Purpose:** Validates edge traversability between nodes

**Key Features:**
- **Distance Calculation:** Computes Euclidean distance between nodes
- **Traversability Threshold:** Configurable distance threshold (default: 10m)
- **Relative Pose:** Computes relative position and orientation

**Algorithm:**
```python
distance = sqrt((x2 - x1)^2 + (y2 - y1)^2)
traversable = distance <= threshold
```

---

## Rate Limiting and Performance

**Current Status:** No rate limiting implemented (development mode)

**Recommended Limits for Production:**
- REST API: 100 requests/minute per client
- WebSocket connections: 10 concurrent connections per client
- Image downloads: 50 requests/minute per client

**Performance Characteristics:**
- REST API latency: 10-50ms (typical)
- WebSocket message latency: <10ms
- Image download: 100-500ms (depends on image size)
- Map loading: 100-1000ms (depends on map size)

---

## Security Considerations

**Current Implementation:** Development mode with no authentication

**Production Recommendations:**

1. **Authentication:**
   - Implement API key authentication for REST endpoints
   - Use JWT tokens for WebSocket connections
   - Integrate with OAuth 2.0 / OIDC providers

2. **Authorization:**
   - Role-based access control (RBAC)
   - Separate read/write permissions
   - Robot-specific access controls

3. **Transport Security:**
   - Enable TLS/SSL for all connections
   - Use WSS (WebSocket Secure) for WebSocket connections
   - Implement certificate pinning for mobile clients

4. **Data Validation:**
   - Validate all input data
   - Sanitize user-provided strings
   - Implement request size limits

5. **Network Security:**
   - Deploy behind reverse proxy (nginx, Traefik)
   - Implement firewall rules
   - Use VPN for robot-to-cloud communication

---

## Appendix: Complete Example Application

Here's a complete example of a simple web application that demonstrates the key integration patterns:

```html
<!DOCTYPE html>
<html>
<head>
  <title>Robot Fleet Manager</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 20px; }
    .robot-card { border: 1px solid #ccc; padding: 10px; margin: 10px 0; }
    .online { color: green; }
    .offline { color: red; }
    button { margin: 5px; padding: 5px 10px; }
  </style>
</head>
<body>
  <h1>Robot Fleet Manager</h1>

  <div id="robots"></div>

  <h2>Send Robot to Location</h2>
  <select id="robot-select"></select>
  <input type="number" id="target-x" placeholder="X" step="0.1">
  <input type="number" id="target-y" placeholder="Y" step="0.1">
  <button onclick="navigate()">Navigate</button>

  <h2>Mission Status</h2>
  <div id="mission-status"></div>

  <script>
    const BASE_URL = 'http://localhost:8000';
    let robots = new Map();
    let robotConnections = new Map();
    let missionWs = null;

    // Initialize
    async function init() {
      await loadRobots();
      subscribeToRobots();
    }

    // Load all robots
    async function loadRobots() {
      const response = await fetch(`${BASE_URL}/api/v1/robots`);
      const robotList = await response.json();

      robotList.forEach(robot => {
        robots.set(robot.name, robot);
      });

      updateUI();
    }

    // Subscribe to robot updates
    function subscribeToRobots() {
      robots.forEach((robot, name) => {
        const ws = new WebSocket(`ws://localhost:8000/ws/robot/${name}`);

        ws.onmessage = (event) => {
          const update = JSON.parse(event.data);
          const robot = robots.get(name);
          if (robot) {
            robot.status = update.status;
            updateUI();
          }
        };

        robotConnections.set(name, ws);
      });
    }

    // Update UI
    function updateUI() {
      const robotsDiv = document.getElementById('robots');
      const select = document.getElementById('robot-select');

      robotsDiv.innerHTML = '';
      select.innerHTML = '';

      robots.forEach((robot, name) => {
        // Robot card
        const card = document.createElement('div');
        card.className = 'robot-card';
        card.innerHTML = `
          <h3>${name}</h3>
          <p>Status: <span class="${robot.status.online ? 'online' : 'offline'}">
            ${robot.status.online ? 'Online' : 'Offline'}
          </span></p>
          <p>State: ${robot.status.state}</p>
          <p>Battery: ${robot.status.battery_level.toFixed(1)}%</p>
          <p>Position: (${robot.status.pose.x.toFixed(2)}, ${robot.status.pose.y.toFixed(2)})</p>
        `;
        robotsDiv.appendChild(card);

        // Select option
        const option = document.createElement('option');
        option.value = name;
        option.textContent = name;
        select.appendChild(option);
      });
    }

    // Navigate robot
    async function navigate() {
      const robotName = document.getElementById('robot-select').value;
      const targetX = parseFloat(document.getElementById('target-x').value);
      const targetY = parseFloat(document.getElementById('target-y').value);

      if (!robotName || isNaN(targetX) || isNaN(targetY)) {
        alert('Please fill all fields');
        return;
      }

      const response = await fetch(`${BASE_URL}/api/v1/navigate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          robot_name: robotName,
          target_x: targetX,
          target_y: targetY
        })
      });

      if (!response.ok) {
        const error = await response.json();
        alert(`Navigation failed: ${error.detail}`);
        return;
      }

      const result = await response.json();
      subscribeToMission(result.mission_name);
    }

    // Subscribe to mission updates
    function subscribeToMission(missionName) {
      if (missionWs) {
        missionWs.close();
      }

      missionWs = new WebSocket(`ws://localhost:8000/ws/mission/${missionName}`);

      missionWs.onmessage = (event) => {
        const update = JSON.parse(event.data);
        const statusDiv = document.getElementById('mission-status');

        statusDiv.innerHTML = `
          <h3>Mission: ${missionName}</h3>
          <p>State: ${update.status.state}</p>
          <p>Progress: ${(update.status.progress * 100).toFixed(1)}%</p>
        `;

        if (update.status.state === 'COMPLETED') {
          statusDiv.innerHTML += '<p style="color: green;">✓ Mission completed!</p>';
          missionWs.close();
        } else if (update.status.state === 'FAILED') {
          statusDiv.innerHTML += '<p style="color: red;">✗ Mission failed</p>';
          missionWs.close();
        }
      };
    }

    // Start application
    init();
  </script>
</body>
</html>
```

Save this as `index.html` and open in a browser to see a working fleet management interface!

---

## Support and Resources

- **API Base URL:** `http://localhost:8000`
- **WebSocket Base URL:** `ws://localhost:8000`
- **Health Check:** `GET /health`
- **Service Info:** `GET /`

For questions or issues, please refer to the service logs or contact the backend development team.



#### Scene-graph parameters: `scene_graph_params` and `robot_name` on `POST /api/v1/maps`

`POST /api/v1/maps` accepts two optional fields that fix how the map's scene graph is built. They are stored in the map spec (Postgres `MapSpecV1.scene_graph_params`), cannot be changed afterwards (`PATCH` does not accept them), and are returned by `GET /api/v1/maps` and `GET /api/v1/maps/{map_id}`.

`scene_graph_params`: object, every key optional (a missing key, or a missing object, takes the default, and the defaults are stored):

| Key | Type | Default | Valid |
|---|---|---|---|
| `schema_version` | int | 1 | 1 (a JSON integer, not a bool) |
| `chart_radius_m` | float (m) | 2.0 | > 0, <= 5.0, and >= `robot_half_side_m` |
| `close_evidence_range_m` | float (m) or `null` | 1.3 | > 0, <= 3.0; `null` = close evidence off (goal acceptance then uses the global `SCENE_GRAPH_GOAL_SNAP_M`) |
| `robot_half_side_m` | float (m) | 0.25 | > 0; normally set by `robot_name`, see below |
| `place_rule` | bool | false | stored; the engine does not act on it yet |

The 5.0 and 3.0 upper bounds are proposals, not validated limits. Unknown keys are 422.

Types are strict: the float keys take a JSON number (an integer is stored as a float, `3` becomes `3.0`) but not a boolean or a string (`true`, `"2.0"` are 422); `place_rule` takes only `true`/`false` (`1`, `"yes"` are 422); `schema_version` only an integer. NaN and infinity are 422. All of these are located at `["body", "scene_graph_params", "<key>"]`.

`robot_name`: optional, a non-empty string without leading or trailing spaces (`""`, `" r1"` and non-strings are 422 at `["body", "robot_name"]`; `null` is the same as absent). The server reads that robot's factsheet `length`/`width` and stores `robot_half_side_m` = half of the smaller side (the builder's rule, `cloud_common.objects.map.half_side`; to be rethought). `robot_name` is not stored. Do not send `robot_half_side_m` together with it: 422 at `["body", "robot_name"]`, before any database access (an explicit `null` there is invalid on its own). Errors: 404 unknown robot; 409 the robot has not reported its footprint yet (no silent default, the value is fixed for the map); 422 if the half side exceeds `chart_radius_m`, with the same shape whether the half side came from `robot_name` or from the request: `loc` `["body", "scene_graph_params"]` and a message naming `chart_radius_m` and `robot_half_side_m`.

`scene_graph_params` is `null` on a legacy map (created before this existed, not the defaults): it is built with the builder's global config and the robot's live footprint. A map that has it is built with its stored values, and the graph's manifest keeps them from the first build on.

Example: `{"name": "hall", "type": "local", "robot_name": "r1", "scene_graph_params": {"chart_radius_m": 3.0, "close_evidence_range_m": null}}`.

#### SLAM maps: `slam_map` on `POST /api/v1/maps`

`POST /api/v1/maps` accepts `slam_map: bool` (default `false`); `true` only with `type: "local"` (else 422, `loc` `["body","slam_map"]`). Map views and lists return `slam_map`. It can be changed with `PATCH /api/v1/maps/{id}` `{"slam_map": true|false}` (local map only; 409 for a geo map, an open or paused mapping session, or a SLAM save still pending; `null` is 422; returns the map view; `MAP.SLAM_CHANGED`), and `POST /api/v1/maps/{id}/type` to `geo` clears it (a note is added to `warnings`).

A mapping session on such a map also records a SLAM map on the robot's orchestrator under the name `cloud-<map>` (`PUT /localization {"mode":"slam"}`, saved in the background after `.../finish` with `POST /localization/save`). The session responses (`POST .../sessions`, `.../finish`) carry `slam_warning` (string) only when something went wrong; the session itself is never failed or rolled back for it. `robot_actions` (see packages/api/README.md) lists the SLAM step too; `mapping_warning` joins the failed actions' labels.
