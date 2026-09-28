# SatiNav Maps: redesign

**Status:** 2026-09-28. M0 (coordinate conversion, §5) deployed. **M1 built, not deployed** (branch off `d4296cd`; deploy script `~/pg-cutover/scripts/mapsm1.sh`): map type/geo/state, `map_sessions`, migration of today's maps, the new map routes next to the old ones (§4, §7, §12, §13.1). M2 onwards: not started.

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

**Robot object:** `current_map` is replaced by `mapping_session` (read-only; the open session, if any). The `'GEO'` / `'LOCAL'` sentinels go away: "no map" is simply no session. Whether a mission is mapped or mapless stays on the mission (`Mission.mode`).

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

- **Mapping switch over MQTT.** `sati_topo_mapping` (and later `sati_grid_mapping`) subscribes to `{prefix}/{robot}/mapping/set` (`{enabled, session_id}`) and publishes `{prefix}/{robot}/mapping/state` (retained: `enabled`, `session_id`, node counter). It already has an MQTT connection, so no orchestrator or VDA5050 change is needed. It tags every node and image with `session_id`.
- **Starting the topomap service itself:** the session-start call checks, through the existing orchestrator proxy, whether the mapping service is running. If it isn't, the client tells the user to start it; nothing is started automatically (Q3, decided).
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
4. ~~`robot.current_map` is cleared~~ **Moved to M2**: M1 leaves `current_map` and the dispatcher's datum auto-seed alone, because graph-builder still ingests by `current_map` until M2.
5. The live map `map` (datum 47.4979, 19.0402, frame `enu`) becomes `geo`, zone 34 N, origin E 352 397.33 / N 5 262 357.80.

**Left for M2 — legacy node poses.** Nodes in ArangoDB are **not** rewritten in M1. A geo map's frame is UTM grid metres from the origin, but a migrated map's legacy nodes are in its old datum frame. For a `utm` datum with bearing 0 the two are the same. For an `enu` datum they differ by the grid convergence at the datum (−1.445° for `map`, i.e. about 2.5 cm per metre from the origin) and by the UTM scale factor. M2 must either rewrite those nodes into the map frame (rotate by the convergence; also store `robot_pose`), or give the legacy session the real transform (`map_geo.session_transform(geo, datum)`) instead of identity. Until then the old display path (`POST /map/load` `transform`, from the `datum_*` fields) places them exactly as before.

## 13. Plan

| Step | Content | Repos |
|---|---|---|
| M0 | Coordinate conversion fix (§5) | all four; in progress |
| M1 | Map spec + `map_sessions` + migration of today's data (§4, §12); new map endpoints (§7) behind the old ones | cloud_server; **built, not deployed** (§13.1) |
| M2 | graph-builder ingest by session (§6); `MAP.INGEST_REJECTED`; clear `robot.current_map` (§12 step 4); legacy node poses (§12) | cloud_server |
| M3 | Robot mapping switch over MQTT, session tagging (§8) | sati_ros_navstack |
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

## 14. Questions

Decided 2026-09-28:

- **Q1.** A geo map's origin is **the first session's datum**.
- **Q3.** When a session starts and the mapping service isn't running, the client **only tells the user**; nothing is started automatically. A map can have both a topo and a grid layer at the same time.
- **Q5.** The grid map is **uploaded once, at session end**.

- **Q2.** Hand-editing a finished map (deleting nodes, editing edges): **out of scope** for now; revisit later.
- **Q4.** Maps are **not linked to sites**. Whether the Phase 0 sites concept stays at all is under review.
- **Q6.** Both publishers wrote the identical retained topic `uagv/v2/RobotCompany/<robot>/datum`, so the last one won. On the real robot the orchestrator published a UM982 startup fix at every orchestrator (re)start, overwriting the pose module's map origin, and its UM982 read competed with the navstack's own `um982_driver` for the serial port. **Fixed** (satibot_orchestrator `6587a6a`): the real robot's config no longer has `um982_gps`, so the VDA5050 client is the only datum source wherever the pose module runs. The orchestrator's `gps_anchor` publishing stays for robots without a pose module (the sim).
