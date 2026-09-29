# Map reconstruction service: developer spec

For the developer who builds the reconstruction service. The service is a **separate FastAPI
application in its own repository** (not in cloud_server, not `pcconstruction`). It may run on
the cloud host or on another machine (e.g. with a GPU). You don't need to know cloud_server:
everything the service needs arrives in one JSON **manifest**, and everything it produces goes
out through URLs and callbacks given in that manifest.

Background and the reasons for each choice: [`design.md`](design.md) (§ numbers point there).
Status (2026-09-29): the cloud side is **deployed** (robot depth R1 built, depth storage R2 and
the gateway R3 live in production, switched off until the service is configured), so this page is
the contract the running gateway speaks. Test against the synthetic scene of §9, then against real
maps (the sim records depth now).

**If you built against the earlier draft of this page** (routes `/maps/{map}/reconstruction`, the
service reading ArangoDB/MinIO itself): the pipeline stays; what changes is the edge of the service.

| Earlier draft | This contract |
|---|---|
| `POST /maps/{map}/reconstruction` with settings only | `POST /jobs` with the whole manifest (§2.1): frames, map-frame poses, camera params, presigned GET URLs |
| the service reads ArangoDB and MinIO with credentials | no credentials: only the manifest's URLs (a new input source) |
| results kept by the service, fetched from it | results PUT to the presigned URLs, then the `finish` callback (§3, §7) |
| `cloud.ply` + `meta.json` | the same: **only** `cloud.ply` + `meta.json` (§7). The cloud makes the 2.5D top view (`ortho.png`, `height.png`) itself from `cloud.ply` |
| no auth, no callbacks | bearer key on every call to the service; HMAC-token callbacks for progress / finish / fail (§2, §3) |
| depth layout `depth_cameras`, `{cam}.json` | irrelevant to the service: camera params arrive in the manifest |

---

## 1. What the service does

1. Receives a job (`POST /jobs`) with a manifest: a map's camera frames with map-frame poses,
   camera parameters, and presigned URLs for each RGB and depth image.
2. Runs one job at a time: downloads the images, back-projects the depth into 3D in the map
   frame, voxel-filters and removes outliers.
3. Uploads `cloud.ply` and `meta.json` to the presigned PUT URLs. (The 2.5D top view is made
   by cloud_server from `cloud.ply`; the service does not make rasters.)
4. Reports progress, success or failure to the callback URLs; answers status polls.

It has **no** database, no MinIO credentials, no MQTT, and knows nothing about maps beyond the
manifest. It never computes or changes a pose frame: poses in the manifest are final.

```
cloud_server gateway                          reconstruction service
  POST /jobs (manifest) ───────────────────▶  queue (spool on disk)
                                              worker: GET rgb/depth URLs  ──▶ MinIO
  ◀── POST {callback}/progress (every ≤ 10 s)
                                              PUT outputs                 ──▶ MinIO (staging)
  ◀── POST {callback}/finish  (or /fail)
  GET /jobs/{id}  (poll, if callbacks go quiet)
  POST /jobs/{id}/cancel
```

---

## 2. The service's HTTP API

Every route except `GET /health` requires `Authorization: Bearer <RECON_SERVICE_KEY>` (401
otherwise). JSON everywhere. Bind to `127.0.0.1` or a Tailscale address only (§8).

### 2.1 `POST /jobs`

Request body: the manifest. Example with one node and one camera (URLs shortened; a real one
is ~400 characters and must be used **byte for byte**):

