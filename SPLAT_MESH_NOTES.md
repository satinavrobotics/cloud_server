# 3D map spike: splat and mesh results (office7, 2026-10-16/17)

Status: superseded (2026-10-21) by the mesh service: gateway `packages/api/mesh.py`, design `docs/mesh/design.md`, service spec and recipe in `cloud_reconstruction/docs/mesh_service_spec_2026-10-21.md` and `mesh_quality_handover_2026-10-20.md`. This spike (licence-clean TSDF mesh judged to have holes, AGS-Mesh non-commercial) is kept as the record of the earlier decision. The scripts live in a scratchpad, not in this repo.

## Setup

- Room "office7": 94 robot frames (image 448x336, depth, pose). COLMAP (pycolmap 4.2.1, fixed intrinsics, Sim3 to robot poses) registers 95/95, 94 kept.
- Held-out set: 12 frames, every 8th (`range(3, 94, 8)`).
- Image metrics: PSNR / SSIM / LPIPS against the original photos.
- Mesh metrics: raycast the mesh at the held-out poses and compare with the sensor's own depth, in the cropped room. This scorer only measures where a mesh exists near the sensor depth, so it does not penalise missing thin structures and it favours depth-supervised methods.

## Results

Splat quality (held-out): plain 2DGS + COLMAP poses, 3k steps 22.24 dB / 0.732 / 0.386. AGS-Mesh splat 21.43 / 0.729 / 0.338 (best LPIPS). Our licence-clean gsplat 2DGS 7k steps 21.7 / 0.742 / 0.346; 30k steps 21.00 / 0.715 / 0.341 (PSNR drops with more steps).

Mesh geometry (coverage / median error / within 5 cm / within 10 cm):

| Mesh | Cov | Median | <5 cm | <10 cm | Licence |
|---|---|---|---|---|---|
| AGS-Mesh (bounded TSDF 2 cm) | 0.856 | 2.0 cm | 79.4% | 89.5% | non-commercial |
| Ours, gsplat 2DGS 30k, 2 cm / 6 cm trunc | 0.818 | 2.5 cm | 76.6% | 88.6% | clean |
| Ours, 30k, 2.5 cm / 10 cm trunc | 0.865 | 2.8 cm | 73.4% | 87.0% | clean |
| Robot completed depth (bil), own TSDF | 0.82 | 2.4 cm | n/a | 85% | clean |
| MVSAnywhere | 0.957 | 3.3 cm | 61.5% | 77.8% | non-commercial |
| DA3 depth | 0.97 | 5.9 cm | n/a | 68% | Giant is non-commercial |
| MapAnything + poses | 0.82 | 25 cm | n/a | 25% | Apache weights |

## Decision (user, 2026-10-17)

Do not use our licence-clean mesh. Compared with the voxel map shown in 3D and the AGS-Mesh it has many holes, for example the stairs' guard-rail segments. The metric scores hide this. The reference geometries stay the AGS-Mesh mesh (demo only, non-commercial) and the voxel map.

## What did not help

Training length (15k to 30k), the MoGe-2 normal prior, Real-ESRGAN photos, Difix (removed; non-commercial and hallucinates), DA3 splat head, GS-SDF (GPL, below the plain baselines), svox2 (17.7 dB, overfits), MapAnything depth, planar wall/floor shell. UV-textured mesh (xatlas, best-view projection) looks the most photographic.

## Licences

Out for a product: anything that vendors the Inria/MPII rasterizer (2DGS, GOF, RaDe-GS, PGSR, MILo, SuGaR, GS2Mesh, Gaussian Surfels, AGS-Mesh), DSINE, Omnidata, DA3 Giant/Large, MVSAnywhere, DUSt3R/MASt3R, GS-SDF (GPL). Clean: gsplat, nerfstudio splatfacto, dn-splatter code, Open3D TSDF, xatlas, MoGe-2, DA3 Small/Base/Metric/Mono, MapAnything `-apache` weights.

## Possible next big improvements

1. Fuse the robot's completed depth into the same TSDF as the model's rendered depth; the sensor depth sees thin structures that 2DGS drops.
2. Add a thin-structure score (coverage per height band, points on rails and steps) so holes are measured.
3. Copy AGS-Mesh's recipe step by step to find which step gives the remaining gap: the bounded 2 cm TSDF, the largest-component clean-up, the normals from a permissive model (Lotus, StableNormal).
4. Capture more views near the stairs and rails; coverage and the 448x336 photos are the limit.
5. Fill holes with the voxel map as a prior, or ship the voxel map as geometry and use the splat only for looks.
6. A depth-supervised licence-clean method (splatfacto or dn-splatter code) with rendered and sensor depth together.

## Practical notes

- Artifact (viewer, tables, gallery): https://claude.ai/artifact/86vSNEsAXXqPsqNujZ2LFm
- Splat floater clean-up (opacity >= 0.02, anisotropy <= 5x, max scale 0.1) is lossless and shrinks the splat 36%; not yet applied to the final splat.
- Open3D 0.20 `ScalableTSDFVolume` returns empty meshes; use 0.19 or the own GPU TSDF. Hugging Face downloads fail inside the venvs; use curl.
- Disk: the root filesystem is 100% full (16 GB free); the scratchpad is about 28 GB.
