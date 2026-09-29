# SatiNav Maps: 3D reconstruction

**Status:** design, revised 2026-09-29. Not built. Build order: reconstruction starts **after
maps U6** (in progress now; `docs/satinav-maps-redesign.md` §14.9). The spec for the external
service's developer is [`handover.md`](handover.md).

**Revision 2026-09-29 (user decision):** the reconstruction runs in a **separate FastAPI
service in its own repository**, outside cloud_server (not `~/satinavrobotics/pcconstruction`).
It may run on this machine or on another one (e.g. with a GPU). cloud_server gets a thin
**gateway** that owns the job, sends it and its data to the service, and receives the result.
The service has no database or MinIO credentials.

---

## 1. Goal

Rebuild a 3D point cloud of a topological map from its nodes: each node's pose, its RGB image
and a new **depth image**. Show a cheap 2.5D view of it in the client's map view.

Why: it is mainly a UI feature. The robot's Odin driver relocalizes on its own (we do not touch
it). When it does, the operator wants to see on the map whether that worked: the robot marker,
its live costmap and its camera should line up with the walls and objects of the
reconstruction.

**Goals**

- Robot sends a dense depth image and camera parameters with every node.
- An external service turns a map's nodes into one 3D point cloud, as a background job with
  status and progress. cloud_server owns the job, its lifecycle and the result.
- The full cloud is stored as **3D** (binary PLY). A **2.5D** top-down product is derived from
  it for the client.
- One reconstruction per map. It is marked **stale** when the map changed after it was built,
  and replaced when it is rebuilt.
- It goes away with its map.

**Non-goals (v1)**

- No pose refinement (ICP, bundle adjustment, loop closure). Poses are good enough (decided).
- No meshes, no textures, no semantic labels.
- Old nodes without depth are not reconstructed; they are skipped (decided).
- No 3D viewer in the client in v1 (the full cloud is downloadable; §14).
- No change to the Odin driver, its relocalization, or `sati_odin_gpu_bridge` (decided, Q-R2).
- No automatic rebuild: only from the button (decided, Q-R4).

---

## 2. Architecture

```
robot (sati_topo_mapping)                                    cloud_server
  ├─ RGB JPEG per camera      ── MQTT robot/image_upload ─▶ graph-builder ─▶ MinIO map-{id}/{node}/images/{cam}
  ├─ depth PNG + camera JSON  ── MQTT robot/depth_upload ─▶ graph-builder ─▶ MinIO map-{id}/{node}/depth/{cam}.png
  │                                                                      └─▶ ArangoDB node.depth.{cam} (params)
  └─ node pose                ── MQTT robot/node_update  ─▶ graph-builder ─▶ ArangoDB nodes_{map} (map-frame pose)

client "Reconstruct" ─▶ POST /api/v1/maps/{map}/reconstruction ─▶ api-delegation-service (gateway)
                                                                    ├─ row map_reconstructions (queued)
                                                                    └─ dispatcher: manifest (poses, params,
                                                                       presigned GET/PUT URLs, callback)
                                                                              │ POST /jobs
                          reconstruction service (own repo, any host) ◀───────┘
                            ├─ GET inputs  ◀── MinIO presigned GET (map bucket)
                            ├─ compute (numpy / GPU)
                            ├─ PUT outputs ──▶ MinIO presigned PUT (staging bucket)
                            └─ POST progress / finish / fail ──▶ gateway /internal/reconstruction/…
gateway on finish: verify outputs ─▶ copy staging → map-{id}/reconstruction/{job}/ ─▶ row succeeded
                   ─▶ MAP.RECONSTRUCTION_FINISHED
client polls GET …/reconstruction ─▶ loads ortho.png + meta.json ─▶ deck.gl BitmapLayer
```

### 2.1 Who owns what

| cloud_server (gateway in api-delegation-service) | reconstruction service (external) |
|---|---|
| Postgres table `map_reconstructions`, job states, map↔job lifecycle | An in-memory job list plus an on-disk spool of manifests |
| Client routes `/api/v1/maps/{map}/reconstruction…`, auth, status polling | `POST /jobs`, `GET /jobs/{id}`, `POST /jobs/{id}/cancel`, `GET /health` |
| Events `MAP.RECONSTRUCTION_*` | Nothing |
| MinIO: inputs, staging, final storage; presigning | Only the presigned URLs it is given |
| Stale digest, map delete, rebuild/supersede | Nothing about maps beyond one manifest |
| Retry, timeout, poll fallback | Idempotency per `job_id`, cancel, memory caps, concurrency 1 |

Why this split:

- **No credentials outside cloud_server.** Presigned URLs give the service exactly the objects
  of one job, for a few hours. A service on another machine cannot read other maps, and a leak
  of its host exposes nothing lasting.
- **The service is replaceable.** Anything that speaks the manifest and callback contract can
  do the work (a GPU version, a TSDF version later) without touching cloud_server.
- **Lifecycle stays where the map lives.** Map delete, archive, stale and supersede are Postgres
  transactions in the API that already owns maps.

---

## 3. Frames: which pose to use

Read `docs/satinav-maps-redesign.md` §3, §4, §6 and §14 first. In short:

- A robot's poses are in its **run frame** (its own `map` TF frame; it resets at every navstack
  start).
- A map has its own **map frame**. A session's `map_T_session` (x, y, yaw) places the robot's
  current run frame in the map frame (`packages/utils/map_sessions.py`).
- graph-builder converts each node at ingest: ArangoDB `pose` = `map_T_session` ⊕ robot pose
  (`ingest.map_pose` → `map_geo.apply_pose`). It keeps the robot-frame pose as `robot_pose`.

**Rule: the reconstruction uses the ArangoDB node's `pose` {x, y, yaw}. It is already in the map
frame. Never use `robot_pose`, and never re-apply a session transform.** The gateway puts only
map-frame poses into the manifest; the service never sees a run-frame pose.

Consequences:

- Since U1 graph-builder only stores nodes of a **placed** session. Depth is only stored for
  nodes that exist, so every node with depth has a valid map-frame pose.
- Legacy nodes (M1/M2, never placed) have no depth and are never used. If M6 later aligns such
  a session, its node poses change and the reconstruction becomes stale (§8.3).
- For a **geo** map, the map frame is UTM grid metres relative to `spec.geo.origin_e/origin_n`
  (zone `spec.geo.utm_zone`). The cloud stays in that local frame (float32 is enough). The CRS
  and origin go into the manifest, `meta.json` and the PLY header.

