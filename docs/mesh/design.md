# SatiNav Maps: mesh service

Status: gateway and client built (2026-10-21); the service (`cloud_mesh`) is built in parallel. Modelled on `docs/semantic/design.md`, which is modelled on `docs/reconstruction/design.md`; only the differences are spelled out. Spec of the service: `cloud_reconstruction/docs/mesh_service_spec_2026-10-21.md`; recipe and measurements: `cloud_reconstruction/docs/mesh_quality_handover_2026-10-20.md`.

## 1. Goal

For one map, produce a photo-textured triangle mesh as a **GLB** (`model.glb`, 4096 px JPEG texture, about 300k triangles) plus a `meta.json` (session used and rejected, frames used, pose source, registration statistics, triangle count, bounds, atlas coverage, per-stage seconds, warnings, parameters). Shown only in the map's own 3D view, as a layer. Not wired into reconstruction or semantics: a mesh job never waits for either.

## 2. Architecture (same shape as semantic)

```
client "Build 3D mesh" -> POST /api/v1/maps/{map}/mesh -> gateway (api-delegation-service)
                            |- row map_meshes (queued)
                            '- dispatcher: manifest (nodes + session_id, params, presigned GET/PUT, callback)
                                           | POST /jobs
        mesh service (own repo `cloud_mesh`, GPU, port 8012) <--'
          |- GET rgb + depth of the nodes
          |- ONE session: COLMAP poses, model depth, splat depth, TSDF, cleanup, atlas, bake, texture optimisation
          |- PUT model.glb, meta.json to staging (meta last)
          '- POST progress / finish / fail -> gateway /internal/mesh/jobs/...
gateway on finish: verify -> copy to map-{id}/mesh/{job}/ -> row succeeded
```

The gateway (`packages/api/mesh.py`) is the semantic gateway's shape (a `ReconstructionGateway` subclass, not a subclass of `SemanticGateway`), with its own table (`map_meshes`, migration `20261021_01_map_meshes`), dispatcher lock (`mesh_dispatcher`), events (`MAP.MESH_STARTED|FINISHED|FAILED`, source `mesh`), config (`MESH_*`, `packages/config.py`) and compose file (`docker_compose/mesh_service.yaml`). The service has no credentials and never touches MinIO or ArangoDB.

## 3. What differs

| Topic | Decision |
|---|---|
| Sessions | A map is several mapping sessions that disagree in pose and lighting; fusing them smears everything. The service builds **one**. `params.session` is `auto` (default: the service tries sessions from the most nodes down and takes the first where COLMAP registers at least 85 % of the frames, min 30 nodes) or a session id. |
| `session_id` per node | Every manifest node carries `session_id`: the ArangoDB node document's `session_id` (projected by `GraphDatabaseService.DEPTH_NODES_AQL`), else the `depth.{camera}` record's, else the string `unknown`. `Frame.session_id` (reconstruction.py) carries it; the stale digest ignores it. |
| Thinning | Per session: every `every`-th node (capture order) with `every` chosen so the session has at most `max_frames` camera frames (request value, capped at `MESH_MAX_FRAMES`, default 400). An explicit session sends only that session's nodes (`422 unknown_session` at POST when the map has none, listing the sessions and their node counts; the same reason fails a queued job whose session vanished). `inputs.sessions` records `{nodes, used, every}` per session. |
| Params | `session`, `quality` (`fast` 3000 splat steps, `standard` 8000), `max_frames` (10-2000), `triangles` (10k-1M, 300k), `atlas` (1024/2048/4096/8192), `splat_steps` (500-100000, only when set), `texture_opt` (true). Validated by `StartBody` with the service's own ranges. |
| Outputs | `mesh` = `model.glb` (`model/gltf-binary`), `meta` = `meta.json`. Finish verifies sizes, `meta.json` parses to an object, and the GLB header (magic `glTF`, version 2, header length == size). |
| Result view | `mesh` in the status: `session`, `sessions_rejected`, `triangles`, `bounds`, `poses_source`, `warnings`, `frames_used`, `duration_s` (read from the service's `result`), plus `params`, `inputs.sessions`, `stale`, file URLs. |
| Failure reasons | The reconstruction ones plus the service's `no_usable_session`, `too_few_frames`, `registration_failed`, `gpu_oom`. |
| Orphan sweep | `ObjectStore.result_jobs(prefix)` takes the prefix, so the mesh sweep lists `mesh/{job}/` only (the semantic gateway still lists `reconstruction/`, a no-op there). |

## 4. Routes

`POST|GET|DELETE /api/v1/maps/{map}/mesh`, `POST .../mesh/cancel`, `GET .../mesh/files/{model.glb|meta.json}` (ETag = job id, `?v=<job_id>` immutable, 304, streamed), `POST /internal/mesh/jobs/{id}/progress|finish|fail`. Capability `mesh` in `GET /api/v1/server-info` (URL, key and callback secret all set). Diagnostics check the container `mesh-service`. No nginx change: the default `/api/` location buffers a response to a temp file (up to 1 GB), the 60 s read timeout applies between reads, and tens of MB pass.

## 5. Not done

- A GLB viewer layer in the client (see `sati-client`: needs `@loaders.gl/gltf` or deck.gl mesh layers).
- Robot compose override (`mission_dispatch_services_robot.yaml` has no semantic block either).
- Automatic re-run: jobs start on demand only; a stale flag shows new or moved nodes.