```json
{
  "manifest_version": 1,
  "job_id": "5b0c8e1e-2f63-4a51-9d0e-6a7c7c0f3a10",
  "attempt": 1,
  "created_at": "2026-10-20T09:14:04Z",
  "expires_at": "2026-10-20T13:14:04Z",
  "map": {
    "name": "lab",
    "type": "geo",
    "crs": {"utm_zone": 34, "utm_north": true, "origin_e": 352397.33, "origin_n": 5262357.80}
  },
  "params": {
    "voxel_m": 0.05, "max_depth_m": 10.0, "clip_z": 2.0,
    "edge_rel": 0.05, "min_neighbours": 2,
    "max_voxels": 10000000, "raster_max_px": 4096
  },
  "nodes": [
    {
      "node_id": "7c1e2a9e-6d0b-4a55-a1f7-2f7f9e0c1b11",
      "pose": {"x": 4.1, "y": -1.9, "yaw": 1.31},
      "cameras": [
        {
          "name": "left",
          "params": {
            "frame_id": "camera", "width": 448, "height": 336,
            "fx": 300.0, "fy": 300.0, "cx": 224.0, "cy": 168.0,
            "distortion_model": "plumb_bob", "d": [0, 0, 0, 0, 0],
            "depth_type": "z", "valid_range_m": [0.2, 15.0],
            "rgb_width": 448, "rgb_height": 336,
            "T_base_cam": {"x": 0.1, "y": 0.0, "z": 0.45, "qx": -0.5, "qy": 0.5, "qz": -0.5, "qw": 0.5}
          },
          "depth_scale": 0.001,
          "pose3d": {"x": 4.1, "y": -1.9, "z": 0.02, "qx": 0.0, "qy": 0.01, "qz": 0.61, "qw": 0.79},
          "rgb_url": "http://sati-cloud:9000/map-lab/7c1e…/images/left?X-Amz-Algorithm=…&X-Amz-Signature=…",
          "depth_url": "http://sati-cloud:9000/map-lab/7c1e…/depth/left.png?X-Amz-Algorithm=…"
        }
      ]
    }
  ],
  "outputs": {
    "cloud":  {"url": "http://sati-cloud:9000/recon-staging/5b0c…/1/cloud.ply?X-Amz-…",  "content_type": "application/octet-stream"},
    "meta":   {"url": "http://sati-cloud:9000/recon-staging/5b0c…/1/meta.json?X-Amz-…",  "content_type": "application/json"}
  },
  "callback": {
    "base_url": "http://sati-cloud:8000/internal/reconstruction/jobs/5b0c8e1e-2f63-4a51-9d0e-6a7c7c0f3a10",
    "token": "q9Xw…"
  }
}
```

Fields:

- `pose`: node pose in the map frame (x, y metres; yaw radians). Always present.
- `pose3d`: optional, per camera: the full base pose in the map frame at that depth image's
  stamp. When present it **replaces** `pose` for that camera (§5).
- `nodes` is sorted by capture time. Only frames with depth are included.
- `map.crs` is `null` for a local map. It is copied into the outputs and not used for math.
- `params` are the cloud's defaults (tuned indoors). The service owns the real defaults: it may
  use other values per map (e.g. outdoor, from `map.type` or the extent of the poses) and must
  report what it used in `meta.json`.

Responses:

| Status | When | Body |
|---|---|---|
| 202 | accepted (new job, or a higher `attempt` of a known `job_id`, which cancels the older attempt) | the job status (§2.2), `state: "queued"` |
| 200 | same `job_id` and `attempt` already known (idempotent repeat) | the current job status |
| 409 | `attempt` lower than one already seen | `{"detail": {"code": "stale_attempt"}}` |
| 413 | body > 32 MB | |
| 422 | invalid manifest (unknown `manifest_version`, missing fields, > 20 000 nodes, a param out of range) | `{"detail": {"code": "invalid_manifest", "message": "…"}}` |
| 429 | queue full (default 4 jobs incl. the running one) | `{"detail": {"code": "queue_full"}}` |

Validate cheaply at POST (structure and ranges); do not fetch anything before answering.

### 2.2 `GET /jobs/{job_id}`

`200`:

```json
{
  "job_id": "5b0c8e1e-2f63-4a51-9d0e-6a7c7c0f3a10",
  "attempt": 1,
  "state": "running",
  "stage": "integrating",
  "progress": 0.43,
  "frames_done": 177,
  "frames_total": 412,
  "queued_at": "2026-10-20T09:14:04Z",
  "started_at": "2026-10-20T09:14:05Z",
  "finished_at": null,
  "result": null,
  "outputs": null,
  "error": null
}
```

- `state`: `queued` | `running` | `succeeded` | `failed` | `cancelled`.
- When `succeeded`, `result` and `outputs` are the same objects as in the finish callback
  (§3.2). When `failed`/`cancelled`, `error` = `{"reason", "stage", "message"}`.
