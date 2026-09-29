# SatiNav Maps: 3D reconstruction

**Status:** design, 2026-09-29. Not built. It is built **after** maps steps U5, U6 and M5
(`docs/satinav-maps-redesign.md` §14.9). The developer guide is [`handover.md`](handover.md).

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
- A new backend service turns a map's nodes into one 3D point cloud, as a background job with
  status and progress.
- The full cloud is stored as **3D**. A **2.5D** top-down product is derived from it for the
  client.
- One reconstruction per map. It is marked **stale** when the map changed after it was built,
  and replaced when it is rebuilt.
- It goes away with its map.

**Non-goals (v1)**

- No pose refinement (ICP, bundle adjustment, loop closure). Poses are good enough (decided).
  Later improvement, §13.
- No meshes, no textures, no semantic labels.
- Old nodes without depth are not reconstructed. They are skipped (decided).
- No 3D viewer in the client in v1 (the full cloud is downloadable; a 3D view is §13).
- No change to the Odin driver or its relocalization.
- No automatic rebuild: the reconstruction runs only from the button (decided, Q-R4).

---

## 2. End-to-end flow

```
robot (sati_topo_mapping)                                   cloud
  keyframe trigger
  ├─ RGB JPEG per camera      ── MQTT robot/image_upload ─▶ graph-builder ─▶ MinIO {node}/images/{cam}
  ├─ depth PNG + camera JSON  ── MQTT robot/depth_upload ─▶ graph-builder ─▶ MinIO {node}/depth/{cam}.png|.json
  └─ node pose                ── MQTT robot/node_update  ─▶ graph-builder ─▶ ArangoDB nodes_{map} (pose in map frame,
                                                                               depth_cameras: [cam])
client map window: "Reconstruct"
  └─ POST /api/v1/maps/{map}/reconstruction ─▶ api-delegation-service (8000)
                                                 └─ proxy ─▶ map-reconstruction-service (8009)
                                                               ├─ row in Postgres map_reconstructions (queued)
                                                               └─ worker: nodes (ArangoDB) + RGB/depth/JSON (MinIO)
                                                                   ─▶ back-project, map frame, voxel, filter
                                                                   ─▶ MinIO reconstruction/{job}/cloud.ply,
                                                                      ortho.png, height.png, meta.json
                                                                   ─▶ row succeeded + MAP.RECONSTRUCTION_FINISHED
client polls GET .../reconstruction (progress) ─▶ then loads ortho.png + meta.json ─▶ deck.gl BitmapLayer
```

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
frame. Never use `robot_pose`, and never re-apply a session transform.**

Consequences:

- Since U1 graph-builder only stores nodes of a **placed** session (`session_unplaced` is
  rejected). Depth is only stored for nodes that exist. So every node with depth has a valid
  map-frame pose.
- Legacy nodes (M1/M2, never placed, `aligned = false`) have no depth, so they are never used.
  If M6 later aligns such a session, its node poses change and the reconstruction becomes stale
  (§8.3).
- For a **geo** map, the map frame is UTM grid metres relative to `spec.geo.origin_e/origin_n`
  (zone `spec.geo.utm_zone`). The cloud stays in that local frame (small numbers, float32 is
  enough). The CRS and origin go into `meta.json` and the PLY header.

**The full camera pose of one depth image:**

```
T_map_cam = T_map_base · T_base_cam
T_map_base = Trans(x, y, z) · Rz(yaw) · Ry(pitch) · Rx(roll)
```

- `x, y, yaw`: the ArangoDB node `pose` (map frame).
- `z, roll, pitch`: from the robot's 3D pose at the depth stamp, sent with the depth (§4.3).
  They pass through `map_T_session` unchanged, because `map_T_session` is only a rotation about
  z plus an x/y translation. Missing (old robot build): 0, 0, 0 (flat-ground assumption).
- `T_base_cam`: the camera extrinsic, `base_link` ← depth image `frame_id`, sent with the depth.

Better, when the robot sends it: `robot_pose3d`, the full 6-DoF `map`→`base_link` TF at the
**depth** stamp. graph-builder converts it to the map frame with the same `map_T_session` and
stores it as `pose3d_map` in the camera JSON. The service then uses `pose3d_map` instead of the
node pose. This also removes the small time offset between the RGB frame (node pose stamp) and
the depth frame (§4.2).

---

## 4. Robot side (sati_ros_navstack, `sati_topo_mapping`)

