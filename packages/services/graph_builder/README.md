# Graph Builder Service

A microservice that builds topological maps by processing new node updates from robots via MQTT.

## Overview

The Graph Builder Service is a critical component of the topological mapping system. It:
- Subscribes to MQTT for new node updates from robots
- Saves images to the Image Database
- Finds nearby nodes using radius search (default 5m threshold)
- Checks traversability using the Similarity Service
- Creates bidirectional edges between traversable nodes
- Saves nodes and edges to the Graph Database

## Which map (maps redesign M2)

Nodes and images go to the robot's **open session** (mapping or operate) (`map_sessions`, resolved by the
payload's `robot_name`; `ingest.py`), never to the old `robot.current_map` (removed in U6) or
a `default` map; a `map_id` in the payload is ignored. The node's `pose` is stored in the map frame (the session's
`map_T_session` applied), with `robot_pose` (as received) and `session_id`. No open session, a
paused one, a map that is not `mapping` or is being deleted, a payload `session_id` that is not
the open session, or a session whose robot datum changed since it started and cannot be re-anchored (a local map,
a datum in another UTM zone; a geo map is re-anchored to the new datum and
`MAP.SESSION_REALIGNED` is written): the data is dropped, counted (`GET /stats`: `nodes_rejected`, `images_rejected`) and reported as
`MAP.INGEST_REJECTED` in `fleet_events` (at most one per robot and reason a minute). The session
lookup is cached for 1 s, so a pause or finish takes effect within about a second. A RUNNING
mission with `register_map = false` still suppresses ingest, as before. `POST /node` (a test
hook) needs `map_id` naming a map in the `mapping` state. See
`docs/satinav-maps-redesign.md` §6, §13.2.

## Architecture

```
Robot (MQTT) → Graph Builder Service
                    ↓
    ┌───────────────┼───────────────┐
    ↓               ↓               ↓
Image DB      Graph DB      Similarity Service
(MinIO)      (ArangoDB)     (Traversability)
```

## Features

- **MQTT Integration**: Subscribes to robot node updates in real-time
- **Image Storage**: Saves robot camera images to MinIO-backed Image Database
- **Spatial Indexing**: Uses R-tree spatial index for fast nearby node queries
- **Traversability Checking**: Validates edges using configurable distance threshold
- **Bidirectional Edges**: Automatically creates edges in both directions
- **REST API**: Provides health checks, statistics, and manual node processing
- **WebSocket Updates**: Publishes real-time map updates to connected clients

## Dependencies

The service requires the following services to be running:
- **MQTT Broker** (e.g., Mosquitto) - for receiving node updates
- **Image Database Service** (port 6002) - for storing images
- **Graph Database Service** (port 6001) - for storing graph nodes/edges

## Installation

### 1. Install Dependencies

```bash
cd packages/services/graph_builder
pip install -r requirements.txt
```

### 2. Configure Environment

Copy the example environment file and edit as needed:

```bash
cp .env.example .env
```

Edit `.env`:
```bash
MQTT_HOST=localhost
MQTT_PORT=1883
MQTT_TOPIC=robot/node_update
IMAGE_DB_URL=http://localhost:6002
GRAPH_DB_URL=http://localhost:6001
SIMILARITY_SERVICE_URL=http://localhost:8003
RADIUS_THRESHOLD=5.0
```

## Usage

### Start the Service

```bash
python -m packages.services.graph_builder.main --port 8004
```

Or with custom configuration:

```bash
python -m packages.services.graph_builder.main \
  --port 8004 \
  --mqtt-host localhost \
  --mqtt-port 1883 \
  --mqtt-topic robot/node_update \
  --radius-threshold 5.0 \
  --log-level INFO
```

### Command Line Arguments

