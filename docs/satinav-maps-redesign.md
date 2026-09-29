# SatiNav Maps: redesign

**Status:** 2026-09-29. M0 (coordinate conversion, §5) deployed. M1 (map type/geo/state, `map_sessions`, migration of today's maps, the new map routes; §13.1) deployed 2026-09-28. **M2 deployed 2026-09-28** (`~/pg-cutover/scripts/mapsm2.sh`): graph-builder ingests by session, the `PUT /robots/{r}/map` shim, map frame vs robot frame for missions, legacy nodes rewritten into the map frame, `MAP.DELETED` / `MAP.INGEST_REJECTED` (§13.2). **M3 built, not deployed** (robot mapping switch over MQTT; §8, §13.3; deploy `~/pg-cutover/scripts/mapsm3.sh`, API only, plus a topomap rebuild on each robot). M4 (client Maps page, mapping bar, session start) built in sati-client. **§14 (using maps: operate sessions, placement, the map window) designed 2026-09-29; it revises M5-M7.**

**Goal:** make a map a real, explicitly managed object: typed (`local` or `geo`), holding versioned contents (topo graph now, grid map later), with a lifecycle and explicit mapping sessions. The client shows every map through **one** map view.

**Decided (user, 2026-09-28)**

- A map is a real object with a type. `local` and `geo` maps stay **separate**; a map never mixes the two.
- A map has contents: the topological graph today, and a grid map from the upcoming `sati_grid_mapping` package.
- Maps have a lifecycle, and nodes are only accepted while a mapping session is active.
- The client gets one map view.
- **Not now:** sending the map to the robot, and the robot relocalizing in a stored map. That needs server↔robot map sync, like the models, and is a later phase (§11).
- Several robots sharing and extending one map: yes eventually, but it needs more planning (§10). The schema must not block it.

---

## 1. Today (what this replaces)

Found in the 2026-09-28 survey; file references are to that date.

| Area | Today | Problem |
|---|---|---|
| Map object | `MapObjectV1` spec = description + datum lat/lon/bearing only (`cloud_common/objects/map.py`) | No type, no status, no contents besides the graph |
| Creation | Implicit. Assigning "NEW MAP" stores a name on the robot; Arango collections are auto-created on the first node; the Postgres row appears only when someone calls `POST /map/load` | A map can exist in Arango and be invisible in the map list |
| Map type | Inferred from the id string (`'GEO'`, `'LOCAL'` sentinels in `robot.current_map`) and from whether a datum happens to exist | A real Postgres map named `GEO` exists; the dispatcher logs "Failed to auto-seed datum for map 'LOCAL'" on every datum message |
| Node ingest | graph-builder writes to `robot.current_map` whenever no mission is running; falls back to a silent `"default"` map; images take `map_id` from the payload, nodes from the DB | Maps grow forever; data lands in `"default"`; nodes and images can diverge |
| Assignment | `PUT /robots/{r}/map` sets `current_map` in Postgres only | The name suggests the robot uses the map; it does not |
| Datum | Seeded once from the robot's datum; the robot's datum changes every run | Nodes from a second run are shifted by the distance between the two start points |
| Coordinates | Cloud and client treat local x/y as true east/north; the real robot's frame is UTM grid | 2.5 m error per 100 m in Budapest (§5) |
| Client | DeckGLMap, MapLibreMap, GeoMap (Google + MapLibre), FleetGlobe: separate views, the same layers wired 4 times | Every feature is built 4 times; the background depends on a datum guess |
| Background | The robot's live costmap; street tiles for anything with a datum | A stored map has no image of its own |

Live data on 2026-09-28 (survey): map `map` had datum (0, 0); map `GEO` existed as a real map. At the M1 build (same day, later): one Postgres map, `map`, datum 47.4979, 19.0402 (frame `enu`); no `GEO`/`LOCAL` rows. ArangoDB also holds map collections without a Postgres row: `default` (624 nodes), `example trajectory` (441), `Test` (0), and the empty `map_nodes`/`map_edges`.

---

## 2. Concepts

**Map:** a named, typed container for spatial knowledge of one place.

- `type: local`: its own metric frame, no link to the Earth. Shown on a metric grid, no street tiles.
- `type: geo`: anchored to the Earth. Positions are stored in one **UTM zone** fixed per map (`utm_zone`, `utm_north`), as metres relative to a map origin (`origin_e`, `origin_n`). Shown over street tiles, which can be switched off.

**Mapping session:** one continuous period in which one robot adds data to one map. It records who, when, and the transform from the robot's frame for that run into the map frame (`map_T_session`). Every node, image and grid upload belongs to exactly one session.

**Contents:** layers owned by the map.

- `topo`: nodes, edges and node images (exists today).
- `grid`: occupancy or traversability grid images plus metadata, versioned (from `sati_grid_mapping`, later).

**Lifecycle:**

```
draft ──start session──▶ mapping ◀──▶ paused
                           │
                        finish
                           ▼
                         ready ──archive──▶ archived ──restore──▶ ready
                           │
                     start session (extend) ──▶ mapping
```

- `draft`: created, no data yet.
- `mapping` / `paused`: a session is open. Paused keeps the session but the robot records nothing (the topomap `~/set_enabled` switch, commit 62fd26a5).
- `ready`: no open session. Usable for missions. **No robot data is accepted.** Extending means explicitly starting a new session.
- `archived`: hidden from normal lists, kept. Deleting stays a separate, explicit action (existing background delete).

---

## 3. Frames and how sessions line up

This is the hard part, and it is why sessions exist.

The robot does **not** relocalize in a stored map (decided). So each robot run has its own origin:

- **Geo map, robot with GNSS (UTM frame):** every run publishes its datum. The session stores it, `map_T_session` is a pure translation (the datum's UTM minus the map origin, same zone), and all sessions line up automatically. The first session's datum becomes the map origin (Q1, decided).
- **Geo map, robot without a UTM datum (ENU anchor, e.g. the sim's fixed `gps_anchor`):** the datum is converted to the map's UTM zone. `map_T_session` then includes the grid-convergence rotation at that point (§5).
- **Local map:** there is no shared reference between runs. The first session defines the map frame (`map_T_session` = identity). A later session starts with identity **and is flagged `unaligned`**. The user aligns it in the map view (drag/rotate the session's nodes onto the existing ones) before the map is used for missions. Automatic alignment (matching node images) is a later improvement.

Nodes are stored in the **map frame** (converted at ingest with the session's transform), and the session keeps the robot-frame pose as well. So an alignment fix only needs to rewrite that session's nodes.

A geo map needs a geo-capable robot run (a datum present). A local map ignores any datum.

---

## 4. Storage

**Postgres (`mapobjectv1`, existing object convention; spec fields added, all optional):**

```
spec:   display_name, description, type ('local'|'geo'),
        geo?: { utm_zone, utm_north, origin_e, origin_n }
status: state ('draft'|'mapping'|'paused'|'ready'|'archived'), node_count, edge_count,
        grid_version?, open_session_id?, (existing delete bookkeeping)
```

**New table `map_sessions`** (Alembic migration):

```sql
CREATE TABLE map_sessions (
  session_id   uuid PRIMARY KEY,
  map_name     text NOT NULL,
  robot_name   text NOT NULL,
  started_at   timestamptz NOT NULL,
  paused_at    timestamptz,            -- set while paused
  ended_at     timestamptz,
  datum        jsonb,                  -- as received: frame, lat/lon, utm zone/e/n
  map_T_session jsonb NOT NULL,        -- {tx, ty, yaw}
  aligned      boolean NOT NULL,       -- false for a later local-map session until aligned
  node_count   int NOT NULL DEFAULT 0
);
-- at most one open session per robot
CREATE UNIQUE INDEX ON map_sessions (robot_name) WHERE ended_at IS NULL;
```

The one-open-session-per-*map* rule is **not** a DB constraint. That keeps multi-robot mapping (§10) open. (The M1 API still refuses a second open session on a map; lifting that is an API change only.)

As built in M1 (migration `20260928_01_map_sessions`): the column is `map_t_session` (Postgres folds unquoted names; the API shows `map_T_session`), `started_at` defaults to `now()`, and there is a `kind` column (`live` | `legacy`) with a second partial unique index, one `legacy` session per map. No foreign key to `mapobjectv1` (that table is created at runtime, not by Alembic); the map delete removes a map's sessions in the same transaction as its row.

**ArangoDB (unchanged layout, `nodes_{map}` / `edges_{map}`):** node documents add `session_id` and `robot_pose` (the robot-frame pose). `pose` is the map-frame pose.

**MinIO bucket `map-{id}`:**

- `{node_id}/images/...` (unchanged)
- `grid/{version}/{layer}.png` plus `grid/{version}/meta.yaml` (resolution, origin in the map frame, source session(s)). ROS `map_server`-style, so a later robot-sync phase can hand it to the robot as is. The robot uploads the grid **once, at session end** (Q5); each upload is a new version. A map can hold topo and grid layers at the same time.

**Robot object:** `current_map` goes away and is **not** replaced by a stored field (superseded by §14: the robot view derives `session` from `map_sessions`). The `'GEO'` / `'LOCAL'` sentinels go away: "no map" is simply no session. Whether a mission is mapped or mapless stays on the mission (`Mission.mode`).

---

## 5. Coordinate conversion (fix in progress)

The robot's local frame is one of:

- **UTM:** `sati_pose_module` keeps the datum as absolute UTM from the first fix of the run; local x/y are UTM grid offsets. The VDA5050 client publishes it with `bearing_deg: 0`.
- **ENU:** the orchestrator's registration anchor (sim `gps_anchor`, or a UM982 fix): true east/north.

The cloud and client treat everything as ENU (spherical, 111 320 m/°). For UTM data this is wrong by the grid convergence (−1.45° in Budapest, zone 34):

| Distance from datum | Error today |
|---|---|
| 100 m | 2.5 m |
| 1 km | 27 m |
| 5 km | 125 m |

**Fix:**

- The datum message carries `frame` (`utm` + zone, or `enu`).
- The server (`packages/utils/geo.py`) and client (`utils/mapTransform.ts`) do an exact UTM transverse-Mercator conversion for `utm`, and an ellipsoidal tangent plane for `enu`.
- Shared golden tests keep the two implementations in agreement.

This redesign builds on it: geo maps store UTM directly, so converting for display is one exact UTM → lat/lon step.

**Status:** built on 2026-09-28 across all four repos (not yet deployed): the datum carries `frame`; cloud `ff5268b..6174c3d`, client `eb31164..2b2d727`, robot `d5b6785c`, orchestrator `11da006`.

**Datum topic conflict: fixed** (Q6): the real robot's orchestrator no longer publishes a datum, so the VDA5050 client is the only source wherever the pose module runs.

---

## 6. Ingest (graph-builder)

- Resolve the robot's **open session**, not `current_map`. No open session, or session paused → drop the node and image. Count the drops and emit a rate-limited `MAP.INGEST_REJECTED` event, so nothing is lost silently.
- The silent `"default"` map is removed.
- Nodes and images resolve the session the same way (fixes the payload-vs-DB split).
- Convert the pose with `map_T_session`; store both poses.
- A map in `ready` / `archived` / `DELETING` never accepts writes.

---

## 7. API (sketch)

| Method | Path | Does |
|---|---|---|
| POST | `/api/v1/maps` | Create `{name, type, description?}` → `draft` |
| GET | `/api/v1/maps?type=&state=` | List, archived excluded by default |
| GET | `/api/v1/maps/{id}` | Spec, status, sessions summary, grid version |
| PATCH | `/api/v1/maps/{id}` | Rename, description |
| GET | `/api/v1/maps/{id}/graph` | Nodes and edges (replaces `POST /map/load` as the read path) |
| POST | `/api/v1/maps/{id}/sessions` | Start a session `{robot}`: robot must be online, geo needs a datum; turns robot-side mapping on |
| POST | `/api/v1/maps/{id}/sessions/{sid}/pause` · `/resume` · `/finish` | Session control; finishing with no other open session → `ready` |
| POST | `/api/v1/maps/{id}/sessions/{sid}/align` | Set `map_T_session` for a local-map session; rewrites its node poses |
| POST | `/api/v1/maps/{id}/archive` · `/restore` | Lifecycle |
| DELETE | `/api/v1/maps/{id}` | Existing background delete; refused while a session is open |
| GET | `/api/v1/maps/{id}/grid[/{version}]` | Grid metadata and images (later) |


Events: `MAP.CREATED`, `MAP.SESSION_STARTED/PAUSED/RESUMED/FINISHED`, `MAP.ARCHIVED`, `MAP.DELETED`, `MAP.INGEST_REJECTED`.

`PUT /robots/{r}/map`, the `GEO`/`LOCAL` sentinels and `POST /map/load` stay for one release as deprecated shims, then go away.

---

## 8. Robot side (this phase only)

- **Mapping switch over MQTT** (built in M3, §13.3). `sati_topo_mapping` (and later `sati_grid_mapping`) subscribes to the retained `{prefix}/{robot}/mapping/set` (`{enabled, session_id, map}`) and publishes the retained `{prefix}/{robot}/mapping/state` (`online`, `enabled`, `session_id`, `map`, `nodes_sent`, `since`, with a last will `online: false`). `{prefix}` is the VDA5050 prefix (`uagv/v2/RobotCompany`), `{robot}` the VDA5050 serial number. It already has an MQTT connection, so no orchestrator or VDA5050 change is needed. The contract is written down once, in `packages/api/mapping_control.py` (and the table in §13.3).
- **No session tagging for now** (decided 2026-09-28). The server resolves the session from the robot name. Tagging would only catch a late node from a finished session (e.g. re-sent after an MQTT reconnect) landing in the robot's next session; graph-builder already rejects a mismatching `session_id` if one is ever sent, so it can be added later without a server change.
- **Starting the topomap service itself:** the session-start call reports whether the mapping service is running (`mapping_service`). As built (M3) this comes from the robot's retained `mapping/state` (online, with a last will), not from the orchestrator proxy: it is exactly the process that must be up, and it needs no HTTP hop to the robot. If it isn't running, the session still starts and the client tells the user to start it; nothing is started automatically (Q3, decided).
- **No map download, no relocalization.**

---

## 9. Client

**One `MapView`**, replacing DeckGLMap, MapLibreMap and GeoMap for maps. FleetGlobe stays the fleet overview.

- **Background by map type:**
  - `local`: metric grid plus the map's grid layer (when there is one).
  - `geo`: OpenFreeMap tiles (toggle) plus the grid layer placed exactly (4-corner bounds, since UTM is rotated against lat/lon).
- **Overlays through the existing layer toggles:** Costmap (the robot's live costmap), Mission, Travelled path, Topo nodes, and later Grid.
- **Maps page:** the list with type and state badges; "New map" (name, type); archive/restore/delete.
- **Mapping bar** on the map view while the map is `mapping`/`paused`: robot, elapsed time, node count, **Pause / Resume / Finish**. This is where the mapping on/off switch lives. "Start mapping" is on a `draft`/`ready` map (pick a robot) and on the robot panel (pick or create a map).
- **Unaligned sessions** show in a different colour with an "Align" tool: drag and rotate, then save.
- Viewing a map no longer needs a selected robot, or changes one.

---

## 10. Several robots on one map (to plan later)

The schema already allows it: sessions are per robot, and there is no one-per-map constraint. What still needs deciding:

- **Geo maps:** concurrent sessions line up through their datums; mostly a UI and conflict question (duplicate nodes in the same place).
- **Local maps:** every robot's session needs alignment; concurrent unaligned sessions are messy.
- Whether graph-builder should merge nearby nodes across sessions.

---

## 11. Later: map on the robot

Out of scope, recorded so the design doesn't block it:

- Sync map versions to the robot like the models (MinIO → robot cache, version pinning).
- Load the grid for localization.
- Report the loaded map and version back (VDA5050 `mapId` / `agvPosition`).

At that point a mapping session can start *localized* in an existing map, and `map_T_session` comes from the robot instead of the datum or manual alignment.

---

## 12. Migration of today's data

Done by the M1 migration (`20260928_01_map_sessions`, idempotent: typed maps are skipped, the legacy session is `ON CONFLICT DO NOTHING`):

1. Every existing Postgres map (any lifecycle except `DELETED`):
   - → `type = geo` if it has a real datum, i.e. not null and not (0, 0). `geo` = the datum's UTM point in its own zone: a `utm` datum's reported zone/hemisphere/easting/northing when present, else the lat/lon projected in its longitude's zone (`packages/utils/map_geo.py`).
   - → otherwise `type = local`.
   - `state = ready`. The `datum_*` fields stay: they describe the frame the existing nodes are stored in.
2. Every map gets one synthetic `legacy` session: robot `legacy`, ended, `aligned = true`, identity transform, `datum` = the map's datum, `node_count` = the row's stored count (not the live ArangoDB count).
3. Maps named `GEO`, `LOCAL`, `default`, and ArangoDB-only maps with no Postgres row: **nothing is created or deleted**. The deploy script prints them (read-only) for the user to archive or delete.
4. ~~`robot.current_map` is cleared~~ **Moved to M2**: M1 leaves `current_map` and the dispatcher's datum auto-seed alone, because graph-builder still ingests by `current_map` until M2. M2 removed the auto-seed but keeps `current_map` (written by the deprecated shim) for its remaining readers; see §13.2.
5. The live map `map` (datum 47.4979, 19.0402, frame `enu`) becomes `geo`, zone 34 N, origin E 352 397.33 / N 5 262 357.80.

**Legacy node poses (done in M2, `tools/maps_m2_legacy_nodes.py`, §13.2).** Nodes in ArangoDB are **not** rewritten in M1. A geo map's frame is UTM grid metres from the origin, but a migrated map's legacy nodes are in its old datum frame. For a `utm` datum with bearing 0 the two are the same. For an `enu` datum they differ by the grid convergence at the datum (−1.445° for `map`, i.e. about 2.5 cm per metre from the origin) and by the UTM scale factor. M2 must either rewrite those nodes into the map frame (rotate by the convergence; also store `robot_pose`), or give the legacy session the real transform (`map_geo.session_transform(geo, datum)`) instead of identity. Until then the old display path (`POST /map/load` `transform`, from the `datum_*` fields) places them exactly as before.

## 13. Plan

| Step | Content | Repos |
|---|---|---|
| M0 | Coordinate conversion fix (§5) | all four; in progress |
| M1 | Map spec + `map_sessions` + migration of today's data (§4, §12); new map endpoints (§7) behind the old ones | cloud_server; **built, not deployed** (§13.1) |
| M2 | graph-builder ingest by session (§6); `MAP.INGEST_REJECTED`; clear `robot.current_map` (§12 step 4); legacy node poses (§12) | cloud_server; **built, not deployed** (§13.2) |
| M3 | Robot mapping switch over MQTT (§8); no session tagging | sati_ros_navstack, cloud_server; **built, not deployed** (§13.3) |
| M4 | Client: Maps page, mapping bar, session start (§9, first half) | sati-client |
| M5 | Client: one `MapView` replacing the three map views (§9, second half) | sati-client |
| M6 | Local-map session alignment tool | cloud_server, sati-client |
| M7 | Grid layer storage and display, once `sati_grid_mapping` produces output | all |

M1–M2 and M3 can run in parallel. M5 is the largest client change and doesn't depend on M1, but it's simpler once map types exist.

### 13.1 M1 as built

Code: `cloud_common/objects/map.py` (fields), `packages/utils/map_geo.py` (classification, origin, `map_T_session`), `packages/api/maps.py` (routes' logic), migration `20260928_01_map_sessions`. Route reference: `packages/api/README.md`. Tests: `tests/unit/test_maps_m1.py`, `tests/integration/maps/run.sh` (also rehearses the migration on a production dump: `--dump FILE`).

- **Model:** `spec.type`, `spec.geo`, `status.state`, `status.open_session_id`, `status.grid_version`, all optional. Rows without them read as the migration would type them (`effective_type()`: geo iff a real datum; `effective_state()`: ready). Responses only gained keys.
- **Routes (§7):** `POST /maps` (draft), `GET /maps?type=&state=&include_archived=`, `GET /maps/{id}` (+ type/geo/state and a sessions summary), `PATCH /maps/{id}` (description only: the name keys Postgres, ArangoDB and MinIO, so rename is not in M1), `GET /maps/{id}/graph`, `POST /maps/{id}/sessions`, `.../sessions/{sid}/pause|resume|finish`, `/archive`, `/restore`; `DELETE /maps/{id}` refused while a session is open. Old routes unchanged.
- **Events:** `MAP.CREATED`, `MAP.ARCHIVED`, `MAP.RESTORED`, `MAP.SESSION_STARTED/PAUSED/RESUMED/FINISHED`, written in the change's transaction (savepoint: a failed event write never fails the change). `MAP.DELETED` and `MAP.INGEST_REJECTED` are not in M1.
- **Session start:** robot must exist and be online; a robot has at most one open session (index); **one open session per map** (API rule, not a constraint); geo needs the robot's current datum (`robot.datum`, not (0, 0)). The first session of a geo map without an origin sets `geo` from that datum (Q1) **and fills the map's `datum_*` fields (when unset) with the origin as a `utm` datum, bearing 0**, which is exactly the map frame, so the old client, planner and `POST /map/load` `transform` show a new geo map correctly. Local map: identity, `aligned` only for the map's first session (a migrated local map has its legacy session, so a new one starts unaligned).
- **Not wired yet:** a session does not route robot data (graph-builder ingests by `current_map` until M2), so `session.node_count` stays 0 and pausing does not stop ingest; no MQTT to the robot (M3); no orchestrator check of the mapping service (M3/M4).
- **Legacy routes:** `POST /map/load` types the maps it registers (same rule as the migration) and never retypes an existing one. `PUT /maps/{id}/datum` updates the datum only (it does not retype a local map; revisit in M2). An archived map can still be assigned with `PUT /robots/{r}/map` (legacy path; goes away with the shims).
- **Deploy:** API (routes + migration) and mission-dispatch. Dispatch because its datum auto-seed writes the whole map spec back and the old model would drop `type`/`geo`; the planner only reads maps and ignores unknown keys; graph-builder never reads map objects. Checked on the running images.
- **Client (sati-client):** nothing to change for M1; all response changes are additive.

---

### 13.2 M2 as built

Code: `packages/services/graph_builder/ingest.py` (+ `server.py`), `packages/api/maps.py`
(`assign_robot_map`), `packages/utils/map_geo.py` (`robot_frame_in_map`, `invert_transform`,
`apply_pose`), `packages/services/mission_planner/server.py`, `packages/controllers/mission/server.py`
(`_route_in_robot_frame`), `packages/api/map_delete.py`, `tools/maps_m2_legacy_nodes.py`, migration
`20260929_01_maps_m2`. Tests: `tests/unit/test_maps_m2.py`, `tests/integration/maps/run_m2.sh`
(Postgres, ArangoDB, MinIO, mosquitto and graph-builder on a private network; `--dump` /
`--arango-dump` rehearse on production dumps). Deploy: `~/pg-cutover/scripts/mapsm2.sh` (API,
graph-builder, dispatch, planner; with 2e0b63a).

- **Ingest (§6).** graph-builder resolves the robot (payload `robot_name`) to its open session in
  one query (`map_sessions` ⋈ `mapobjectv1` ⋈ `robotobjectv1`), cached 1 s per robot, so a
  pause/finish takes effect within ~1 s without a second LISTEN connection. Nodes **and** images
  go to the session's map; the payload `map_id` is ignored (images used to land in `default`).
  Dropped, with a reason: `no_session`, `session_paused`, `map_not_mapping`, `map_deleting`,
  `map_missing`, `session_mismatch` (a payload `session_id` that is not the open one; untagged
  payloads are accepted until M3), `datum_changed` (a session whose robot datum changed since
  it started and cannot be re-anchored: a local map, or a datum in another UTM zone; on a geo
  map a restart is re-anchored, see §13.4),
  `lookup_failed`. The images buffered for a dropped node are dropped with it. The silent
  `default` map is gone (`POST /node`, a test hook, needs a map in `mapping`). Stored: `pose` =
  `map_T_session` applied (rigid, as in M1: no UTM scale factor, ≤1.3 cm per 100 m), `robot_pose`,
  `session_id`; `map_sessions.node_count` += 1 per node (inline; the map's status counts stay
  "fresh from ArangoDB on read", as in M1).
- **`MAP.INGEST_REJECTED`**: source `graph_builder` (new; the migration widens
  `fleet_events_source_check`), per robot and reason: the first drop at once, then at most one a
  minute with `dropped_nodes` / `dropped_images` since `since`; a 10 s flush reports the tail.
- **Missions unchanged:** a RUNNING mission with `register_map = false` still suppresses ingest
  (the node goes only to the waypoint log, robot frame). With `register_map = true` (or no
  mission) the session decides; a running mission's waypoint log gets the map-frame pose when the
  node is stored, the robot-frame pose otherwise.
- **Map frame vs robot frame** (not in the M2 plan; needed once nodes are map-frame). The robot's
  pose and VDA5050 orders are in the robot's current run frame. `map_geo.robot_frame_in_map(map,
  robot.datum)` = map_T_robot: a geo map with an origin → `session_transform` of the robot's
  current datum; a local map (or no origin yet) → identity; a geo map and a robot without a datum
  → unknown (no conversion, as before). The **planner** compares the robot's position in the map
  frame (closest start node, nearby nodes) and converts GPS goals with the map's UTM origin (the
  legacy datum only for maps without `geo`). The **dispatcher** sends route waypoints whose
  `map_id` names a real map through robot_T_map at order time (stored missions stay map-frame, so
  the client's display and reroute stay consistent); mapless / GEO / LOCAL waypoints are sent as
  they are. For every map live today this is identity except `map` with the sim robot (a
  −1.445° rotation about the origin).
- **Shim** (`PUT /api/v1/robots/{r}/map`, deprecated): a real map → finish the robot's session on
  another map, create the map if missing (typed from the robot's datum), start a session (the
  errors of `POST .../sessions`); `GEO` / `LOCAL` / null → finish. One transaction. It still
  writes `current_map`. `POST /map/load` no longer registers `GEO`/`LOCAL` as maps.
- **Datum auto-seed removed** (dispatcher). A geo map's origin and legacy `datum_*` come from its
  first session (Q1, M1). Nothing else needed it: the seeded `GEO` row was what the old client's
  mapless GEO view loaded; without it `POST /map/load GEO` returns `transform: null` and the
  client falls back to the robot's own datum (the right frame for mapless waypoints).
- **Legacy nodes** (`python -m tools.maps_m2_legacy_nodes [--apply]`, in the API image; dry run
  by default, idempotent, `--revert [--include-live]` from `robot_pose`): per geo map with a
  legacy session, the legacy session gets the real transform (`map`: yaw −1.4451°, no
  translation), nodes without `robot_pose` are rewritten (`robot_pose` = old pose, `pose` =
  transformed, `session_id` = legacy), `datum_*` become the origin as a `utm` datum with bearing 0
  (so `POST /map/load`'s `transform`, `GET /maps/{id}/graph` and the client's
  `utils/mapTransform.ts`, which handles `utm` exactly since eb31164, describe the map frame), and
  the legacy session's `node_count` and the map's status counts come from ArangoDB. Local maps:
  identity, tags only. On production: `map`, 5 nodes, each moved by < 7 cm.
- **`MAP.DELETED`**: when the background delete removes the row, in the same transaction
  (savepoint), once per delete.
- **`robot.current_map` — remaining readers (for M4/M5):** the old client (mission mode
  unassigned/geo/local/mapped and `register_map` in `CreateMissionOverlay`, per-waypoint `map_id`
  in `missionApi.toMissionWaypoint`, `RerouteMissionOverlay`, `MissionBuilder` map load, the map
  the UI opens on robot select in `useRobotSelection`, `MapSelectionPanel`, `MapPickerDropdown`,
  header labels, robot cards); the run recorder (`mission_runs.map_id` = `current_map` or the
  pose's map id; `GEO`/`LOCAL` land there); the bag upload metadata (`map_id`); the API's generic
  robot create/update (`current_map` in the body, no session effect). graph-builder and the
  dispatcher no longer read it. It goes when the client stops sending/reading it (M4: sessions;
  M5: one map view), replaced by the robot's open session (`mapping_session`, §4).
- **Deferred / known:** the client draws the live robot marker through the **map's** transform:
  on `map` with the sim robot it is now off by the −1.445° rotation about the origin (2.5 cm per
  m) until M5 projects `robot.status.pose` with the robot's own datum (or `map_T_robot`).
  Client-built mission waypoints are map-frame and converted by the dispatcher, so missions are
  right. A robot restart mid-session on a geo map was rejected (`datum_changed`) here; it is
  re-anchored since §13.4. graph-builder's `GET /health`
  `mqtt_connected` is always false (pre-existing flag, never set).

### 13.3 M3 as built

Code: robot `sati_topo_mapping/mapping_switch.py` (pure, unit-tested) + `topomap_node.py`, and
`sati_mqtt_common` (subscribe with re-subscribe on reconnect, last will, on-connect hooks, publish
with a wait); cloud `packages/api/mapping_control.py` (contract, publish, state cache),
`packages/api/maps.py` (`notify_robot`, `sync_all_robots`), `packages/api/server.py` / `main.py`
(wiring, routes, robot view), `packages/utils/mqtt_client.py` (connect listeners). Tests: robot
`test/test_mapping_switch.py` (+ a live smoke against a test mosquitto: start-up, enable, pause,
local override, finish, clean shutdown, restart picking up the retained set, kill -9 -> last will);
cloud `tests/unit/test_maps_m3.py`, and the M3 step of `tests/integration/maps/run_m2.sh`
(`checks_m3.py`). Deploy: `~/pg-cutover/scripts/mapsm3.sh` (API only, no migration).

**The contract** (`{prefix}` = VDA5050 prefix `uagv/v2/RobotCompany`, config
`MQTT_VDA5050_PREFIX` / robot param `mqtt.control_prefix`; `{robot}` = robot name = VDA5050
`serial_number` = topomap `mqtt.robot_name`, as for `.../datum`):

| Topic | Direction | Retained, QoS | Payload | When |
|---|---|---|---|---|
| `{prefix}/{robot}/mapping/set` | API → robot | yes, 1 | `{enabled, session_id, map, issued_at}` | after every committed session change of the robot (start/resume → `enabled: true` + session + map; pause → `false` + session + map; finish / no session → `false`, nulls), and for every robot on each API (re)connect to the broker |
| `{prefix}/{robot}/mapping/state` | robot → API | yes, 1 | `{online: true, enabled, session_id, map, nodes_sent, since, stamp, source}` | on every change (set message, `~/set_enabled`, each node sent) and every (re)connect |
| (same, last will) | broker → API | yes, 1 | `{online: false, enabled: false, session_id: null, map: null, nodes_sent: 0, since: null}` | the topomap's connection drops (crash, power, network); the same (+ `stamp`) is published on a clean shutdown |

`source`: `mqtt` (the last set message), `local` (`~/set_enabled`), `startup`. `nodes_sent`
counts per session. `since`: when `enabled` last flipped.

- **Robot.** With `mqtt.control_prefix` set (the default, `uagv/v2/RobotCompany`) the topomap
  **starts disabled** and waits for the retained set message; the `enabled` param is the start-up
  state only with `mqtt.control_prefix: ''` (no MQTT control). Enabled = the `~/set_enabled true`
  effect (fresh baseline, trigger timer on); disabled = no TF polling, no triggers, **no image
  subscription** (the on-demand capture is unchanged). A set message is applied only if its
  `(enabled, session_id, map)` differs from the last one applied, so a retained re-delivery after
  a reconnect does not undo a local `~/set_enabled` override (which lasts until the cloud sends a
  different one). MQTT messages arrive on paho's thread and are applied on the ROS executor
  through a guard condition. Last will: **added to `sati_mqtt_common`** (it had none), no periodic
  heartbeat; the broker publishes it at once on a dropped TCP connection (0 s in the kill -9 test)
  and after 1.5 × keepalive (60 s → 90 s) on a silent link loss.
- **API.** Publishes on the diagnostics MQTT connection (one per API process), after the
  transaction commits, re-reading the robot's open session under a per-robot lock (so the last
  message is the newest committed state; a no-op repeat of pause/resume/finish re-sends it). Waits
  up to 2 s for the broker's PUBACK: `robot_notified` = acknowledged. A failure is logged and never
  fails the call. `mapping_service` = `running` iff the last state is `online` (Q3: the session
  starts either way). Responses only gain keys (`packages/api/README.md`, "Robot mapping switch").
  The broker keeps no retained messages across its own restart (no persistence), hence the
  re-publish on every API reconnect; robots re-publish their state on reconnect.
- **For the client (M4).** Start / shim responses: `robot_notified`, `mapping_service`,
  `mapping_state`; pause / resume / finish: `robot_notified`, `mapping_state`; `GET
  /api/v1/maps/{id}`: `sessions.mapping_state`, `sessions.mapping_service`; `GET
  /api/v1/robots[/{r}]`: `mapping_state`; WS `/ws/robot/{r}`: `mapping_state_update`.
  `mapping_state.status` is `on` / `off` / `unreachable`, or the whole field is null (never
  heard from). "Robot confirmed" = `mapping_state.session_id` equals the open session and
  `enabled` matches its state.
- **Not done / deferred.** No per-node `session_id` tagging (decided). No session end when the
  robot's run ends (the §13.2 note): the last will says the topomap went away, but the session
  stays open; a geo session is re-anchored to the robot's new datum after a restart (§13.4). The
  client shows the state; ending sessions automatically is a later decision. The API re-publishes to
  every robot row on each (re)connect, including robots without the new topomap (harmless: a
  retained message nobody reads). Rare race: an API reconnect re-publish that reads the database
  just before a concurrent session change commits can land after that change's publish; the next
  change or reconnect corrects it.
- **Robot rollout.** Rebuild `sati_mqtt_common` and `sati_topo_mapping` (colcon) on the sim
  workspace and on the real robot, then restart the service that runs the topomap: the
  orchestrator service whose launch has `enable_topomap:=true` (sim: `sim_topomap.launch.py` or
  `sim_base_services.launch.py` with `enable_topomap`; real robot: the navstack service launch
  through `components/communication.launch.py`). No config change: the default prefix matches
  every checked-in VDA5050 config (`uagv` / `v2` / `RobotCompany`).

### 13.4 Fixes after M3 (as built)

- **Geo session after a robot restart (re-anchoring).** The real robot takes a new datum at every
  navstack start. A geo map's frame is absolute (UTM grid metres from `spec.geo`), so a new datum
  fixes where the robot's new run sits in it: graph-builder no longer rejects with
  `datum_changed` but re-derives the session's transform (`map_geo.session_transform` of the new
  datum, as at session start), stores it and the new datum in `map_sessions` (`datum`,
  `map_t_session`) and writes `MAP.SESSION_REALIGNED` (source `graph_builder`; payload: `datum`,
  `old_datum`, `map_T_session`, `old_map_T_session`). The write is a compare-and-set on the old
  stored datum in one transaction with the event, so concurrent ingests agree: one wins and emits
  the event, the others re-read and use the stored transform (`ingest.py`: `plan_realign`,
  `REALIGN_SQL`, `SessionResolver`). Only when the session is otherwise accepted (mapping, not
  paused, matching session id). Nodes stored before keep their map-frame poses; edges are, as
  ever, proximity edges between nodes within the distance threshold, so nothing links "the last
  node before the restart" to "the first after" except geometry (they meet only if the new run
  really starts near them). Still rejected (`datum_changed`): local maps (no absolute frame), a
  new datum in another UTM zone or hemisphere than the map, a geo map without an origin, a robot
  without a datum. No migration (the event code has no DB constraint). Caveat: the robot's datum
  reaches Postgres a moment after its restart; nodes arriving in that gap are placed with the old
  transform (as before this change), the first node after the datum arrives realigns.
- **Map delete** resets `current_map` of robots still pointing at the map to their mapless
  sentinel: `GEO` for a `gps` robot, `LOCAL` otherwise (what `PUT /robots/{r}/map` with no map
  writes), in the delete's finishing transaction, with the robot NOTIFY.
- **Planner without `map_id`**: the robot's `current_map` (a real map, not `GEO`/`LOCAL`); with no
  robot or no usable current map: 400 (`POST /api/v1/navigate`, `GET /missions/{id}/plan`; the
  API passes the 400 on). The implicit `default` map is gone (`default_map_id` is an opt-in
  constructor argument, unset in production). The API's `plan_mission` passes `map_id` on.
- **`PUT /maps/{id}/datum`** on a geo map: 409 when the map has nodes or sessions (its origin is
  the first session's datum and fixed); on an empty geo map with an origin, `spec.geo` follows
  the new datum. Local maps unchanged.

---

## 14. Using maps: operate sessions and the map window

**Status:** design, 2026-09-29. Not built. Revises the M5–M7 plan (§14.9).

### 14.1 The gap

M1–M4 built **making** maps: create, map with a robot, pause, finish, archive. They did not build **using** one:

- The robot panel's "Assign" (`AssignMapModal`) either picks a mapless mode (`GEO`/`LOCAL`, the deprecated `PUT /robots/{r}/map`) or starts a mapping session. A finished (`ready`) map cannot be chosen for a robot, except by starting a new mapping session on it, which records more nodes.
- What a robot "is on" is still `robot.current_map`, a free string that the shim writes. The dispatcher, the planner, the run recorder and the bag metadata read it, or they derive the transform from the map and the robot's datum without any session (`map_geo.robot_frame_in_map`).
- A **local** map can't be reused after a robot restart. The robot's pose is in its odom frame, which resets to the robot's position at every navstack start (`agvPosition` from `/odom`, `mapId` always the literal `"map"`). `robot_frame_in_map` assumes identity for local maps, so after a restart every mission waypoint and the robot marker are off by wherever the robot was started. Nothing detects this. Local sessions store no datum, so graph-builder's `datum_changed` check never fires for them.
- A later mapping session on a local map starts `unaligned` with identity. Its nodes land at wrong positions until the M6 alignment tool exists.
- The client offers no choice of mapping service. The contract (`mapping/set`, `mapping/state`) has no service name. The only grid producers on the robot, `sati_map_builder` (an odom-frame `/map`) and a `slam_toolbox` wrapper, are in no bringup launch and upload nothing.

### 14.2 The model

**A robot uses a map through an open session.** A session gets a **purpose**:

- `mapping`: the robot adds data to the map (as today).
- `operate`: the robot uses the map for missions and display and adds nothing.

Both purposes carry the same thing: `map_T_session`, where the robot's **current run frame** sits in the map frame. Everything that meets robot-frame data with map-frame data reads it from the robot's open session:

- the dispatcher (order waypoints),
- the planner (the robot's position, the default map),
- the client (the robot marker),
- graph-builder (node poses, mapping sessions only).

That makes `robot.current_map` and `robot_frame_in_map` redundant, and M5 can remove them.

Rules:

1. **One open session per robot, of any purpose.** The existing partial unique index `map_sessions_one_open_per_robot` already enforces this. Mapping implies using, so a robot mapping map A is also on map A. "No open session" means mapless, and the mission's `mode` says so; the `GEO`/`LOCAL` sentinels go away.
2. **Per map:** at most one open `mapping` session (API rule, as now) and any number of `operate` sessions. Operate sessions do not change the map's lifecycle state: a `ready` map stays `ready` while robots use it.
3. **Where `map_T_session` comes from** (the source is recorded on the session):
   - *Geo map:* from the robot's datum (`session_transform`, as today), re-derived on every datum change (§13.4).
   - *Local map:* from **placement**. The user puts the robot on the map by hand (position and heading). `map_T_session = P_map ⊕ P_robot⁻¹`, where `P_map` is the placed pose and `P_robot` is the robot's own pose at that moment.
   - *First mapping session of an empty local map:* identity. The run defines the map frame, as today.
   - *Later:* relocalization. The robot reports its pose in the loaded map (§11), and this uses the same `place` operation with source `robot`.
4. **Placed or not.** The existing `aligned` column now means "`map_T_session` is valid for the robot's current run"; the UI calls it *placed*. A session that is not placed:
   - captures nothing (`mapping/set` has `enabled: false`; graph-builder rejects with `session_unplaced`),
   - gets no route orders on that map,
   - gets no planned paths.
   It never guesses identity.
5. **Robot restart = run change.** When the robot's run frame resets, every open session of the robot becomes unplaced (`MAP.SESSION_UNPLACED`, reason `run_changed`).
   - A geo session is re-placed automatically when the new datum arrives (`MAP.SESSION_REALIGNED`, as §13.4). This also closes §13.4's gap: nodes that arrive between the restart and the new datum are no longer placed with the old transform.
   - A local session waits for the user to place the robot again.
6. **Extending a local map:** a `mapping` session on a local map that already has nodes starts unplaced, and the user places the robot before capture turns on. New data is then aligned when it is recorded. This replaces M6's "later sessions start unaligned" for all new data (§14.9).

**Detecting a run change** (dispatcher, which already receives `state`, `connection` and `datum` per robot):

- *GNSS robots:* a changed datum. This already works.
- *Every robot:* the VDA5050 client's header ids restart at 0 per process, for `connection`, `state` and `factsheet`. A `connection: ONLINE`, or a `state` whose `headerId` is lower than the last one seen, means a new client process. That is a navstack restart, and with it a new odom frame. After a dispatcher restart the last header id is unknown, so the first message changes nothing.
- *Limits:* an odom reset inside a running navstack (e.g. a VIO reset) is missed. A restart of only the VDA5050 client is a false positive, which costs one extra placement.
- A robot-side `run_id` would make this exact. It is a later, small robot change (Q-U3).

**What the robot needs to know:** nothing new for `operate`. Capture is the only thing it does for the cloud, and `mapping/set` covers that. The robot gets a map only with relocalization (§11). At that point the placement becomes the robot's initial pose.

### 14.3 API

Changed:

- `POST /api/v1/maps/{id}/sessions` body:
  - `robot`
  - `purpose: "mapping" | "operate"` (default `mapping`)
  - `services: ["topo"]` (mapping only; default `["topo"]`)
  - `placement?: {pose: {x, y, yaw}, robot_pose: {x, y, theta}}`
  - `replace: bool` (default false)

  `replace: true` finishes the robot's open session in the same transaction, so "Use this map" works when the robot is on another map.

  Errors:
  - 404: unknown map or robot.
  - 409:
    - the robot is offline;
    - the robot has an open session (without `replace`);
    - the map is archived, deleting, or `draft` for `operate` (nothing to use);
    - the map already has an open mapping session (for `mapping`);
    - geo map and the robot has no datum;
    - `robot_pose` is more than 0.10 m or 3° from the robot's current pose (the robot moved; place again).
  - 422:
    - `placement` on a geo map;
    - `services` on `operate`;
    - an unknown service.

  The response is as today. The session adds `purpose`, `services`, `aligned` (placed) and `placement`. `mapping_service` becomes per service (`mapping_services: {topo: "running"}`).
- `POST .../sessions/{sid}/finish`: both purposes. For `operate` it is the "Stop using" action.
- `POST .../sessions/{sid}/pause|resume`: 409 on `operate`.
- `GET /api/v1/maps/{id}`: `sessions` gains `operating: [{robot, session_id, aligned}]`.
- `GET /api/v1/maps/{id}/graph`: nodes gain `session_id`, for highlighting one session in the history.
- `GET /api/v1/robots[/{r}]` and WS `/ws/robot/{r}`: a derived, read-only `session` key: the open session `{session_id, map, purpose, state, aligned, map_T_session}` or null. It is read from `map_sessions` and not stored on the robot. It replaces `current_map` for the client.
- `DELETE /maps/{id}` and `POST .../archive`: 409 while **any** session is open. The message names the robots using the map (Q-U2).

New:

- `POST /api/v1/maps/{id}/sessions/{sid}/place` `{pose: {x, y, yaw}, robot_pose: {x, y, theta}}`.
  - It sets `map_T_session`, `aligned = true` and `placement` (`{pose, robot_pose, source: "user", actor, at}`).
  - It writes `MAP.SESSION_PLACED` and re-publishes `mapping/set`, so capture turns on for a placed mapping session.
  - 404 unknown session; 409 finished; 409 geo map (placed by its datum); 409 a *placed* `mapping` session (re-placing would split its nodes; alignment after the fact is M6); 409 robot moved (as above).
  - An `operate` session can be re-placed at any time, as a correction.
- `GET /api/v1/maps/{id}/sessions?limit=&before=`: the full history, newest first, paged. The summary stays capped at 50.

Removed at the end (step U6): `PUT /api/v1/robots/{r}/map`, `robot.current_map`, the `GEO`/`LOCAL` sentinels, the map delete's `current_map` reset, and the fallbacks of §14.6.

Events:
- `MAP.SESSION_STARTED/FINISHED` payloads gain `purpose`.
- New: `MAP.SESSION_PLACED` (source `api`, payload `map_T_session`, `placement`).
- New: `MAP.SESSION_UNPLACED` (source `dispatcher`, reason `run_changed`, the header ids or datums that showed it).

### 14.4 Data model and migration

Migration `…_maps_use` (idempotent):

```sql
ALTER TABLE map_sessions
  ADD COLUMN purpose   text   NOT NULL DEFAULT 'mapping'
      CHECK (purpose IN ('mapping', 'operate')),
  ADD COLUMN services  text[],          -- mapping: e.g. {topo}; operate: NULL
  ADD COLUMN placement jsonb;           -- {pose, robot_pose, source, actor, at}
UPDATE map_sessions SET services = '{topo}' WHERE purpose = 'mapping' AND services IS NULL;
ALTER TABLE map_sessions ADD CONSTRAINT map_sessions_legacy_mapping_check
  CHECK (kind <> 'legacy' OR purpose = 'mapping');
```

- `kind` (`live`/`legacy`) stays as it is: provenance, not purpose.
- The unique indexes are unchanged.
- Existing open local sessions with `aligned = false` stay unplaced and stop capturing until placed. At deploy the script lists them.
- The event code needs no DB change.
- The robot object gets **no new field**.

### 14.5 Robot and MQTT

- **`mapping/set`** (one retained message per robot, as M3):
  - adds `services: [..]`;
  - `enabled` is true only for an open, unpaused, placed **mapping** session;
  - an `operate` session publishes the no-session payload.
  - Each mapping service enables itself iff `enabled` and its name is in `services`. A missing `services` field means `["topo"]`, so an M3 topomap keeps working.
- **State per service:** `{prefix}/{robot}/mapping/{service}/state`, with the same payload plus `service`, and a last will per service process (each service has its own MQTT connection).
  - The topomap publishes `mapping/topo/state`.
  - The API subscribes to `{prefix}/+/mapping/+/state` and still reads the old `mapping/state` as `topo` for one release (or not at all, if M3 is not yet deployed when this lands: Q-U6).
  - `mapping_state` in API responses stays the topo state. `mapping_services` adds all of them.
- **Services named now:** `topo` (sati_topo_mapping). `grid` is reserved for `sati_grid_mapping`, which doesn't exist yet. `sati_map_builder` or `slam_toolbox` would need a bringup entry, the switch and a map-frame upload at session end (Q5) first. A service the robot has never reported shows as "not available on this robot".
- **No robot change for operate or placement.** Optional later: a `run_id` in the VDA5050 client (Q-U3).

### 14.6 What changes for the consumers

| Consumer | Today | Now |
|---|---|---|
| Dispatcher `_route_in_robot_frame` | `robot_frame_in_map(map, robot.datum)` per waypoint map | waypoints on the robot's session map go through `inverse(map_T_session)`. A waypoint on another map, or a session that is not placed, **fails the node** ("robot is not placed on map X" / "robot is not using map X"). Mapless waypoints are unchanged |
| Planner `_resolve_map`, `_robot_xy_in_map` | `robot.current_map`, `robot_frame_in_map` | the session's map and `map_T_session`; unplaced → 409 |
| Run recorder `mission_runs.map_id` | `current_map` or the pose's map id | the session's map (null when mapless) |
| Bag metadata `map_id` | `current_map` | the session's map, plus `session_id` |
| graph-builder `decide()` | open session | plus `not_mapping_session` (operate) and `session_unplaced` |
| Client marker | the map's transform (off by −1.445° on `map` with the sim, §13.2) | `robot.session.map_T_session` |

**Transition (U2 to U6):** while the old client still sends missions without a session, a waypoint on a map the robot has no session on falls back to today's `robot_frame_in_map`, with a warning. U6 removes the fallback.

### 14.7 Client: the map window

`AssignMapModal` (480 px) is replaced by a **map window**: `ModalShell` size `lg` (900 px), height `fill` (85 %), full screen on compact screens. It opens from the robot panel. The "Assign" link under the robot name becomes "Map…", and `RobotMappingLine` shows the session.

Regions:

1. **Header:** icon `map-outline`, title `MAPS`, and the robot as the subject pill.
2. **Robot strip** (top, full width): what the robot is on now. For example:
   - "Using `map` · placed", with the buttons *Place again* (local maps only) and *Stop using*;
   - "Mapping `lab` · 42 nodes" (the mapping bar stays on the map view);
   - "Using `lab` · **not placed** — the robot restarted", warn tone, with the button *Place robot*;
   - "No map · mapless missions".
3. **Map list** (left, about 40 %):
   - search, type chips All / Geo / Local, "Show archived";
   - rows with name, `MapBadges`, node count, last activity, and chips for robots using or mapping the map;
   - the header button **Create new map**.
4. **Preview** (right, top):
   - the selected map's nodes and edges (DeckGL `fitToContent` for local, MapLibre with tiles for geo), the grid when there is one, and robots using it;
   - a map without data shows the empty state "No data yet — start mapping".
5. **History / Details tabs** (right, bottom):
   - History lists sessions, newest first: purpose icon, robot, start → end or "open", duration, nodes, placed/not placed. Selecting a session highlights its nodes in the preview.
   - Details: type, UTM zone and origin, description, created.
6. **Action bar** (bottom, for the selected map and this robot):
   - **Use for {robot}** (primary);
   - **Map more** (start a mapping session, with the service choice);
   - disabled reasons shown inline (offline, no GNSS datum for geo, draft has nothing to use, another robot is mapping it).

Flows:

- **Use a geo map:** *Use* → confirm, which shows the datum check → `POST sessions {purpose: operate, replace: true}` → the strip says "Using · placed".
- **Use a local map:** *Use* → **place mode**.
  - The preview becomes interactive. Click to set the position, drag the arrow to set the heading, or type x, y and heading.
  - The note "Keep the robot still" is shown, with the robot's live pose next to it.
  - *Confirm* sends `start` with `placement` (or `place` for an existing session). *Cancel* leaves the session as it was.
- **Create new map:** name, type (Local/Geo), then **mapping service** cards:
  - *Topological map* (`topo`), with its state: running / not running;
  - *Occupancy grid* (`grid`): "not available on this robot" until the robot reports it.
  - *Create & start mapping* runs `POST /maps`, then `POST sessions {purpose: mapping, services}`. If the start fails, the draft map stays in the list, marked "draft".
- **Map more on an existing map:** the service cards; on a local map with nodes, place mode first; then start.
- **Mapless:** *Stop using* / *Finish mapping* (with confirmation) in the strip.

The screen-by-screen brief for the mockup is in `map-window-brief.md` (scratchpad, not in the repo).

### 14.8 Tests

**Unit (cloud):**
- The start rules as a matrix: purpose × map state × map type × robot online/datum × existing session × `replace`.
- Placement math, as a property test: the robot pose placed through `map_T_session` gives the placed pose.
- The `place` rules, and the robot-moved tolerance.
- `set_payload`: operate gives off; unplaced gives off; `services`.
- `decide()`: the new reasons.
- The run-change detector over header-id sequences: fresh ONLINE, a decreasing state, a dispatcher restart, and the datum path.
- Geo re-place after a run change.
- Dispatcher route conversion from the session, and its refusals.
- Planner resolution.
- History paging.
- The robot view's `session` key.
- The migration on a copy of production (`run_m2.sh --dump`).

**Integration** (extend `tests/integration/maps/run_m2.sh`):
- Local operate with placement → a mission's order on MQTT carries robot-frame waypoints.
- Simulated restart (state `headerId` back to 0) → `MAP.SESSION_UNPLACED`, the order is refused, `mapping/set` is off.
- Re-place → the order goes out.
- Geo operate + a new datum → realigned.
- Extending a local map → no capture until placed; after placement the nodes land at the placed offset.
- `replace` is atomic: a refused start keeps the old session.

**Client:**
- Pure-logic tests for the window model, like `buildMappingBarModel`: region contents and enabled actions per state.
- Screen-to-pose conversion in place mode.

**Manual, with the sim robot:**
1. Use `map`, run a mission.
2. Restart the sim navstack and check "not placed".
3. Place the robot, run the mission again.
4. Create a local map and map it, finish, restart, use it with placement.

### 14.9 Plan (revises M5–M7)

| Step | Content | Repos |
|---|---|---|
| U1 | Migration; `purpose`/`services`/`placement`/`replace` on start; `place`; history endpoint; robot view `session`; graph `session_id`; `set_payload` and ingest rules; events | cloud_server |
| U2 | Consumers read the session (§14.6), with the transition fallback | cloud_server |
| U3 | Run-change detection in the dispatcher; unplace; geo re-place on the datum write (the same compare-and-set as graph-builder's) | cloud_server |
| U4 | Map window (§14.7), place mode, robot strip, marker via the session; retire `AssignMapModal`'s GEO/LOCAL rows | sati-client |
| U5 | Per-service state topics and `services` in `mapping/set` | sati_ros_navstack, cloud_server |
| U6 | Remove `current_map`, the shim, the sentinels and the fallbacks | cloud_server, sati-client |
| M5 | One `MapView` (unchanged goal; its local/geo rendering starts from U4's preview) | sati-client |
| M6 | **Shrinks** to aligning *existing* unaligned sessions (legacy data), since new ones are placed before capture. Drop it if none remain | cloud_server, sati-client |
| M7 | Grid storage and display once `sati_grid_mapping` exists; it plugs in as service `grid` | all |

U1 → U2 → U3 are sequential. U4 needs U1 (and U3 for the "not placed" state). U5 is independent. U6 comes last, after U4 is deployed.

### 14.10 Open questions (with recommendation)

- **Q-U1.** API name for the purpose: `operate` or `use`? *Rec:* `operate` in the API, "Use / Using" in the UI.
- **Q-U2.** Deleting or archiving a map that robots are using: refuse (409), or stop their sessions? *Rec:* refuse, and name the robots.
- **Q-U3.** Restart detection for robots without GNSS: header-id reset only, or also a robot-side `run_id`? *Rec:* header ids now (no robot change); add `run_id` with relocalization.
- **Q-U4.** Require placement before capture when extending a local map (a change from M1, where later sessions record unaligned)? *Rec:* yes. M6 shrinks.
- **Q-U5.** May a robot use a map that another robot is mapping at the same time? *Rec:* yes. It sees the map grow; missions plan on what exists.
- **Q-U6.** If M3 is not deployed yet, rename `mapping/state` to `mapping/topo/state` before it is? *Rec:* yes. Otherwise keep the alias for one release.
- **Q-U7.** How far may the robot move during placement before it is refused? *Rec:* 0.10 m / 3°. Placement is meant with the robot standing still.
- **Q-U8.** Manual correction of a geo map's placement (a datum off by metres)? *Rec:* no, not now; the datum defines it.
- **Q-U9.** End an operate session automatically when a robot stays offline? *Rec:* no. It stays open and becomes unplaced on the next run change.

---

## 15. Questions

Decided 2026-09-28:

- **Q1.** A geo map's origin is **the first session's datum**.
- **Q3.** When a session starts and the mapping service isn't running, the client **only tells the user**; nothing is started automatically. A map can have both a topo and a grid layer at the same time.
- **Q5.** The grid map is **uploaded once, at session end**.

- **Q2.** Hand-editing a finished map (deleting nodes, editing edges): **out of scope** for now; revisit later.
- **Q4.** Maps are **not linked to sites**. Whether the Phase 0 sites concept stays at all is under review.
- **Q6.** Both publishers wrote the identical retained topic `uagv/v2/RobotCompany/<robot>/datum`, so the last one won. On the real robot the orchestrator published a UM982 startup fix at every orchestrator (re)start, overwriting the pose module's map origin, and its UM982 read competed with the navstack's own `um982_driver` for the serial port. **Fixed** (satibot_orchestrator `6587a6a`): the real robot's config no longer has `um982_gps`, so the VDA5050 client is the only datum source wherever the pose module runs. The orchestrator's `gps_anchor` publishing stays for robots without a pose module (the sim).
