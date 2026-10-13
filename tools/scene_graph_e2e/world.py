"""Synthetic world of the end-to-end harness: raw occupancy at 0.05 m (0 free, 100 blocked,
255 unknown), and the costmap windows a robot driving through it uploads.

`ring` is a 6 x 6 m world of four rooms (1.6 - 1.8 m) at the corners joined in a loop by 1.2 m
corridors on the lines x, y = 1 and 5, solid in between. Under the persistence partition a room is
a region (its peak clearance is well above the saddle of the corridor that leaves it) and a
corridor is not (it hangs on a room), so the loop is four charts and four links, with two routes
between rooms on opposite corners: a blocked overlay on one corridor leaves the other way round,
on both none. A plain constant-width ring corridor would be ONE chart: a ridge of constant
clearance has no peak per corner, the persistence merge takes it whole (`corridor_ring`, used by
the stamp-order test). The windows are 120 x 120 cells (6 m), like the unit-test worlds.
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


CORRIDOR = 1.2      # width of the corridors of `ring` (m)


def ring() -> Tuple[np.ndarray, List[Tuple[float, float]]]:
    """(world, driven polyline): four rooms of slightly different size joined by corridors; the
    loop is traversed once, ending away from the start. The rooms are centred on the corners of
    the driven square, so the straight way between two rooms is the corridor's centre line."""
    w = np.full((int(6.0 / RES), int(6.0 / RES)), 100, np.uint8)
    for x0, y0, x1, y1 in ((0.2, 0.2, 2.0, 2.0), (4.2, 0.2, 5.8, 1.8),
                           (4.0, 4.0, 5.8, 5.8), (0.2, 4.2, 1.8, 5.8)):
        _rect(w, x0, y0, x1, y1, 0)
    h = CORRIDOR / 2
    _rect(w, 1.0, 1.0 - h, 5.0, 1.0 + h, 0)       # bottom, y 0.4 .. 1.6
    _rect(w, 1.0, 5.0 - h, 5.0, 5.0 + h, 0)       # top
    _rect(w, 5.0 - h, 1.0, 5.0 + h, 5.0, 0)       # right, x 4.4 .. 5.6
    _rect(w, 1.0 - h, 1.0, 1.0 + h, 5.0, 0)       # left
    return w, [(1.0, 1.0), (5.0, 1.0), (5.0, 5.0), (1.0, 5.0), (1.0, 1.6)]


def corridor_ring() -> Tuple[np.ndarray, List[Tuple[float, float]]]:
    """A 6 x 6 m room with a 2.6 m block in the middle: a 1.5 m wide loop of constant width
    (one chart under the persistence partition)."""
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