- `--host`: Host to bind to (default: 0.0.0.0)
- `--port`: Port to bind to (default: 8004)
- `--mqtt-host`: MQTT broker host (default: localhost)
- `--mqtt-port`: MQTT broker port (default: 1883)
- `--mqtt-topic`: MQTT topic for node updates (default: robot/node_update)
- `--image-db-url`: Image DB service URL (default: http://localhost:6002)
- `--graph-db-url`: Graph DB service URL (default: http://localhost:6001)
- `--distance-threshold`: Distance threshold in meters for edge creation (default: 3.0)
- `--radius-threshold`: Radius threshold in meters (default: 5.0)
- (no default map since maps M2: see "Which map")
- `--log-level`: Logging level (DEBUG, INFO, WARNING, ERROR)

## API Endpoints

### Health Check
```
GET /health
```

Returns service health status and dependency status.

**Response:**
```json
{
  "status": "healthy",
  "service": "graph_builder",
  "mqtt_connected": true,
  "dependencies": {
    "image_db": true,
    "graph_db": true
  }
}
```

### Get Statistics
```
GET /stats
```

Returns service statistics.

**Response:**
```json
{
  "nodes_processed": 42,
  "images_saved": 84,
  "edges_created": 156,
  "errors": 0,
  "mqtt_connected": true,
  "radius_threshold": 5.0
}
```

### Manual Node Processing (Testing)
```
POST /node
```

Manually process a node update (for testing purposes).

**Request Body:**
```json
{
  "node_id": 1001,
  "x": 10.5,
  "y": 20.3,
  "yaw": 1.57,
  "map_id": "warehouse",
  "images": [
    {
      "image_id": "front_camera",
      "data": "base64_encoded_image_data",
      "content_type": "image/jpeg"
    }
  ],
  "metadata": {
    "robot_id": "robot_01",
    "timestamp": "2024-01-15T10:30:00Z"
  }
}
```

**Response:**
```json
{
  "success": true,
  "node_id": 1001,
  "message": "Node processed successfully"
}
```

### WebSocket Real-Time Updates
```
WS /ws/updates/{map_id}
```

Subscribe to real-time map updates for a specific map. This endpoint is typically consumed by the API Delegation Service to proxy updates to client applications.

**Connection:**
```javascript
const ws = new WebSocket('ws://localhost:8004/ws/updates/warehouse');

ws.onmessage = (event) => {
  const update = JSON.parse(event.data);
  console.log('Map update:', update);
};
```

**Update Message Format:**
```json
{
  "type": "node_added",
  "map_id": "warehouse",
  "node": {
    "node_id": 1001,
    "x": 10.5,
    "y": 20.3,
    "yaw": 1.57
  },
  "edges": [
    {
      "from_node_id": 1001,
      "to_node_id": 1000
    },
    {
      "from_node_id": 1000,
      "to_node_id": 1001
    }
  ],
  "timestamp": "2024-01-15T10:30:00Z"
}
```

**Note:** In production, clients should connect through the API Delegation Service at `ws://api-delegation:8000/ws/map/{map_id}`, which proxies to this endpoint.

## MQTT Message Format

The service expects node update messages on the configured MQTT topic with the following format:

```json
{
  "node_id": 1001,
  "x": 10.5,
  "y": 20.3,
  "yaw": 1.57,
  "map_id": "warehouse",
  "images": [
    {
      "image_id": "front_camera",
      "data": "base64_encoded_image_data",
      "content_type": "image/jpeg",
      "metadata": {
        "camera_name": "front_camera",
        "timestamp": "2024-01-15T10:30:00Z"
      }
    },
    {
      "image_id": "back_camera",
      "data": "base64_encoded_image_data",
      "content_type": "image/jpeg",
      "metadata": {
        "camera_name": "back_camera",
        "timestamp": "2024-01-15T10:30:00Z"
      }
    }
  ],
  "metadata": {
    "robot_id": "robot_01",
    "timestamp": "2024-01-15T10:30:00Z"
  }
}
```

### Required Fields
- `node_id`: Unique integer identifier for the node
- `x`: X coordinate in meters
- `y`: Y coordinate in meters

### Optional Fields
- `yaw`: Orientation in radians (default: 0.0)
- `map_id`: Map identifier (default: from config)
- `images`: List of image data (base64 encoded)
- `metadata`: Additional node metadata

### Node times: receipt time and robot capture time

A stored node has two separate times:

- `timestamp` and `created_at`: the **cloud's receipt time** (naive `datetime.now().isoformat()` of
  the graph-builder host). Always set; unchanged by anything the robot sends.
- `capture_time` (optional): the **robot's capture time**, ISO 8601 UTC with offset
  (`2026-10-11T11:59:00+00:00`), read from the payload by `packages/utils/capture_time.py`.
  Absent when the payload carries no sane capture time. Never taken from `metadata.capture_time`.

The robot has no dedicated field. Sources, first sane wins: `capture_time`, `stamp`, then
top-level `timestamp` (ISO string with offset, or epoch s / ms / us / ns number, unit told by
magnitude), then `camera_metadata[].timestamp` (what the robot's topomap node sends: epoch
**milliseconds** of the TF stamp at the camera frame, earliest wins; only nodes with images have
one). `metadata.timestamp` (robot-local naive time) is not used. A value is dropped when it is not
a number / ISO string with an offset, is before 2020-01-01 (e.g. ROS sim time) or more than 300 s
in the future. A robot clock that is wrong but plausible keeps its values: only differences
between nodes of one session are meaningful (that is what the session trace uses), not the
absolute time. Nodes stored before this field existed have no `capture_time`; the session trace
derives one from their stored `camera_metadata[].timestamp` when present.

### Depth images (3D reconstruction R2)

`robot/depth_upload` (`MQTT_DEPTH_TOPIC`): one message per node and camera with a lossless
u16-millimetre PNG (`depth_data`, base64), `depth_scale`, the stamps, the robot's 6-DoF pose at
the depth stamp (`robot_pose3d`, run frame) and the camera block (intrinsics, `T_base_cam`).
Resolved by session and buffered like an image. Stored as `map-{id}/{node}/depth/{camera}.png`
and, on the ArangoDB node, `depth.{camera}` = the camera block, scale, stamps, session and
`pose3d_map` (the pose in the map frame). The PNG's header must be a 16-bit grayscale PNG of the
camera block's `width` x `height`. A dropped one counts as `dropped_depth` in
`MAP.INGEST_REJECTED`. Payload: `docs/reconstruction/design.md` §4.3, §5.

### Costmap layers (`robot/costmap_upload`)

`robot/costmap_upload` (`MQTT_COSTMAP_TOPIC`, default `robot/costmap_upload`, empty = not
subscribed; subscribed at QoS 1): one message per node and layer with an occupancy PNG
(u8: 0..100 = occupancy percent, 255 = unknown, 253 = person-labelled, 254 reserved; the PNG's pixels
are not decoded when it is received, only its header: signature, width x height equal to `width` x
`height`, 8-bit grayscale).

```json
{
  "session_node_id": 7, "robot_name": "r1", "layer": "occupancy",
  "costmap_data": "<base64 PNG>", "content_type": "image/png",
  "costmap_encoding": "u8_occ100_unknown255",
  "width": 200, "height": 200, "resolution": 0.05,
  "origin": {"x": -5.0, "y": -5.0, "yaw": 0.0},
  "origin_pose3d": {"x": 0, "y": 0, "z": 0, "qx": 0, "qy": 0, "qz": 0, "qw": 1},
  "frame": "map", "source_frame": "odom",
  "costmap_stamp_ms": 1727600000123, "keyframe_stamp_ms": 1727600000084,
  "stamp_offset_ms": 39, "source_topic": "/local_costmap/costmap"
}
```

Required: `session_node_id`, `robot_name`, `layer` (1-32 characters of `A-Z a-z 0-9 _ -`,
it names the MinIO object and the node attribute), `costmap_data`,
`width`/`height` (positive integers), `resolution` (finite, > 0), `origin` (`x`, `y`, `yaw`,
finite). `content_type` defaults to, and must be, `image/png`; `costmap_encoding` defaults to
`u8_occ100_unknown255` and must be that or `u8_occ100_unknown255_person253` (the same grid where 253 =
a person-class vote within the robot's last seconds, the belief under it erased; the encoding is
informational and is not stored on the node). `origin_pose3d` is optional (7 finite fields, non-zero quaternion).
The base64 is decoded strictly when saving. An invalid message is logged and counted in
`errors` (no event).

The scene adapter reads 253 (person-labelled) and 254 (reserved) as unknown (255) when it decodes the
occupancy window, before any fold (`scene_adapter.occupancy_window`): such a cell is no free, occupied or
unobserved-gap vote and never blocks or frees anything. 0..100 and 255 are unchanged.

Resolved by session and buffered exactly like depth (per `(robot, node)` and layer, 30 s,
overwritten per layer, dropped with a rejected node). The buffers (image, depth, costmap; each)
are capped at `UPLOAD_BUFFER_MAX_BYTES` (256 MiB of base64) and `UPLOAD_BUFFER_MAX_ENTRIES`
(5000): a further upload is dropped and counted as `buffer_full` in `MAP.INGEST_REJECTED`
(logged once a minute). A buffered upload keeps its parsed fields; the record, with its map-frame
fields, is made when it is stored, with the map_T_session the node was stored with. Stored as `map-{id}/{node}/costmap/{layer}.png`
(PNG first) and, on the ArangoDB node, `costmap.{layer}` = only these fields (anything else in
the message is not stored): `layer`, `width`, `height`, `resolution`, `origin` and
`origin_pose3d` (robot frame, parsed to numbers; `origin_pose3d` only when sent), `frame`,
`source_frame`, `costmap_stamp_ms`, `keyframe_stamp_ms`, `stamp_offset_ms` and `source_topic`
(as sent, each only when present), plus `origin_map` (`x`, `y`, `yaw` through the
session's map_T_session, as the node pose), `origin_pose3d_map` (when `origin_pose3d` was sent)
and `session_id`. A record is set only once its node's document exists: one that arrives while
the node is still being written is buffered and stored with it. A dropped one counts as
`dropped_costmap` in `MAP.INGEST_REJECTED`; stats: `costmap_saved`, `costmap_rejected`,
`buffered_costmap`. Several layers (or cameras) buffered for one node are written to its
document in one update.

### Resent and buffered uploads (phase 1)

A robot may send a node or its parts again (buffered while offline, MQTT redelivery). The service
is built for it; robots that send nothing new keep working.

- **QoS.** `robot/node_update`, `image_upload`, `depth_upload` and `costmap_upload` are subscribed
  at QoS 1 (the granted QoS is the lower of the robot's publish QoS and this): a robot must publish
  at QoS 1 to get the broker's redelivery. This is not an acknowledgement of processing (the
  PUBACK goes out when the message is handed to the service, before the node is stored; see
  "Robot ingest contract" below for the acks).
- **Persistent session (opt-in).** `GRAPH_BUILDER_MQTT_PERSISTENT_SESSION=true` makes graph-builder
  connect with `GRAPH_BUILDER_MQTT_CLIENT_ID` (default `graph_builder`) and `clean_session=False`,
  so the broker queues QoS 1 messages while it is down. Default off: the queue is bounded by the
  broker (mosquitto `max_queued_messages` / `max_queued_bytes`, not configured in this repo) and
  only one graph-builder may use the client id. Topics are subscribed again on every reconnect and
  the reconnect watchdog is unchanged.
- **Deterministic node ids.** The global node id is
  `uuid5(ns, "{robot_name}:{session_id}:{session_node_id}")`, with the open cloud session's id (not
  the payload's), and `:{run_id}` appended when the robot sends one. The same robot node always
  has the same id, so uploads find it again after a restart.
- **Duplicate nodes.** A node whose id is stored already (in memory, the node-id LRU
  `NODE_EXISTS_CACHE_SIZE`, or ArangoDB) and complete is a no-op: first write wins (a resend with
  another pose or metadata does not change the node), no `add_node`, no new edges, no
  `node_count`, no WebSocket `node_added`; counted in `nodes_duplicate`. Buffered images / depth /
  costmaps are still attached.
- **Complete nodes.** A new node is written with `ingest_complete: false` and set to `true` only
  after its edges are in and `node_count` was bumped. A resend of a node that is stored but not
  complete (edges or count failed, or graph-builder died in between) is repaired: its missing
  edges are created and it is counted once, then marked complete. A node document *without* the
  field was stored before the marker existed and counts as complete, so existing graphs are never
  re-processed. New edges get the deterministic `_key` `"{from}:{to}"` and are inserted with
  `overwrite_mode="ignore"`, so a repair never duplicates an edge; edges of older nodes keep their
  auto keys. Residuals: if the count succeeded but writing the marker failed (or graph-builder
  died between the count and the marker), a later repair counts the node again, so `node_count`
  can be 1 too high for that node. A repair's `edges_created` also counts the edges that already
  existed and were ignored. If the edges fail on the first attempt the node is not counted or
  published until its resend repairs it. If only the count fails, the node is published at once
  and a repair in the same process does not publish (or log its mission waypoint) a second time;
  after a restart the repair does. A manual `POST /node` (no session) has no marker.
- **Concurrent resends.** A resend that arrives while the same node is being stored waits (a
  worker thread blocked on an event, no lock, no polling; at most `NODE_CLAIM_WAIT_S` = 15 s; at most `NODE_CLAIM_WAIT_THREADS` = 8 resends wait at once,
  the rest are dropped as duplicates immediately, and node storing runs on its own pool of that
  size + 4 so waiters never starve the attempt they wait for)
  until that attempt is finished, then looks again: a finished node makes it a duplicate, a failed
  attempt lets it store or repair the node. After the timeout it is dropped as a duplicate.
- **ArangoDB errors.** When an existence lookup fails the node is neither absent nor present:
  nothing is written and the error is counted (`errors`); a part is buffered. The robot's resend
  or the buffer cleanup settles it.
- **Parts after a restart.** An image is stored at once when its node exists in ArangoDB; a depth
  or costmap record is set on the existing document. Only a node that does not exist yet makes an
  upload wait in the buffer (30 s).
- **`run_id` (optional, all four payloads).** A string that identifies the robot's node-id
  *sequence*: created once, persisted, surviving robot restarts, changed only when the ids are
  deliberately reset. The same `run_id` with higher ids is a continuation, also across a Finish
  and a new cloud session. A changed `run_id` resets the robot's state (as a smaller id used to);
  a lower `session_node_id` with the same `run_id` is just a resend.
- **Mixed `run_id` presence.** A node may have been stored with or without it. A message is looked
  up under the ids it could have: the payload's run, the robot's last-known run, and no run (a
  node message with a `run_id` only under its own; an upload of a run other than the robot's
  current one only under its own). Memory is checked first, then ArangoDB per candidate, stopping
  at the first hit. After a restart the robot's last run is looked up once per robot (one unindexed
  scan of the robot's nodes in the session; a failed lookup is retried after 30 s, not on every
  message) when a message lacks `run_id`. A robot known to use `run_id`
  never gets the legacy reset below from a message without one. A `run_id` that appears
  mid-stream loses nothing: the earlier run-less nodes are found as duplicates. Limit: if the
  first `run_id` coincides with a counter reset, the new run's ids can be taken for the run-less
  nodes' ids by an upload. Buffered uploads carrying another `run_id` than the node are
  discarded when the node arrives.
- **Legacy robots (no `run_id` ever): limits.** The fallback is the old heuristic: a smaller id
  than any seen before is a restart. It is not deterministic across a graph-builder restart (the
  reset token is memory only; after a restart a restarted counter looks like duplicates), a resend
  of an older backlog is not deduplicated (it is taken for a restart and creates new nodes), and a
  resend that straddles a Finish / new cloud session creates a second node in the new session's
  map (the cloud session is part of the id). Robots should send `run_id`.

### Robot ingest contract (acks, ingest_state, spool_state, run_id)

Phase 2. QoS 1 PUBACK only means "handed to graph-builder"; the robot learns what happened to a
node from these messages (`acks.py`). Robots that send none of the new fields keep working: they
get one ack per node with `run_id: null`, `complete: null`.

**Messages from the robot.** `robot/node_update` may carry `parts`: the parts it will send for the
node, `[{"kind": "image"|"depth"|"costmap", "sub": "<camera or layer>"}]` (the `sub` of an image
is its `camera_name`, of a depth image its `camera_name`, of a costmap its `layer`), and `run_id`
(on all four message types or on none). Unknown extra fields, and manifest entries that do not
match, are ignored. The robot also publishes `robot/<robot_name>/spool_state`, retained, QoS 1:
`{"run_id", "last_spooled_id", "depth"}`; graph-builder keeps the latest per robot (bounded by
robots) and shows it in `GET /stats` as `spool_state`. `depth` is the number of spool entries
not yet complete or terminally rejected.

**Finish gate.** `GET /ingest/backlog/{robot_name}[?run_id=]` (read-only) answers
`{robot_name, run_id, known, pending, depth, last_spooled_id, highest_complete_id, open_trackers,
spool_age_s}` for the robot's current run (`run_id` if given, else the latest accepted node's run,
else the spool_state's). `pending` is `depth > 0`; `known` is false when there is no spool_state of
that run with an integer depth (then `pending` is false and `depth` null). The API's Finish of a
mapping session polls it (packages/api/ingest_backlog.py) and waits for `depth == 0`: 409
`backlog_pending` after 60 s without progress (depth falls, `highest_complete_id` rises, or
`open_trackers` falls); a drain that keeps progressing is not cut off, only by a hard ceiling of
30 min in all (`FINISH_BACKLOG_MAX_S`, reason `cap`); `?force=true` finishes anyway. `highest_complete_id` is
only a liveness signal there, never the gate (it is a maximum, not a prefix). A paused session
refuses uploads (`session_paused`, retryable), so its backlog does not drain.
`POST /api/v1/maps/{id}/sessions` with `replace: true` runs the same wait on the mapping session it
replaces (same limits, 409 and `?force=true`); delete paths never wait.

**Ack.** Topic `robot/<robot_name>/ingest_ack`, QoS 1, not retained:

```json
{"v": 1, "robot_name": "r1", "run_id": "…"|null, "session_node_id": 7,
 "status": "accepted"|"duplicate"|"rejected", "reason": null|"<reason>",
 "resend": "none"|"parts"|"node",
 "parts_stored": [{"kind": "image", "sub": "left"}], "parts_missing": [{"kind": "depth", "sub": "left"}],
 "complete": true|false|null, "session_id": "<cloud session id>"|null, "ts": 1760000000000}
```

An ack is keyed by `(run_id, session_node_id)`: the robot ignores one whose `run_id` is not the one
of the bundle it is sending. It is sent only when the state is durable: after the node is stored,
counted and marked complete (not when the message arrives), after a part is in MinIO (and, for
depth / costmap, on the node document), or when the message is refused.

- `accepted`: the node was newly stored. `duplicate`: it was stored already (a success for the
  robot). `rejected`: the node was **not** stored; the one exception is the final ack of a bundle
  one of whose parts was refused for a terminal reason, which says `rejected` with that reason
  although the node is stored (the robot drops the bundle either way).
- `resend`: `node` = send the node_update again (only for a rejected node with a retryable reason);
  `parts` = send exactly the parts in `parts_missing`, never the node_update; `none` = nothing
  (bundle complete, or a terminal rejection).
- `complete`: `true` when every declared part is stored (an empty manifest counts); `false` while
  parts are missing or pending; `null` when the node has no manifest, so its parts cannot be
  judged. A node without a manifest is acked once, when it is stored (`complete: null`, `resend:
  none`), and does not advance `ingest_state`.
- **With a manifest** the first ack comes when the node is accepted / duplicate / rejected
  (`complete: false`, `parts_missing` = declared parts not stored yet, `resend: none` for a new
  node because its parts are on the way, `parts` for a duplicate). A **final** ack with the same
  key follows once, when the last declared part is stored (`complete: true`, `resend: none`), or as
  soon as every declared part is either stored or was refused (`complete: false`, `resend: parts`
  with the refused ones; a terminal refusal ends the bundle with `rejected`), or
  `INGEST_ACK_PART_TIMEOUT_S` (120) after the node was accepted: `complete: false`, `resend: parts`,
  `parts_missing` = what never arrived. The tracker survives a timeout ack, so the resent parts
  still end in the final ack (at most 3 timeout acks per bundle, `MAX_TIMEOUT_ACKS`).
- **Duplicate nodes** are answered from storage, not memory: `depth.{sub}` / `costmap.{sub}`
  records of the node document (one read) and the node's images in MinIO (one listing, only for
  declared images), on the duplicate path only. Complete: `duplicate`, `complete: true`, `resend:
  none`; otherwise `resend: parts` with `parts_missing`. A read that fails counts the parts as
  missing (a resent part is idempotent).
