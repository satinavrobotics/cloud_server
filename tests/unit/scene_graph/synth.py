"""Synthetic worlds with a robot that senses what it can see (line of sight, 5 m), at 0.05 m cells.

A world is a boolean array (True = wall). `sense` returns the costmap window the robot would send:
0 where it sees free floor, 100 where it sees a wall, 255 elsewhere.
"""
from typing import List, Tuple

import numpy as np

from packages.scene_graph import Leaf

RES = 0.05
WIN = 200            # 10 m window, as the robot's costmap
RANGE = 100          # 5 m of sight, in cells


def room(w_m: float, h_m: float, pad_m: float = 0.5) -> np.ndarray:
    w, h, p = int(w_m / RES), int(h_m / RES), int(pad_m / RES)
    world = np.ones((h + 2 * p, w + 2 * p), bool)
    world[p:p + h, p:p + w] = False
    return world


def box(world: np.ndarray, x0, y0, x1, y1, pad_m=0.5) -> None:
    p = int(pad_m / RES)
    world[p + int(y0 / RES):p + int(y1 / RES), p + int(x0 / RES):p + int(x1 / RES)] = True


def carve(world: np.ndarray, x0, y0, x1, y1, pad_m=0.5) -> None:
    p = int(pad_m / RES)
    world[p + int(y0 / RES):p + int(y1 / RES), p + int(x0 / RES):p + int(x1 / RES)] = False


def sense(world: np.ndarray, ci: int, cj: int) -> np.ndarray:
    """Costmap window centred on cell (ci, cj) (world indices)."""
    h, w = world.shape
    win = np.full((WIN, WIN), 255, np.uint8)
    rr, cc = np.mgrid[-RANGE:RANGE, -RANGE:RANGE]
    tr, tc = cj + rr, ci + cc
    inside = (tr >= 0) & (tr < h) & (tc >= 0) & (tc < w) & (rr * rr + cc * cc <= RANGE * RANGE)
    free_path = np.ones(rr.shape, bool)
    for t in np.linspace(0.0, 1.0, 80, endpoint=False)[1:]:      # samples strictly before the target
        sr = np.clip(np.round(cj + rr * t).astype(int), 0, h - 1)
        sc = np.clip(np.round(ci + cc * t).astype(int), 0, w - 1)
        free_path &= ~world[sr, sc]
    seen = inside & free_path
    vals = np.where(world[np.clip(tr, 0, h - 1), np.clip(tc, 0, w - 1)], 100, 0).astype(np.uint8)
    sub = win[WIN // 2 - RANGE:WIN // 2 + RANGE, WIN // 2 - RANGE:WIN // 2 + RANGE]
    sub[seen] = vals[seen]
    return win


def drive(world: np.ndarray, path_m: List[Tuple[float, float]], step_m: float = 1.0, pad_m: float = 0.5) -> List[Leaf]:
    """Leaves every `step_m` along a polyline given in room coordinates (metres)."""
    p = pad_m
    pts = []
    for (x0, y0), (x1, y1) in zip(path_m[:-1], path_m[1:]):
        n = max(1, int(np.hypot(x1 - x0, y1 - y0) / step_m))
        pts += [(x0 + (x1 - x0) * k / n, y0 + (y1 - y0) * k / n) for k in range(n)]
    pts.append(path_m[-1])
    leaves = []
    for k, (x, y) in enumerate(pts):
        ci, cj = int(round((x + p) / RES)), int(round((y + p) / RES))
        win = sense(world, ci, cj)
        # window[0, 0] is cell (ci - WIN/2, cj - WIN/2); the world frame is the room frame
        leaves.append(Leaf(f"s{k}", (ci + 0.5) * RES, (cj + 0.5) * RES, ci - WIN // 2, cj - WIN // 2, win, "synthetic"))
    return leaves
