# SatiNav Maps: semantic layer (DRAFT)

Status: draft for review. Nothing is built. Modelled on `docs/reconstruction/design.md`; only the differences are spelled out.

## 1. Goal

For one map, produce:

1. **Object instances**: class label, map-frame oriented 3D box (centre, size, yaw), confidence, number of supporting views.
2. **Per-point labels**: a class id per point of the map's `cloud.ply` (plus a 2D top-view class raster for the map view).

Shown **only in the map's own view** (map window / `MapView`), as a layer. Not in fleet or mission views.

Out of scope: live/online mapping, relocalization, VPR, anything that changes a pose.

## 2. Architecture (same shape as reconstruction)

```
client "Analyse" ─▶ POST /api/v1/maps/{map}/semantic ─▶ gateway (api-delegation-service)
                      ├─ row map_semantics (queued)
                      └─ dispatcher: manifest (poses, params, presigned GET/PUT, callback)
                                      │ POST /jobs
        semantic service (own repo `cloud_semantic`, GPU) ◀──┘
          ├─ GET frames (+ depth, + cloud.ply if used)
          ├─ segment → lift to 3D → merge across views
          ├─ PUT instances.json, labels.bin, meta.json to staging
          └─ POST progress / finish / fail ─▶ gateway /internal/semantic/…
gateway on finish: verify → copy to map-{id}/semantic/{job}/ → derive top-view class raster → row succeeded
```

Same ownership split as reconstruction §2.1: the gateway owns Postgres, routes, events, MinIO, stale/delete; the service only sees presigned URLs and has no credentials. Reuse the job machinery (dispatcher, retry, poll fallback, spool, callbacks) from `reconstruction*.py` rather than writing it twice; factor the shared parts out if the copy would be large.

## 3. Decisions taken

| Question | Decision |
|---|---|
| Outputs | Both instances and per-point labels |
| Code reuse | Copy a **minimal subset** of `satiperception/packages/satisemantic` into a new repo `cloud_semantic`; no runtime dependency on satiperception |
| Vocabulary | **Per-job choice**: `closed` (EoMT panoptic classes) or `open` (text prompts matched with C-RADIO/SigLIP) |
| Input | **Try both first** (see §4); pick one after measuring |

## 4. Input modes to compare

Selectable per job via `params.depth_source` during the experiment:

- **A. `sensor`**: the reconstruction manifest as is (posed RGB + sensor depth PNG + camera params). Lift masks with the sensor depth. Cheapest; fails on frames with bad depth.
- **B. `da3`**: RGB + poses only; metric depth from Depth-Anything-V3 as in satisemantic. Independent of reconstruction and of sensor quality, but heavier and scale can drift.

(`cloud.ply`-based labelling, i.e. projecting 2D masks onto the finished cloud, is the natural way to produce the per-point labels in either mode, so the semantic job should depend on a *current* reconstruction for output 2. Output 1 does not need it.)

Open point: whether "both" also meant "A and B fused". Assumed here to mean "run both and compare".

## 5. What to take from satisemantic (and what to drop)

Keep (trim heavily):
- `perception/segmentation.py` (EoMT `Segmenter`, `Detection`) and `perception/mask_processing.py`
- `perception/unprojection.py` + `clustering.py` (mask → 3D points, DBSCAN) and `floor_obb.py` (oriented box)
- `perception/radio_extractor.py` or `siglip_encoder.py` (only for the open vocabulary; one of them, not both)
- `backbone/depth.py` (only for mode B)
- A **simplified** cross-view merge: gate on class (or embedding similarity for open vocab) + 3D box overlap/centre distance, union-find as in `mapping/detection_graph.py`.

Drop:
- place recognition (VPR, AnyLoc, LightGlue), keyframe selection/store, relocalization, EKF smoothing, the background thread, CUDA kernels (use numpy/torch), Rerun and the visualizer, the `scripts/`, `_archive/`, report and plot files.

Reason: those solve live, drifting-pose localization. Here poses are given and the job is offline, so frames can be processed in node order with a plain merge pass at the end.

## 6. Contract sketch