- **A rejected part** of a node that has a pending tracker is recorded (see the final ack above).
  A rejected part without one (the node is unknown, was rejected, or graph-builder restarted)
  gets its own ack: `rejected`, the reason, `resend: parts` (retryable) or `none` (terminal),
  `parts_missing` = that part, with the part's own `run_id` and `session_node_id`. Identical acks
  of one key are sent at most once a second (`acks_coalesced`). A malformed part that names its
  node gets `invalid_payload`; a message that cannot name its node (no `robot_name` or
  `session_node_id`) is only counted (`acks_unackable`, `errors`).

| reason | resend | when |
|---|---|---|
| `no_session` | none | no open session (also a RUNNING mission with `register_map = false`) |
| `map_deleting`, `map_missing` | none | the session's map is being deleted / has no row |
| `datum_changed` | none | the session's datum cannot be re-anchored: finish the session |
| `run_id_mismatch` | none | see below |
| `invalid_payload` | none | the node or part cannot be stored (missing / malformed fields) |
| `session_paused`, `session_unplaced` | node (part: parts) | try again when the session takes data |
| `lookup_failed` | node (part: parts) | Postgres or an ArangoDB existence check failed |
| `buffer_full` | node (part: parts) | a part found the upload buffer at its cap |
| `store_failed` | node (part: parts) | the write (node, edges, count, MinIO, record) failed |