File: `sati_topo_mapping/sati_topo_mapping/topomap_node.py`. The robot repo is not changed by
this design; this section is the spec for step R1.

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
  `yaw_offset`), then the node on `robot/node_update` (`session_node_id`, `robot_name`, `x`,
  `y`, `yaw`, `camera_metadata`).

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
  0.2–15 m. It is published **only** by the GPU pipeline with `enable_dense_depth:=true`
  (`sati_odin_bridge/sati_odin_driver/launch/odin_pipeline_ros2.launch.py`). It is projected
  onto the published RGB grid (448x336, undistorted), so the RGB intrinsics apply to it.
- Sim: Isaac Sim `/left/depth`. It must be **distance to image plane** (z depth), not distance
  to camera. Check in R1.

**Time matching.** The Odin tensor packer stamps the depth with the **LiDAR cloud** stamp, not
the image stamp (`odin_tensor_packer_node.cpp`: `header.stamp = cloud_msg->header.stamp`). The
image is about 39 ms before the cloud (p99 55 ms; pairing limit `sync_max_interval_s` 0.08 s).
So stamps never match exactly on the real robot. The rule:

1. At `start_capture()`, also subscribe to the depth topic. Keep the last 5 depth messages in a
   small ring.
2. When the RGB frame of that camera is captured, wait (inside the same capture, same timeout)
   until a depth message with stamp ≥ the RGB stamp has arrived.
3. Pick the depth message with the **nearest** stamp. Accept it only if the difference is ≤
   `depth_max_stamp_diff`. Otherwise send the node without depth (warn once per minute).
4. Record both stamps.

At 10 Hz frames are 100 ms apart, so nearest-stamp picks the depth built from our image in
almost all cases. The rare p99 case (55 ms > 50 ms) can pick the neighbour frame: ≈ 10 cm error
at 1 m/s. Accepted: `sati_odin_gpu_bridge` is not to be changed (decided, Q-R2).

Depth is only subscribed while a capture is pending, like the RGB (no cost between keyframes).

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
(robot run frame). `T_base_cam` is the TF `base_link`→`frame_id` of the depth image. The depth
geometry is assumed to be in the **optical** convention (x right, y down, z forward); if the
depth `frame_id` is not an optical frame, the robot must send the optical transform (Q-R1).
`d` must be all zeros (rectified images); the service refuses a camera with distortion.

**Camera parameters: per node, inside each depth message.** Not once per session. Reasons:

- The robot does not know when a session starts (it only follows `mapping/set`), and a missed
  once-per-session message would make a whole session useless. Per node, every depth image is
  self-contained.
- Calibration can change: a robot with another camera extends the map, a resolution change, a
  recalibration. Per node is always right.
- It is cheap: about 0.5 KB of JSON per node, against a 40 KB JPEG.
- `camera_info` on Odin is latched once (transient-local QoS). The topomap caches the last
  `camera_info` per camera and the static TF; it does not re-read them per node.

### 4.4 Encoding

- `32FC1` metres → `uint16` millimetres: `round(d * 1000)`; 0 = no data.
- NaN, ±inf, ≤ 0, and values > 65.535 m → 0. Do **not** clip large values to 65535: that would
  create a fake wall at 65 m.
- `16UC1` input is already mm: pass it through.
- Lossless PNG, `cv2.imencode('.png', mm, [cv2.IMWRITE_PNG_COMPRESSION, 3])` (fast on the
  Jetson; level 9 saves ~10 % more).
- Resolution: 1 mm, range up to 65.5 m. Odin is valid to 15 m, so this is enough.

### 4.5 Message sizes

Measured on synthetic 448x336 frames (OpenCV PNG level 9; scratch script, not in the repo):

| Payload | Raw | Encoded | base64 (on MQTT) |
|---|---|---|---|
| RGB JPEG q85 (today) | 452 KB | ~25–45 KB | ~35–60 KB |
| Odin dense depth (64x48 grid, 7x nearest upsample, 30 % empty) | 301 KB | **~9–12 KB** | ~12–16 KB |
| Sim dense depth, per-pixel, smooth + 2 mm noise | 301 KB | ~90 KB | ~120 KB |
| Sim dense depth, 1 cm noise | 301 KB | ~150 KB | ~200 KB |
| Worst case (pure noise) | 301 KB | ~300 KB | ~400 KB |
| Camera JSON + pose | — | ~0.5 KB | ~0.5 KB |

Odin's dense depth compresses very well because of its 7x7 blocks (`dense_depth_scale` 7 →
a 64x48 grid upsampled to 448x336). One node with one camera goes from ~50 KB to ~65 KB on the
real robot, and to at most ~0.5 MB in the sim.

**Broker limit.** `packages/utils/mosquitto.sh` writes the broker config. It sets no
`message_size_limit` and no `max_packet_size`, so Mosquitto's default applies: no broker limit
below MQTT's protocol maximum of 256 MB. The depth fits easily. The test broker in
`tests/integration/maps/run_m2.sh` has no limit either.

