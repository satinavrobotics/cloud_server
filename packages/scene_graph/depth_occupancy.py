"""Occupancy window from one depth image (for nodes recorded before the robot sent a costmap).

Pure numpy, no I/O. Input is what a node's depth record carries: the u16 depth image, `depth_scale`, the camera
(fx, fy, cx, cy, valid_range_m), T_base_cam and the map-frame pose of base_link at the depth stamp. Output is a window in the
robot's costmap encoding (0 free, 100 occupied, 255 unknown) on a grid the caller chooses (default: 200 x 200 at 0.05 m).

Rule, per depth image:
  1. back-project every valid pixel to the map frame; heights are measured from the floor of THIS leaf, the dominant
     horizontal plane near the base (histogram mode, refined by the median) - the floor is not always at base height;
  2. a point within `floor_tolerance` of the floor is floor evidence (free); a point above it and below `robot_height`
     is an obstacle the robot would hit (occupied); anything higher (ceilings, table tops) is ignored;
  3. cells crossed by the 2D ray from the camera to a floor or obstacle point, before that point, are free;
  4. everything else is unknown; where free and occupied meet in one cell, occupied wins (the fold's tie side).
Measured on office8 against the robot's own costmap: floor residual sigma 0.02 m (so 0.08 m = 4 sigma), navigable-set
overlap 0.8. It is a fallback, tagged source=depth; a robot costmap always wins at the same stamp.
"""
import math

import numpy as np

UNKNOWN = 255
OCCUPIED = 100
FREE = 0


def quat_matrix(x, y, z, w):
    n = math.sqrt(x * x + y * y + z * z + w * w)
    x, y, z, w = x / n, y / n, z / n, w / n
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def points_in_map(depth_u16, depth_scale, camera, t_base_cam, pose3d_map):
    """(points [n, 3] in the map frame, camera position [3] in the map frame) of the valid depth pixels."""
    z = depth_u16.astype(np.float64) * depth_scale
    lo, hi = camera.get("valid_range_m", (0.0, np.inf))
    v, u = np.nonzero((z > lo) & (z <= hi))
    zz = z[v, u]
    p_opt = np.stack([(u - camera["cx"]) * zz / camera["fx"], (v - camera["cy"]) * zz / camera["fy"], zz], 1)
    Rb = quat_matrix(t_base_cam["qx"], t_base_cam["qy"], t_base_cam["qz"], t_base_cam["qw"])
    tb = np.array([t_base_cam["x"], t_base_cam["y"], t_base_cam["z"]])
    p = pose3d_map
    Rm = quat_matrix(p["qx"], p["qy"], p["qz"], p["qw"])
    tm = np.array([p["x"], p["y"], p["z"]])
    return (p_opt @ Rb.T + tb) @ Rm.T + tm, Rm @ tb + tm


def floor_level(heights, search=0.4, bin_m=0.02, refine=0.04):
    """Height of the dominant horizontal plane within +-search of the base (histogram mode, median refined)."""
    hist, edges = np.histogram(heights, bins=np.arange(-search, search, bin_m))
    z0 = edges[hist.argmax()] + bin_m / 2
    near = np.abs(heights - z0) < refine
    return float(np.median(heights[near])) if near.sum() > 20 else float(z0)


def occupancy_from_depth(depth_u16, depth_scale, camera, t_base_cam, pose3d_map, origin, shape=(200, 200), res=0.05,
                         robot_height=0.5, floor_tolerance=0.08, min_points=1):
    """Window u8 [rows, cols] with cell (0, 0) at `origin` (map frame, metres)."""
    rows, cols = shape
    pts, cam = points_in_map(depth_u16, depth_scale, camera, t_base_cam, pose3d_map)
    out = np.full(shape, UNKNOWN, np.uint8)
    if len(pts) == 0:
        return out
    h = pts[:, 2] - pose3d_map["z"]
    h = h - floor_level(h)
    floor = np.abs(h) <= floor_tolerance
    obst = (h > floor_tolerance) & (h <= robot_height)
    ix = lambda x: np.floor((x - origin[0]) / res).astype(int)
    iy = lambda y: np.floor((y - origin[1]) / res).astype(int)
    inb = lambda a, b: (a >= 0) & (a < cols) & (b >= 0) & (b < rows)
    occ = np.zeros(shape, int); fl = np.zeros(shape, int); free = np.zeros(shape, bool)
    for m, tgt in ((obst, occ), (floor, fl)):
        a, b = ix(pts[m, 0]), iy(pts[m, 1]); k = inb(a, b); np.add.at(tgt, (b[k], a[k]), 1)
    hit = pts[floor | obst]
    if len(hit):
        d = hit[:, :2] - cam[:2]; L = np.hypot(d[:, 0], d[:, 1])
        step = res / 2
        n = int(L.max() / step) + 1
        t = (np.arange(n) * step)[None, :]
        unit = d / np.maximum(L[:, None], 1e-9)
        keep = t < (L[:, None] - res / 2)
        X = cam[0] + unit[:, 0:1] * t; Y = cam[1] + unit[:, 1:2] * t
        a, b = ix(X[keep]), iy(Y[keep]); k = inb(a, b); free[b[k], a[k]] = True
    out[free | (fl > 0)] = FREE
    out[occ >= min_points] = OCCUPIED
    return out
