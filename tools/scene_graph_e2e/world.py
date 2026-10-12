"""Synthetic world of the end-to-end harness: raw occupancy at 0.05 m (0 free, 100 blocked,
255 unknown), and the costmap windows a robot driving through it uploads.

`ring` is a 6 x 6 m room with a 2.6 m block in the middle: a 1.5 m wide loop with two routes
between any two points on opposite corners, so a blocked overlay on one side has an alternative
and on both sides none. The windows are 120 x 120 cells (6 m), like the unit-test worlds.
"""
import math
from typing import List, Tuple

import numpy as np

RES = 0.05
WIN = 120


def _rect(w: np.ndarray, x0: float, y0: float, x1: float, y1: float, v: int) -> None:
    w[int(round(y0 / RES)):int(round(y1 / RES)), int(round(x0 / RES)):int(round(x1 / RES))] = v


def _room(width: float, height: float) -> np.ndarray:
    w = np.full((int(height / RES), int(width / RES)), 100, np.uint8)
    _rect(w, 0.2, 0.2, width - 0.2, height - 0.2, 0)
    return w


def ring() -> Tuple[np.ndarray, List[Tuple[float, float]]]:
    """(world, driven polyline). The loop is traversed once, ending away from the start."""
    w = _room(6.0, 6.0)
    _rect(w, 1.7, 1.7, 4.3, 4.3, 100)
    return w, [(1.0, 1.0), (5.0, 1.0), (5.0, 5.0), (1.0, 5.0), (1.0, 1.6)]


def two_rooms() -> Tuple[np.ndarray, List[Tuple[float, float]]]:
    """Two rooms joined by a 0.9 m door (a few leaves; used for the small maps)."""
    w = _room(6.0, 3.0)
    _rect(w, 3.0, 0.2, 3.15, 2.8, 100)
    _rect(w, 3.0, 1.05, 3.15, 1.95, 0)
    return w, [(1.0, 1.5), (2.8, 1.5), (3.4, 1.5), (5.0, 1.5)]


def drive(way: List[Tuple[float, float]], step: float = 0.5) -> List[Tuple[float, float, float]]:
    """(x, y, yaw) every `step` m along `way`; yaw = bearing of the leg, the last pose keeps it."""
    pts: List[Tuple[float, float, float]] = []
    for (x0, y0), (x1, y1) in zip(way[:-1], way[1:]):
        n = max(1, int(math.hypot(x1 - x0, y1 - y0) / step))
        yaw = math.atan2(y1 - y0, x1 - x0)
        for k in range(n):
            t = k / n
            pts.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, yaw))
    last = way[-1]
    pts.append((last[0], last[1], pts[-1][2] if pts else 0.0))
    return pts


def window_at(world: np.ndarray, x: float, y: float) -> Tuple[np.ndarray, Tuple[float, float]]:
    """(window u8 [WIN, WIN], map-frame origin of cell (0, 0)): the world as seen on a WIN x WIN
    grid centred on (x, y); cells outside the world are unknown (255). Row 0 = lowest y."""
    gx = int(round(x / RES)) - WIN // 2
    gy = int(round(y / RES)) - WIN // 2
    win = np.full((WIN, WIN), 255, np.uint8)
    ys0, xs0 = max(0, gy), max(0, gx)
    ys1, xs1 = min(world.shape[0], gy + WIN), min(world.shape[1], gx + WIN)
    win[ys0 - gy:ys1 - gy, xs0 - gx:xs1 - gx] = world[ys0:ys1, xs0:xs1]
    return win, (gx * RES, gy * RES)