**If it ever becomes too big** (e.g., several cameras at full resolution over a weak uplink),
in this order:

1. Keep depth at the published 448x336 (never full sensor resolution); for the sim, send depth
   at half resolution with a scaled `camera` block.
2. Send the PNG as a binary MQTT payload (a small JSON header + bytes) instead of base64 JSON:
   −25 %.
3. Upload big payloads over HTTP to a presigned MinIO URL, as ROS bags do
   (`packages/topomap_dbs/rosbag_db/server.py`).
4. Set `max_packet_size 4194304` in `mosquitto.sh` as a guard, so one runaway message cannot
   hurt the broker (Q-R5).

---

## 5. Ingest (graph-builder)

graph-builder already resolves sessions and buffers images until their node arrives
(`packages/services/graph_builder/server.py`: `_handle_image_upload`, `image_buffer`,
`session_to_global_map`). Depth follows the same path.

- Subscribe to `robot/depth_upload` (new env `MQTT_DEPTH_TOPIC`, default in
  `packages/config.py`).
- Resolve the session with `self.sessions.resolve(robot_name, session_id)` exactly like an
  image. A rejected depth is counted as kind `depth` in `MAP.INGEST_REJECTED`
  (`dropped_depth`, new optional field of `MapIngestRejected`).
- Buffer it until the node exists (same key `(robot_name, session_node_id)`, same timeout
  `IMAGE_BUFFER_TIMEOUT`), then store it.
- Store in the map bucket `map-{id}` (`ImageDatabaseService._bucket_name`):
  - `{node_id}/depth/{camera}.png`, content type `image/png`, user metadata `depth_scale`,
    `depth_stamp_ms`.
  - `{node_id}/depth/{camera}.json`: the message's `camera` block, both stamps,
    `robot_pose3d`, and `pose3d_map` (= `robot_pose3d` with the session's `map_t_session`
    applied: x, y, yaw change; z, roll, pitch do not), plus `session_id`.
- Add `camera` to the ArangoDB node field `depth_cameras` (a list), with
  `graph_db.update_node(map_id, node_id, metadata=...)`. The reconstruction service uses it to
  list nodes with depth without touching MinIO.
- The node's WS update is unchanged. No new event.

Why `{node_id}/depth/` and not `{node_id}/images/`: `ImageDatabaseService.list_images` and the
client's image list only look at `{node}/images/`, so depth never shows up as a photo.

One fix needed: `ImageDatabaseService.get_stats(map_id)` counts every top-level prefix as a
node, so `reconstruction/` would count as one. Filter it (R2).

---

## 6. Reconstruction service

New service **`map-reconstruction-service`**, package `packages/services/map_reconstruction/`,
port **8009** (free: `packages/config.py` uses 8000 and 8004–8008; nothing listens on 8009 on
the host). FastAPI, one process, one worker. Details for the builder: `handover.md`.

### 6.1 Algorithm (per job)

1. **List nodes.** `TopomapDatabaseClient.graph.get_all_nodes(map_name)` → keep nodes with a
   non-empty `depth_cameras`. Sort by `created_at`. Record the input digest (§8.3).
2. **Per node and camera** (streamed in batches of 32 frames):
   1. Fetch `{node}/depth/{cam}.png`, `{node}/depth/{cam}.json`, and the RGB
      `{node}/images/{cam}` (`image.get_image(cam, node_id, map_id)`).
   2. Decode depth (`uint16` → metres × `depth_scale`). Mask 0 and values outside
      `valid_range_m` and the job's `max_depth_m` (default 10 m: far LiDAR points are sparse and
      noisy).
   3. **Edge filter** (flying pixels): drop a pixel if any 4-neighbour differs by more than
      `edge_rel` × depth (default 0.05). Odin's dense depth has blocky edges; the sim has
      mixed pixels at silhouettes.
   4. RGB: decode the JPEG; if its size differs from the depth size, resize it to the depth
      size. Colour per pixel.
   5. **Back-project** (z depth, optical frame): `X = (u − cx)·d/fx`, `Y = (v − cy)·d/fy`,
      `Z = d`.
   6. **To the map frame:** `p_map = T_map_base · T_base_cam · p_cam` (§3). Use `pose3d_map`
      when present, else the node `pose` plus flat z/roll/pitch.
   7. **Voxel accumulate** at `voxel_m` (default 0.05 m): key = packed int64 of
      `floor(p / voxel)`; per voxel keep the count, the sum of offsets from the voxel corner
      (float32) and the sum of RGB (uint32). Reduce each batch with `np.unique` + `np.add.at`,
      then merge into the global accumulator.
   8. Every batch: update progress, check `cancel_requested` and the map's lifecycle.
