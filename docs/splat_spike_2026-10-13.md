# Gaussian-splat reconstruction spike (2026-10-13)

Feasibility spike, read-only against the live stack. Scripts: `docs/splat_spike_scripts/`
(export.py reads ArangoDB + MinIO read-only, train.py is a small gsplat trainer). Outputs and
renders live in the session scratchpad `.../scratchpad/splat_spike/out/<run>/` (not committed).

## Setup that worked
- Host torch 2.9.1+cu128 already supports sm_120. venv (system-site-packages) + `gsplat 1.5.3`
  (JIT-compiled once, 42 s, with `/usr/local/cuda-12.8` nvcc; the default nvcc 12.0 cannot target
  sm_120), lpips, plyfile, ninja. Network pip worked. Own ~200-line trainer on gsplat
  (DefaultStrategy densification optional). No nerfstudio.
- GPU: RTX 5090, 250 MiB used by others at start. Process memory cap 30 % (9.8 GB); actual peak
  allocated 0.25-1.2 GB (SH3: 2.4 GB).

## Data (office7: 95 nodes; office8: 98 nodes)
- 1 camera ("primary") per node, 448x336 RGB JPEG + u16-mm depth of the same size, plumb_bob d = 0
  (already rectified, so nothing to undistort), fx = fy = 174 (HFOV 104 deg, VFOV 88): wide angle,
  small image. Camera 0.25 m above the floor, low and looking along it.
- Poses: `pose3d_map` on every frame. Extent only 7 x 4 m.
- Node spacing: nearest-neighbour distance median 0.14 m (p10 0.06, p90 0.32, max 0.68), but
  consecutive nodes are 0.5-0.8 m apart: the dense neighbours are mostly other passes. View-direction
  difference to the nearest node: median 54 deg (office7), p90 171 deg (opposite directions).
  24 nodes within 2 m on average. Yaw spread is broad (8-bin histogram 4..21 per bin).
- Depth is a regular lattice, only ~21 % of pixels valid, median 1.4-1.6 m. Back-projected: 2.6 M
  raw points for 83 train frames.
- Pose consistency check (neighbour depth re-projected into a held-out frame): median relative depth
  residual 2.8 %, p90 6.5 % (~4 cm at 1.5 m). Fine for geometry, but several pixels of misalignment
  for texture, which caps splat sharpness.
- office8 was recorded in the dark (evening); its high PSNR is not comparable. office7 is the
  meaningful map (bright windows, mixed exposure).

## Results (office7, held-out = every 8th node, 12 frames, 83 train; wall times are train only)
| run | gaussians | train s | VRAM MB | PSNR | SSIM | LPIPS | ply MB | .splat MB |
|---|---|---|---|---|---|---|---|---|
| depth init 3 cm, no densify, 3k | 769k | 15 | 727 | 17.84 | 0.599 | 0.490 | 50 | 23 |
| depth init, densify, 3k | 865k | 17 | 857 | 17.25 | 0.591 | 0.493 | 56 | 26 |
| depth init, densify, 7k | 1.03M | 41 | 941 | 17.21 | 0.573 | 0.481 | 67 | 31 |
| depth init, densify, 15k | 1.29M | 97 | 1059 | 16.23 | 0.533 | 0.508 | 84 | 39 |
| depth 6 cm voxels, 7k | 510k | 35 | 675 | 16.88 | 0.580 | 0.491 | 33 | 16 |
| sparse init (20k pts), 7k | 398k | 33 | 617 | 17.55 | 0.591 | 0.469 | 26 | 12 |
| random init 100k, 7k | 124k | 29 | 317 | 11.57 | 0.452 | 0.789 | 8 | 4 |
| depth init + depth loss 0.1, 7k | 1.07M | 47 | 958 | 17.67 | 0.590 | 0.442 | 70 | 33 |
| SH1 / SH3, 7k | 1.03M | 43 / 62 | 1154 / 2413 | 17.2 / 17.1 | 0.57 | 0.48 | 102 / 245 | 32 |
| depth loss + exposure + scale reg, no densify, 3k | 769k | ~19 | 741 | 18.1 | 0.612 | 0.472 | 50 | 23 |
| same, 1.5k steps | 769k | 10 | 743 | 18.4 | 0.626 | 0.498 | 50 | 23 |
| depth loss, 6 cm voxels, scale reg, 3k | 339k | 17 | 596 | 18.34 | 0.633 | 0.479 | 22 | 10 |
| depth loss, sparse init, scale reg, 3k | 186k | 16 | 535 | 18.76 | 0.640 | 0.487 | 12 | 5.7 |
| half-res images (224x168), 3k | 325k | 15 | 251 | 18.5 | 0.60 | 0.336 (other res) | 21 | 10 |
| harder: hold out 12 consecutive nodes, 3k | 755k | 19 | 739 | 16.6 | 0.486 | 0.657 | 49 | 23 |