- Terminal jobs are kept (in memory and in the spool) for **24 h**, so the gateway can recover a
  lost callback by polling. Unknown `job_id` → `404`.

### 2.3 `POST /jobs/{job_id}/cancel`

No body. `200` with the job status (`state` stays `running` until the worker stops, at most one
batch, then `cancelled`). A queued job becomes `cancelled` at once. Already terminal → `200`
with that state. Unknown → `404`. After a cancel the service sends `/fail` with reason
`cancelled`.

### 2.4 `GET /health`

No auth. `200`:

```json
{"status": "healthy", "version": "1.0.0",
 "queue": {"running": 1, "queued": 0, "max": 4},
 "gpu": false}
```

---

## 3. Callbacks the service calls on cloud_server

Base URL and token come from `manifest.callback`. Each call:
`POST {base_url}/{progress|finish|fail}`, header `Authorization: Bearer {token}`, JSON body
with the `attempt` from the manifest.

### 3.1 `progress`

Send once when the job starts running, then at least every 10 s and at each stage change.
Fire and forget (a 2 s timeout, no retry).

```json
{"attempt": 1, "stage": "integrating", "progress": 0.43, "frames_done": 177, "frames_total": 412}
```

### 3.2 `finish`

After **all four** outputs were uploaded successfully.

```json
{
  "attempt": 1,
  "result": {
    "points": 2310455,
    "frames_total": 412,
    "frames_used": 409,
    "frames_skipped": {"missing_depth": 1, "unsupported_camera": 0, "bad_depth": 0, "no_valid_depth": 2},
    "frames_without_rgb": 0,
    "voxel_m": 0.05,
    "voxel_m_requested": 0.05,
    "bounds3d": {"min": [-12.35, -40.10, -1.2], "max": [79.65, 25.9, 6.3]},
    "duration_s": 58.2,
    "peak_rss_mb": 910
  },
  "outputs": {
    "cloud":  {"bytes": 39277735, "sha256": "…"},
    "meta":   {"bytes": 612,      "sha256": "…"}
  }
}
```

### 3.3 `fail`

```json
{"attempt": 1, "reason": "url_expired", "stage": "integrating", "message": "GET depth for node 7c1e… returned 403"}
```

`reason` is one of: `error` (unexpected exception), `crashed` (worker process died, e.g. out
of memory), `timeout`, `cancelled`, `url_expired` (any 403 from a presigned URL),
`upload_failed`, `no_points` (nothing valid to reconstruct).

### 3.4 Answers from cloud_server

| Status | Body | What the service does |
|---|---|---|
| 200 | `{"action": "continue"}` | carry on |
| 200 | `{"action": "cancel"}` (progress only) | cancel the job as if `/cancel` was called |
| 410 | `{"action": "stop"}` | stop the job at once, send nothing more, mark it `cancelled` locally |
| 401 | | log an error; keep going (the gateway will poll) |
| other / no answer | | `progress`: ignore. `finish`/`fail`: retry with backoff 2 s, 5 s, 15 s, 30 s, then every 60 s, for up to 10 min; then give up (the job stays terminal for the poll) |

---

## 4. Fetching inputs

- Presigned URLs are **opaque**: use them exactly as given. Do not parse, re-encode, or add
  query parameters. For GET send no extra headers. The signature covers the host, so do not
  rewrite the host either.
- Download in parallel (e.g. 8 connections) and ahead of the worker (a bounded prefetch
  queue), so compute and network overlap.
- Per request: timeout 30 s; retry 3× on connection errors, timeouts and 5xx (1 s, 3 s, 10 s).
- `403` → the URL expired or was revoked: fail the job, reason `url_expired`.
- `404` on a depth URL → skip that frame (`frames_skipped.missing_depth`). `404` on an RGB URL
  → use the frame with grey (128, 128, 128) and count `frames_without_rgb`.
- RGB: JPEG. `cv2.imdecode(buf, cv2.IMREAD_COLOR)` returns **BGR**: convert to RGB before
  writing colours.
- Depth: PNG, single channel, **uint16 millimetres**, 0 = no data.
  `raw = cv2.imdecode(buf, cv2.IMREAD_UNCHANGED)`; require `raw.dtype == uint16`, `raw.ndim
  == 2` and shape `(height, width)` from `params`, else skip (`bad_depth`).
  `d = raw * depth_scale` (metres; `depth_scale` is 0.001).