All except `invalid_payload` and `store_failed` are the `MAP.INGEST_REJECTED` reasons (the event
keeps its rate limit and counts); `run_id_mismatch` is one of them too.

**Import (replay into a named session).** A bundle recorded earlier (a spool kept while no session
was open) is replayed with `import_session_id` (or the robot's spelling: `import: true` and `session_id` = the
target) on the node and on every part: the target is that
cloud session, finished or open, not the robot's open one. Optional `source_session_id` is the
cloud session the bundle was recorded in; when present it must equal the target. Reasons, in
addition to the table (`ingest.py` "Import"):

| reason | resend | when |
|---|---|---|
| `session_unknown` | none | no such session, or it is another robot's |
| `source_mismatch` | none | `source_session_id` is not the target (another frame) |
| `session_busy` | node (part: parts) | the robot has an open session: replay when it is closed |

`map_missing`, `map_deleting`, `session_unplaced` and `run_id_mismatch` apply to the target. The
target's own `map_T_session` is used; the robot's datum is not compared and nothing is realigned.
An import must carry `run_id` (else `invalid_payload`) and the target as a uuid (else
`session_unknown`, terminal). Its acks carry `"import": true`, `import_session_id` (the target,
canonical) and the target as `session_id` (also when rejected: the requested target), are tracked per target
session (the same ids replayed to two sessions do not collide), and **never** advance
`ingest_state` or count in `open_trackers`. No UI node or image update and no mission waypoint is made, and its rejections are counted
apart (`import_rejected`), not as live drops. An import into the robot's open session is
refused (`session_busy`): that check is read fresh per message.
The robot must delete a replayed bundle only on `complete: true`. There is no authentication
beyond `robot_name`: restrict who may publish on the node topics with the broker's ACLs.