- **Manifest**: reconstruction's, plus `params`: `vocabulary` (`closed|open`), `prompts` (open only), `depth_source`, `min_views`, `merge_iou`, `max_depth_m`, and optionally `cloud` GET URL.
- **Outputs** (staging PUT URLs): `instances.json` (list of `{id, label, score, views, box:{c,size,yaw}}`), `labels.bin` (uint8 or uint16 per cloud point, same order as `cloud.ply`) plus `labels.json` (id→name), `meta.json`.
- Top-view class raster derived by the gateway like `ortho.png` (reconstruction §7.2).

## 7. Cloud and client changes

- Postgres `map_semantics` table and states as `map_reconstructions` §8 (unique active job per map, one current result), new Alembic migration, events `MAP.SEMANTIC_*`.
- Routes `/api/v1/maps/{map}/semantic[...]`, mirroring §9.1; config keys in `packages/config.py` (`SEMANTIC_SERVICE_URL/KEY`); compose file `semantic_service.yaml`.
- Client: `useSemantic` hook (like `useReconstruction3D`), status component, a "Semantic" row in the map window Details tab, and a map layer for boxes/labels and the class raster. Gated to the map view only.

## 8. Decisions on the open questions

1. **Independent of reconstruction.** A semantic job never waits for `cloud.ply`. Semantics are also an *input* to reconstruction: dynamic classes (people, vehicles, ...) become per-frame masks that reconstruction can drop. So the job produces (a) instances, (b) its own labelled point set lifted from the frames (not indexed against `cloud.ply`), (c) optional per-frame dynamic-object masks. Labelling an existing `cloud.ply` is a later, separate step. This replaces the "point labels require a reconstruction" idea in §4/§6.
2. **Open-vocabulary prompts are a saved list per map** (`map_semantic_prompts`, edited in the map window); a job snapshots the list into its params.
3. **Closed vocabulary: ADE20K** EoMT checkpoint (to be verified on the HF hub; the satisemantic default is the COCO one).
4. **GPU: run in parallel on the same GPU, guarded by free VRAM.** The host has one RTX 5090 (32 GB); reconstruction peaks at ~12 GB and the semantic models should fit in ~8-10 GB. Same policy as reconstruction: each service checks free VRAM before loading (`SEMANTIC_GPU_MIN_FREE_MIB`) and queues/refuses otherwise, plus a duty pacing knob. Revisit if it contends in practice.
5. **No automatic re-run.** Semantic jobs start on demand only and are separate from reconstruction rebuilds. A stale flag (new/moved nodes) is shown, as for reconstruction.

## 9. Proposed build order

1. Offline prototype in `cloud_semantic` on one recorded map: closed vocabulary, mode A, instances only; compare with mode B.
2. Per-point labels against `cloud.ply`; open vocabulary.
3. Service wrapper (`POST /jobs`, callbacks, spool) copied from `cloud_reconstruction`.
4. Gateway module, migration, routes, events.
5. Client hook, status row, map layer.

## 10. Prototype status (2026-10-16)

`web_stack/cloud_semantic/`, closed vocabulary (ADE20K), both depth sources. Test map `odin_glare`, every 5th node (82 frames), RTX 5090.

| | sensor depth | DA3 (RGB only) |
|---|---|---|
| time | 8.7 s | 12.8 s |
| instances | 44 (none > 2.5 m footprint) | 46 (2 oversized: a 3.8 m window, a 2.7 m chair) |
| label voxels (10 cm) | 25.5k | 56k (fills depth holes, but walls extend past the cloud) |

- Agreement between the two: 28/44 instances found in the other (same class, centres < 0.5 m); 77 % same label on shared voxels.
- DA3METRIC ignores the intrinsics passed to it; metres = output x focal / 300 (focal in network-input pixels). With that, DA3 is 0.93x the sensor depth, abs-rel 13.5 %, 92 % of pixels within 25 %.
- Dynamic classes (person, ...) are excluded from instances and static labels and exported as dilated per-frame masks (`dynamic_masks/`).
- Same-class instances are merged in two passes (per-frame overlap, then `consolidate`), 60 -> 44 instances on the sensor run.
- Conclusion so far: sensor depth for instances; DA3 is a fallback for maps without depth. Still no ground truth.
- Not done: open vocabulary, service wrapper, gateway, client.