Speed: ~165 it/s at 448x336 (more than 10x faster than a typical 1080p scene). Total job time
est. ~30-45 s for 100 nodes (load 0.4 s, init 1 s, train 10-20 s, eval/export excluded).

Findings:
- More steps or more gaussians do NOT help held-out views (train PSNR rises 25 -> 30, test falls).
  Training is overfitting to few views per surface; 1.5-3k steps is enough.
- Depth init is essential (random init: 11.6 dB, LPIPS 0.79). Subsampled "sparse" init is as good as
  the full cloud at 1/4 to 1/5 of the size. Densification adds nothing here.
- Depth loss helps LPIPS slightly, exposure compensation and scale regularisation ~0.2-0.3 dB.
  SH > 0 is useless (bigger file, no quality gain). No quality knob moved test PSNR above ~18.8 dB.
- Honest quality statement: office7 renders are recognisable (windows, shelves, chairs, board) with
  correct geometry/depth, but blurry and floaty; the floor smears (grazing angles, low camera), bright
  windows are blown out, and held-out PSNR is only 17-19 dB. This is a data limit
  (448x336, 21 % depth, auto-exposure, few-cm pose error, ~25 views of the same patch from similar
  low heights), not a trainer one. It is a "coloured blobs for orientation" product, not photoreal.
- Compressed formats: I wrote .splat (32 B/gaussian, 23-26 MB for 770-870k; gzip saves ~5 %).
  SPZ / SOG / ksplat were not generated (no tool installed); SPZ/SOG typically give ~10x vs ply
  (unmeasured here), so ~5 MB for a 400k splat is plausible. The 186k-gaussian sparse run is 12 MB
  ply / 5.7 MB .splat already.

Images (scratchpad): `out/g_depthsup_7k/heldout.png` (GT | render | depth, 6 held-out), `out/*/novel.png`
(3 novel views), `out/sample_frames.jpg` (inputs + depth).

## Viewers (read-only research, from a web search; versions/licences to verify)
- Spark (sparkjsdev, three.js, MIT): .ply, .spz, .splat, .ksplat, .sog. Likely best pick.
- GaussianSplats3D (mkkellogg, three.js, MIT): ply, splat, ksplat, spz.
- Reall3dViewer (three.js; .spx, .sog, .spz, .splat, .ply, glTF KHR_gaussian_splatting).
- Search reports native splat support in three.js r186 core (unverified by me).
- deck.gl: no native splat layer found; would be a custom luma.gl layer (work) or an overlaid
  three.js canvas sharing the camera. A separate three.js viewer panel is the cheaper first step.
  The scene is in the map frame (metres, z up), so no georeferencing is needed for a local viewer.

## Integration notes for the reconstruction contract
- Manifest unchanged (poses, pose3d, camera params, rgb/depth URLs are all that was used). Add
  optional `params.splat: {steps, init_voxel_m, max_gaussians}` and optional `outputs.splat`
  (PUT URL, `application/octet-stream`; format ply or .splat/.spz) and `outputs.splat_meta` or a
  `result.splat` block (gaussians, bytes, sha256, train_s, held_out_psnr). Service reports only if
  the output is requested; failure of the splat must not fail cloud.ply (separate state `splat_failed`).
- Gateway: allow `outputs.splat` in the finish check (size cap, no PLY-17-byte rule), copy it to
  `map-{id}/reconstruction/{job}/splat.*`, add a file route; expose it in the reconstruction view.
- Sizes: cap ~40 MB output (<= ~1M gaussians raw ply); target 100-400k gaussians. Cost: +30-60 s
  GPU, <1.2 GB VRAM at 448x336, so it runs next to the existing service.