**The full camera pose of one depth image:**

```
T_map_cam  = T_map_base · T_base_cam
T_map_base = pose3d_map                          when present (full 6-DoF, at the depth stamp)
           = Trans(x, y, 0) · Rz(yaw)            else (node pose, flat-ground assumption)
```

- `x, y, yaw`: the ArangoDB node `pose` (map frame).
- `pose3d_map`: the robot's full `map`→`base_link` TF at the **depth** stamp (`robot_pose3d`,
  §4.3), converted by graph-builder with the same `map_T_session`. `map_T_session` is a rotation
  about z plus an x/y translation, so z, roll and pitch pass through unchanged. It also removes
  the small time offset between the RGB frame (node pose stamp) and the depth frame (§4.2).
- `T_base_cam`: the camera extrinsic, `base_link` ← depth image `frame_id` (optical frame).

The exact math, conventions and a worked example are in `handover.md` §5.

---

## 4. Robot side (sati_ros_navstack, `sati_topo_mapping`) — step R1

File: `sati_topo_mapping/sati_topo_mapping/topomap_node.py`. This section is the spec for R1.

### 4.1 How capture works today

- A keyframe trigger (dt/radius gate or an external trigger) calls `start_capture()`.
- It subscribes (best-effort QoS) to each camera's raw `sensor_msgs/Image` topic, takes the
  **first** frame per camera, JPEG-encodes it once (`encode_image`, optional resize, quality
  85), and drops the subscription.
- `capture_tick()` closes the capture when all cameras delivered or after `capture_timeout`
  (2 s). The node pose is the TF `map`→`base_link` at the **earliest** frame stamp (x, y, yaw
  only), with a 0.5 s retry, then the latest TF.
- `create_new_node()` publishes one JSON message per camera on `robot/image_upload`
  (`session_node_id`, `robot_name`, `camera_name`, `image_data` = base64 JPEG, `timestamp` ms,
  `yaw_offset`), then the node on `robot/node_update`.

Config today: real robot `communication_odin.yaml` → camera `left`, `/odin1/image`
(448x336). Sim `navstack_sati.yaml` → camera `left`, `/left/image_raw`.

### 4.2 Depth capture and time matching

New parameters (per camera, by index like `image_topics`):

| Parameter | Real robot | Sim | Meaning |
|---|---|---|---|
| `depth_topics` | `/odin1/depth_dense_image` | `/left/depth` | `''` = no depth for that camera |
| `camera_info_topics` | `/odin1/camera_info` | `/left/camera_info` (check) | intrinsics |
| `depth_max_stamp_diff` | `0.08` | `0.02` | max \|depth stamp − RGB stamp\| (s) |
| `send_depth` | `true` | `true` | master switch, default `false` |

- Real robot: `odin1/depth_dense_image` is `32FC1` metres, optical-axis (z) depth, valid
  0.2–15 m. Published **only** by the GPU pipeline with `enable_dense_depth:=true`
  (`sati_odin_bridge/sati_odin_driver/launch/odin_pipeline_ros2.launch.py`). It is projected
  onto the published RGB grid (448x336, undistorted), so the RGB intrinsics apply to it.
- Sim: Isaac Sim `/left/depth`. It must be **distance to image plane** (z depth), not distance
  to camera. Check in R1 (Q-R1).

**Time matching.** The Odin tensor packer stamps the depth with the **LiDAR cloud** stamp, not
the image stamp (`odin_tensor_packer_node.cpp`: `header.stamp = cloud_msg->header.stamp`). The
image is about 39 ms before the cloud (p99 55 ms; pairing limit `sync_max_interval_s` 0.08 s).
The rule:

1. At `start_capture()`, also subscribe to the depth topic. Keep the last 5 depth messages in a
   small ring.
2. When the RGB frame of that camera is captured, wait (same capture, same timeout) until a
   depth message with stamp ≥ the RGB stamp has arrived.
3. Pick the depth message with the **nearest** stamp. Accept it only if the difference is ≤
   `depth_max_stamp_diff` (80 ms on the robot). Otherwise send the node without depth (warn
   once per minute).
4. Record both stamps.

At 10 Hz frames are 100 ms apart, so nearest-stamp picks the depth built from our image in
almost all cases. The rare p99 case can pick the neighbour frame: ≈ 10 cm error at 1 m/s.
Accepted (Q-R2: `sati_odin_gpu_bridge` is not changed). Depth is only subscribed while a
capture is pending.

### 4.3 Payload

Depth goes on a **new topic `robot/depth_upload`**, one message per camera. Not on
`robot/image_upload`: graph-builder keys images by `camera_name`, so today's graph-builder
would store the depth PNG **over** the RGB JPEG. A new topic is ignored by an old graph-builder,
so robot and cloud can be deployed in any order.

```json
{
  "session_node_id": 42,
  "robot_name": "robot",
  "camera_name": "left",
  "depth_data": "<base64 PNG>",
  "content_type": "image/png",
  "depth_encoding": "u16_mm",
  "depth_scale": 0.001,
  "depth_stamp_ms": 1727600000123,
  "rgb_stamp_ms": 1727600000084,
  "robot_pose3d": {"x": 1.2, "y": -0.4, "z": 0.02, "qx": 0.0, "qy": 0.01, "qz": 0.38, "qw": 0.92},
  "camera": {
    "frame_id": "camera",
    "width": 448, "height": 336,
    "fx": 0.0, "fy": 0.0, "cx": 0.0, "cy": 0.0,
    "distortion_model": "plumb_bob", "d": [0, 0, 0, 0, 0],
    "depth_type": "z",
    "valid_range_m": [0.2, 15.0],
    "rgb_width": 448, "rgb_height": 336,
    "T_base_cam": {"x": 0.1, "y": 0.0, "z": 0.45, "qx": -0.5, "qy": 0.5, "qz": -0.5, "qw": 0.5}
  }
}
```

(Numbers are placeholders.) `robot_pose3d` is the TF `map`→`base_link` at `depth_stamp_ms`
(run frame). `T_base_cam` is the TF `base_link`←`frame_id` of the depth image, in the
**optical** convention (x right, y down, z forward); if the depth `frame_id` is not an optical
frame, the robot sends the optical transform (Q-R1). `d` must be all zeros (rectified).

**Camera parameters: per node, inside each depth message** (decided). Not once per session:

- The robot does not know when a session starts (it only follows `mapping/set`); a missed
  once-per-session message would make a whole session useless. Per node, every depth image is
  self-contained.
- Calibration can change (another robot extends the map, a resolution change, a recalibration).
- It is cheap: about 0.5 KB of JSON per node, against a 40 KB JPEG.
- `camera_info` on Odin is latched (transient-local). The topomap caches the last
  `camera_info` per camera and the static TF; it does not re-read them per node.

### 4.4 Encoding

- `32FC1` metres → `uint16` millimetres: `round(d * 1000)`; 0 = no data.
- NaN, ±inf, ≤ 0, and values > 65.535 m → 0. Do **not** clip to 65535 (a fake wall at 65 m).
- `16UC1` input is already mm: pass it through.
- Lossless PNG, `cv2.imencode('.png', mm, [cv2.IMWRITE_PNG_COMPRESSION, 3])`.

### 4.5 Message sizes

Measured on synthetic 448x336 frames (scratch script, not in the repo):

| Payload | Raw | Encoded | base64 (on MQTT) |
|---|---|---|---|
| RGB JPEG q85 (today) | 452 KB | ~25–45 KB | ~35–60 KB |
| Odin dense depth (64x48 grid, 7x upsample, 30 % empty) | 301 KB | **~9–12 KB** | ~12–16 KB |
| Sim dense depth, smooth + 2 mm noise | 301 KB | ~90 KB | ~120 KB |
| Sim dense depth, 1 cm noise | 301 KB | ~150 KB | ~200 KB |
| Worst case (pure noise) | 301 KB | ~300 KB | ~400 KB |
| Camera JSON + pose | — | ~0.5 KB | ~0.5 KB |

`packages/utils/mosquitto.sh` sets no `message_size_limit` / `max_packet_size`, so the broker
accepts up to MQTT's 256 MB. If payloads ever grow too big: keep depth at 448x336 (half
resolution in the sim), send binary instead of base64 (−25 %), upload over a presigned URL like
ROS bags, and set `max_packet_size 4194304` as a guard (Q-R5).

---

## 5. Ingest (graph-builder) — step R2

graph-builder already resolves sessions and buffers images until their node arrives
(`packages/services/graph_builder/server.py`: `_handle_image_upload`, `image_buffer`). Depth
follows the same path.

- Subscribe to `robot/depth_upload` (env `MQTT_DEPTH_TOPIC`, default in `packages/config.py`).
- Resolve the session with `self.sessions.resolve(robot_name, session_id)` exactly like an
  image. A rejected depth is counted as kind `depth` in `MAP.INGEST_REJECTED`
  (`dropped_depth`, new optional field of `MapIngestRejected`).
- Buffer until the node exists (key `(robot_name, session_node_id)`, `IMAGE_BUFFER_TIMEOUT`).
- Store the PNG in the map bucket `map-{id}`: `{node_id}/depth/{camera}.png`, content type
  `image/png`.
- Store the parameters **on the ArangoDB node** (not as a MinIO JSON), with
  `graph_db.update_node(map_id, node_id, metadata=...)`:

  ```json
  "depth": {
    "left": {
      "camera": { "...": "the message's camera block" },
      "depth_scale": 0.001,
      "depth_stamp_ms": 1727600000123, "rgb_stamp_ms": 1727600000084,
      "robot_pose3d": {"x": 1.2, "y": -0.4, "z": 0.02, "qx": 0, "qy": 0.01, "qz": 0.38, "qw": 0.92},
      "pose3d_map":   {"x": 4.1, "y": -1.9, "z": 0.02, "qx": 0, "qy": 0.01, "qz": 0.61, "qw": 0.79},
      "session_id": "…"
    }
  }
  ```

  `pose3d_map` = `robot_pose3d` with the session's `map_T_session` applied (x, y, yaw change;
  z, roll, pitch do not). Why on the node: the gateway builds the manifest (poses **and**
  camera parameters) from one ArangoDB collection scan, instead of one MinIO GET per node.
  ~0.7 KB per camera per node document.
- The node's WS update is unchanged. No new event.

Why `{node_id}/depth/` and not `{node_id}/images/`: `ImageDatabaseService.list_images` and the
client's image list only look at `{node}/images/`, so depth never shows up as a photo.

One fix needed: `ImageDatabaseService.get_stats(map_id)` counts every top-level prefix as a
node, so `reconstruction/` would count as one. Filter it (R2).

---

## 6. The contract between gateway and service

Full request/response examples are in `handover.md` §2–§3. This section records the choices.

### 6.1 The manifest (gateway → service, `POST /jobs`)

One JSON document per job attempt, built **at dispatch time** (not when the user clicks), so
the URLs are fresh:

