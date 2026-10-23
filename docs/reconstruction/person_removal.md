# Person removal (dynamic objects) in the reconstruction and the mesh

Status 2026-10-23: built on branch `feat/person-removal` (cloud_server + sati-client), unit-tested, **not deployed**,
no image built. Optional and **off by default**.

People walking through a mapping run end up in the 3D reconstruction (`cloud.ply`, its top view) and in the textured
mesh (`model.glb`) as smeared ghosts. This step finds people in every keyframe photo, and the reconstruction and mesh
jobs then leave those pixels out.

## 1. Where the masks are applied, and why there

What consumes a keyframe's depth and colour:

| Consumer | Reads | Effect of a masked frame |
|---|---|---|
| Reconstruction service, `raw` backend (the Pi, `reconstruction_service_robot.yaml`) | sensor depth + RGB colours, from the manifest URLs | **full**: person pixels have depth 0 = no data (handover §4), so no points |
| Reconstruction service, MapAnything backend (cloud `auto`) | RGB -> model depth, anchored to the sensor depth | partial: the sensor anchor is gone there, the model still predicts the person; needs `mask_url` support (§6) |
| Mesh service (`cloud_mesh`) | RGB (COLMAP poses, model depth, splats, texture), sensor depth as anchor | partial for the same reason; needs `mask_url` support (§6) |
| Scene graph / occupancy from depth (`depth_rebuild.py`), robot costmaps | raw depth / robot-made costmaps | none (not wired; §8) |
| Client (deck.gl point cloud, mesh layer, top view) | the services' outputs | gets the cleaned result after the reconstruction / mesh is run again |

Decision: the masks are made and stored **by the cloud_server side, once per keyframe camera**, next to the raw data,
and both gateways put them into their manifests. Reasons:

- both services read exactly the same per-node RGB + depth through the manifest, so one place feeds both;
- the services have no credentials and no state (handover §1): they could not keep masks between jobs, and computing
  people twice (reconstruction and mesh) would double the CPU on the Pi;
- the raw objects are never written: removal is turned off by a setting and redone by another run;
- the masked depth works with the services **as deployed today** (the field `depth_url` simply points elsewhere), and the
  extra `mask_url` lets a service that knows it also drop the pixels from model depth and textures.

## 2. Data flow

```
client "Remove people" -> POST /api/v1/maps/{map}/person-removal          (packages/api/person_removal.py, a job in the API)
   per node camera with depth (not done yet with this model + margin, unless force):
     GET  {node}/images/{camera}            (RGB, MinIO)
     POST person-mask service /v1/segment   -> mask PNG at RGB size (255 = person)
     mask -> fitted to the depth size (conservative resize) -> grown by dilate_px (square)
     depth (raw, never modified) -> copy with mask pixels = 0
     PUT  {node}/person_mask/{camera}.png   (u8)           \  only when a person was found
     PUT  {node}/depth_masked/{camera}.png  (u16 mm PNG)   /
     node.person_mask.{camera} = {version, model, dilate_px, persons, pixels, ratio, width, height, at, job_id}
   end: the service is told to unload its model

POST .../reconstruction | .../mesh  (body person_removal: true|false|absent)
   job.params.person_removal = the run's value, else the map's setting, else PERSON_REMOVAL_DEFAULT
   manifest, per camera, when on and the frame has a person:
     depth_url = {node}/depth_masked/{camera}.png, mask_url = {node}/person_mask/{camera}.png
   otherwise the raw depth, exactly as before; `person_removal` itself is never sent to the services
   job inputs.person_removal = {enabled, masked, clear, unchecked}  (shown in the result view)
```

The PNGs are written before the node record, so a manifest never points at a file that is not there. A frame never
checked (or checked with an older record version) is `unchecked` and sent raw. The stale digest of a reconstruction /
mesh does not include the masks (a new mask run does not mark results stale; run them again to apply the masks).

Temporal dynamic objects (things that move between keyframes without being people) are **not** handled: a cheap version
would vote voxels across frames (free-space carving) inside the reconstruction service, not here.

## 3. Settings

