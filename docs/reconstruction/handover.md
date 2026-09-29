# Map reconstruction service: handover

For the developer who builds `map-reconstruction-service`. You don't need to know the rest of
the repo first; this page says what to read and where. **Why** things are the way they are is in
[`design.md`](design.md) (section numbers below point there).

Before you start: read the repo's `CLAUDE.md` (stack, env vars, ports, tests) and
`docs/satinav-maps-redesign.md` §3, §4 and §14.2 (maps, sessions, frames).

Precondition: R1 (robot sends depth) and R2 (graph-builder stores it) are done, so map buckets
contain `{node_id}/depth/{camera}.png|.json` and ArangoDB nodes have `depth_cameras`. Until then,
build and test against the synthetic scene (§8 below).

---

## 1. What the service does

1. Accepts a request to reconstruct one map. Stores it as a job row in Postgres.
2. Runs one job at a time in the background: reads the map's nodes, their RGB and depth images
   and camera parameters, builds one coloured 3D point cloud in the **map frame**, voxel-filters
   it, removes outliers, and derives a 2.5D top-down image.
3. Uploads the results to the map's MinIO bucket, marks the job `succeeded`, deletes the
   previous result.
4. Reports status and progress, and serves the result files.
5. Tells whether the result is **stale** (the map changed since).

It does not talk to robots, MQTT, or other services over HTTP. The API gateway talks to it.

---

## 2. Inputs

### 2.1 The map

Postgres table `mapobjectv1` (object table; `MapObjectV1.table_name()` in
`cloud_common/objects/map.py`):

```sql
SELECT lifecycle, spec->>'type' AS type, spec->'geo' AS geo FROM mapobjectv1 WHERE name = %s
```

- `lifecycle = 'DELETING'` or no row → refuse / stop the job.
- `type`: `local` or `geo`. For `geo`, `geo` = `{utm_zone, utm_north, origin_e, origin_n}`:
  goes into `meta.json` and the PLY header, nothing else (the map frame is already local UTM
  metres).

### 2.2 Nodes with poses in the map frame

```python
from packages.topomap_dbs.client import TopomapDatabaseClient
db = TopomapDatabaseClient()                   # reads packages/config.py
nodes = db.graph.get_all_nodes(map_name)       # ArangoDB collection nodes_{map_name}
```

Each node is a dict (written by graph-builder `_process_topology` → `graph_db.add_node`):

| Field | Meaning |
|---|---|
| `_key`, `node_id` | global node id (UUID string); also the MinIO prefix |
| `pose` | `{x, y, yaw}` **in the map frame**. Use this |
| `robot_pose` | `{x, y, yaw}` in the robot's run frame. **Do not use** |
| `session_id`, `robot_name`, `session_node_id` | provenance |
| `created_at` | ISO time of ingest (sort key) |
| `camera_metadata` | `[{camera_name, timestamp}]` |
| `depth_cameras` | (R2) cameras that have depth for this node, e.g. `["left"]` |

Keep only nodes with a non-empty `depth_cameras`. Nodes without depth are skipped and counted
(`nodes_skipped.no_depth`). Do not re-apply any session transform: `pose` is final (design §3).

### 2.3 RGB, depth, camera parameters

Bucket: `db.image._bucket_name(map_name)` → `map-{name lower-cased, _ → -}`.

| Object | How to read |
|---|---|
| RGB JPEG `{node_id}/images/{camera}` | `db.image.get_image(image_id=camera, node_id=node_id, map_id=map_name)` → bytes or None |
| Depth PNG `{node_id}/depth/{camera}.png` | R2 adds `db.image.get_depth(camera, node_id, map_id)`; until then `db.image.client.get_object(bucket, key)` |
| Camera JSON `{node_id}/depth/{camera}.json` | R2 adds `db.image.get_depth_meta(...)`; same fallback |

Depth PNG: 16-bit unsigned, **millimetres**, 0 = no data (multiply by `depth_scale`, 0.001).
Decode with `cv2.imdecode(buf, cv2.IMREAD_UNCHANGED)` (dtype must be `uint16`).