- Recommended trainer settings: depth-voxel init subsampled to ~200-400k points, no densification,
  SH0, 1.5-3k steps, depth L1 weight 0.1, per-image exposure gain/bias, scale regulariser, lr as in
  train.py; hold out nothing in production.

## Recommendation
Conditional go for a cheap optional splat output (it is fast and small); no-go for expecting photoreal
quality from the current capture. Needed for better quality: higher-res RGB (>= 1280x960, 4x the
pixels of today), full-density depth or monocular depth prior, fixed exposure, camera height variation
or a second higher camera, and an RGB-pose time-sync check. Untested: pose refinement (excluded by the
brief), mapping office8 in daylight, spz/sog encoders.

---

# Round 2: what actually fixes the noise (office7)

Trainer: `docs/splat_spike_scripts/train2.py` (strategies, 2DGS, dense depth, normals, pose deltas).
All runs: 1500 steps unless noted, every-8th hold-out (12 frames), GPU cap 30 %, peak 0.4-0.7 GB.

## Root cause: per-frame pose error, not the trainer and not the resolution
- Warping the nearest GROUND-TRUTH neighbour image into a held-out frame through the (densified)
  depth gives only 17.4 dB (18.3 exposure-aligned) -- the same ceiling every splat run hit.
- Fitting a free 6-DoF correction per pair lifts that to 22.5 dB, with corrections of mean 2.0 deg and
  6 cm (some 15 cm). A single shared camera-frame correction (extrinsic) does nothing (17.16 -> 17.07), so
  it is random per frame (pose jitter / time offset between image stamp and pose), not a calibration
  bias. A 2 deg error is ~6 px at fx = 174, which is why surfaces blur and the model overfits.
- Resolution is not the binding constraint: rendering/scoring at 224x168 gains only +0.2-0.35 dB.
  Exposure is a minor factor (aligned PSNR is +0.5 dB).
- (This contradicts "poses are good, no refinement needed" at the pixel level: they are good at the cm
  level for the planner, not for splatting at 6 px/2 deg.)

## Options tried (test PSNR with held-out poses fitted / with raw manifest pose, SSIM, LPIPS)
Pose delta per frame learned during training (lr 1e-3, from step 300); held-out frames get a 100-step
pose fit on the frozen scene (an optimistic "fitted" number; the raw-pose number is in brackets).
| config | gaussians | train s | PSNR fitted (raw) | SSIM | LPIPS | MB .splat |
|---|---|---|---|---|---|---|
| round-1 best (raw sparse depth init, no densify, expo, 3k) | 219k | 15 | - (18.57) | 0.631 | 0.482 | 6.7 |
| MCMC 200k, no pose opt | 200k | 8 | - (18.32) | 0.639 | 0.541 | 6.1 |
| MCMC 200k + pose opt | 200k | 9 | 20.65 (18.54) | 0.695 | 0.551 | 6.1 |
| MCMC 3k steps | 200k | 17.5 | 21.52 (19.04) | 0.714 | 0.520 | 6.1 |
| + dense depth loss (bilateral-filled) | 200k | 9 | 20.79 (18.73) | 0.703 | 0.550 | 6.1 |
| + depth-derived normal loss | 200k | 10 | 20.61 (18.60) | 0.704 | 0.552 | 6.1 |
| init from service cloud.ply (299k pts) | 200k | 9 | 20.86 (18.81) | 0.704 | 0.551 | 6.1 |
| init from dense (filled) depth | 200k | 9 | 20.77 (19.18) | 0.703 | 0.560 | 6.1 |
| antialiased rasterization | 200k | 9 | 20.79 (18.43) | 0.699 | 0.552 | 6.1 |
| SSIM weight 0.5 | 200k | 9 | 20.58 (18.33) | 0.700 | 0.548 | 6.1 |
| scale clamp 0.003-0.15 m | 200k | 9 | 20.50 (18.19) | 0.694 | 0.559 | 6.1 |
| default densify + pose opt | 357k | 9.5 | 20.43 (18.44) | 0.684 | 0.535 | 10.9 |
| MCMC budget 100k / 400k | 100k/400k | 8/10 | 20.75 / 20.83 | 0.693 / 0.706 | 0.568 / 0.540 | 3 / 12 |
| 2DGS + default densify | 380k | 16 | 20.92 (18.72) | 0.700 | 0.472 | 11.6 |
| 2DGS + MCMC 200k | 200k | 15 | 20.93 (18.94) | 0.700 | 0.513 | 6.1 |
| **2DGS + default + dense depth 0.2 + normals 0.05 + pose lr 3e-4** | 388k | **17** | **20.77-20.82 (18.7-18.8)** | 0.703 | **0.434** | 11.8 |
| same, 3000 steps, pose lr 5e-4 | 502k | 37 | 21.01 (18.58) | 0.712 | 0.422 | 15.3 |
| same with 500 steps | 318k | 6 | 20.09 (18.71) | 0.681 | 0.493 | 9.7 |
| pose lr 3e-3 | 420k | 34 | 20.3 (train PSNR collapses to 20.6) | 0.66 | 0.54 | 13 |
| stronger normal (0.3) + depth TV | 396k | 17 | 20.23 | 0.678 | 0.473 | 12.1 |