| Where | Name | Default | Meaning |
|---|---|---|---|
| API env / profile | `PERSON_MASK_SERVICE_URL` | unset | the service; unset = feature off (capability `person_removal` false, POST 503) |
| API env / secrets | `PERSON_MASK_SERVICE_KEY` | unset | bearer key, if the service has one |
| API env / profile, robot setting | `PERSON_REMOVAL_DEFAULT` | `false` | jobs on maps without their own choice use the masks |
| API env / profile | `PERSON_REMOVAL_MODEL` | empty | the service model name to use (empty = the service's default) |
| API env / profile, robot setting | `PERSON_REMOVAL_DILATE_PX` | `6` | mask margin in depth pixels (0-64) |
| API env | `PERSON_MASK_TIMEOUT_S` | `120` | per request |
| per map (Postgres `map_person_removal`) | `{enabled, model, dilate_px}` | all null | null = the server's value |
| per run | `person_removal` in the reconstruction / mesh POST body | absent | true / false overrides the map |
| per run | `model`, `dilate_px`, `force`, `session_id` in the person-removal POST body | | |
| robot setting | `MEM_PERSON_MASK` | `768m` | the service container's limit; counts in the memory budget (`check_budget`, 95 %) only when `PERSON_MASK_SERVICE_URL` is set in the profile |
| service env | `PERSON_MASK_MODELS` | `nano=onnx:/models/yolo11n-seg.onnx` | `name=<kind>:<path>;...`, kinds `onnx`, `ultralytics`, `grounded-sam2` |
| service env | `PERSON_MASK_DEFAULT_MODEL`, `PERSON_MASK_DEVICE` (`cpu`/`cuda`), `PERSON_MASK_CONF` (0.35), `PERSON_MASK_THREADS`, `PERSON_MASK_IDLE_UNLOAD_S` (120; robot 60) | | |

The robot settings appear in the app's robot settings (Server health) through `robot_config.py`; the profiles carry
commented lines only, so no machine's config hash changes until someone turns it on.

## 4. API

```
GET  /api/v1/maps/{map}/person-removal
  200 {map_name, available, settings: {enabled, source: map|server, model, dilate_px}, stored_settings,
       coverage: {frames, checked, masked} | null (null while a job runs), job: <job> | null}
POST /api/v1/maps/{map}/person-removal   {session_id?, model?, dilate_px?, force?}
  202 <job> (200: the same scope already runs) | 404 map_not_found, session_not_found | 409 map_deleting,
  session_active, no_depth, job_active (+job) | 422 | 503 not_configured
POST /api/v1/maps/{map}/person-removal/cancel     200 <job> | 404 no_active_job
PUT  /api/v1/maps/{map}/person-removal/settings   {enabled: bool|null, model: str|null, dilate_px: int|null}
  200 {map_name, settings, stored_settings}  (all null removes the map's row)

<job> = {job_id, map_name, session_id, state: queued|running|succeeded|failed|cancelled, progress 0-1, model,
         dilate_px, force, requested_by, requested_at, started_at, finished_at, nodes_total, frames_total, done,
         masked, clear, persons, skipped {done_before}, failed, failed_by_reason {rgb_missing, depth_missing,
         unreadable, segment_failed, store_failed, error}, failures [{frame, reason, detail}], seconds_per_frame,
         warnings, error {reason, message} | null, cancel_requested}
```

Job failures: `service_unavailable`, `unknown_model`, `model_unavailable` (weights not mounted / package missing),
`nothing_written`, `map_deleting`, `stopped` (API restart). Like the rebuild from depth, the job lives in the API
process (a restart forgets it; written masks stay and the next run skips them), one job per map, one map at a time.

Reconstruction and mesh: the POST bodies accept `person_removal`; the result views (`reconstruction`, `mesh`) carry
`person_removal: {enabled, masked, clear, unchecked}` (null for results made before this). `GET /api/v1/server-info`
has `capabilities.person_removal`.

Manifest extension (both services; additive, older services ignore it): per camera, optional `mask_url` (presigned GET,
PNG u8 at the depth image size, 255 = a dynamic object to leave out). When it is present `depth_url` already has those
pixels at 0.

## 5. The person-mask service (`packages/services/person_mask`)

A FastAPI process with no database and no MinIO access (`GET /health`, `POST /v1/segment?model=`, `POST /v1/unload`;
contract in `server.py`). The model is loaded on the first frame, only one model is resident, and it is unloaded after
each API run and after `PERSON_MASK_IDLE_UNLOAD_S` without frames (with `malloc_trim`, so the RSS really drops).
Heavy packages are imported only by the backend that needs them; a missing package or missing weights is a 503
`model_unavailable` whose message names what to install or mount.

- **Robot (Pi 5, arm64, no GPU)**: CPU image (`Dockerfile`, `requirements.txt`: onnxruntime + numpy + Pillow, no
  torch), backend `onnx` with a nano YOLO-seg model, 2 threads, `cpus: 2`, `MEM_PERSON_MASK` 768m.
  `docker_compose/person_mask_service_robot.yaml`.
- **Cloud / dev box (GPU)**: the same Dockerfile with `BASE` = a PyTorch CUDA 12.8 image and `requirements-gpu.txt`;
  models `large` (`ultralytics`, e.g. yolo11x-seg) and `gsam2` (`grounded-sam2`: Grounding DINO prompted "person." +
  SAM 2.1 large, from the Grounded-SAM-2 checkout on this box) next to `nano`. Selected per run / map / server by name.
  `docker_compose/person_mask_service.yaml`.

### Weights (never in git, never baked into an image)

Mounted read-only at `/models` from `PERSON_MASK_MODELS_DIR` (robot default `/srv/sati/person_mask_models`, cloud
default `~/satinavrobotics/person_mask_models`). To make the robot's ONNX file (on the dev box, which has ultralytics):

```
yolo export model=yolo11n-seg.pt format=onnx imgsz=640 opset=17     # -> yolo11n-seg.onnx (~11 MB)
scp yolo11n-seg.onnx satipi@<pi>:/srv/sati/person_mask_models/
```

Any ultralytics `*-seg` export works (v8 / 11 / 26, classic or end-to-end head; checked: yolo26n-seg). Grounded-SAM-2:
`sam2.1_hiera_large.pt` and `groundingdino_swinb_cogcoor.pth` in `/models/grounded_sam2/` (both are in
`~/satinavrobotics/Grounded-SAM-2/{checkpoints,gdino_checkpoints}` on the dev box) and the checkout mounted at `/gsam2`.

**Licences**: Ultralytics YOLO weights and the `ultralytics` package are **AGPL-3.0** (an Enterprise licence is needed
for closed commercial use); SAM 2 and Grounding DINO are Apache-2.0. Decide before shipping the YOLO path to customers.

## 6. What the services must do with `mask_url` (other repos, not done)

- `cloud_reconstruction` (MapAnything backend) and `cloud_mesh`: parse the optional `mask_url` in `manifest.py`
  (unknown keys are already ignored, so nothing breaks before that), fetch it with the frame, and set the **model**
  depth to 0 where the mask is set before fusion (`pipeline/depth.py` `calibrate` / TSDF), and give those pixels zero
  weight in texturing (`bake.py` / `texopt.py`) so a wall behind a person is textured from another view. The raw backend
  needs nothing (the masked sensor depth already removes the points).
- Splat training in the mesh service could also ignore masked pixels in the photometric loss.

## 7. Resources

Measured on the dev box (x86-64, onnxruntime 1.24 CPU, yolo26n-seg 640, 2 threads; not the Pi): ~80 ms per frame,
process RSS 34 MB idle -> 91 MB with the model loaded -> 216 MB peak after inference -> 71 MB after unload. Masks matched
ultralytics' own on its two sample images (IoU 0.95 and 0.99).

**Estimates, not measured** on the Pi 5 (Cortex-A76, 4 cores, 2 threads): 0.3-0.6 s per 640 px frame for a nano model,
so roughly 5-10 min per 1 000 frames, plus ~0.1 s per frame of PNG work in the API; peak RSS of the service 250-400 MB
(768m limit), nothing held between runs. The API side holds one frame at a time (a few MB). Larger models
(yolo11s/m-seg) are 3-10x slower: not for the Pi. GPU (dev box): yolo11x-seg tens of ms per frame; Grounded-SAM-2 large
is several GB of VRAM and ~0.2-0.5 s per frame (estimate).

## 8. Not done / follow-ups

- `mask_url` in the two services (§6). Until then the cloud MapAnything / mesh paths are only partly cleaned.
- Scene graph / occupancy from depth (`depth_rebuild.py`) still uses the raw depth; it could take `depth_masked` when
  the map's removal is on. Robot-made costmaps are not covered.
- A map export (`packages/migration/export.py`) is not checked for the new `person_mask` field and objects.
- Old derived PNGs of a frame that a later run finds clear are left in place (the record says clear, so unused).
- Measure on the Pi: time per frame and peak RSS (the job reports `seconds_per_frame`; the container in Server health).