---

## 5. Geometry

### 5.1 Conventions

- **Map frame:** right-handed, z up, metres. For geo maps x = east, y = north (UTM grid,
  relative to `crs.origin_e/origin_n`). Output coordinates stay in this frame.
- **Base frame** (`base_link`): x forward, y left, z up.
- **Camera frame:** the **optical** frame: x right, y down, z forward (into the scene).
- **Pixels:** `u` = column, `v` = row, from the top-left; the pixel centre is at integer
  `(u, v)` (the OpenCV/ROS convention that `cx, cy` use).
- **Depth:** `depth_type = "z"`: distance along the optical axis (not along the ray).
- **Quaternions:** `(qx, qy, qz, qw)`, Hamilton, normalize before use.
- **yaw:** radians, counter-clockwise about +z, 0 = +x.

A camera is accepted only when `depth_type == "z"`, `d` is all zeros, and `fx, fy > 0`; else
skip it (`unsupported_camera`).

### 5.2 Transforms

A transform `T = (R, t)` maps a point from its source frame into its target frame:
`p_target = R · p_source + t`.

Rotation from a quaternion:

```
R(q) = [[1-2(qy²+qz²),  2(qx·qy-qz·qw), 2(qx·qz+qy·qw)],
        [2(qx·qy+qz·qw), 1-2(qx²+qz²),  2(qy·qz-qx·qw)],
        [2(qx·qz-qy·qw), 2(qy·qz+qx·qw), 1-2(qx²+qy²) ]]
```

- **`T_base_cam`** (`params.T_base_cam`): camera → base. `R_bc = R(q)`, `t_bc = (x, y, z)`. It is
  ROS `lookup_transform(target="base_link", source=frame_id)`. Example: a forward-looking
  camera has `q = (-0.5, 0.5, -0.5, 0.5)`, `R_bc = [[0,0,1],[-1,0,0],[0,-1,0]]` (camera z →
  base +x, camera x → base −y, camera y → base −z).
- **`T_map_base`**: base → map.
  - with `pose3d`: `R_mb = R(qx, qy, qz, qw)`, `t_mb = (x, y, z)`.
  - without: `R_mb = Rz(yaw) = [[cos, −sin, 0], [sin, cos, 0], [0, 0, 1]]`,
    `t_mb = (pose.x, pose.y, 0)`.

### 5.3 Back-projection to the map frame

For each valid pixel `(u, v)` with depth `d`:

```
p_cam = ((u − cx) · d / fx,  (v − cy) · d / fy,  d)
p_map = R_mb · (R_bc · p_cam + t_bc) + t_mb
```

Vectorized: `P_map = (R_mb @ R_bc) @ P_cam + (R_mb @ t_bc + t_mb)`, one 3x3 matrix and one
vector per frame.

Valid pixel: `raw > 0`, `valid_range_m[0] ≤ d ≤ valid_range_m[1]`, `d ≤ params.max_depth_m`,
and it passes the edge filter (§6).

**Worked example** (use it as a unit test): camera as above with `t_bc = (0.1, 0, 1.0)`,
`fx = fy = 300`, `cx = 224`, `cy = 168`; a wall at map x = 5.

| Node pose | Pixel | Depth | `p_map` |
|---|---|---|---|
| (0, 1, yaw 0) | (224, 168) | 4.9 | (5.0, 1.0, 1.0) |
| (0, 1, yaw 0) | (324, 168) | 4.9 | (5.0, −0.6333, 1.0) |
| (1, 0, yaw π/2), wall at map y = 3 | (224, 168) | 2.9 | (1.0, 3.0, 1.0) |
| (1, 0, yaw π/2), wall at map y = 3 | (324, 168) | 2.9 | (1.9667, 3.0, 1.0) |

RGB: if `rgb_width/rgb_height` differ from `width/height`, resize the RGB to the depth size
(`cv2.INTER_AREA`) before sampling; the intrinsics are for the depth image.

---

## 6. Algorithm

