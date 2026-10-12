"""Resample a costmap window (any resolution, any rotation) onto the global lattice.

The fold works on axis-aligned cells of `target_res` whose corners sit at multiples of it. A leaf window is a rotated
grid: cell (i, j) has its centre at  origin + R(yaw) * ((i + .5) * res, (j + .5) * res).

Rule: every target cell takes the value of the source cell under its centre (nearest neighbour: values are never
mixed, so 0 / 100 / 255 stay exact). When the source is finer than the target the cell is sampled k x k times and the
conservative priority applies (blocked > free > unknown), the same side the fold's tie rule takes. A target cell whose
centre lies outside the source window is unknown. Returns the smallest axis-aligned window holding the source footprint.
"""
import math

import numpy as np

UNKNOWN = 255
BLOCKED = 100


def to_lattice(window, res, origin, yaw=0.0, target_res=0.05):
    """window: u8 [rows, cols] (row = y of the window frame). Returns (u8 window, (x0, y0) of its cell (0, 0))."""
    H, W = window.shape
    c, s = math.cos(yaw), math.sin(yaw)
    corners = np.array([[0, 0], [W * res, 0], [0, H * res], [W * res, H * res]])
    xs = origin[0] + corners[:, 0] * c - corners[:, 1] * s
    ys = origin[1] + corners[:, 0] * s + corners[:, 1] * c
    ix0, iy0 = math.floor(xs.min() / target_res), math.floor(ys.min() / target_res)
    ix1, iy1 = math.ceil(xs.max() / target_res), math.ceil(ys.max() / target_res)
    cols, rows = ix1 - ix0, iy1 - iy0
    k = max(1, math.ceil(target_res / res - 1e-9))
    sub = (np.arange(k) + 0.5) / k
    out = np.full((rows, cols), UNKNOWN, np.uint8)
    gy, gx = np.mgrid[0:rows, 0:cols]
    best_blocked = np.zeros((rows, cols), bool); best_free = np.zeros((rows, cols), bool)
    for fy in sub:
        for fx in sub:
            px = (ix0 + gx + fx) * target_res - origin[0]
            py = (iy0 + gy + fy) * target_res - origin[1]
            u = (px * c + py * s) / res            # inverse rotation into window coordinates
            v = (-px * s + py * c) / res
            ui, vi = np.floor(u).astype(int), np.floor(v).astype(int)
            ok = (ui >= 0) & (ui < W) & (vi >= 0) & (vi < H)
            val = np.full((rows, cols), UNKNOWN, np.uint8)
            val[ok] = window[vi[ok], ui[ok]]
            best_blocked |= val >= BLOCKED
            best_free |= val < BLOCKED
            if k == 1: out = val
    if k > 1:
        out = np.where(best_blocked, BLOCKED, np.where(best_free, 0, UNKNOWN)).astype(np.uint8)
    return out, (ix0 * target_res, iy0 * target_res)
