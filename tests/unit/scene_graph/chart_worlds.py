"""Raw-occupancy worlds at 0.05 m (0 free, 100 blocked, 255 unknown) and the leaves a robot driving through them delivers."""
import math

import numpy as np

from packages.scene_graph.api import Leaf

R = 0.05


def _rect(w, x0, y0, x1, y1, v):
    w[int(round(y0 / R)):int(round(y1 / R)), int(round(x0 / R)):int(round(x1 / R))] = v


def _blank(W, H):
    w = np.full((int(H / R), int(W / R)), 100, np.uint8)
    _rect(w, 0.2, 0.2, W - 0.2, H - 0.2, 0)
    return w


def two_rooms(door=0.9, W=6.0, H=3.0):
    w = _blank(W, H)
    _rect(w, W / 2, 0.2, W / 2 + 0.15, H - 0.2, 100)
    _rect(w, W / 2, H / 2 - door / 2, W / 2 + 0.15, H / 2 + door / 2, 0)
    way = [(1.0, H / 2), (W / 2 - 0.2, H / 2), (W / 2 + 0.4, H / 2), (W - 1.0, H / 2), (W - 1.0, 0.8), (1.0, 0.8)]
    return w, way


def corridor(width=0.55):
    W, H = 9.0, 3.0
    w = np.full((int(H / R), int(W / R)), 100, np.uint8)
    _rect(w, 0.2, 0.2, 2.6, H - 0.2, 0); _rect(w, 6.4, 0.2, W - 0.2, H - 0.2, 0)
    _rect(w, 2.6, H / 2 - width / 2, 6.4, H / 2 + width / 2, 0)
    return w, [(1.0, H / 2), (8.0, H / 2), (8.0, 0.8), (1.0, 0.8)]


def pillar_room():
    W, H = 6.0, 5.0
    w = _blank(W, H); _rect(w, 2.4, 1.8, 3.4, 2.8, 100)
    return w, [(1.0, 1.0), (5.0, 1.0), (5.0, 4.0), (1.0, 4.0), (1.0, 1.0)]


def ring():
    W, H = 6.0, 6.0
    w = _blank(W, H); _rect(w, 1.7, 1.7, 4.3, 4.3, 100)
    return w, [(1.0, 1.0), (5.0, 1.0), (5.0, 5.0), (1.0, 5.0), (1.0, 1.0)]


def island():
    W, H = 7.0, 5.0
    w = _blank(W, H); _rect(w, 2.6, 1.7, 4.4, 3.3, 100)
    return w, [(1.0, 1.0), (6.0, 1.0), (6.0, 4.0), (1.0, 4.0), (1.0, 1.0)]


def drive(world, way, step=0.5, win=120, session="s", r_d=0.25, t0=0, yaw=0.0):
    """leaves every `step` m along `way`; each window is the world seen on a win x win grid centred on the pose"""
    pts = []
    for (x0, y0), (x1, y1) in zip(way[:-1], way[1:]):
        n = max(1, int(math.hypot(x1 - x0, y1 - y0) / step))
        for t in np.linspace(0, 1, n, endpoint=False): pts.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t))
    pts.append(way[-1])
    out = []
    for i, (x, y) in enumerate(pts):
        gx = int(round(x / R)) - win // 2; gy = int(round(y / R)) - win // 2
        w = np.full((win, win), 255, np.uint8)
        ys0, xs0 = max(0, gy), max(0, gx); ys1, xs1 = min(world.shape[0], gy + win), min(world.shape[1], gx + win)
        w[ys0 - gy:ys1 - gy, xs0 - gx:xs1 - gx] = world[ys0:ys1, xs0:xs1]
        out.append(Leaf(key="%s%03d" % (session, i), session=session, stamp_ms=t0 + 1000 * i, x=x, y=y, yaw=yaw, resolution=R,
                        origin=(gx * R, gy * R), window_u8=w, r_d=r_d))
    return out


def rotated(world, leaf, deg, win=120):
    """the same leaf as a robot whose window grid is rotated by `deg` about the window centre"""
    yaw = math.radians(deg); c, s = math.cos(yaw), math.sin(yaw)
    cx, cy = leaf.x, leaf.y
    half = win * R / 2
    ox = cx - (half * c - half * s); oy = cy - (half * s + half * c)
    j, i = np.mgrid[0:win, 0:win]
    x = ox + ((i + .5) * R) * c - ((j + .5) * R) * s
    y = oy + ((i + .5) * R) * s + ((j + .5) * R) * c
    xi, yi = np.floor(x / R).astype(int), np.floor(y / R).astype(int)
    ok = (xi >= 0) & (xi < world.shape[1]) & (yi >= 0) & (yi < world.shape[0])
    w = np.full((win, win), 255, np.uint8); w[ok] = world[yi[ok], xi[ok]]
    return Leaf(key=leaf.key, session=leaf.session, stamp_ms=leaf.stamp_ms, x=leaf.x, y=leaf.y, yaw=leaf.yaw, resolution=R,
                origin=(ox, oy), window_u8=w, r_d=leaf.r_d, origin_yaw=yaw)