Block hold-out (12 consecutive nodes), same new best vs round-1 best: PSNR 18.31 (17.20 raw) vs 17.36,
SSIM 0.562 vs 0.538, LPIPS 0.575 vs 0.660.

Depth densification: a joint-bilateral fill of the 21 % lattice (RGB-guided, 2 s for 95 frames) predicts held-out
lattice points to 0.5 % median relative error (94 % within 5 %). Monocular prior (Depth Anything V2-Small,
aligned to the lattice) was unusable on this wide-angle, dark footage: median 49 % error, correlation
0.15-0.9; MoGe-2 was not run. Dense depth supervision helps LPIPS and sharpness, not PSNR.

Everything lands on the same ~20.8 dB (fitted) / ~18.7 dB (raw) plateau, i.e. the data (pose jitter),
not the strategy, is binding. Held-out PSNR without pose fitting stops improving after ~500 steps
(18.5-18.75 from 250 steps on), so early stopping at 500-1000 steps is safe.

## Best configuration (quality per second)
`--family 2dgs --strategy default --poseopt 1 --pose_lr 3e-4 --dd bil --dd_w 0.2 --nrm_w 0.05 --steps 1500`,
SH0, init = raw depth voxelised to 5 cm, per-frame exposure. 17 s train (+2 s depth fill, ~1 s init), 0.64 GB VRAM,
388k surfels, 11.8 MB .splat. Vs round 1: LPIPS 0.49 -> 0.43, SSIM 0.63 -> 0.70, PSNR +2 dB with fitted poses.
Comparison images (scratchpad `out/`): `compare_heldout.jpg` (GT | round-1 best | new best | new best depth, 6 held-out)
and `compare_novel.jpg` (round-1 best | new best | new-best depth, 3 novel views).
Visual judgement: the new best is clearly sharper and less fragmented in the far field (windows, shelves,
ceiling, chair legs) and its depth maps are clean with sharp edges; round 1 had holes, smeared blobs and floating
fragments. Remaining defects: the floor and objects closer than ~1 m (camera 0.25 m high, grazing angle) are
streaky/smeared with some needle artifacts; bright windows are partly blown out; dark areas stay blurry. It is
better, still not photoreal.

## Feed-forward (AnySplat / DepthSplat / MVSplat): not run
AnySplat (MIT licence on GitHub and HF) is the only candidate checked. It predicts its own poses (cannot use
the known ones without a Sim(3) alignment), takes 448x448 square input, and needs pinned heavy deps
(pytorch3d, xformers 0.0.24, torch_scatter, e3nn). A pip install attempt hung and was abandoned in the time box;
a derived VGGT backbone may carry non-commercial terms (not verified). Given that the binding problem here is
pose jitter, a feed-forward model that ignores our poses would not address it. No numbers.

## Updated recommendation
Go for the optional splat output with the config above (about 20-25 s total per 100 nodes, 12 MB), expected to be
"clean enough for orientation/visual context". For real photo quality the fixes are on the capture side:
pose/image timestamp alignment (<10 ms) or per-frame pose refinement against the images, higher RGB resolution
is secondary (half-res scoring changes only ~0.3 dB), a higher or tilted-down camera for the floor.