1. **Per frame** (in batches, e.g. 32 frames):
   1. Decode depth and RGB (§4).
   2. Mask invalid pixels (§5.3).
   3. **Edge filter** (flying pixels): drop a pixel if any valid 4-neighbour differs by more
      than `edge_rel × d`.
   4. Back-project and transform (§5.3).
   5. **Voxel accumulate** at `voxel_m`: key = `floor(p / voxel_m)` packed into an int64 (21
      bits per axis, offset so negatives fit); per voxel the pixel count, the sum of positions
      (float64, or offsets from the voxel corner in float32), and the sum of RGB (uint32).
      Reduce each batch (`np.unique` + `np.add.at`, or the GPU equivalent), merge into the
      global accumulator.
   6. Every batch: progress, cancel check, timeout check.
2. If the voxel count exceeds `max_voxels`: multiply `voxel_m` by 1.5 and restart (report
   `voxel_m` and `voxel_m_requested`). Above 0.5 m: fail (`error`, "too large").
3. **Neighbour filter:** drop a voxel with fewer than `min_neighbours` occupied voxels in its
   26-neighbourhood (hash lookups on the keys; no k-d tree needed).
4. **Per voxel:** mean position, mean colour, count (clamped to 65535).
5. No points left → fail `no_points`.
6. Write `cloud.ply` and `meta.json` (§7), upload, call `finish`.

Frame counts: `frames_total` = all node-cameras in the manifest; `frames_used` = frames that
contributed at least one point; the rest go into `frames_skipped` by reason (`missing_depth`,
`bad_depth`, `unsupported_camera`, `no_valid_depth`).

Progress: `integrating` covers 0–0.85 (by frames), then `filtering` 0.87, `writing` 0.9,
`uploading` 0.95–1.0. Stages in order: `queued`, `integrating`, `filtering`, `writing`,
`uploading`.

---

## 7. Outputs

Upload each file with `PUT {outputs.<name>.url}`, body = the file bytes, header
`Content-Type: {outputs.<name>.content_type}` (and `Content-Length`). Stream large files from
a temp file. Retry 3× on connection errors and 5xx; `403` → `url_expired`; anything else →
`upload_failed`. Report `bytes` and `sha256` of what you uploaded.

### 7.1 `cloud.ply`

Binary little-endian PLY:

```
ply
format binary_little_endian 1.0
comment satinav map=lab job=5b0c8e1e-… voxel_m=0.05 frame=map
comment crs=utm zone=34 north=1 origin_e=352397.33 origin_n=5262357.80
element vertex N
property float x
property float y
property float z
property uchar red
property uchar green
property uchar blue
property ushort count
end_header
<N × 17 bytes>
```

The `crs` comment only for geo maps. Coordinates: map frame, metres. Write with a numpy
structured dtype `[('x','<f4'),('y','<f4'),('z','<f4'),('red','u1'),('green','u1'),
('blue','u1'),('count','<u2')]` and `tobytes()`.

### 7.2 Top view

Not the service's job: cloud_server derives `ortho.png` and `height.png` from `cloud.ply` (and
the manifest's poses) after `finish`. `params.clip_z` and `params.raster_max_px` are for that
step; the service accepts them and ignores them.

### 7.3 `meta.json`

```json
{
  "version": 1,
  "map_name": "lab", "job_id": "5b0c8e1e-…", "map_type": "geo",
  "frame": "map",
  "crs": {"utm_zone": 34, "utm_north": true, "origin_e": 352397.33, "origin_n": 5262357.80},
  "bounds3d": {"min": [-12.35, -40.10, -1.2], "max": [79.65, 25.9, 6.3]},
  "points": 2310455, "voxel_m": 0.05
}
```

`crs` is `null` for a local map. `bounds3d` is over all cloud points.

---

## 8. Running it

### 8.1 Jobs, restarts, cancel

- **Concurrency 1:** one worker; others wait in the queue (max 4 incl. the running one).
- **Spool:** each accepted manifest is written to `RECON_SPOOL_DIR/{job_id}.json` with its
  state. On start, the service reloads the spool: queued jobs stay queued, a job that was
  running restarts from the beginning (outputs are overwritten, which is safe), terminal jobs
  older than 24 h are deleted. This is the only state the service keeps.
- **Idempotent per `(job_id, attempt)`** (§2.1). Never run two attempts of one job at once.
- **Heavy work in a child process** (one per job): the API stays responsive, all memory is
  returned after a job, and an out-of-memory kill is reported as `crashed` instead of taking
  the service down.