3. **Outlier filter** on the voxel grid: drop a voxel with fewer than `min_neighbours`
   (default 2) occupied voxels in its 26-neighbourhood. Cheap (hash lookups on the keys), no
   k-d tree. Optionally drop voxels seen by only one frame when `min_frames` > 1.
4. **Colour and position:** mean RGB and mean position per voxel.
5. **Write** `cloud.ply`, then derive the 2.5D rasters (§7.2), then `meta.json`.
6. **Upload** to `map-{id}/reconstruction/{job_id}/`. **Commit** (§8.2). Then delete the
   previous result's prefix.

No new heavy dependency: numpy and opencv-python-headless (PNG/JPEG decode and encode). Not
Open3D (a ~400 MB wheel for things we do in 50 lines of numpy).

### 6.2 Limits and memory

| Limit | Default | Why |
|---|---|---|
| One job at a time (whole service) | 1 | memory; jobs queue |
| Container memory | `mem_limit: 2g` | same caps as the tests |
| Voxels | ≤ 10 M; above that the job coarsens `voxel_m` ×1.5 and restarts (recorded in the result) | ~40 B per voxel → ~0.4 GB; ~1 GB peak while merging |
| Frames per batch | 32 | ~32 × 150 k points × ~30 B ≈ 150 MB transient |
| Nodes per job | ≤ 20 000 | runtime |
| Job timeout | 30 min | then `failed` (`timeout`) |
| `voxel_m` | 0.02–0.5 m | request validation |

The heavy work runs in a **child process** (`multiprocessing`, one per job), so the FastAPI
event loop stays responsive and all memory is returned when the job ends. The child writes
progress straight to Postgres (its own connection) and uploads the files. The parent does the
commit and the events. A child killed by the memory limit (exit −9) makes the job `failed`
(`crashed`), and the service keeps running.

Estimated runtime (to be measured in R3): decoding and back-projecting one 448x336 frame takes a
few ms; about 1 minute per 1 000 nodes, dominated by MinIO reads.

---

## 7. Storage formats

### 7.1 Full 3D: binary PLY

**Recommendation: one binary little-endian PLY per reconstruction**, `cloud.ply`:

```
ply
format binary_little_endian 1.0
comment satinav map=<name> job=<job_id> voxel_m=0.05 frame=map
comment crs=EPSG:32634 origin_e=352397.33 origin_n=5262357.80   (geo maps only)
element vertex N
property float x
property float y
property float z
property uchar red
property uchar green
property uchar blue
property ushort count
end_header
```

17 bytes per point. Coordinates in the map frame, metres.

Why PLY:

- Every tool reads it: Open3D, CloudCompare, MeshLab, PDAL, and in the browser loaders.gl
  `PLYLoader` (fits deck.gl `PointCloudLayer` for a later 3D view).
- Writing it is `header + structured_ndarray.tobytes()`: no dependency.
- Colour and a per-point observation count fit naturally.

Alternatives considered:

| Format | Size / point | Why not in v1 |
|---|---|---|
| PCD binary | 16 B | Same as PLY, fewer tools outside ROS/PCL; the client can't load it easily |
| LAZ (compressed LAS) | ~3–5 B | 3–5× smaller and geo-native, but needs `laspy` + `lazrs` and integer scale/offset handling. Good **export** later if sizes hurt |
| Draco | ~1–2 B | Lossy quantisation; a browser-delivery format, not an archive |
| COPC / 3D Tiles / Potree (tiled) | — | For clouds > ~50 M points with streaming LOD. Our clouds are 1–10 M points. Later, if maps grow |

**Sizes** (estimates; the voxel filter bounds them):

| Map | Points after 5 cm voxel | `cloud.ply` |
|---|---|---|
| Odin, 300 nodes (~300 m path) | ~0.5–1 M (only ~3 k depth samples per frame, 64x48 grid) | 9–17 MB |
| Odin, 1 000 nodes | ~1.5–3 M | 25–50 MB |
| Sim, 1 000 nodes (per-pixel depth) | ~3–10 M | 50–170 MB |

At 10 cm the point count drops about 4× on surfaces. The default is **5 cm**: fine enough to see
a door frame, coarse enough for the sizes above.

### 7.2 Derived 2.5D product for the map view

The client (deck.gl, `sati-client/components/DeckGLMap.tsx`, which already draws costmaps with
a `BitmapLayer`) needs something it can draw as one image. The service also writes:

