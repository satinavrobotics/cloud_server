# SatiNav Maps: redesign

**Status:** draft for discussion, 2026-09-28. Nothing here is built yet, except the coordinate-conversion fix in §5 (in progress separately).

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

Live data on 2026-09-28: map `map` has datum (0, 0); map `GEO` exists as a real map.

---

## 2. Concepts

**Map:** a named, typed container for spatial knowledge of one place.

- `type: local`: its own metric frame, no link to the Earth. Shown on a metric grid, no street tiles.
- `type: geo`: anchored to the Earth. Positions are stored in one **UTM zone** fixed per map (`utm_zone`, `utm_north`), as metres relative to a map origin (`origin_e`, `origin_n`). Shown over street tiles, which can be switched off.
- Optional `site_id`, linking to the Phase 0 sites table.

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
- `ready`: no open session. Usable for missions. **No data is accepted.** Extending means explicitly starting a new session.
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
spec:   display_name, description, type ('local'|'geo'), site_id?,
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

The one-open-session-per-*map* rule is **not** a DB constraint. That keeps multi-robot mapping (§10) open.

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

**Known conflict, not fixed yet:** the orchestrator and the VDA5050 client both publish the retained datum on the same MQTT topic. On a real robot the orchestrator's UM982 point (the robot's position at orchestrator start, **not** the map origin) can overwrite the pose module's datum. Proposal: the orchestrator publishes only when the robot has no pose module (sim, or GNSS-less robots), or on its own topic.

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
| POST | `/api/v1/maps` | Create `{name, type, description?, site_id?}` → `draft` |
| GET | `/api/v1/maps?type=&state=&site=` | List, archived excluded by default |
| GET | `/api/v1/maps/{id}` | Spec, status, sessions summary, grid version |
| PATCH | `/api/v1/maps/{id}` | Rename, description, site |
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
- **Maps page:** the list with type and state badges; "New map" (name, type, site); archive/restore/delete.
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

1. Every existing Postgres map:
   - → `type = geo` if it has a real datum, i.e. not null and not (0, 0). The UTM zone comes from the datum longitude; the origin is the datum's UTM point.
   - → otherwise `type = local`.
   - `state = ready`.
2. Its existing nodes get one synthetic `legacy` session (`aligned = true`, identity transform).
3. Maps named `GEO`, `LOCAL`, `default`, and Arango-only maps with no Postgres row: listed for the user to archive or delete. Nothing is removed automatically.
4. `robot.current_map` is cleared; no session is opened automatically.
5. Map `map` with datum (0, 0) becomes `local`, unless the user gives it a real datum.

---

## 13. Plan

| Step | Content | Repos |
|---|---|---|
| M0 | Coordinate conversion fix (§5) | all four; in progress |
| M1 | Map spec + `map_sessions` + migration of today's data (§4, §12); new map endpoints (§7) behind the old ones | cloud_server |
| M2 | graph-builder ingest by session (§6); `MAP.*` events | cloud_server |
| M3 | Robot mapping switch over MQTT, session tagging (§8) | sati_ros_navstack |
| M4 | Client: Maps page, mapping bar, session start (§9, first half) | sati-client |
| M5 | Client: one `MapView` replacing the three map views (§9, second half) | sati-client |
| M6 | Local-map session alignment tool | cloud_server, sati-client |
| M7 | Grid layer storage and display, once `sati_grid_mapping` produces output | all |

M1–M2 and M3 can run in parallel. M5 is the largest client change and doesn't depend on M1, but it's simpler once map types exist.

---

## 14. Questions

Decided 2026-09-28:

- **Q1.** A geo map's origin is **the first session's datum**.
- **Q3.** When a session starts and the mapping service isn't running, the client **only tells the user**; nothing is started automatically. A map can have both a topo and a grid layer at the same time.
- **Q5.** The grid map is **uploaded once, at session end**.

Still open:

- **Q2.** Should small edits to a `ready` map (delete a bad node, add or remove an edge, re-align a session) be allowed directly in the map view, or only by starting a new session?
- **Q4.** Should a map belong to a site (required, optional, or not at all)?
- **Q6.** The datum topic conflict (§5): both publishers write the identical retained topic `uagv/v2/RobotCompany/<robot>/datum`, so the last one wins. On the real robot the orchestrator publishes a UM982 startup fix at boot (and again on every orchestrator restart); the VDA5050 client publishes the pose module's datum once anchored, and again on MQTT reconnect. Proposal: the VDA5050 client is the only datum source wherever the pose module runs; the orchestrator's datum publishing is kept only for robots without a pose module (the sim's `gps_anchor`).