**`ingest_state`.** Topic `robot/<robot_name>/ingest_state`, QoS 1, **retained**:
`{"v": 1, "robot_name", "run_id", "highest_complete_id": <int>, "ts"}`: the highest
`session_node_id` whose bundle is complete (the node stored and `complete: true`) for the current
`run_id`. It only grows within a `run_id` (ids may have gaps), and restarts at the first complete
id of a new `run_id`. It is published on change, at most every `INGEST_STATE_MIN_INTERVAL_S` (1 s)
per robot; a periodic task (every second) publishes the last value that was held back. After a
restart graph-builder subscribes to `robot/+/ingest_state` and, for `INGEST_STATE_SEED_SETTLE_S`
(5 s) after its first connect, takes the retained values as the starting state of robots it has no
state for (later messages are mostly its own publishes echoing back and are ignored). The broker's
retained message is the only persistence: if the broker loses its retained store the value
restarts from the next completed node. Nothing is written to Postgres. The seed is merged with a
state this process already created (a completion before the retained message arrived): same
`run_id`, the higher id; another `run_id`, the local one. **Latest run:** the run most recently
seen on a node accepted by this process (run_ids are unique, a run seen before never becomes the
latest again). A completion of any other run (a late part or node of a superseded run) is still
acked but never changes `ingest_state`. A node whose handler fails or is cancelled after the
bundle was announced and before its first ack is answered `rejected: store_failed, resend: node`;
a tracker that never got an ack is dropped after 2 x `INGEST_ACK_PART_TIMEOUT_S`.