| File | Content |
|---|---|
| `ortho.png` | RGBA 8-bit top-down image. Per cell: the colour of the **highest** voxel **below** `clip_z` (default: floor + 2.0 m, so ceilings and tree tops don't hide the floor and walls). Alpha 0 = no data |
| `height.png` | 16-bit grey. Per cell: that voxel's z as `round((z − z_offset) / z_scale)` + 1; 0 = no data (`z_scale` 0.01 m) |
| `meta.json` | grid and frame description, below |

```json
{
  "version": 1,
  "map_name": "lab", "job_id": "…", "map_type": "geo",
  "frame": "map",
  "crs": {"utm_zone": 34, "utm_north": true, "origin_e": 352397.33, "origin_n": 5262357.80},
  "resolution_m": 0.05,
  "origin": {"x": -12.35, "y": -40.10},
  "width": 1840, "height": 1320,
  "z_offset": -1.2, "z_scale": 0.01,
  "clip_z": 2.0,
  "bounds3d": {"min": [-12.35, -40.10, -1.2], "max": [79.65, 25.9, 6.3]},
  "points": 2310455, "voxel_m": 0.05
}
```

`origin` is the **lower-left corner** of pixel row `height−1`, column 0 (image row 0 is the
north/+y edge). `crs` is null for a local map.

- Resolution 5 cm, but the longest side is capped at **4096 px** (safe WebGL texture size on
  every device); a bigger map gets coarser cells (`resolution_m = extent / 4096`). A
  200 m x 100 m map at 5 cm = 4000 x 2000 px, `ortho.png` ≈ 3–8 MB (sparse, mostly
  transparent), `height.png` ≈ 1–4 MB.
- Tiling (slippy tiles) only when maps exceed ~4096 cells at 5 cm per side: later.

**How the client fetches and draws it:**

1. `GET /api/v1/maps/{map}/reconstruction` → `files.meta.url`, `files.ortho.url`.
2. Fetch `meta.json`, then `ortho.png` (an `<img>`/`ImageBitmap`; URLs carry `?v={job_id}` so
   the browser can cache them forever).
3. A `BitmapLayer` with `image = ortho.png`:
   - **local map:** `bounds` = the 4 map-frame corners through the view's existing
     world→pixel projection (as nodes and the costmap).
   - **geo map:** the 4 corners map frame → UTM (+ origin) → lat/lon with
     `utils/mapTransform.ts` (exact TM, since M0). Four corners, because UTM is rotated
     against lat/lon (as §9 of the maps design says for the grid layer).
4. Optional "colour by height": the client turns `height.png` into a colour ramp on a canvas;
   no server change.

---

## 8. Data model and lifecycle

### 8.1 Postgres

New table, Alembic migration in the API's chain (`packages/api/migrations/versions/`, named
like `20261015_01_map_reconstructions`, `down_revision` = the head at build time; today that is
`20260930_01_maps_use`). No foreign key to `mapobjectv1` (that table is created at runtime, as
for `map_sessions`).

```sql
CREATE TABLE map_reconstructions (
  job_id           uuid PRIMARY KEY,
  map_name         text NOT NULL,
  state            text NOT NULL CHECK (state IN
                     ('queued','running','succeeded','failed','cancelled','superseded')),
  requested_at     timestamptz NOT NULL DEFAULT now(),
  requested_by     text,
  started_at       timestamptz,
  finished_at      timestamptz,
  heartbeat_at     timestamptz,
  cancel_requested boolean NOT NULL DEFAULT false,
  stage            text,              -- listing|integrating|filtering|writing|rasterizing|uploading|committing
  progress         real NOT NULL DEFAULT 0,   -- 0..1
  params           jsonb NOT NULL DEFAULT '{}'::jsonb,  -- voxel_m, max_depth_m, clip_z, ...
  inputs           jsonb,             -- {nodes_total, nodes_with_depth, last_node_at, digest}
  result           jsonb,             -- {points, nodes_used, nodes_skipped:{reason:n}, bounds3d, voxel_m}
  artifacts        jsonb,             -- {bucket, prefix, files:{cloud|ortho|height|meta:{key,bytes,content_type}}}
  error            text
);
-- one queued/running job per map
CREATE UNIQUE INDEX map_reconstructions_one_active
  ON map_reconstructions (map_name) WHERE state IN ('queued','running');
-- one current result per map
CREATE UNIQUE INDEX map_reconstructions_one_current
  ON map_reconstructions (map_name) WHERE state = 'succeeded';
CREATE INDEX map_reconstructions_by_map ON map_reconstructions (map_name, requested_at DESC);
```

The same migration widens `fleet_events_source_check` with the source `reconstruction` (as
`20260929_01_maps_m2` did for `graph_builder`).