### 10.1 Instance method (revised 2026-10-16, evening)

Frame-to-frame association (same-class merge, then label voting and cannot-link) was fragile: a looser lift gave more fragments, a stricter one dropped objects. Replaced as the default (`--instances cc`) by **voxel class vote + 3D connected components** per thing class (`semantic_service/components.py`); the association merger stays available as `--instances merge`.

Checks that now exist: `tools/overlay_points.py` (visible instance points on the RGB frame, one colour per instance), `tools/evaluate.py` (reprojection precision, fragmentation, unmatched segments), pose consistency between frames (median 1-2 cm depth residual) and lift self-consistency (1.0). On `odin_glare`: 91 instances, median footprint 0.47 m, max 2.2 m; reprojection precision 0.45 (own class within 4 px: 0.55). The precision number is pessimistic (sensor depth covers only ~19 % of pixels, class labels flicker at edges) and is for comparing runs, not an accuracy claim.

Known flaws: touching objects of one class stay one instance; some stray points (a "bottle" floating on the floor); small objects (a bucket) still missed; about half the thing segments have no instance by the evaluate rule. Fixes under consideration: use the 2D panoptic ids to split touching objects, fill the sensor depth holes with DA3.

### 10.2 Acceptance test (2026-10-16)

`cloud_semantic/eval/ground_truth.json`: 39 objects hand-listed by Claude on 10 frames of `odin_glare` (11 chairs, 3 tables, 7 storage units, 18 people), **not yet reviewed by a human**. Scored by `tools/acceptance.py`: found / single instance / split, plus listed objects merged into one instance. People are dynamic (no instances), scored on the dynamic mask. Bar: >= 80 % of listed objects found as a single instance.

| run | static single | split | missed | merged | overall |
|---|---|---|---|---|---|
| connected components per class | 16/21 | 4 | 1 | 0 | 33/39 = 0.85 |
| + DA3 hole fill (`hybrid`) | 16/21 | 4 | 1 | 0 | 34/39 = 0.87 |
| bridge 2 / 3 voxels | 16/21 | 4 | 1 | 0 / 1 | 0.85 |
| **+ class kinds grouped (default)** | **19/21** | 1 | 1 | 0 | **36/39 = 0.92** |

What the test showed: the failures were splits caused by class-name flicker (shelf / bookcase / cabinet for one unit), not touching objects merged, so the 2D-id split was not needed (0 merges) and was not built. Depth hole filling and gap bridging did not help. Remaining: one chair with a small fragment split off, one long desk missed (chairs in front), one person missed by the segmenter. Caveat: the kind grouping was found and checked on the same 10 frames; confirm on other frames/maps before trusting 0.92. Small objects are not in the list.

### 10.3 Overlap resolution and integration (2026-10-17)

`overlaps.py` merges boxes of one family that lie mostly inside each other (couch named sofa/seat/bed/box across views) and absorbs small strays into seating furniture; books in a shelf or a box in a cabinet stay separate. `odin_glare` acceptance unchanged (36/39).

Integrated like reconstruction (contract: `web_stack/cloud_semantic/CONTRACT.md`): service `cloud_semantic` (image `sem-service:1.0.0`, port 8010, compose `docker_compose/semantic_service.yaml`), gateway `packages/api/semantic.py` (subclass of the reconstruction gateway, table `map_semantics`, migration `20261015_01_map_semantics`, events `MAP.SEMANTIC_*`), client `SemanticRow` + `semanticLayer3D` (map's own 3D view only). Deployed 2026-10-17; first real job on `office7`: 296 frames, 46.7 s, 140 instances, 364k label voxels, 89 frames with moving objects.

Not done: open vocabulary (POST answers 422), per-map saved prompts, consuming `dynamic.zip` in reconstruction, DA3 inside the image (needs the checkout installed), hide-above slider and class filter in the client, `MapLibreMap` 3D view, a browser check of the layer.