- **Timeout:** `RECON_JOB_TIMEOUT_S` (default 3600) → `timeout`.
- **Cancel** from `/cancel`, a progress answer `cancel`, or a `410`: stop within one batch.

### 8.2 Limits

| Limit | Default |
|---|---|
| Request body | 32 MB |
| Nodes per manifest | 20 000 |
| Queue | 4 jobs |
| Container memory | 2 GB (`mem_limit`); peak for a 1 000-node map should stay under ~1 GB |
| Voxels | `params.max_voxels` (10 M) |
| Job time | 1 h |
| Temp disk | outputs of one job (≤ ~300 MB) |

### 8.3 Config (env)

| Variable | Default | Meaning |
|---|---|---|
| `RECON_HOST` | `127.0.0.1` | Bind address (`127.0.0.1` on the cloud host, the Tailscale IP elsewhere). Never `0.0.0.0` on a public interface |
| `RECON_PORT` | `8009` | |
| `RECON_SERVICE_KEY` | — (required) | Bearer key the gateway sends |
| `RECON_SPOOL_DIR` | `/var/lib/recon/spool` | Persistent volume |
| `RECON_QUEUE_MAX` | `4` | |
| `RECON_JOB_TIMEOUT_S` | `3600` | |
| `RECON_FETCH_CONCURRENCY` | `8` | |

The service has no other secrets. It must reach the MinIO host and the callback host named in
the URLs (both on the cloud host: port 9000 and port 8000, over Tailscale when remote).

### 8.4 Dependencies

FastAPI, uvicorn, httpx (or requests in the worker), numpy, opencv-python-headless. Pin them.
No Open3D (a large wheel for what is ~50 lines of numpy). A GPU path (CuPy / PyTorch) is
optional and must give the same outputs.

---

## 9. Testing standalone

You do not need cloud_server. Write a small **fake gateway** (`tools/fake_gateway.py` in the
service repo) that:

1. Renders the synthetic scene below into RGB JPEGs and depth PNGs in a temp dir.
2. Serves them over HTTP on `127.0.0.1` at URLs with a dummy query (`?sig=test`), accepts PUTs
   of the outputs, and receives the callbacks (checking the bearer token).
3. Builds the manifest, `POST /jobs`, waits for `finish`, then checks the outputs.
4. Can return 403 / 404 / 410 on demand for the negative tests.

**Memory caps are mandatory on the cloud host.** An uncapped test once took 55 GB and
OOM-killed the user's processes. Run tests in containers with `--memory=2g --memory-swap=2g`
and the kill timeout **inside** the container (`… IMAGE timeout -s KILL 300 python -m pytest
…`); a host-side `timeout` only kills the docker client.

### 9.1 Synthetic scene

Map frame, local map (`crs: null`):

| Surface | Geometry | Colour (RGB) |
|---|---|---|
| Floor | z = 0, x ∈ [−1, 6], y ∈ [−4, 4] | (128, 128, 128) |
| Wall A | x = 5, y ∈ [−4, 4], z ∈ [0, 2.8] | (200, 40, 40) |
| Wall B | y = 3, x ∈ [−1, 6], z ∈ [0, 2.8] | (40, 200, 40) |
| Box | x ∈ [1.5, 2.5], y ∈ [−1.5, −0.5], z ∈ [0, 0.5] (all 5 visible faces) | (40, 40, 200) |
| Ceiling | z = 2.8, x ∈ [−1, 6], y ∈ [−4, 4] | (240, 240, 240) |

Camera: 448x336, `fx = fy = 300`, `cx = 224`, `cy = 168`, `d` zeros, `depth_type` `z`,
`valid_range_m` [0.2, 15], `T_base_cam = (0.1, 0, 1.0)` with `q = (-0.5, 0.5, -0.5, 0.5)`.

Nodes (28):

- 24 planar nodes: `x ∈ {0, 0.5, …, 3.5}`, `y = 1.0`, `yaw ∈ {0, π/2, −π/2}`; no `pose3d`.
- 4 nodes with `pose3d`: `x ∈ {0.5, 1.5, 2.5, 3.5}`, `y = 1.0`, `z = 0.05`, yaw 0, pitch
  +5° (nose down), roll 2°. `pose` = the same x, y, yaw.

