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