### 8.2 States

```
queued ──▶ running ──▶ succeeded ──(newer job succeeds, or DELETE)──▶ superseded
              │
              ├──▶ failed      (error, timeout, crashed, interrupted)
              └──▶ cancelled   (user cancel, map deleting)
```

- **Start:** insert `queued` (the unique index refuses a second active job → 409).
- **Claim:** the worker takes the oldest `queued` row with `FOR UPDATE SKIP LOCKED`, sets
  `running`, `started_at`, `heartbeat_at`.
- **Heartbeat:** every batch (≤ 10 s).
- **Commit** (one transaction): the map row is not `DELETING`; old `succeeded` → `superseded`;
  this job → `succeeded` with `result` and `artifacts`; `MAP.RECONSTRUCTION_FINISHED` in a
  savepoint. Then (outside) delete the old prefix. A failed or cancelled job keeps the previous
  result untouched: the map always has at most one good reconstruction, and a failed rebuild
  never removes it.
- **Service start:** `running` rows with an old heartbeat → `failed` (`interrupted`); delete
  their prefix. Also delete any `reconstruction/{job}/` prefix whose job is not `running` or
  `succeeded` (orphans).
- `superseded` rows are kept as history (a few hundred bytes each).

### 8.3 Stale flag

`inputs.digest` = SHA-256 over the sorted list of `(node_id, round(x, 3), round(y, 3),
round(yaw, 4), sorted(depth_cameras))` of the nodes with depth. On `GET`, the service recomputes
it from ArangoDB (one collection scan; cached 10 s per map) and reports:

- `stale: true` when it differs, with `stale_reason: {new_nodes, removed_nodes, moved_nodes}`.

This catches a growing map (new nodes), node deletion, and M6 alignment (moved poses).

**Rebuild** = a new job. On success the old reconstruction is deleted (§8.2). One
reconstruction per map.

### 8.4 MinIO keys

Bucket: the map's bucket `map-{id}` (`ImageDatabaseService._bucket_name`: lower case, `_` →
`-`), as the maps design does for `grid/`.

| Key | Written by |
|---|---|
| `{node_id}/images/{camera}` | graph-builder (today) |
| `{node_id}/depth/{camera}.png` | graph-builder (R2) |
| `{node_id}/depth/{camera}.json` | graph-builder (R2) |
| `reconstruction/{job_id}/cloud.ply` | service |
| `reconstruction/{job_id}/ortho.png` | service |
| `reconstruction/{job_id}/height.png` | service |
| `reconstruction/{job_id}/meta.json` | service |

The service **never creates the bucket** (no `_ensure_bucket`). If the bucket is gone, the
upload fails and the job fails. This way a job racing a map delete cannot re-create the bucket
and leave an orphan.

### 8.5 Map delete, archive

- **Delete** (`packages/api/map_delete.py`): the bucket delete already removes every object
  above. Add one statement to `_finish`, next to `SESSIONS_SQL`:
  `DELETE FROM map_reconstructions WHERE map_name = %s`, in the same transaction.
- A running job checks the map's `lifecycle` every batch and before commit; on `DELETING` or
  a missing row it stops as `cancelled` (reason `map_deleting`) and uploads nothing more.
- Race: if the job uploads while the bucket is being emptied, `remove_bucket` fails
  (`BucketNotEmpty`), the delete saga retries with backoff, and the next attempt succeeds.
- **Archive / restore:** the reconstruction is kept. Starting a job on an archived map is
  allowed (it is read-only work).
- Maps cannot be renamed (M1), so no rename path.

---

## 9. API

### 9.1 Gateway (api-delegation-service, port 8000; what the client calls)

| Method | Path | Does |
|---|---|---|
| POST | `/api/v1/maps/{map}/reconstruction` | Start (or rebuild). Body optional: `{voxel_m?, max_depth_m?, clip_z?}`. 202 + the job |
| GET | `/api/v1/maps/{map}/reconstruction` | Current result (+ `stale`) and the active or last failed job |
| POST | `/api/v1/maps/{map}/reconstruction/cancel` | Cancel the active job |
| DELETE | `/api/v1/maps/{map}/reconstruction` | Delete the current result (and cancel an active job). 204 |
| GET | `/api/v1/maps/{map}/reconstruction/files/{name}` | `cloud.ply`, `ortho.png`, `height.png`, `meta.json`; streamed |

The gateway checks the map exists and is not `DELETING` (404 / 409), then proxies to the
service with an httpx client (`packages/services/map_reconstruction/client.py`, like
`MissionPlannerClient`). Service down → 503. The client reaches these through its nginx
`location /api/` (no nginx change). JSON examples: `handover.md` §5.