- `job_id` (the Postgres row's uuid), `attempt`, `map` {name, type, crs or null}.
- `params`: `voxel_m`, `max_depth_m`, `clip_z`, `edge_rel`, `min_neighbours`, `max_voxels`,
  `raster_max_px`.
- `nodes`, sorted by `created_at`: `node_id`, map-frame `pose` {x, y, yaw}, and per camera with
  depth: the camera parameters, `depth_scale`, optional `pose3d` (= `pose3d_map`), a presigned
  **GET** URL for the RGB JPEG and one for the depth PNG.
- `outputs`: a presigned **PUT** URL and content type for `cloud.ply`, `ortho.png`,
  `height.png`, `meta.json`, all in the **staging bucket** (§6.4).
- `callback`: base URL of the gateway's callback routes for this job, and a per-job bearer
  token.
- `expires_at`: when the URLs stop working.

Size: ~1 KB per node-camera (two ~400-byte URLs plus parameters), so ~1 MB for 1 000 nodes and
~20 MB at the 20 000-node limit. The service accepts bodies up to 32 MB. No paging: one
self-contained document is easier to test, replay and debug.

### 6.2 URL expiry

Decision: **URLs valid for `RECONSTRUCTION_URL_EXPIRY_S` = 4 h; no refresh endpoint in v1.**

- The gateway sends at most `RECONSTRUCTION_MAX_INFLIGHT` = 1 job at a time; everything else
  waits as `queued` in Postgres and is presigned only when sent. So URLs never age in the
  service's queue; a job only has to finish within its own timeout (1 h).
- 4 h > job timeout (1 h) + a service-side restart and replay (§6.5). MinIO allows up to 7 days.
- If a URL expires anyway (a clock skew, a very slow host), the service gets 403, fails the job
  with reason `url_expired`, and the gateway retries it once with a new manifest (§6.5). A
  refresh endpoint would cost a third API for a case the retry already covers.

### 6.3 Callbacks (service → gateway)

Routes on the API (port 8000), **not under `/api/`**, so the client's nginx (`location /api/`)
never forwards them to browsers:

| Route | When | Body |
|---|---|---|
| `POST /internal/reconstruction/jobs/{job_id}/progress` | at start, then every ≤ 10 s | `{attempt, stage, progress, frames_done, frames_total}` |
| `POST /internal/reconstruction/jobs/{job_id}/finish` | after all outputs are PUT | `{attempt, result, outputs: {name: {bytes, sha256}}}` |
| `POST /internal/reconstruction/jobs/{job_id}/fail` | on error or after a cancel | `{attempt, reason, stage, message}` |

- **Auth:** `Authorization: Bearer <token>`. The token is
  `base64url(HMAC-SHA256(RECONSTRUCTION_CALLBACK_SECRET, job_id))`: nothing secret is stored in
  Postgres, the gateway recomputes and compares in constant time. Wrong token → 401.
- **Answers:** `200 {"action": "continue"}`; `200 {"action": "cancel"}` when the user asked to
  cancel (so a lost `POST /jobs/{id}/cancel` still reaches the service); `410 {"action":
  "stop"}` when the job is no longer `running` in Postgres or the attempt is old (cancelled,
  superseded, timed out, map deleted). A 410 tells the service to stop and forget the job.
- `finish` and `fail` are retried by the service (backoff up to 10 min); `progress` is fire and
  forget. The gateway's poll (§6.5) covers anything that is still lost.

### 6.4 Where outputs go: a staging bucket, then a copy

The service PUTs into a separate bucket, `RECONSTRUCTION_STAGING_BUCKET` = `recon-staging`,
key `{job_id}/{attempt}/{file}`. On `finish` the gateway checks each object (`stat_object`:
size matches, `meta.json` parses), copies it server-side into the map bucket at
`reconstruction/{job_id}/{file}`, then deletes the staging prefix.

Why not presigned PUTs straight into the map bucket:

- **Late writes are harmless.** A cancelled or superseded job whose service is still running
  can only write to its own staging prefix. The staging bucket has a MinIO lifecycle rule
  (expire after 1 day) set by the gateway at start, so stray objects disappear on their own.
  Nothing unverified ever appears next to the map's data.
- **Map delete stays simple.** The map bucket is written only by cloud_server. The copy never
  creates a bucket; a copy into a bucket that is gone fails, and the job fails
  (`map_deleting`).
- Cost: one server-side copy (≤ ~200 MB, a few seconds on the same disk). The 2.5D files are
  small.

`recon-staging` cannot collide with a map bucket (those all start with `map-`).

### 6.5 Dispatch, retries, poll fallback

A dispatcher task in the API process (started in the app lifespan next to the map-delete saga,
guarded by a Postgres advisory lock so only one runs) loops every 5 s and on each `POST`:

1. **Send.** If fewer than `MAX_INFLIGHT` jobs are `running`, take the oldest `queued` row with
   `next_try_at ≤ now()` (`FOR UPDATE SKIP LOCKED`), build the manifest, `POST /jobs`.
   - 202/200 → `running`, `dispatched_at`, `attempts + 1`, `last_contact_at = now()`.
   - Connection error, timeout, 5xx, 429 (service queue full) → stays `queued`, `next_try_at`
     backoff 10 s, 30 s, 1 min, then every 2 min. After `RECONSTRUCTION_QUEUE_TIMEOUT_S` (30 min)
     since `requested_at` → `failed` (`service_unavailable`).
   - 4xx other than 409/429 → `failed` (`rejected`, with the service's message).
2. **Poll.** A `running` row with no callback for 60 s → `GET /jobs/{id}`:
   - `running`/`queued` → `last_contact_at = now()`, copy progress.
   - `succeeded` → run the finish handling with the poll body (a lost `finish` callback).
   - `failed`/`cancelled` → run the fail handling.
   - 404 (the service lost it: a wiped spool, another host) → resubmit with a new manifest if
     `attempts < 2`, else `failed` (`lost`).
   - Unreachable for more than 5 min → `failed` (`service_unavailable`).
3. **Timeout.** `running` longer than `RECONSTRUCTION_JOB_TIMEOUT_S` (1 h) → best-effort
   `POST /jobs/{id}/cancel`, `failed` (`timeout`).
4. **Auto-retry once** for `url_expired` and `lost` (new attempt, same `job_id`). No auto-retry
   for `error`, `crashed`, `timeout`: the user sees the error and clicks Retry.

Why queue in Postgres and not fail at once when the service is down: the service may run on a
machine that is booting or sleeping; one click should survive a short outage, but a job must
not wait forever. An API restart loses nothing: the dispatcher resumes from the table.

The service side (handover §6): idempotent per `(job_id, attempt)` (a repeat POST returns the
existing job; the same `job_id` with a higher attempt replaces the old one), concurrency 1, a
small queue (default 4), manifests spooled on disk so a restart resumes queued and running
jobs (a running job restarts from the beginning), terminal states kept 24 h for the poll.

### 6.6 Failure modes

| Case | What happens |
|---|---|
| Service down at dispatch | Row stays `queued`, retried with backoff; 30 min → `failed` (`service_unavailable`) |
| Service dies mid-job | Its spool resumes the job on restart; if not back in 5 min → `failed` (`service_unavailable`); a later callback gets 410 |
| Callback lost | Poll after 60 s of silence reads the service's job state and applies it |
| Service forgot the job | Poll gets 404 → one resubmit, then `failed` (`lost`) |
| URL expired | Service fails `url_expired` → one automatic retry with fresh URLs |
| Service out of memory | The service's child dies → it reports `crashed` → `failed` (`crashed`) |
| User cancel | Row `cancel_requested`; gateway `POST /jobs/{id}/cancel`; also answered on the next progress callback. The service's `fail` (`cancelled`) or a 60 s grace ends the row `cancelled`; staging prefix deleted |
| Map deleted mid-job | Map delete marks the active job `cancelled` (`map_deleting`) in the same transaction, then the gateway sends a best-effort cancel. Late callbacks → 410. Late PUTs land in staging and expire. A finish racing the delete: the gateway checks the map is not `DELETING` before the copy, and the copy cannot create the deleted bucket. If a copy lands while the bucket is being emptied, `remove_bucket` fails `BucketNotEmpty` and the delete saga retries |
| Map grew mid-job | The input digest is taken when the manifest is built; the next `GET` reports `stale: true` (§8.3). No action |
| Superseded attempt reports | Callbacks carry `attempt`; an old attempt gets 410 |
| API restarts | Dispatcher resumes from Postgres; running jobs are polled |
| Finish verification fails (object missing, size mismatch) | `failed` (`bad_output`); the previous result stays |

A failed or cancelled job never touches the previous good result.

### 6.7 Network and security

- **The service is never public.** It binds to `127.0.0.1` (same host) or to the host's
  Tailscale address (another host), and its host firewall accepts port 8009 only from the
  cloud_server host. No nginx route points to it.
- **Gateway → service:** `Authorization: Bearer RECONSTRUCTION_SERVICE_KEY` on every call
  (defence in depth on top of Tailscale).
- **Service → gateway:** per-job HMAC token (§6.3); the callback base URL is
  `RECONSTRUCTION_CALLBACK_BASE_URL` (default `http://localhost:8000`; for another host, the
  cloud host's Tailscale name, e.g. `http://sati-cloud:8000`).
- **Service → MinIO:** presigned URLs only. WireGuard (Tailscale) encrypts the traffic; plain
  `http://` inside the tailnet is fine.
- **Client → gateway:** the same as every other `/api/v1` route (the client's nginx on the
  tailnet; the API has no per-user auth today). `requested_by` is filled when an identity
  exists.

**MinIO endpoint for presigning.** MinIO runs with host networking on port 9000 and listens on
all interfaces. A presigned URL embeds the host it was signed for, and SigV4 signs the `Host`
header, so a URL **cannot be rewritten** after signing (`MinioBase._rewrite_presigned_url`
is a no-op for this reason). The gateway therefore presigns with a second MinIO client created
with `RECONSTRUCTION_MINIO_ENDPOINT`:

- Service on the same host: `localhost:9000` (default).
- Service on another host: the cloud host's Tailscale name or IP, e.g. `sati-cloud:9000`, which
  the service host must resolve and reach. The cloud host's firewall must allow 9000 from the
  tailnet only.
- The presign client gets `region="us-east-1"` so presigning makes no network call.

### 6.8 Where things go in cloud_server

| What | Where |
|---|---|
| Config keys (§6.9) | `packages/config.py` |
| Gateway logic: SQL, dispatcher, manifest builder, presigning, callbacks, finish/commit, stale digest, file streaming | `packages/api/reconstruction.py` (pure helpers testable without I/O) |
| httpx client for the service | `packages/api/reconstruction_client.py` |
| Client routes + callback routes | `packages/api/main.py` (logic in `reconstruction.py`) |
| Migration | `packages/api/migrations/versions/<date>_01_map_reconstructions.py`, `down_revision` = head at build time (today `20261001_01_run_epochs`) |
| Map delete hook | `packages/api/map_delete.py`: in `request()` (same transaction as `MARK_SQL`) mark the active job `cancelled` (`map_deleting`), then gateway cancel; in `_finish`, `DELETE FROM map_reconstructions WHERE map_name = %s` next to `SESSIONS_SQL` |
| Events | `packages/events/codes.py`, `packages/events/schemas.py`, source `reconstruction` |
| Depth ingest | `packages/services/graph_builder/` (R2), `get_stats` filter in `packages/topomap_dbs/image_db/server.py` |
| Docs | `packages/api/README.md` (routes), `CLAUDE.md` (data flow: the external service) |

Nothing goes into `docker_compose/`: the service has its own repo and its own deployment.

### 6.9 Config keys (`packages/config.py`)

| Key | Default | Meaning |
|---|---|---|
| `RECONSTRUCTION_SERVICE_URL` | unset | Service base URL. Unset = feature off (`POST` → 503 `not_configured`) |
| `RECONSTRUCTION_SERVICE_KEY` | unset | Bearer key for gateway → service |
| `RECONSTRUCTION_CALLBACK_SECRET` | unset | HMAC key for per-job callback tokens |
| `RECONSTRUCTION_CALLBACK_BASE_URL` | `http://localhost:8000` | How the service reaches the API |
| `RECONSTRUCTION_MINIO_ENDPOINT` | `localhost:9000` | MinIO host:port as the service sees it (presigning) |
| `RECONSTRUCTION_MINIO_SECURE` | `false` | https for presigned URLs |
| `RECONSTRUCTION_STAGING_BUCKET` | `recon-staging` | Output staging bucket (1-day expiry rule) |
| `RECONSTRUCTION_URL_EXPIRY_S` | `14400` | Presigned URL lifetime |
| `RECONSTRUCTION_JOB_TIMEOUT_S` | `3600` | Running → `failed` (`timeout`) |
| `RECONSTRUCTION_QUEUE_TIMEOUT_S` | `1800` | Queued with the service down → `failed` |
| `RECONSTRUCTION_MAX_INFLIGHT` | `1` | Jobs sent to the service at once |
| `RECONSTRUCTION_VOXEL_M`, `_MAX_DEPTH_M`, `_CLIP_Z` | `0.05`, `10.0`, `2.0` | Default job parameters |
| `MQTT_DEPTH_TOPIC` | `robot/depth_upload` | R2 |

The three secrets are **not** added to the import-time required list (that would break every
other service); the gateway refuses to start jobs (503 `not_configured`) when any is missing.

---

## 7. Algorithm and storage formats

The algorithm (decode, edge filter, back-project, transform, voxel accumulate, neighbour filter,
rasterize) is specified for the service's developer in `handover.md` §5–§7. Parameters and
limits:

| Parameter | Default | Range / note |
|---|---|---|
| `voxel_m` | 0.05 | 0.02–0.5; coarsened ×1.5 and restarted when above `max_voxels` |
| `max_depth_m` | 10.0 | 0.5–65; far LiDAR points are sparse and noisy |
| `clip_z` | 2.0 | relative to the floor estimate (median base z); −5–20 |
| `edge_rel` | 0.05 | flying-pixel filter |
| `min_neighbours` | 2 | of 26 |
| `max_voxels` | 10 M | memory bound |
| `raster_max_px` | 4096 | safe WebGL texture size |
| Nodes per job | ≤ 20 000 | gateway refuses more (422) |

### 7.1 Full 3D: binary PLY

One binary little-endian PLY per reconstruction, `cloud.ply`: `float x, y, z; uchar red, green,
blue; ushort count` (17 bytes per point), map frame, metres; comments carry map, job, voxel and
(geo maps) CRS and origin.

Why PLY: every tool reads it (Open3D, CloudCompare, MeshLab, PDAL, loaders.gl `PLYLoader` for a
later deck.gl `PointCloudLayer`); writing it is `header + ndarray.tobytes()`; colour and count
fit naturally.

| Format | Size / point | Why not in v1 |
|---|---|---|
| PCD binary | 16 B | Same as PLY, fewer tools outside ROS/PCL; the client can't load it easily |
| LAZ | ~3–5 B | Smaller and geo-native, needs `laspy` + `lazrs`; a good **export** later |
| Draco | ~1–2 B | Lossy; a delivery format, not an archive |
| COPC / 3D Tiles / Potree | — | For > ~50 M points; ours are 1–10 M |

Sizes (estimates): Odin 300 nodes ≈ 0.5–1 M points, 9–17 MB; Odin 1 000 nodes ≈ 25–50 MB; sim
1 000 nodes (per-pixel depth) ≈ 50–170 MB.

### 7.2 Derived 2.5D product for the map view

| File | Content |
|---|---|
| `ortho.png` | RGBA 8-bit top view. Per cell the colour of the **highest** voxel **below** the clip height (floor + `clip_z`), so ceilings and tree tops don't hide the floor and walls. Alpha 0 = no data |
| `height.png` | 16-bit grey. That voxel's z as `round((z − z_offset) / z_scale) + 1`; 0 = no data (`z_scale` 0.01 m) |
| `meta.json` | grid, frame, CRS, bounds (format in `handover.md` §7.4) |

Resolution = `max(voxel_m, longest extent / 4096)`. A 200 m x 100 m map at 5 cm = 4000 x 2000 px,
`ortho.png` ≈ 3–8 MB, `height.png` ≈ 1–4 MB. Tiling only when maps get bigger: later.

**How the client draws it:** `GET …/reconstruction` → fetch `meta.json`, then `ortho.png`
(`?v={job_id}`, cached forever) → a `BitmapLayer` with the 4 map-frame corners: through the
view's world→pixel projection for a local map; map frame → UTM (+ origin) → lat/lon with
`utils/mapTransform.ts` for a geo map (four corners, because UTM is rotated against lat/lon).
Optional "colour by height" from `height.png` on a canvas.

---

## 8. Data model and lifecycle (cloud_server)

### 8.1 Postgres

New table, Alembic migration in the API's chain. No foreign key to `mapobjectv1` (created at
runtime, as for `map_sessions`).

```sql
CREATE TABLE map_reconstructions (
  job_id           uuid PRIMARY KEY,
  map_name         text NOT NULL,
  state            text NOT NULL CHECK (state IN
                     ('queued','running','succeeded','failed','cancelled','superseded')),
  requested_at     timestamptz NOT NULL DEFAULT now(),
  requested_by     text,
  attempts         smallint NOT NULL DEFAULT 0,   -- manifests sent
  next_try_at      timestamptz,                   -- queued: next dispatch try
  dispatched_at    timestamptz,                   -- last successful POST /jobs
  started_at       timestamptz,                   -- first progress callback
  finished_at      timestamptz,
  last_contact_at  timestamptz,                   -- last callback or successful poll
  cancel_requested boolean NOT NULL DEFAULT false,
  stage            text,                          -- as reported by the service
  progress         real NOT NULL DEFAULT 0,       -- 0..1
  params           jsonb NOT NULL DEFAULT '{}'::jsonb,
  inputs           jsonb,   -- {nodes_total, nodes_with_depth, frames, digest, nodes:[[id,x,y,yaw],…]}
  result           jsonb,   -- from the finish callback: {points, frames_used, frames_skipped, bounds3d, voxel_m, duration_s}
  artifacts        jsonb,   -- {bucket, prefix, files:{cloud|ortho|height|meta:{key,bytes,sha256,content_type}}}
  error            jsonb    -- {reason, stage, message}
);
CREATE UNIQUE INDEX map_reconstructions_one_active
  ON map_reconstructions (map_name) WHERE state IN ('queued','running');
CREATE UNIQUE INDEX map_reconstructions_one_current
  ON map_reconstructions (map_name) WHERE state = 'succeeded';
CREATE INDEX map_reconstructions_by_map ON map_reconstructions (map_name, requested_at DESC);
CREATE INDEX map_reconstructions_active ON map_reconstructions (state) WHERE state IN ('queued','running');
```

The same migration widens `fleet_events_source_check` with the source `reconstruction` (as
`20260929_01_maps_m2` did for `graph_builder`).

### 8.2 States

```
queued ──▶ running ──▶ succeeded ──(newer job succeeds, or DELETE)──▶ superseded
  │           ├──▶ failed      (error, crashed, timeout, service_unavailable, lost,
  │           │                 url_expired after its retry, rejected, bad_output)
  │           └──▶ cancelled   (user, map_deleting)
  └──▶ failed (service_unavailable, rejected) / cancelled
```

- **Start:** insert `queued` (the unique index refuses a second active job → 409).
- **Commit on finish** (after the staging copy, one transaction): map row exists and is not
  `DELETING`; old `succeeded` → `superseded`; this job → `succeeded` with `result` and
  `artifacts`; `MAP.RECONSTRUCTION_FINISHED` in a savepoint. Then (outside) delete the old
  result's prefix in the map bucket and the staging prefix.
- A failed or cancelled job keeps the previous result untouched: at most one good
  reconstruction per map, and a failed rebuild never removes it.
- **API start:** the dispatcher resumes; also delete any `reconstruction/{job}/` prefix in a map
  bucket whose job is not `succeeded` (orphans from a crash between copy and commit).
- `superseded` rows are kept as history (a few hundred bytes each; the `inputs.nodes` list is
  dropped on supersede).

### 8.3 Stale flag

`inputs.digest` = SHA-256 over the sorted list of `(node_id, round(x, 3), round(y, 3),
round(yaw, 4), sorted(depth cameras))` of the nodes with depth, taken when the manifest is
built. On `GET`, the gateway recomputes it from ArangoDB (one scan; cached 10 s per map) and
reports `stale: true` with `stale_reason: {new_nodes, removed_nodes, moved_nodes}` (from
`inputs.nodes`). This catches a map that grew (also during the job), node deletion, and M6
alignment. **Rebuild** = a new job; on success the old reconstruction is superseded and its
objects deleted.

### 8.4 MinIO keys

| Bucket / key | Written by |
|---|---|
| `map-{id}/{node_id}/images/{camera}` | graph-builder (today) |
| `map-{id}/{node_id}/depth/{camera}.png` | graph-builder (R2) |
| `recon-staging/{job_id}/{attempt}/{cloud.ply,ortho.png,height.png,meta.json}` | the service, via presigned PUT |
| `map-{id}/reconstruction/{job_id}/{cloud.ply,ortho.png,height.png,meta.json}` | the gateway (server-side copy) |

The gateway creates `recon-staging` at start (with the 1-day expiry rule). It **never creates a
map bucket**.

### 8.5 Map delete, archive

- **Delete** (`packages/api/map_delete.py`): the bucket delete removes the reconstruction
  objects. `request()` cancels the active job (§6.6); `_finish` deletes the rows in the same
  transaction as the map row.
- **Archive / restore:** the reconstruction is kept. Starting a job on an archived map is
  allowed (read-only work).
- Maps cannot be renamed (M1), so no rename path.

---

## 9. API (gateway, api-delegation-service, port 8000)

### 9.1 Client routes

| Method | Path | Does |
|---|---|---|
| POST | `/api/v1/maps/{map}/reconstruction` | Start (or rebuild). Body optional: `{voxel_m?, max_depth_m?, clip_z?}`. 202 + the job |
| GET | `/api/v1/maps/{map}/reconstruction` | Current result (+ `stale`) and the active or last failed job |
| POST | `/api/v1/maps/{map}/reconstruction/cancel` | Cancel the active job |
| DELETE | `/api/v1/maps/{map}/reconstruction` | Delete the current result (and cancel an active job). 204 |
| GET | `/api/v1/maps/{map}/reconstruction/files/{name}` | `cloud.ply`, `ortho.png`, `height.png`, `meta.json`; streamed from the map bucket, `ETag` = job id, immutable cache with `?v=` |

Errors: 404 `map_not_found`; 409 `map_deleting`; 409 `job_active` (body has the job); 409
`no_depth`; 422 bad parameters or too many nodes; 503 `not_configured`. The service being down
is **not** an error at `POST` (the job queues; the status shows "waiting for the reconstruction
service").

Status body (`GET`):

```json
{
  "map_name": "lab",
  "reconstruction": {
    "job_id": "1f7d…", "finished_at": "2026-10-19T17:02:41Z",
    "params": {"voxel_m": 0.05, "max_depth_m": 10.0, "clip_z": 2.0},
    "points": 2310455, "nodes_total": 450, "nodes_used": 412,
    "frames_skipped": {"no_valid_depth": 3},
    "bounds3d": {"min": [-12.35, -40.10, -1.2], "max": [79.65, 25.9, 6.3]},
    "stale": true, "stale_reason": {"new_nodes": 12, "removed_nodes": 0, "moved_nodes": 0},
    "files": {
      "cloud": {"name": "cloud.ply", "bytes": 39277735, "url": "/api/v1/maps/lab/reconstruction/files/cloud.ply?v=1f7d…"},
      "ortho": {"name": "ortho.png", "bytes": 4120334, "url": "…/files/ortho.png?v=1f7d…"},
      "height": {"name": "height.png", "bytes": 1893002, "url": "…/files/height.png?v=1f7d…"},
      "meta": {"name": "meta.json", "bytes": 612, "url": "…/files/meta.json?v=1f7d…"}
    }
  },
  "job": {
    "job_id": "5b0c…", "state": "running", "stage": "integrating", "progress": 0.43,
    "frames_done": 177, "frames_total": 412, "attempts": 1,
    "waiting_for_service": false, "error": null
  }
}
```

`job` is the active job; if none, the newest job **if** it failed or was cancelled after the
current result; else `null`.

### 9.2 Internal callback routes

`/internal/reconstruction/jobs/{job_id}/progress|finish|fail` (§6.3; examples in `handover.md`
§3). Not under `/api/`.

### 9.3 Events

Source `reconstruction`, no robot, discriminator `map:<name>:reconstruction:<job_id>:<state>`:

| Code | Severity | Payload |
|---|---|---|
| `MAP.RECONSTRUCTION_STARTED` | info | `map_name, job_id, params, nodes_with_depth` (on the first progress callback) |
| `MAP.RECONSTRUCTION_FINISHED` | info | `map_name, job_id, points, nodes_used, frames_skipped, voxel_m, duration_s` |
| `MAP.RECONSTRUCTION_FAILED` | warning | `map_name, job_id, reason, stage, message` (reasons of §8.2, incl. `cancelled`, `map_deleting`) |

Progress is **not** an event. The client polls `GET …/reconstruction` every 2 s while a job is
active. A WebSocket push can hook into the progress callback later (the API already has the WS
manager); not needed for minute-long jobs.

---

## 10. Client (sati-client) — step R5

Builds on the map window (§14.7 of the maps design, `components/mapWindow/`) and `MapView`.

- **Map window, Details tab:** a "3D reconstruction" row.
  - none → **Reconstruct** (disabled with a reason when the map has no depth nodes:
    "Recorded before depth capture").
  - queued → "Waiting for the reconstruction service…" when `waiting_for_service`.
  - running → progress bar with stage and %, **Cancel**.
  - succeeded → "Built {time} · {points} points · {nodes_used}/{nodes_total} nodes",
    **Show on map**, **Rebuild**, **Download (PLY)**. Stale: warn tone "Map changed since
    ({new_nodes} new nodes)", Rebuild becomes primary.
  - failed → the error and **Retry**.
- **Model:** a pure `buildReconstructionModel(status)` in `utils/`, unit-tested.
- **Layer:** `MapLayerId` `reconstruction` ("3D reconstruction (top view)"), above background
  and grid, below costmap, nodes and robot; `BitmapLayer` with linear texture filtering.

---

## 11. Tests

All test containers on this host are memory-capped (`--memory=2g --memory-swap=2g`) with the
kill timeout **inside** the container. Pydantic v1 in cloud_server.

**cloud_server unit** (`tests/unit/test_map_reconstruction_*.py`):

- Manifest builder: only nodes with depth; map-frame poses (never `robot_pose`); `pose3d` passed
  through; URLs for the right keys; node limit.
- Callback token: HMAC, constant-time check, 401 on a wrong token; 410 for a non-running job,
  an old attempt, a deleted map; `cancel` action when `cancel_requested`.
- Dispatcher: backoff and queue timeout; 202 → running; 404 on poll → one resubmit then `lost`;
  poll `succeeded` → finish; job timeout; `url_expired` → one retry.
- Finish: staging verification (missing, size mismatch → `bad_output`), copy, supersede, old
  prefix deleted, failure keeps the old result, refused for a `DELETING` map.
- Digest and stale reasons (new, removed, moved).
- Routes: 404/409/422/503 mapping; file streaming headers.
- Presign client uses `RECONSTRUCTION_MINIO_ENDPOINT` (the URL host is that endpoint).
- graph-builder: depth buffered and stored; `depth.{cam}` on the node; `pose3d_map` keeps
  z/roll/pitch; rejected like an image; old robots unchanged.
- Map delete: `request()` cancels the active job; `_finish` deletes the rows.
- Event codes and schemas registered.

**cloud_server integration** (`tests/integration/reconstruction/run.sh`, harness of
`tests/integration/maps/run_m2.sh`): Postgres, ArangoDB, MinIO, mosquitto, graph-builder, the
API from the checkout, and a **stub service** (a few dozen lines in the test dir) that accepts
the manifest, GETs every input URL, PUTs fixed small outputs and calls back. Checks the
plumbing: presigned URLs work from another container (the endpoint rule), staging copy,
supersede, cancel, map delete mid-job (late PUT and late callback harmless, no bucket
re-created), stub down → queued then `service_unavailable`, stub loses the job → resubmit.

**Service** (its own repo): the synthetic scene of `handover.md` §9 checks the geometry.

**Sim end-to-end (R6, manual):** Isaac Sim with `/left/depth`. Map a loop with a placed session
at a non-identity `map_T_session`, reconstruct, compare the ortho view with the sim scene. Then
restart the navstack, place the robot (local) or wait for the datum (geo), and check the live
costmap lines up with the reconstruction.

---

## 12. Build steps

**Starts after maps U6** (in progress now).

| Step | Content | Repo | Needs |
|---|---|---|---|
| R1 | Topomap depth capture (§4): nearest-stamp ≤ 80 ms, u16 mm PNG, camera params per node (cached `camera_info` + static TF), `robot_pose3d`, `robot/depth_upload`; codec/matcher unit tests; `enable_dense_depth:=true` in the navstack launch; verify optical frame and z depth (Q-R1) | sati_ros_navstack | — |
| R2 | graph-builder depth ingest (§5): `MQTT_DEPTH_TOPIC`, PNG in MinIO, `depth.{cam}` on the node, `pose3d_map`, `get_stats` filter, `dropped_depth` | cloud_server | — |
| R3 | Gateway (§6, §8, §9): config, migration, `reconstruction.py` + client, routes, callbacks, dispatcher, staging bucket, events, map-delete hook, stub-service integration test, README | cloud_server | the §5 node shape (not R2's code) |
| R4 | The reconstruction service, per `handover.md`, tested standalone with the synthetic scene | own repo | the contract only |
| R5 | Client: reconstruction row, polling, `reconstruction` layer (local + geo) | sati-client | R3's API (can start on mocked responses) |
| R6 | Deploy (cloud: `~/pg-cutover/scripts/recon.sh`, `--dry-run` with the migration on a throwaway copy; then API and graph-builder; the service by its own deploy), robot rollout, sim end-to-end | all | R1–R5 |

**Parallel:** R1, R2, R3 and R4 can all run at the same time (the MQTT topic, the node shape of
§5 and the manifest/callback contract decouple them). R5 can start with R3 and finishes after
it. R6 needs everything.

---

## 13. Deployment notes

- Same host: the service runs in its own compose project with host networking, bound to
  `127.0.0.1:8009` (8009 is free here), `mem_limit` set; the API env gets
  `RECONSTRUCTION_SERVICE_URL=http://127.0.0.1:8009` and the two secrets.
- GPU host: the service binds to its Tailscale IP; the API env points
  `RECONSTRUCTION_SERVICE_URL` there and sets `RECONSTRUCTION_MINIO_ENDPOINT` and
  `RECONSTRUCTION_CALLBACK_BASE_URL` to the cloud host's Tailscale name. Check from the GPU host:
  `curl http://sati-cloud:9000/minio/health/live` and `curl http://sati-cloud:8000/health`.
- Secrets live in `docker_compose/.env` (cloud) and the service's own env file; never committed.

---

## 14. Later

- Pose refinement (ICP between neighbouring frames, or a pose graph); would also fix M6-style
  misalignment.
- A 3D view in the client: a decimated `preview.ply` and deck.gl `PointCloudLayer`.
- LAZ export for GIS tools on geo maps. Tiled output (COPC / 3D Tiles) for very large maps.
- Incremental update (only new nodes). Meshing (TSDF / Poisson).
- WebSocket progress push. A URL refresh endpoint if jobs ever outlive 4 h.

---

## 15. Open questions

- **Q-R1. Camera frame and depth type.** Is Odin's depth `frame_id` an optical frame (z
  forward)? Is Isaac Sim's `/left/depth` distance-to-image-plane? Assumed; R1 verifies with a
  flat wall at a known distance.
- **Q-R2 (decided 2026-09-29).** No change to `sati_odin_gpu_bridge`; nearest-stamp ≤ 80 ms.
- **Q-R3 (decided 2026-09-29).** Odin sends **dense** depth (`odin1/depth_dense_image`).
- **Q-R4 (decided 2026-09-29).** Rebuild only from the button.
- **Q-R5.** Set a broker `max_packet_size` guard (e.g. 4 MB) now?
- **Q-R6.** Default voxel and clip height (5 cm, floor + 2 m); different for outdoor maps?
- **Q-R7.** Cameras other than `left`: supported by the design, none exists today.
- **Q-R8 (decided 2026-09-29).** The service is external (own repo, any host), reached through
  a gateway with presigned URLs and callbacks.
- **Q-R9.** Where does the service run first (this host, or which GPU host), and its repo name?
- **Q-R10.** Is MinIO port 9000 on the cloud host reachable only from the tailnet today? A
  remote service needs it from the tailnet, and it must not be public.