Rendering (per pixel): the ray in the camera is `r_cam = ((u − cx)/fx, (v − cy)/fy, 1)`; in the
map `o = T_map_cam · 0`, `r = R_mc · r_cam`. The nearest positive hit `t` over the bounded
surfaces is exactly the z depth (because `r_cam.z = 1`). Depth PNG: `round(t · 1000)` as
uint16, 0 for no hit or `t > 65.535`. RGB: the surface colour, JPEG quality 95. Render with the
**same** pose the manifest carries (`pose3d` when present).

Params: `voxel_m 0.05`, `max_depth_m 10`, `clip_z 2.0`, others default.

### 9.2 Expected output

| Check | Expected |
|---|---|
| Worked example (§5.3) | exact to 1e-4 m as a unit test of the transform |
| Accuracy | ≥ 99 % of cloud points within `voxel_m/2 + 1 cm` of a scene surface |
| Coverage | ≥ 90 % of the voxels of the true hit points (from the renderer) present in the cloud |
| Colour | points within 1 cm of wall A: mean colour within ±30 per channel of (200, 40, 40); same for B, box, floor |
| Frames | `frames_total = 28`, `frames_used = 28` (every frame sees something) |
| Ceiling | `bounds3d.max[2] ≈ 2.8` (the ceiling is in the cloud) |
| meta.json | `points` = the PLY's N; `bounds3d` = the PLY's min/max; `voxel_m` as used |
| PLY | reads back in Open3D/CloudCompare or a 10-line numpy reader with the same N and values |

A frame-order or sign bug shows immediately: the yaw ±π/2 nodes put wall B at y ≠ 3 or mirror
the box, and the `pose3d` nodes tilt the floor.

### 9.3 Negative tests

- Duplicate `POST /jobs` (same attempt) → 200, no second run. Higher attempt → the old attempt
  stops, the new one runs.
- 403 on one depth URL → `fail` with `url_expired`.
- 404 on a depth URL → frame skipped; on an RGB URL → grey, `frames_without_rgb = 1`.
- Progress answered with `{"action": "cancel"}` → `fail` with `cancelled` within one batch.
- Progress answered with 410 → stops, no further callbacks, no further PUTs.
- `finish` callback failing for a while → retried; `GET /jobs/{id}` shows `succeeded` meanwhile.
- `kill -9` of the service mid-job → on restart the job reruns from the spool and finishes.
- A child killed by the memory limit (run with a tiny `mem_limit`) → `fail` with `crashed`;
  the service keeps serving.
- Queue full → 429. Missing/wrong bearer → 401. Body > 32 MB → 413.

---

## 10. Done checklist

- [ ] Routes: `POST /jobs`, `GET /jobs/{id}`, `POST /jobs/{id}/cancel`, `GET /health`, with
      the status codes of §2; bearer auth on all but health.
- [ ] Manifest validation (version, fields, ranges, node limit).
- [ ] Spool, restart recovery, 24 h retention of terminal jobs; idempotency per
      `(job_id, attempt)`.
- [ ] Worker in a child process; concurrency 1; queue limit; timeout; cancel via all three
      paths; `crashed` on a dead child.
- [ ] Input fetching per §4 (opaque URLs, parallel, retries, 403/404 rules, BGR → RGB).
- [ ] Geometry per §5 with the worked-example unit test and a round-trip property test (a
      random point in the map, projected into a random camera and back, lands on itself).
- [ ] Algorithm per §6 (edge filter, voxel accumulation, coarsening, neighbour filter).
- [ ] Outputs per §7 (PLY read-back test, `meta.json`).
- [ ] Uploads per §7 with `bytes` and `sha256`; callbacks per §3 incl. the answer handling and
      the retry policy.
- [ ] Fake gateway and synthetic scene (§9); all checks of §9.2 and §9.3 green, in memory-capped
      containers.
- [ ] Measured on a real map export (ask for a manifest from R3): runtime, peak memory, output
      sizes; numbers sent back for `design.md`.
- [ ] README: how to run (env of §8.3), how to deploy on the cloud host (bound to
      `127.0.0.1`) and on a Tailscale host.