Errors: 404 unknown map; 409 `map_deleting`; 409 `job_active` (body has the job); 409
`no_depth` (no node of this map has depth); 422 bad parameters; 503 service unavailable.

### 9.2 Service (port 8009, internal)

The same routes without `/api/v1` (`/maps/{map}/reconstruction…`), plus `GET /health`,
`GET /stats`, `GET /`.

### 9.3 Events

New codes in `packages/events/codes.py` (+ payload models in `schemas.py`), source
`reconstruction`, no robot, discriminator `map:<name>:reconstruction:<job_id>:<state>`:

| Code | Severity | Payload |
|---|---|---|
| `MAP.RECONSTRUCTION_STARTED` | info | `map_name, job_id, params, nodes_with_depth` |
| `MAP.RECONSTRUCTION_FINISHED` | info | `map_name, job_id, points, nodes_used, nodes_skipped, voxel_m, duration_s` |
| `MAP.RECONSTRUCTION_FAILED` | warning | `map_name, job_id, reason (error, timeout, crashed, interrupted, cancelled, map_deleting), stage, error` |

Progress is **not** an event (too chatty). The client polls `GET …/reconstruction` every 2 s
while a job is active. A WebSocket push is not needed for minute-long jobs (later, if wanted:
Postgres NOTIFY `map_reconstructions` → the API's existing WS).

---

## 10. Client (sati-client)

Builds on the map window (§14.7, `components/mapWindow/`) and the single `MapView` of M5.

- **Map window, selected map, Details tab:** a "3D reconstruction" row.
  - none → button **Reconstruct** (disabled with a reason when the map has no depth nodes:
    "Recorded before depth capture").
  - queued/running → progress bar with stage and %, **Cancel**.
  - succeeded → "Built {time} · {points} points · {nodes_used}/{nodes_total} nodes",
    **Show on map** toggle, **Rebuild**, **Download (PLY)**. When stale: warn tone "Map changed
    since ({new_nodes} new nodes)" and Rebuild becomes primary.
  - failed → the error and **Retry**.
- **Model:** a pure `buildReconstructionModel(status)` in `utils/` (like
  `buildMappingBarModel`), unit-tested.
- **Layer:** new `MapLayerId` `reconstruction` in `utils/mapLayerVisibility.ts` ("3D
  reconstruction (top view)"). Drawn in `MapView` above the background and grid, below the
  costmap, nodes and robot. `BitmapLayer` as in §7.2, `textureParameters` linear (it is a
  photo, not cells).
- **Use for relocalization:** with the layer on, the operator sees the live robot marker and
  the live costmap over the reconstruction; walls lining up = the robot is where the map says.

---

## 11. Tests

All test containers are memory-capped (`--memory=2g --memory-swap=2g`), with the kill timeout
**inside** the container (`… IMAGE timeout -s KILL 300 python -m pytest …`). Pydantic v1 only.

**Unit** (`tests/unit/test_map_reconstruction_*.py`, marker `unit`):

- Depth codec: float m → u16 mm → float m round trip; NaN/inf/>65.535 m → 0.
- Back-projection with a known K: a pixel at (cx, cy) with depth d → (0, 0, d).
- Transform chain, as a property test: random node pose, extrinsic, pitch/roll; a point built
  in the map frame, projected into the camera and back-projected, lands on itself (catches an
  inverted transform).
- `pose3d_map` = `map_T_session` ⊕ `robot_pose3d` keeps z/roll/pitch.
- Voxel accumulator: mean colour, mean position, counts; batch merge = single pass.
- Neighbour filter: an isolated voxel is removed, a plane survives.
- Raster: highest voxel below `clip_z` wins; ceiling above the clip is ignored; alpha 0 where
  empty; `meta.json` origin/row convention.
- PLY writer: header and bytes read back.
- Digest and stale reasons (new, removed, moved).
- Job state machine: one active per map; supersede on success; failure keeps the old result;
  cancel; restart → interrupted; commit refused for a `DELETING` map.
- API routes: 404/409/422/503 mapping; proxying.
- graph-builder: depth upload buffered and stored under the right keys; rejected like an
  image; `depth_cameras` updated; old robots (no depth topic) unchanged.
- Map delete: `map_reconstructions` rows removed in `_finish`.
- Event codes and schemas registered (`tests/unit/events/test_codes_schemas.py`).

**Integration with a synthetic scene** (`tests/integration/reconstruction/run.sh`, same
harness as `tests/integration/maps/run_m2.sh`: private `--internal` network, Postgres
(timescaledb-ha), ArangoDB, MinIO, mosquitto, graph-builder and the service from the checkout):

1. A known scene: floor z = 0, two walls, a box, each surface with a flat known colour.
2. A local map with a mapping session placed with a **non-identity** `map_T_session` (e.g.
   (3, −2, 30°)), so a frame bug shows.
3. A camera path of ~40 nodes; depth (u16 PNG) and RGB rendered analytically by ray casting;
   published over MQTT exactly as the robot does (`robot/image_upload`,
   `robot/depth_upload`, `robot/node_update`). Half of the nodes with a small pitch/roll in
   `robot_pose3d`.
4. `POST …/reconstruction`, poll to `succeeded`.
5. Assert: ≥ 99 % of output points within `voxel_m/2 + 1 cm` of a true surface; ≥ 90 % of the
   visible surface voxels are covered; colours match the surface; `ortho.png` has the walls at
   the right pixels.
6. Add 10 nodes → `stale: true` with `new_nodes: 10`; rebuild → old prefix gone, one
   `succeeded` row.
7. Delete the map → rows and objects gone; a job started just before the delete ends
   `cancelled` and leaves no bucket.

**Sim end-to-end (manual, R6):** Isaac Sim with `/left/depth`. Map a loop, reconstruct, check
the ortho view against the sim scene. Then the relocalization scenario: restart the navstack,
place the robot (local map) or wait for the datum (geo), and check that the live costmap lines
up with the reconstruction.

---

## 12. Build steps

Order: after U5, U6 and M5.

| Step | Content | Repos |
|---|---|---|
| R1 | Topomap: depth capture with nearest-stamp matching, u16 mm PNG, camera JSON (cached `camera_info` + static TF), `robot_pose3d`, `robot/depth_upload`; params; unit tests of the codec and matcher. Real robot: `enable_dense_depth:=true` in the navstack launch. Check the optical-frame and z-depth assumptions (Q-R1) on both robot and sim | sati_ros_navstack |
| R2 | graph-builder ingest of depth (§5); `MQTT_DEPTH_TOPIC`; `depth_cameras`; `get_stats` filter; `MapIngestRejected.dropped_depth` | cloud_server |
| R3 | Service skeleton (FastAPI, config, compose entry, Dockerfile, health), migration `…_map_reconstructions` (+ event source), job table and worker, algorithm, PLY + rasters, events; unit tests | cloud_server |
| R4 | API gateway routes + client; map delete statement; `README` route docs; synthetic-scene integration test | cloud_server |
| R5 | Client: reconstruction row in the map window, polling, `reconstruction` layer in `MapView` (local + geo) | sati-client |
| R6 | Deploy script (`~/pg-cutover/scripts/recon.sh`, style of `mapsu1.sh`: `--dry-run` with the migration on a throwaway copy of the production schema, then API → graph-builder → new service), robot rollout, sim end-to-end | all |

R1 and R2–R4 can run in parallel (the new topic decouples them). R5 needs R4.

---

## 13. Later

- Pose refinement: ICP between neighbouring frames, or a pose graph with loop closures; would
  also fix M6-style misalignment automatically.
- A 3D view in the client: a decimated `preview.ply` (e.g., 20 cm voxel, ≤ 1 M points) and
  deck.gl `PointCloudLayer` via loaders.gl `PLYLoader`.
- LAZ export for GIS tools on geo maps.
- Tiled output (COPC or 3D Tiles) if maps reach tens of millions of points.
- Incremental update: add only new nodes' voxels instead of a full rebuild.
- Meshing (Poisson / TSDF) for nicer visuals.

---

## 14. Open questions

- **Q-R1. Camera frame and depth type.** Is Odin's depth `frame_id` (`camera`) an optical
  frame (z forward)? Is Isaac Sim's `/left/depth` distance-to-image-plane? The design assumes
  both; R1 verifies with a flat wall at a known distance.
- **Q-R2 (decided 2026-09-29).** Exact depth/RGB pairing on Odin: **no**, do not change
  `sati_odin_gpu_bridge`. Nearest-stamp matching within 80 ms stays.
- **Q-R3 (decided 2026-09-29).** Odin sends **dense** depth (`odin1/depth_dense_image`). It is
  a 64x48 grid upsampled 7x; compare with the sparse depth after the first real
  reconstruction.
- **Q-R4 (decided 2026-09-29).** Rebuild **only from the button**; no automatic rebuild.
- **Q-R5.** Set a broker `max_packet_size` guard (e.g., 4 MB) now?
- **Q-R6. Default voxel and clip height:** 5 cm and floor + 2 m. Different defaults for
  indoor/outdoor maps?
- **Q-R7. Cameras other than `left`:** multi-camera robots are supported by the design (per
  camera files); none exists today.