Camera JSON (written by graph-builder from the robot's message, design §4.3, §5):

```json
{
  "camera": {
    "frame_id": "camera", "width": 448, "height": 336,
    "fx": 0.0, "fy": 0.0, "cx": 0.0, "cy": 0.0,
    "distortion_model": "plumb_bob", "d": [0, 0, 0, 0, 0],
    "depth_type": "z", "valid_range_m": [0.2, 15.0],
    "rgb_width": 448, "rgb_height": 336,
    "T_base_cam": {"x": 0.1, "y": 0.0, "z": 0.45, "qx": -0.5, "qy": 0.5, "qz": -0.5, "qw": 0.5}
  },
  "depth_stamp_ms": 1727600000123,
  "rgb_stamp_ms": 1727600000084,
  "robot_pose3d": {"x": 1.2, "y": -0.4, "z": 0.02, "qx": 0.0, "qy": 0.01, "qz": 0.38, "qw": 0.92},
  "pose3d_map":   {"x": 4.1, "y": -1.9, "z": 0.02, "qx": 0.0, "qy": 0.01, "qz": 0.61, "qw": 0.79},
  "session_id": "…"
}
```

(Numbers are placeholders.) Rules:

- `depth_type` must be `z` and `d` all zeros; otherwise skip the frame
  (`nodes_skipped.unsupported_camera`).
- The camera frame is optical: x right, y down, z forward.
- Pose of the camera: `T_map_cam = T_map_base · T_base_cam`. `T_map_base` = `pose3d_map` if
  present, else `Trans(pose.x, pose.y, 0) · Rz(pose.yaw)`.
- RGB size ≠ depth size → resize the RGB to the depth size (intrinsics are for the depth size).

### 2.4 Postgres access

```python
from packages.database.postgres import PostgresDatabase
database = PostgresDatabase(dbname=POSTGRES_DATABASE_NAME, user=POSTGRES_DATABASE_USERNAME,
                            password=POSTGRES_DATABASE_PASSWORD, host=POSTGRES_DATABASE_HOST,
                            port=POSTGRES_DATABASE_PORT,
                            required_tables=("map_reconstructions", "fleet_events"))
await database.async_init()          # waits until the API's migrations created the tables
async with database.connection() as conn:   # one transaction; commits on clean exit
    await conn.execute(...)
```

Pattern: `packages/services/graph_builder/server.py` (`_fetch_open_session`,
`_realign_session`). Events: `packages.events.emit.emit(conn, Event(...))` inside your
transaction (see `packages/api/map_delete.py::_emit_deleted` for the savepoint pattern). The
child process (§4.3) opens its own sync `psycopg` connection; never share the pool across a fork.

---

## 3. Algorithm (summary)

Design §6.1 has the full list. Per job:

1. List nodes with depth; compute the input digest (§6).
2. For batches of 32 frames: fetch, decode, mask invalid/out-of-range depth, edge filter,
   back-project `X=(u−cx)d/fx, Y=(v−cy)d/fy, Z=d`, transform to the map frame, accumulate into a
   voxel grid (`voxel_m`, default 0.05): count, sum of offsets, sum of RGB.
3. Drop voxels with < 2 occupied neighbours (26-neighbourhood).
4. Mean colour and position per voxel → `cloud.ply`.
5. Top-down rasters `ortho.png` + `height.png` + `meta.json` (design §7.2).

Dependencies: numpy, opencv-python-headless. No Open3D.

---

## 4. Outputs and storage contract

### 4.1 MinIO

Bucket `map-{id}` (the map's own bucket). Prefix per job: `reconstruction/{job_id}/`.

| Key | Content type | Content |
|---|---|---|
| `reconstruction/{job_id}/cloud.ply` | `application/octet-stream` | binary little-endian PLY: `float x,y,z; uchar red,green,blue; ushort count`; map frame, metres (design §7.1) |
| `reconstruction/{job_id}/ortho.png` | `image/png` | RGBA top view, alpha 0 = empty |
| `reconstruction/{job_id}/height.png` | `image/png` | uint16, `(z − z_offset)/z_scale + 1`, 0 = empty |
| `reconstruction/{job_id}/meta.json` | `application/json` | grid, frame, CRS, bounds (design §7.2) |

Rules:

- **Never create the bucket** (`_ensure_bucket` / `make_bucket`). If it is missing, fail the
  job. (A job racing a map delete must not bring the bucket back.)
- Upload with `db.image.client.put_object(bucket, key, stream, length, content_type=...)`;
  for the PLY use `length=-1, part_size=16 MiB` or write to a temp file first.
- Delete a prefix with `list_objects(bucket, prefix, recursive=True)` + `remove_objects`.
- Never touch `{node_id}/…` objects. They belong to graph-builder.

### 4.2 Postgres

Table `map_reconstructions` (design §8.1 has the DDL). You write the migration in the API's
Alembic chain: `packages/api/migrations/versions/<YYYYMMDD>_01_map_reconstructions.py`,
`down_revision` = the current head (check `ls packages/api/migrations/versions/`; at the time of
writing `20260930_01_maps_use`). Follow `20260930_01_maps_use.py`: idempotent
(`IF NOT EXISTS`, drop-then-add constraints), `SET LOCAL lock_timeout`, a real `downgrade()`.
The same migration widens `fleet_events_source_check` with `reconstruction` (copy the approach of
`20260929_01_maps_m2.py`). The API entrypoint runs migrations at start
(`packages/api/entrypoint.py`); your service only waits for the table (`required_tables`).

---

## 5. HTTP API contract

The client calls the **gateway** (`api-delegation-service`, port 8000, paths under
`/api/v1/maps/{map}/reconstruction`). The gateway checks the map (404 unknown, 409 `DELETING`)
and forwards to your service at the same path without `/api/v1`. You build both sides: the
service routes and the gateway proxy (`packages/api/main.py` + a client class in
`packages/services/map_reconstruction/client.py`, modelled on
`packages/services/mission_planner/client.py`).

### 5.1 Start or rebuild

`POST /api/v1/maps/lab/reconstruction`

```json
{"voxel_m": 0.05, "max_depth_m": 10.0, "clip_z": 2.0}
```

All fields optional. Validation (422): `voxel_m` 0.02–0.5, `max_depth_m` 0.5–65, `clip_z`
−5–20.

`202 Accepted`:

```json
{
  "job": {
    "job_id": "5b0c8e1e-2f63-4a51-9d0e-6a7c7c0f3a10",
    "map_name": "lab",
    "state": "queued",
    "stage": null,
    "progress": 0.0,
    "requested_at": "2026-10-20T09:14:03Z",
    "params": {"voxel_m": 0.05, "max_depth_m": 10.0, "clip_z": 2.0},
    "nodes_with_depth": 412
  }
}
```

Errors:

| Status | `detail.code` | When |
|---|---|---|
| 404 | `map_not_found` | no such map |
| 409 | `map_deleting` | map is being deleted |
| 409 | `job_active` | a job is queued or running; `detail.job` is that job |
| 409 | `no_depth` | no node of the map has a depth image |
| 422 | — | bad parameters (FastAPI validation) |
| 503 | `unavailable` | (gateway) the service is not reachable |

Body shape of an error: `{"detail": {"code": "job_active", "message": "…", "job": {…}}}`.

### 5.2 Status

`GET /api/v1/maps/lab/reconstruction` → `200`:

```json
{
  "map_name": "lab",
  "reconstruction": {
    "job_id": "1f7d…",
    "finished_at": "2026-10-19T17:02:41Z",
    "params": {"voxel_m": 0.05, "max_depth_m": 10.0, "clip_z": 2.0},
    "points": 2310455,
    "nodes_total": 450,
    "nodes_used": 412,
    "nodes_skipped": {"no_depth": 38},
    "bounds3d": {"min": [-12.35, -40.10, -1.2], "max": [79.65, 25.9, 6.3]},
    "stale": true,
    "stale_reason": {"new_nodes": 12, "removed_nodes": 0, "moved_nodes": 0},
    "files": {
      "cloud":  {"name": "cloud.ply",  "bytes": 39277735, "url": "/api/v1/maps/lab/reconstruction/files/cloud.ply?v=1f7d…"},
      "ortho":  {"name": "ortho.png",  "bytes": 4120334,  "url": "/api/v1/maps/lab/reconstruction/files/ortho.png?v=1f7d…"},
      "height": {"name": "height.png", "bytes": 1893002,  "url": "/api/v1/maps/lab/reconstruction/files/height.png?v=1f7d…"},
      "meta":   {"name": "meta.json",  "bytes": 612,      "url": "/api/v1/maps/lab/reconstruction/files/meta.json?v=1f7d…"}
    }
  },
  "job": {
    "job_id": "5b0c…",
    "state": "running",
    "stage": "integrating",
    "progress": 0.43,
    "nodes_done": 177,
    "nodes_total": 412,
    "started_at": "2026-10-20T09:14:04Z",
    "error": null
  }
}
```

- `reconstruction`: the current `succeeded` row, or `null`.
- `job`: the active (`queued`/`running`) job; if none, the newest job **if** it `failed` or was
  `cancelled` after the current result (so the UI can show the error); else `null`.
- Unknown map → 404. A map with nothing → `{"map_name": "lab", "reconstruction": null, "job": null}`.

### 5.3 Cancel, delete, files

| Call | Result |
|---|---|
| `POST …/reconstruction/cancel` | `200 {"job": {…, "state": "running", "cancel_requested": true}}`; 409 `no_active_job`. The worker stops within one batch → `cancelled` |
| `DELETE …/reconstruction` | `204`. Cancels an active job, deletes the current result's objects and marks its row `superseded` (history is kept) |
| `GET …/reconstruction/files/{name}` | `name` ∈ `cloud.ply`, `ortho.png`, `height.png`, `meta.json`. Streams the current result's object. `ETag: "<job_id>"`; with `?v=<job_id>` also `Cache-Control: private, max-age=31536000, immutable`. 404 when there is no result or the name is unknown |

Stream with `StreamingResponse` (service: from the MinIO response; gateway: from an
`httpx` stream). Do not load `cloud.ply` into memory in the gateway.

### 5.4 Service-only routes

`GET /health` (use `create_health_response` + `DependencyHealthChecker` from
`packages/utils/service_utils.py`; dependencies `postgres`, `graph_db`, `minio`), `GET /stats`
(jobs run, failed, last duration, queue length), `GET /` (`create_root_response`).

---

## 6. Job lifecycle and status values

`state`: `queued` → `running` → `succeeded` | `failed` | `cancelled`; a `succeeded` job becomes
`superseded` when a newer one succeeds or the result is deleted.

`stage` (while running): `listing` → `integrating` → `filtering` → `writing` → `rasterizing` →
`uploading` → `committing`.

`progress`: 0–1. `integrating` covers 0.05–0.85 (by frames done); the rest are fixed steps.

Failure `error` / event `reason`: `error` (exception text), `timeout` (30 min), `crashed` (the
child died, e.g. the memory limit), `interrupted` (service restarted), `cancelled`,
`map_deleting`.

How it runs:

1. **POST** inserts `queued` (the partial unique index `map_reconstructions_one_active` turns a
   second one into 409 `job_active`) and wakes the worker.
2. **Worker loop** (one asyncio task): claim the oldest `queued` row
   (`UPDATE … SET state='running' … WHERE job_id = (SELECT … FOR UPDATE SKIP LOCKED LIMIT 1)
   RETURNING *`), write `MAP.RECONSTRUCTION_STARTED`, start the child process, wait.
3. **Child**: does design §6.1 steps 1–6 up to the upload. Every batch: `progress`, `stage`,
   `heartbeat_at`; reads `cancel_requested` and the map's `lifecycle`, and stops on either.
   At the end writes `result` and `artifacts` into the row and exits 0.
4. **Parent commit** (one transaction): map not `DELETING`; old `succeeded` → `superseded`;
   this → `succeeded`, `finished_at`; `MAP.RECONSTRUCTION_FINISHED` (savepoint). Then delete the
   old prefix. On any failure: this → `failed`/`cancelled`, event `MAP.RECONSTRUCTION_FAILED`,
   delete this job's prefix. The old result is never touched by a failed job.
5. **Service start**: `running` rows → `failed` (`interrupted`) + event; delete their prefixes and
   any `reconstruction/<job>/` prefix without a `running`/`succeeded` row.

**Stale** (on GET, cached 10 s per map): recompute the digest (design §8.3) over the map's
current nodes with depth and compare with `inputs.digest`. Report `new_nodes`,
`removed_nodes`, `moved_nodes` by comparing node ids and rounded poses with `inputs` (store the
per-node list `[node_id, x, y, yaw]` in `inputs.nodes`; a few hundred KB for 10 k nodes).

Map delete is handled by the API (`packages/api/map_delete.py::_finish` deletes the rows; the
bucket delete removes the objects). Your job only has to notice `DELETING` and stop.

---

## 7. Where the code goes, and repo conventions

```
packages/services/map_reconstruction/
  __init__.py
  main.py          FastAPI app, lifespan, routes, argparse (--host, --port, --log-level) → uvicorn
  server.py        ReconstructionService: job table SQL, worker loop, commit, stale check
  jobs.py          pure state rules + SQL strings (unit-testable, no I/O)
  pipeline.py      pure numpy: codec, back-projection, transforms, voxel grid, filters
  outputs.py       PLY writer, rasters, meta.json
  worker.py        child-process entry: fetch → pipeline → outputs → upload
  client.py        httpx.AsyncClient for the gateway
  requirements.txt fastapi==0.109.1, uvicorn==0.17.6, pydantic==1.9.0,
                   psycopg[binary,pool]==3.0.15, python-arango>=7.9.0, minio>=7.2.0,
                   httpx>=0.27.0, numpy, opencv-python-headless (pin both)
  Dockerfile       python:3.10-slim; copy like packages/services/mission_planner/Dockerfile
                   (service, topomap_dbs, database, events, utils, config.py, cloud_common)
  README.md        routes and config (short)
```

Conventions:

- **Config** only from `packages/config.py`. Add there: `PORT_MAP_RECONSTRUCTION = 8009`,
  `URL_MAP_RECONSTRUCTION = os.getenv("MAP_RECONSTRUCTION_URL", ...)`, and the tunables
  (`RECON_VOXEL_M`, `RECON_MAX_DEPTH_M`, `RECON_CLIP_Z`, `RECON_MAX_VOXELS`,
  `RECON_JOB_TIMEOUT_S`, `RECON_BATCH_FRAMES`), each `os.getenv` with a default. Also
  `MQTT_DEPTH_TOPIC` (R2). `packages/config.py` raises at import if `ARANGO_PASSWORD`,
  `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY` or `POSTGRES_PASSWORD` is missing; your service needs
  all four anyway.
- **Pydantic v1 only** (`==1.9.0`): `class Config`, `@validator`, `Field(...)`. No
  `model_validate`, no `field_validator`. The local test requirements install Pydantic 2, so a
  green local run proves nothing: run the tests in the service image or the test image (§8).
- **Health, logging, errors**: `packages/utils/service_utils.py` (`HealthResponse`,
  `create_health_response`, `DependencyHealthChecker`, `create_root_response`,
  `configure_service_logging("map-reconstruction", level)`), `packages/utils/fastapi_helpers.py`
  (`add_error_handlers(app)`). `logging.getLogger("MapReconstructionService")`; no prints, no
  emojis in new log lines.
- **Postgres**: `packages/database/postgres.py`. It raises `fastapi.HTTPException` itself for
  object CRUD; don't re-wrap. Your own SQL goes through `database.connection()`.
- **Events**: codes in `packages/events/codes.py` (+ a `Source.RECONSTRUCTION`), payload models
  in `packages/events/schemas.py`, a test row in `tests/unit/events/test_codes_schemas.py`.
- **Compose** (`docker_compose/mission_dispatch_services.yaml`), next to the planner:

  ```yaml
  map-reconstruction-service:
    build: {context: .., dockerfile: packages/services/map_reconstruction/Dockerfile}
    image: ${MAP_RECONSTRUCTION_SERVICE_IMAGE:-map_reconstruction_service:latest}
    command: ["python", "-m", "packages.services.map_reconstruction.main",
              "--port", "${MAP_RECONSTRUCTION_PORT:-8009}", "--host", "0.0.0.0"]
    environment: # as mission-planner-service: ARANGO_*, MINIO_*, POSTGRES_*, DATABASE_NAME
    mem_limit: 2g
    memswap_limit: 2g
    network_mode: "host"
    restart: on-failure
    depends_on: [arangodb, minio, postgres]
  ```

  And in `api-delegation-service`: `MAP_RECONSTRUCTION_URL=http://localhost:${MAP_RECONSTRUCTION_PORT:-8009}`.
- **Scripts/docs** to update: `restart_services.sh` (service list), `scripts/check_health.sh`
  (8009), `CLAUDE.md` port table and Key Packages, `packages/api/README.md` (gateway routes).
- Commits: small, one step each; tests with the code.

---

## 8. Run and test locally

**Memory caps are mandatory on this host.** An uncapped test once took 55 GB and OOM-killed the
user's processes. Every test container gets `--memory=2g --memory-swap=2g` (or less) and the kill
timeout goes **inside** the container (a host-side `timeout` only kills the docker client).

Test image (once), based on the service's own requirements:

```bash
docker build -t recon-test:py310 -f - . <<'DOCKERFILE'
FROM python:3.10-slim
COPY packages/services/map_reconstruction/requirements.txt /r.txt
RUN pip install --no-cache-dir -r /r.txt pytest==7.4.4 pytest-asyncio==0.21.1 \
    alembic==1.13.3 SQLAlchemy==2.0.36 paho-mqtt>=1.6.1
ENV PYTHONDONTWRITEBYTECODE=1
DOCKERFILE
```

(The existing `wp6-test:py310` has no numpy/OpenCV; see `tests/integration/dispatch_phase0/README.md`
for how it was built.)

Unit tests, one file per short-lived capped container:

```bash
docker run --rm --memory=2g --memory-swap=2g --network none -v "$PWD":/src:ro -w /src \
  -e ARANGO_PASSWORD=x -e MINIO_ACCESS_KEY=x -e MINIO_SECRET_KEY=x -e POSTGRES_PASSWORD=x \
  recon-test:py310 timeout -s KILL 300 \
  python -m pytest -p no:cacheprovider -q -m unit tests/unit/test_map_reconstruction_pipeline.py
```

Integration (synthetic scene; design §11): `tests/integration/reconstruction/run.sh`, a copy of
the harness in `tests/integration/maps/run_m2.sh`: private `--internal` docker network, no
published ports, Postgres (`timescale/timescaledb-ha:pg17.11-ts2.30.1`), ArangoDB, MinIO,
mosquitto, graph-builder and your service from the checkout, each with `--memory` and
`--entrypoint timeout … -s KILL 900`, everything removed on exit. The scene generator
(ray-cast floor, walls, a box; known colours; a placed session with a non-identity
`map_T_session`) goes in `tests/integration/reconstruction/scene.py` so unit tests can reuse it
at small sizes.

Run the service by hand (only against the test containers, never production):

```bash
python -m packages.services.map_reconstruction.main --port 8009 --host 127.0.0.1
```

with the env of `.env.example` pointing at your test Postgres/ArangoDB/MinIO.

---

## 9. What not to touch

- The robot's Odin driver and its relocalization. The robot side (R1) is a separate step in
  `sati_ros_navstack`.
- Node documents in ArangoDB and `{node_id}/…` objects in MinIO: read only (graph-builder owns
  them).
- `map_sessions`, `mapobjectv1`, the map lifecycle: read only.
- `packages/api/map_delete.py`: only the one `DELETE FROM map_reconstructions` statement in
  `_finish`.
- Production: no deploy, no restarts, no writes to the production databases, no `docker compose
  up` of the main stack. Deploys go through a reviewed script (`~/pg-cutover/scripts/`, style of
  `mapsu1.sh`) after an explicit go-ahead.
- No Pydantic v2 syntax, no Open3D, no new infrastructure containers.
- Bucket creation (see §4.1).

---

## 10. Done checklist

- [ ] `packages/config.py`: port 8009, URL, tunables, `MQTT_DEPTH_TOPIC`.
- [ ] Migration `…_map_reconstructions`: table, two partial unique indexes, the `by_map` index,
      `fleet_events_source_check` + `reconstruction`; idempotent; downgrade; rehearsed up / re-run /
      down / up on a throwaway Postgres.
- [ ] Event codes `MAP.RECONSTRUCTION_STARTED/FINISHED/FAILED`, `Source.RECONSTRUCTION`,
      payload models, schema test.
- [ ] `pipeline.py`: codec, back-projection, `T_map_base · T_base_cam`, voxel grid, edge and
      neighbour filters; unit tests incl. the round-trip property test.
- [ ] `outputs.py`: PLY (read-back test), `ortho.png`, `height.png`, `meta.json` (row/origin
      convention tested).
- [ ] Job table logic: one active per map, claim, heartbeat, cancel, timeout, crash, restart
      recovery, commit with supersede, failure keeps the old result, orphan prefix sweep.
- [ ] Stale check with `new/removed/moved` counts.
- [ ] Never creates a bucket; stops on `DELETING`.
- [ ] Service routes + health/stats/root; gateway routes + `client.py`; 404/409/422/503 as in §5;
      streaming files with ETag / cache headers.
- [ ] `map_delete.py::_finish` deletes the rows (unit test).
- [ ] Compose entry with `mem_limit: 2g`; gateway env `MAP_RECONSTRUCTION_URL`;
      `restart_services.sh`, `scripts/check_health.sh`.
- [ ] Docs: service `README.md`, `packages/api/README.md` routes, `CLAUDE.md` port table,
      design.md "as built" notes for anything that differs.
- [ ] Synthetic-scene integration test green (accuracy, coverage, colours, stale, rebuild, map
      delete race).
- [ ] All tests run in capped containers with the in-container kill timeout, in a Pydantic v1
      image.
- [ ] Measured on a real map: runtime, peak memory, file sizes; numbers written into design.md.