**`ingest_mode`.** Topic `robot/<robot_name>/ingest_mode`, QoS 1, **retained**:
`{"v": 1, "robot_name", "register_map"?: <bool>, "ts"}`, the `register_map` flag of the robot's
RUNNING mission (the robot's order parser drops it). It is a sibling of `ingest_state`, not a field
of it, so that publishing it can never overwrite the retained `highest_complete_id` (before a seed
or without a completed bundle there is no honest value to put next to it). `register_map: false`:
the mission does not register the map, every node would be answered `rejected: no_session`, so the
robot may skip spooling. `true`: as before. **A missing field, or a missing/unreadable topic, is
"unknown": spool as before.** It is never `false` unless a RUNNING mission says so; a mission that
ends, or a failed lookup, never produces `false` (a failed lookup publishes nothing). A periodic
task (every 2 s) reads the RUNNING missions and publishes only on change (no flapping); a robot
that had a value and has no RUNNING mission any more gets the payload without `register_map`. After
a restart the retained values are read (at any time, not only in the `ingest_state` settle window,
for robots this process has not published for) so a stale `false`, even one delivered late, is
cleared at the next tick unless a RUNNING mission says `false`. A value counts as published only
once the MQTT client accepted it (queued while disconnected counts); with no client, a full queue
or an error the next tick retries. Publishing failures are counted (`acks_failed` /
`acks_dropped`) and never break ingest. Counter: `ingest_mode_published`.
**Lag invariant for the robot:** after a mission ends, a `false` can stay up to ~2 s (one tick)
(longer while the broker is unreachable). The robot must therefore re-read `ingest_mode` when a
mapping session starts, and treat `false` as authoritative only for the mission it belongs to.

**`run_id` guard.** A cloud session binds the first `run_id` it sees (found with the newest stored
node of the session that has one, else the first message's; remembered per session id, at most
1024 sessions). A node or part with another `run_id` is refused as `run_id_mismatch` before any
state is touched: in particular before the run_id-change reset, so it cannot wipe the legitimate
run's mappings or buffers. A message without `run_id` is never refused. If the lookup fails the
message is let through and nothing is bound (retried after 30 s). A genuine counter restart of the
robot therefore needs a new cloud session (finish and start one); `run_id` changes across
sessions still reset the robot's in-memory state as before.

**For the robot.**
1. Send the `node_update` first (with `parts` and `run_id`), then its parts, in increasing id
   order, one bundle in flight.
2. Wait for an ack with the bundle's `(run_id, session_node_id)`; delete the bundle only on
   `complete: true` or a terminal rejection (`resend: none` with `status: rejected`).
3. Never send the `node_update` again unless `resend == "node"`; on `resend == "parts"` send only
   `parts_missing`. After a restart or when no ack comes (see below) resend the node: a duplicate
   is answered from storage and says what is missing.
4. `ingest_state` (retained, so available when the robot reconnects) tells the highest complete id
   without waiting for the acks it missed.

**Guarantees and non-guarantees.**
- Acks are QoS 1 but are lost if the robot is offline when they are published (not retained, and
  the robot's session may be clean): use `ingest_state` and resending. The resend is always safe.
- graph-builder's pending trackers (and the ack coalescing table) are in memory, bounded by
  `INGEST_ACK_TRACKER_MAX` (20000 bundles, oldest evicted, `ack_trackers_evicted`) and lost on a
  restart; the robot's resend then gets the truth from storage. A part stored with no tracker is
  not acked.
- Acks and `ingest_state` wait in paho's outgoing queue while the broker is away, capped at
  `INGEST_MQTT_MAX_QUEUED` (10000; a publish that does not fit is `acks_dropped`). A failing
  publish is logged and counted (`acks_failed`) and never reaches ingest. A robot name that is not
  a single topic level (empty, `/`, `+`, `#`) is not acked (`acks_invalid_robot`).
- The `node_count` residual of phase 1 remains: if the count succeeded but the completion marker
  could not be written, a later repair of that node counts it again. The node is acked
  `accepted` in that case (it is complete in this process).
- A node being stored by another attempt (a concurrent resend) is not acked by the waiting
  attempt; the storing attempt acks it.
- Finish waiting in the API is not implemented here.

**Config** (`packages/config.py`, environment): `INGEST_ACK_PART_TIMEOUT_S` (120),
`INGEST_ACK_TRACKER_MAX` (20000), `INGEST_STATE_MIN_INTERVAL_S` (1.0),
`INGEST_STATE_SEED_SETTLE_S` (5.0), `INGEST_MQTT_MAX_QUEUED` (10000, 0 = unlimited). Counters in
`GET /stats`: `acks_sent`, `acks_failed`, `acks_dropped`, `acks_coalesced`, `acks_invalid_robot`,
`acks_unackable`, `ack_trackers_evicted`, `ack_timeouts`, `ingest_state_published`,
`run_id_mismatch`; plus `ingest_state`, `spool_state` and `ack_trackers` (bundles waiting for
parts).

## How It Works

### Node Processing Pipeline

1. **Receive MQTT Message**: Robot publishes node update to MQTT topic
2. **Save Images**: Store all images to Image Database (MinIO)
3. **Find Nearby Nodes**: Query Graph Database for nodes within radius threshold (default 5m)
4. **Check Traversability**: For each nearby node, use Similarity Service to check if traversable
5. **Create Edges**: Create bidirectional edges to all traversable nodes
6. **Save Node**: Store node with metadata to Graph Database
7. **Save Edges**: Store all edges to Graph Database

### Example Flow

```
Robot publishes node at (10.5, 20.3)
    ↓
Graph Builder receives MQTT message
    ↓
Save 2 images to Image DB
    ↓
Find nodes within 5m radius → Found 3 nodes
    ↓
Check traversability:
  - Node 100 at (12.0, 21.0): distance=1.8m → traversable ✓
  - Node 101 at (8.5, 19.5): distance=2.4m → traversable ✓
  - Node 102 at (15.0, 25.0): distance=6.5m → not traversable ✗
    ↓
Create 4 edges:
  - 1001 → 100
  - 100 → 1001
  - 1001 → 101
  - 101 → 1001
    ↓
Save node 1001 to Graph DB
Save 4 edges to Graph DB
    ↓
Done! ✅
```

## Docker Deployment

### Build Image

```bash
cd packages/services/graph_builder
docker build -t graph-builder-service:latest .
```

### Run Container

```bash
docker run -d \
  --name graph-builder-service \
  -p 8004:8004 \
  -e MQTT_HOST=mosquitto \
  -e MQTT_PORT=1883 \
  -e IMAGE_DB_URL=http://image-db-service:6002 \
  -e GRAPH_DB_URL=http://graph-db-service:6001 \
  -e DISTANCE_THRESHOLD=5.0 \
  --network mission-dispatch-network \
  graph-builder-service:latest
```

## Monitoring

### Check Service Health

```bash
curl http://localhost:8004/health
```

### View Statistics

```bash
curl http://localhost:8004/stats
```

### Check Logs

```bash
# If running with Docker
docker logs -f graph-builder-service

# If running directly
# Logs are output to stdout
```

## Troubleshooting

### MQTT Not Connected

- Check MQTT broker is running: `docker ps | grep mosquitto`
- Verify MQTT host/port configuration
- Check network connectivity

### Images Not Saving

- Verify Image DB service is running: `curl http://localhost:6002/health`
- Check image data is base64 encoded
- Verify MinIO is accessible

### No Edges Created

- Check Similarity Service is running: `curl http://localhost:8003/health`
- Verify radius threshold is appropriate for your use case
- Check that nearby nodes exist in the database

### Nodes Not Saving

- Verify Graph DB service is running: `curl http://localhost:6001/health`
- Check ArangoDB is accessible
- Ensure node_id is unique

## Configuration

### Radius Threshold

The radius threshold determines how far to search for nearby nodes. Default is 5.0 meters.

- **Too small**: Nodes won't connect, graph will be fragmented
- **Too large**: Too many edges, slower queries, less meaningful connections

Recommended values:
- Indoor environments: 3-5 meters
- Outdoor environments: 5-10 meters
- Warehouse: 5-7 meters

### MQTT Topic

Default topic is `robot/node_update`. You can customize this based on your robot fleet:

- Single robot: `robot/node_update`
- Multiple robots: `robot/+/node_update` (wildcard)
- Specific robot: `robot/robot_01/node_update`

## Development

### Running Tests

```bash
# TODO: Add tests
pytest packages/services/graph_builder/tests/
```

### Code Structure

```
packages/services/graph_builder/
├── __init__.py           # Package initialization
├── server.py             # Core GraphBuilderService logic
├── main.py               # FastAPI application
├── requirements.txt      # Python dependencies
├── .env.example          # Example configuration
├── Dockerfile            # Docker build configuration
└── README.md             # This file
```

